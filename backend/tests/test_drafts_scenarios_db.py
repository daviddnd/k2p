"""DB-backed draft scenarios (opt-in; needs a migrated PostgreSQL).

The draft lifecycle end to end against a real database: generation over master
data plus approved change events, the resolved snapshot and per-item
provenance, the version lineage, the prior-filing cross-check, the §4.1
invalidation legs, the §5.1 gate's denial, tenancy, the §4.2 transitions, the
export's FilingPackage with its stored artifacts and the events' submission,
and the audit chain.

The claims that need a database rather than a fake are here:

- **the events move** — generation writes ``applied_to_draft`` through the
  change-events executor, export writes ``submitted``, and a withdrawal moves
  the drafts back — which is a row's status, not a pure computation;
- **``applied_event_ids`` is a JSONB containment lookup** — the thing that makes
  invalidation cheap — and **the composite foreign keys** refuse a cross-tenant
  draft at the database;
- **the audit chain *is* the record**: every mutation appends its entry, and
  ``verify_chain`` recomputes it — a refused export's chain is byte-identical
  to the chain before the attempt;
- **the package artifacts are real stored objects** — the flattened PDF reads
  back with no live form fields, the checklist carries the fee figures, and
  the download URLs issue for the package documents.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to head.
Every test runs inside a transaction that is rolled back, so the review database
stays clean.
"""

import io
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.forms.registry import load_form_registry
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.company_master import (
    CompanyContactFact,
    CompanyNameRecord,
    CompanyStatusRecord,
    RegisteredOfficeRecord,
    ShareCapitalClassRecord,
)
from app.adapters.persistence.documents import Document
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.filing_packages import FilingPackage
from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.prior_filing import ImportBatch, ImportBatchRecord
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import change_events, company_master
from app.core.config import settings
from app.domain.audit import AuditAction, verify_chain
from app.domain.change_events import ChangeEventStatus
from app.domain.documents import (
    MAX_UPLOAD_BYTES,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
    ScanStatus,
)
from app.domain.drafts import (
    SOURCE_CHANGE_EVENT,
    SOURCE_MASTER_RECORD,
    DraftStatus,
    SignatureStatus,
)
from app.domain.prior_filing import FormCode
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2027-03-01",
    "type": "private",
}

PUBLIC_COMPANY = {**COMPANY, "br_number": "87654321", "type": "public"}

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}

MOVED_ADDRESS = {
    "flat": "5B",
    "building": "Harbour House",
    "street": "2 Connaught Road",
    "district": "Sheung Wan",
}

TARGET_DATE = date(2026, 9, 30)
# The coverage period every generated draft is gated against (today is the
# decision day; the target date is the draft's own).
COVERAGE = {"start": date(2026, 1, 1), "end": date(2027, 12, 31)}

MARK = {"action": "mark_under_review"}


@pytest.fixture(autouse=True)
def provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provision the deployment's tier: ``automated_prefill`` is Standard+.

    The gate's leg (a) — the vendor-level capability — comes from the
    entitlement seam, which reads deployment configuration until a lease
    exists. Without it the generation is denied ``tier_capability_missing``,
    which is the Free tier's real behaviour and is asserted separately.
    """
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes (the documents tests' own)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@dataclass(frozen=True)
class Deployment:
    """One tenant's world: its client (as the Administrator), ids and company."""

    client: TestClient
    tenant: Tenant
    admin: User
    company_id: str
    store: FakeStore


def make_client(
    session: AsyncSession, actor: ActorRef, store: FakeStore | None = None
) -> TestClient:
    """The real app over the test session, with the real RBAC loader."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    fake_store = store if store is not None else FakeStore()
    app.dependency_overrides[get_storage] = lambda: fake_store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


async def deployment(
    session: AsyncSession, *, company: dict = COMPANY, cover: bool = True
) -> Deployment:
    """A tenant, its Administrator as the client, one company — covered or not.

    ``cover`` assigns a package whose ``included_services`` list carries
    ``automated_prefill`` and whose coverage period spans today; without it the
    §5.1 gate denies the generation (``no_assignment``).
    """
    tenant, admin = await seed_tenant(session)
    store = FakeStore()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id), store)
    response = client.post("/api/v1/companies", json=company)
    assert response.status_code == 201, response.text
    company_id = response.json()["id"]
    if cover:
        package = client.post(
            "/api/v1/service-packages",
            json={
                "name": f"pkg-{uuid4()}",
                "included_services": ["automated_prefill"],
                "covered_forms": ["NAR1", "ND2A"],
            },
        )
        assert package.status_code == 201, package.text
        assignment = client.post(
            f"/api/v1/companies/{company_id}/service-assignments",
            json={
                "package_id": package.json()["id"],
                "coverage_start": COVERAGE["start"].isoformat(),
                "coverage_end": COVERAGE["end"].isoformat(),
            },
        )
        assert assignment.status_code == 201, assignment.text
    return Deployment(
        client=client, tenant=tenant, admin=admin, company_id=company_id, store=store
    )


async def seed_company_facts(
    session: AsyncSession,
    *,
    tenant: Tenant,
    company_id: str,
    name_en: str = "BRIGHT LANTERN LIMITED",
) -> None:
    """The company's master data as of the target date, through the write path."""
    common = {"tenant_id": tenant.id, "company_id": UUID(company_id), "created_by": None}
    for model, payload in (
        (
            CompanyNameRecord,
            {"name_en": name_en, "name_zh": "明燈有限公司", "business_name": None},
        ),
        (RegisteredOfficeRecord, {**ADDRESS, "country": None}),
        (
            CompanyContactFact,
            {"email": "info@brightlantern.example.hk", "contact_phone_hk": "21234567"},
        ),
        (CompanyStatusRecord, {"status": "active"}),
        (
            ShareCapitalClassRecord,
            {
                "classes": [
                    {
                        "class_name": "Ordinary",
                        "currency": "HKD",
                        "total_shares": 1000,
                        "total_amount": "1000.00",
                        "total_paid_up": "1000.00",
                    }
                ]
            },
        ),
    ):
        await company_master.record_fact(
            session,
            model=model,
            effective_date=date(2020, 1, 1),
            **common,
            **payload,
        )
    await session.flush()


def create_officer(client: TestClient, company_id: str, **overrides: object) -> dict:
    body: dict = {
        "person": {
            "person_type": "natural_person",
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "surname_en": "CHAN",
            "other_names_en": "Tai Man",
            "email": "chan@brightlantern.example.hk",
            "hkid_partial": "A1234",
        },
        "role": "director",
        "capacity": ["director"],
        "valid_from": "2024-01-01",
        "correspondence_address": ADDRESS,
    }
    body.update(overrides)
    response = client.post(f"/api/v1/companies/{company_id}/officers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_shareholding(client: TestClient, company_id: str, **overrides: object) -> dict:
    body: dict = {
        "person": {"person_type": "natural_person", "name_en": "CHAN Tai Man"},
        "class_name": "Ordinary",
        "shares_held": 1000,
        "percentage_held": "100.00",
        "allotment_date": "2020-01-01",
        "amount_paid": "1000.00",
        "amount_unpaid": "0.00",
        "address": ADDRESS,
        "valid_from": "2020-01-01",
    }
    body.update(overrides)
    response = client.post(f"/api/v1/companies/{company_id}/shareholdings", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def enter_event(client: TestClient, company_id: str, **overrides: object) -> dict:
    """Enter one change event by hand and walk it to ``confirmed``."""
    body: dict = {
        "type": "director_appointment",
        "effective_date": "2026-06-01",
        "payload": {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_zh": "黃少峰",
                    "surname_en": "WONG",
                    "other_names_en": "Siu Fung",
                    "capacity": ["director"],
                    "appointment_date": "2026-06-01",
                }
            }
        },
    }
    body.update(overrides)
    response = client.post(f"/api/v1/companies/{company_id}/change-events", json=body)
    assert response.status_code == 201, response.text
    return confirm_event(client, response.json()["id"])


def confirm_event(client: TestClient, event_id: str) -> dict:
    response = None
    for action in ("mark_under_review", "mark_reviewed", "confirm"):
        response = client.post(
            f"/api/v1/change-events/{event_id}/transitions", json={"action": action}
        )
        assert response.status_code == 200, response.text
    assert response is not None
    return response.json()


def generate(
    client: TestClient,
    company_id: str,
    *,
    form_type: str = "nar1",
    target: date = TARGET_DATE,
) -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/drafts",
        json={"form_type": form_type, "target_date": target.isoformat()},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def audit_actions(session: AsyncSession, tenant_id: UUID) -> list[str]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return [row.action for row in rows.all()]


async def current_draft(session: AsyncSession, draft_id: str) -> Draft:
    """The draft's current version row, straight from the table."""
    row: Draft | None = await session.scalar(
        select(Draft)
        .where(Draft.id == UUID(draft_id))
        .order_by(Draft.version_no.desc())
        .limit(1)
    )
    assert row is not None
    return row


def _now() -> datetime:
    return datetime.now(UTC)


async def seed_prior_batch(
    session: AsyncSession,
    *,
    tenant: Tenant,
    company_id: str,
    values: dict[str, str],
    rejected: dict[str, str] | None = None,
) -> ImportBatch:
    """One prior-filing run with read values, as the extraction path writes them."""
    document = Document(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        kind="prior_filing",
        origin="upload",
        retention_class="statutory_filing",
        sha256="0" * 64,
        storage_key=f"prior/{uuid4()}.pdf",
        byte_size=1024,
        content_type="application/pdf",
        scan_status="clean",
        uploaded_by=None,
    )
    session.add(document)
    await session.flush()
    batch = ImportBatch(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        document_id=document.id,
        form="nar1",
        shape="acroform",
        created_by=None,
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        ImportBatchRecord(
            batch_id=batch.id,
            tenant_id=tenant.id,
            target=target,
            value=value,
            confidence="0.990",
            status=status,
            effective_date=None,
            # The records await review: the filing's read is the basis either
            # way, and an undecided one is the shape a fresh run lands in.
            decided_by=None,
            decided_at=None if status == "pending" else _now(),
        )
        for status, records in (("pending", values), ("rejected", rejected or {}))
        for target, value in records.items()
    )
    await session.flush()
    return batch


def registry_sha256(form: str) -> str:
    return load_form_registry(FormCode(form)).template_sha256


# --- Generation --------------------------------------------------------------


async def test_generation_resolves_master_data_and_events(session: AsyncSession) -> None:
    place = await deployment(session)
    client, tenant, company_id = place.client, place.tenant, place.company_id
    await seed_company_facts(session, tenant=tenant, company_id=company_id)
    officer = create_officer(client, company_id)
    create_shareholding(client, company_id)
    appointment = enter_event(client, company_id)
    office_change = enter_event(
        client,
        company_id,
        type="registered_office_change",
        effective_date="2026-05-01",
        payload={"registered_office": {"address": MOVED_ADDRESS}},
    )

    draft = generate(client, company_id)
    assert draft["company_id"] == company_id
    assert draft["form_type"] == "nar1"
    assert draft["version_no"] == 1
    assert draft["target_date"] == "2026-09-30"
    assert draft["status"] == "generated"
    assert draft["signature_status"] == "not_sent"
    assert draft["private_company_statement"] is True
    assert draft["form_version"] == {
        "specification_no": "2/2025 (May 2025)",
        "template_sha256": registry_sha256("nar1"),
    }
    assert draft["filing_metadata"] == {
        "return": {
            "made_up_date": "2026-03-01",
            "fs_period_start": None,
            "fs_period_end": None,
        },
        "member_particulars_delivery": None,
        "presentor": None,
        "signature": None,
        "consent": None,
    }

    state = draft["resolved_state"]
    assert state["company"] == {
        "br_number": "12345678",
        "cr_number": "1234567",
        "type": "private",
        "date_of_incorporation": "2010-03-01",
        "anniversary_date": "2027-03-01",
        "name": {
            "name_en": "BRIGHT LANTERN LIMITED",
            "name_zh": "明燈有限公司",
            "business_name": None,
        },
        "registered_office": MOVED_ADDRESS,
        "business_nature": None,
        "contact": {
            "email": "info@brightlantern.example.hk",
            "contact_phone_hk": "21234567",
        },
        "filing": None,
        "records_kept": [],
        "share_capital": {
            "classes": [
                {
                    "class_name": "Ordinary",
                    "currency": "HKD",
                    "total_shares": 1000,
                    "total_amount": "1000.00",
                    "total_paid_up": "1000.00",
                }
            ],
            "total": {
                "derived": True,
                "currency": "HKD",
                "total_shares": 1000,
                "total_amount": "1000.00",
                "total_paid_up": "1000.00",
            },
        },
        "status": "active",
    }

    officers = state["officers"]
    assert len(officers) == 2
    base, added = officers
    assert base["person_id"] == officer["person"]["id"]
    assert base["role"] == "director"
    assert base["valid_from"] == "2024-01-01"
    assert base["valid_to"] is None
    assert base["source"] == {"type": SOURCE_MASTER_RECORD, "id": officer["id"]}
    assert base["correspondence_address"] == ADDRESS
    assert added["role"] == "director"
    assert added["surname_en"] == "WONG"
    assert added["valid_from"] == "2026-06-01"
    assert added["source"] == {"type": SOURCE_CHANGE_EVENT, "id": appointment["id"]}

    shareholders = state["shareholders"]
    assert len(shareholders) == 1
    assert shareholders[0]["class_name"] == "Ordinary"
    assert shareholders[0]["shares_held"] == 1000
    assert shareholders[0]["percentage_held"] == "100.00"
    assert shareholders[0]["source"]["type"] == SOURCE_MASTER_RECORD

    assert [change["event_id"] for change in state["changes"]] == [
        office_change["id"],
        appointment["id"],
    ]
    assert draft["applied_event_ids"] == [office_change["id"], appointment["id"]]
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unapplied_event"] == []
    # No prior filing for this company: nothing changed, no cross-check flags.
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unexpected_change"] == []
    assert all(entry["changed"] is False for entry in draft["field_provenance"].values())

    # Provenance: the statics trace to the company, the moved office to its
    # event, the officer blocks to the event that added an officer to them.
    assert draft["field_provenance"]["HDR-BRN"] == {
        "target": "company.br_number",
        "source_type": SOURCE_MASTER_RECORD,
        "source_id": company_id,
        "changed": False,
    }
    assert draft["field_provenance"]["6"]["source_type"] == SOURCE_CHANGE_EVENT
    assert draft["field_provenance"]["6"]["source_id"] == office_change["id"]
    assert draft["field_provenance"]["12A.name"]["source_type"] == SOURCE_CHANGE_EVENT
    assert draft["field_provenance"]["12A.name"]["source_id"] == appointment["id"]
    assert draft["field_provenance"]["11"]["source_type"] == SOURCE_MASTER_RECORD
    assert draft["field_provenance"]["PR"]["source_type"] == "none"
    assert draft["field_provenance"]["SIG.name"]["source_type"] == "none"

    # The events moved through the change-events executor, on this transaction.
    listed = {
        item["id"]: item["status"]
        for item in client.get(f"/api/v1/companies/{company_id}/change-events").json()
    }
    assert listed[office_change["id"]] == ChangeEventStatus.APPLIED_TO_DRAFT.value
    assert listed[appointment["id"]] == ChangeEventStatus.APPLIED_TO_DRAFT.value

    actions = await audit_actions(session, tenant.id)
    assert actions.count(AuditAction.DRAFT_GENERATED.value) == 1
    # Three transitions per event to confirm, plus the apply each generation made.
    assert actions.count(AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value) == 8


async def test_a_second_generation_appends_version_two(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    first = generate(place.client, place.company_id)
    second = generate(place.client, place.company_id)
    assert second["id"] == first["id"]
    assert second["version_no"] == 2

    listed = place.client.get(f"/api/v1/companies/{place.company_id}/drafts").json()
    assert [item["version_no"] for item in listed] == [2]
    assert [entry["version_no"] for entry in listed[0]["history"]] == [1]

    detail = place.client.get(f"/api/v1/drafts/{first['id']}").json()
    assert detail["version_no"] == 2
    assert detail["history"][0]["status"] == DraftStatus.GENERATED.value


async def test_the_gate_denial_writes_nothing(session: AsyncSession) -> None:
    place = await deployment(session, cover=False)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    response = place.client.post(
        f"/api/v1/companies/{place.company_id}/drafts",
        json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: no_assignment"
    assert (await session.scalars(select(Draft))).all() == []
    assert AuditAction.DRAFT_GENERATED.value not in await audit_actions(session, place.tenant.id)


async def test_a_free_tier_is_denied_by_the_capability_leg(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "license_tier", "free")
    place = await deployment(session)
    response = place.client.post(
        f"/api/v1/companies/{place.company_id}/drafts",
        json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: tier_capability_missing"
    assert (await session.scalars(select(Draft))).all() == []


async def test_nar1_on_a_non_private_company_is_refused_and_writes_nothing(
    session: AsyncSession,
) -> None:
    place = await deployment(session, company=PUBLIC_COMPANY)
    response = place.client.post(
        f"/api/v1/companies/{place.company_id}/drafts",
        json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "nar1_rule_not_established"
    assert (await session.scalars(select(Draft))).all() == []


async def test_an_nd2a_draft_reads_the_events_payloads(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    appointment = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id, form_type="nd2a")
    assert draft["private_company_statement"] is None
    assert draft["filing_metadata"]["return"]["made_up_date"] is None
    assert draft["applied_event_ids"] == [appointment["id"]]
    assert draft["field_provenance"]["3"]["source_type"] == SOURCE_CHANGE_EVENT
    assert draft["field_provenance"]["3"]["source_id"] == appointment["id"]
    assert draft["field_provenance"]["0"]["source_type"] == SOURCE_MASTER_RECORD
    assert draft["field_provenance"]["0"]["source_id"] == place.company_id
    # The §1 protected class has no column and no payload field: the ND2A PI
    # sheet item is unresolved, which is F4's stated boundary.
    assert draft["field_provenance"]["PI"]["source_type"] == "none"


# --- The prior-filing cross-check -------------------------------------------


async def test_the_cross_check_flags_an_unexplained_change(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    await seed_prior_batch(
        session,
        tenant=place.tenant,
        company_id=place.company_id,
        values={
            "company.name_en": "OLD NAME LIMITED",
            "company.br_number": "12345678",
        },
        # A rejected record is staff's decision that the filing did not state
        # the value: it is not part of the basis.
        rejected={"company.contact_phone_hk": "99999999"},
    )
    draft = generate(place.client, place.company_id)
    assert draft["field_provenance"]["1"]["changed"] is True
    assert draft["field_provenance"]["HDR-BRN"]["changed"] is False
    assert draft["qc_flags"] == [
        {
            "kind": "unexpected_change",
            "target": "company.name_en",
            "detail": "OLD NAME LIMITED -> BRIGHT LANTERN LIMITED",
        }
    ]


async def test_an_event_explained_change_is_not_flagged(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    await seed_prior_batch(
        session,
        tenant=place.tenant,
        company_id=place.company_id,
        values={"company.registered_office.flat": "12A"},
    )
    enter_event(
        place.client,
        place.company_id,
        type="registered_office_change",
        effective_date="2026-05-01",
        payload={"registered_office": {"address": MOVED_ADDRESS}},
    )
    draft = generate(place.client, place.company_id)
    assert draft["field_provenance"]["6"]["changed"] is True
    assert draft["field_provenance"]["6"]["source_type"] == SOURCE_CHANGE_EVENT
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unexpected_change"] == []


async def test_a_prior_batch_for_another_form_is_not_the_basis(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    appointment = enter_event(place.client, place.company_id)
    await seed_prior_batch(
        session,
        tenant=place.tenant,
        company_id=place.company_id,
        values={"company.name_en": "OLD NAME LIMITED"},
    )
    draft = generate(place.client, place.company_id, form_type="nd2a")
    assert draft["applied_event_ids"] == [appointment["id"]]
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unexpected_change"] == []
    assert all(entry["changed"] is False for entry in draft["field_provenance"].values())


# --- The §4.1 invalidation legs ---------------------------------------------


async def test_reverting_an_applied_event_invalidates_and_regeneration_relinks(
    session: AsyncSession,
) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)

    response = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "revert"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.INVALIDATED.value
    assert row.invalidated_at is not None
    assert row.invalidation_reason == f"change event {event['id']} revert"
    assert AuditAction.DRAFT_INVALIDATED.value in await audit_actions(session, place.tenant.id)

    # The event stays available; a regeneration links it again.
    second = generate(place.client, place.company_id)
    assert second["version_no"] == 2
    assert second["applied_event_ids"] == [event["id"]]
    assert second["status"] == DraftStatus.GENERATED.value


async def test_deleting_an_applied_event_invalidates_the_draft(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    response = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "delete"}
    )
    assert response.status_code == 200, response.text
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.INVALIDATED.value
    assert row.invalidation_reason == f"change event {event['id']} delete"


async def test_editing_an_applied_event_invalidates_the_draft(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    response = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions",
        json={"action": "edit", "effective_date": "2026-06-15"},
    )
    assert response.status_code == 200, response.text
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.INVALIDATED.value
    assert row.invalidation_reason == f"change event {event['id']} edit"


async def test_a_staff_recorded_client_withdrawal_invalidates_the_draft(
    session: AsyncSession,
) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    without_reason = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions",
        json={"action": "client_withdraw"},
    )
    assert without_reason.status_code == 422
    response = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions",
        json={
            "action": "client_withdraw",
            "rationale": "client telephoned to withdraw it",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ChangeEventStatus.WITHDRAWN.value
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.INVALIDATED.value
    assert row.invalidation_reason == f"change event {event['id']} client_withdraw"


async def test_a_cessation_closes_only_the_office_its_type_names(session: AsyncSession) -> None:
    """The sole director who is also the company secretary — the common Hong Kong
    shape: one person, two records. Each cessation closes only the office its
    type names, through the real API and a real generation (identity alone would
    have matched both records and left both events unapplied)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    director = create_officer(place.client, place.company_id)
    secretary = create_officer(
        place.client,
        place.company_id,
        person=None,
        person_id=director["person"]["id"],
        role="company_secretary",
        capacity=["company_secretary"],
    )
    assert secretary["person"]["id"] == director["person"]["id"]
    office_cessation = enter_event(
        place.client,
        place.company_id,
        type="director_cessation",
        effective_date="2026-08-01",
        payload={
            "cessation": {
                "officer": {
                    "role": "director",
                    "name_zh": "陳大文",
                    "name_en": "CHAN Tai Man",
                    "hkid_partial": "A1234",
                    "cessation_date": "2026-08-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    secretary_cessation = enter_event(
        place.client,
        place.company_id,
        type="secretary_cessation",
        effective_date="2026-09-01",
        payload={
            "cessation": {
                "officer": {
                    "role": "company_secretary",
                    "name_zh": "陳大文",
                    "name_en": "CHAN Tai Man",
                    "hkid_partial": "A1234",
                    "cessation_date": "2026-09-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    draft = generate(place.client, place.company_id)
    assert draft["applied_event_ids"] == [office_cessation["id"], secretary_cessation["id"]]
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unapplied_event"] == []
    closings = {
        entry["role"]: entry["valid_to"] for entry in draft["resolved_state"]["officers"]
    }
    assert closings == {"director": "2026-08-01", "company_secretary": "2026-09-01"}


async def test_an_event_no_draft_linked_touches_no_draft(session: AsyncSession) -> None:
    """§4.1's leg reads the *linked* drafts: an event the overlay never applied
    (a cessation matching no in-office record) is a no-op when it is deleted."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(
        place.client,
        place.company_id,
        type="director_cessation",
        effective_date="2026-07-01",
        payload={
            "cessation": {
                "officer": {
                    "role": "director",
                    "name_en": "SOMEONE ELSE",
                    "cessation_date": "2026-07-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    draft = generate(place.client, place.company_id)
    assert draft["applied_event_ids"] == []
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unapplied_event"]
    response = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "delete"}
    )
    assert response.status_code == 200, response.text
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.GENERATED.value
    assert row.invalidated_at is None


# --- The §4.2 executor -------------------------------------------------------


async def test_the_transitions_and_the_edit_flow(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"

    offered = place.client.get(url).json()
    assert offered["status"] == "generated"
    assert [item["action"] for item in offered["actions"]] == ["edit", "mark_under_review"]

    # An edit that states nothing is refused; a shape the domain rejects is
    # refused with its named reason; a good edit moves the draft.
    assert place.client.post(url, json={"action": "edit"}).status_code == 422
    # P6-2: ``confirm_statement`` is ``approve``'s shape alone — an edit
    # carrying it is refused by name and writes nothing (the edit-fields
    # pattern verbatim).
    carrying = place.client.post(url, json={"action": "edit", "confirm_statement": True})
    assert carrying.status_code == 422
    assert carrying.json()["detail"] == "edit: carries confirm_statement"
    bad = place.client.post(
        url,
        json={"action": "edit", "filing_metadata": {"member_particulars_delivery": "pigeon"}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == "filing_metadata_invalid: member_particulars_delivery"
    ok = place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {
                "return": {
                    "fs_period_start": "2025-04-01",
                    "fs_period_end": "2026-03-31",
                },
                "member_particulars_delivery": "schedule_1",
                "presentor": {"name": "K2P SERVICES LIMITED", "reference": "NAR1-2026"},
            },
            "rationale": "figures from the client's accounts",
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "under_review"
    assert body["filing_metadata"]["return"] == {
        "made_up_date": "2026-03-01",
        "fs_period_start": "2025-04-01",
        "fs_period_end": "2026-03-31",
    }
    assert body["filing_metadata"]["presentor"]["reference"] == "NAR1-2026"
    assert body["rationale"] == "figures from the client's accounts"

    # From under_review the one wired row is the release (F4b); the rows
    # Phase 6 has since wired refuse this status rather than half-run.
    assert place.client.post(url, json=MARK).status_code == 409
    assert [item["action"] for item in place.client.get(url).json()["actions"]] == [
        "send_for_signature"
    ]
    for action, reason in (
        ("upload_signed", "transition_not_allowed"),
        ("recall", "transition_not_allowed"),
        # P6-2 wired the approval rows: from `under_review` they refuse as
        # inadmissible (a 409 about the table), not as unwired.
        ("mark_reviewed", "transition_not_allowed"),
        ("approve", "transition_not_allowed"),
        # P6-4 wired the export: from `under_review` it refuses as
        # inadmissible like every other wired row, not as unwired.
        ("export", "transition_not_allowed"),
        # P6-3 wired regeneration: from `under_review` it refuses as
        # inadmissible like every other wired row, not as unwired.
        ("regenerate", "transition_not_allowed"),
        # `invalidate` is the system's: a staff POST cannot perform it.
        ("invalidate", "transition_not_allowed"),
    ):
        refused = place.client.post(url, json={"action": action})
        assert refused.status_code == 409, action
        assert refused.json()["detail"] == reason

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.UNDER_REVIEW.value
    assert row.updated_at is not None


async def test_the_audit_chain_verifies(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    place.client.post(f"/api/v1/drafts/{draft['id']}/transitions", json=MARK)
    place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "revert"}
    )
    entries = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.tenant_id == place.tenant.id)
                .order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []


# --- The release: the signature-ready package (F4b) --------------------------


async def test_the_release_stores_the_signature_ready_package(
    session: AsyncSession,
) -> None:
    """``send_for_signature`` end to end: plan → fill → store → move → audit.

    A real draft over real master data, released through the real route with
    the pin's own store: the filled CR PDF lands in the registry as a WORM
    ``generated_draft`` document under the tenant's ``generated`` naming
    template, the draft and its signature lifecycle move, the response carries
    the package block with the fill's escalations, the chain verifies, and the
    download route serves the bytes.
    """
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    create_officer(place.client, place.company_id)
    create_shareholding(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"

    # An edit stating the delivery channel only: the presentor and the
    # signature are still to be made, which is ordinary work in progress and
    # what the package's escalations will name.
    edit = place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["status"] == "under_review"
    assert edit.json()["signature_status"] == "not_sent"

    released = place.client.post(url, json={"action": "send_for_signature"})
    assert released.status_code == 200, released.text
    body = released.json()
    assert body["status"] == "sent_for_signature"
    assert body["signature_status"] == "sent_for_signature"

    package = body["signature_package"]
    assert package is not None
    escalations = package["escalations"]
    assert escalations, "an unstated presentor and signature are the human's checklist"
    assert all(item["kind"] and item["detail"] for item in escalations)
    assert any("presentor" in item["detail"] for item in escalations)

    # The row moved on the same session, and the document is registered.
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SENT_FOR_SIGNATURE.value
    assert row.signature_status == SignatureStatus.SENT_FOR_SIGNATURE.value

    document_id = package["document_id"]
    document = await session.scalar(
        select(Document).where(Document.id == UUID(document_id))
    )
    assert document is not None
    assert document.kind == DocumentKind.GENERATED_DRAFT.value
    assert document.origin == DocumentOrigin.GENERATED.value
    assert document.retention_class == RetentionClass.GENERATED_DRAFT.value
    assert document.content_type == "application/pdf"
    assert document.scan_status == ScanStatus.CLEAN.value
    assert document.supersedes_document_id is None
    # The tenant's ``generated`` template, with this version's number (§5.3).
    assert document.storage_key.endswith("_v1.pdf")
    assert "NAR1_draft" in document.storage_key
    assert document.storage_key.startswith("1234567/")

    # The stored bytes are the filled package: a real PDF of the pinned
    # template's own length (27 pages: main form, sheets and guidance notes).
    stored = place.store.objects[document.storage_key]
    assert stored.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(stored)).pages) == 27
    assert document.byte_size == len(stored)

    # The download route serves it (scanned clean, and a draft is not a
    # client-bound artifact — the release gate covers the confirmation letter).
    download = place.client.get(
        f"/api/v1/companies/{place.company_id}/documents/{document_id}/download"
    )
    assert download.status_code == 200, download.text
    assert place.store.objects[document.storage_key]  # the blob is the served one
    assert document.storage_key in download.json()["url"]

    # Both entries are on the chain, and the chain verifies.
    actions = await audit_actions(session, place.tenant.id)
    assert AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED.value in actions
    assert AuditAction.DOCUMENT_REGISTERED.value in actions
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    package_entry = next(
        entry
        for entry in entries
        if entry.action == AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED.value
    )
    assert package_entry.details["document_id"] == document_id
    assert package_entry.details["escalations"] == escalations


async def test_the_release_stores_an_overflow_package(
    session: AsyncSession,
) -> None:
    """A draft whose package carries a flattened duplicate still releases.

    A third natural-person director puts one copy of Continuation Sheet C in
    place and a **second** one in the package as a flattened duplicate — the
    shape that exceeded the 25 MiB upload cap while each duplicate embedded the
    whole face, so ``send_for_signature`` could not store such a draft at all
    (``UploadTooLargeError``, unhandled, a 500). The release goes through the
    real route and the registry's own write path: the document stores under the
    cap, the draft and its signature lifecycle move, the stored bytes are the
    assembled package (28 pages: the duplicate is in it), and the chain
    verifies.
    """
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    for index in range(3):
        create_officer(
            place.client,
            place.company_id,
            person={
                "person_type": "natural_person",
                "name_zh": "陳大文" if index == 0 else f"黃小明{index}",
                "name_en": f"WONG Siu Ming {index}",
                "surname_en": "WONG",
                "other_names_en": f"SIU MING {index}",
                "hkid_partial": f"B45{index}",
            },
            valid_from=f"2024-0{index + 1}-01",
        )
    create_shareholding(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"
    place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )

    released = place.client.post(url, json={"action": "send_for_signature"})
    assert released.status_code == 200, released.text
    body = released.json()
    assert body["status"] == "sent_for_signature"
    assert body["signature_status"] == "sent_for_signature"

    package = body["signature_package"]
    assert package is not None
    document_id = package["document_id"]
    document = await session.scalar(select(Document).where(Document.id == UUID(document_id)))
    assert document is not None
    assert document.kind == DocumentKind.GENERATED_DRAFT.value
    stored = place.store.objects[document.storage_key]
    assert stored.startswith(b"%PDF")
    assert document.byte_size == len(stored)
    assert document.byte_size <= MAX_UPLOAD_BYTES
    # The package is the template plus the duplicate sheet: 28 pages, the
    # continuation copy the overflow needs.
    assert len(PdfReader(io.BytesIO(stored)).pages) == 28

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SENT_FOR_SIGNATURE.value
    assert row.signature_status == SignatureStatus.SENT_FOR_SIGNATURE.value

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    actions = [entry.action for entry in entries]
    assert AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED.value in actions
    assert AuditAction.DOCUMENT_REGISTERED.value in actions


async def test_a_release_without_a_readable_font_refuses_and_writes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The font refusal is loud, named, and leaves no trace."""
    monkeypatch.setattr(settings, "draft_font_path", "/nonexistent/NotoSansHK.ttf")
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"
    place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )

    refused = place.client.post(url, json={"action": "send_for_signature"})
    assert refused.status_code == 503
    assert refused.json()["detail"] == "draft_font_unavailable"

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.UNDER_REVIEW.value
    assert row.signature_status == SignatureStatus.NOT_SENT.value
    assert place.store.objects == {}
    assert (await session.scalars(select(Document))).all() == []
    assert AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED.value not in await audit_actions(
        session, place.tenant.id
    )


# --- The client signature upload (scope §2.10, P6-1) ---------------------------


async def test_the_signed_upload_moves_the_draft_and_stores_the_worm_document(
    session: AsyncSession,
) -> None:
    """``POST /drafts/{id}/signature`` end to end: store → move → audit.

    A real draft released for signature, then the client-signed copy uploaded
    through the real route: the WORM ``signed_draft`` document lands in the
    registry under the tenant's ``document`` naming template, scanned clean,
    the draft and its signature lifecycle move on the same session, the
    transition's audit entry names the document, and the chain verifies. The
    double upload refuses — ``signature_received`` admits nothing — and
    leaves exactly one signed document behind.
    """
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"
    place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )
    released = place.client.post(url, json={"action": "send_for_signature"})
    assert released.status_code == 200, released.text

    # The offered actions at ``sent_for_signature`` are P6-1's two rows (the
    # Administrator holds all three legs, and the table's order is §4.2's).
    offered = place.client.get(url).json()["actions"]
    assert [item["action"] for item in offered] == ["upload_signed", "recall"]

    # But the upload's performer is the dedicated route: a transitions POST
    # of ``upload_signed`` has no file to store and refuses rather than
    # recording a signature the registry does not hold (the system-row
    # precedent — nothing moves, nothing is stored, the chain gains nothing).
    via_transitions = place.client.post(url, json={"action": "upload_signed"})
    assert via_transitions.status_code == 409
    assert via_transitions.json()["detail"] == "transition_not_allowed"
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SENT_FOR_SIGNATURE.value
    signed_now = list(
        (
            await session.scalars(
                select(Document).where(
                    Document.tenant_id == place.tenant.id,
                    Document.kind == DocumentKind.SIGNED_DRAFT.value,
                )
            )
        ).all()
    )
    assert signed_now == []

    body = b"%PDF-1.4\n%signed-by-the-client\n"
    uploaded = place.client.post(
        f"/api/v1/drafts/{draft['id']}/signature",
        content=body,
        headers={"Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    moved = uploaded.json()
    assert moved["status"] == "signature_received"
    assert moved["signature_status"] == "signature_received"
    assert moved["signature_package"] is None

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SIGNATURE_RECEIVED.value
    assert row.signature_status == SignatureStatus.SIGNATURE_RECEIVED.value

    # Exactly one signed document: the registry's write path, under the
    # tenant's ``document`` template ({CompanyCode}/{Year}/{Form}_{DocType}_
    # {CR}_{YYYYMMDD}.{Ext} — no version token, unlike the release's
    # ``generated`` template), scanned clean, the kind's default retention.
    registered = list(
        (await session.scalars(select(Document).where(Document.tenant_id == place.tenant.id)))
        .all()
    )
    signed = [doc for doc in registered if doc.kind == DocumentKind.SIGNED_DRAFT.value]
    assert len(signed) == 1
    document = signed[0]
    assert document.origin == DocumentOrigin.UPLOAD.value
    assert document.retention_class == RetentionClass.STATUTORY_FILING.value
    assert document.content_type == "application/pdf"
    assert document.scan_status == ScanStatus.CLEAN.value
    assert document.byte_size == len(body)
    assert place.store.objects[document.storage_key] == body
    assert document.storage_key.startswith("1234567/")
    assert "nar1_signed_1234567_" in document.storage_key
    assert document.storage_key.endswith(".pdf")

    # The chain verifies, and the transition's entry names the document and
    # carries the signature pair's before/after.
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    transition_entry = next(
        entry
        for entry in entries
        if entry.action == AuditAction.DRAFT_TRANSITION_RECORDED.value
        and entry.details.get("action") == "upload_signed"
    )
    assert transition_entry.details["document_id"] == str(document.id)
    assert transition_entry.details["before_values"] == {"signature_status": "sent_for_signature"}
    assert transition_entry.details["after_values"] == {
        "signature_status": "signature_received"
    }

    # The double upload: a second POST lands on ``signature_received``, which
    # admits nothing — 409, and exactly one signed document stays.
    second = place.client.post(
        f"/api/v1/drafts/{draft['id']}/signature",
        content=body,
        headers={"Content-Type": "application/pdf"},
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "transition_not_allowed"
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SIGNATURE_RECEIVED.value
    signed_again = list(
        (
            await session.scalars(
                select(Document).where(
                    Document.tenant_id == place.tenant.id,
                    Document.kind == DocumentKind.SIGNED_DRAFT.value,
                )
            )
        ).all()
    )
    assert len(signed_again) == 1
    entries_after = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert len(entries_after) == len(entries)
    assert verify_chain(entries_after) == []


async def test_a_signed_upload_refused_by_validation_writes_nothing(
    session: AsyncSession,
) -> None:
    """The documents seam's refusals happen before anything is written: a
    body declared ``text/plain`` that is really a PDF answers 422 with the
    seam's own words, and no document row, no status move, no audit entry
    exists for it."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = f"/api/v1/drafts/{draft['id']}/transitions"
    place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )
    released = place.client.post(url, json={"action": "send_for_signature"})
    assert released.status_code == 200, released.text

    actions_before = await audit_actions(session, place.tenant.id)
    refused = place.client.post(
        f"/api/v1/drafts/{draft['id']}/signature",
        content=b"this is not a PDF at all\n",
        headers={"Content-Type": "application/pdf"},
    )
    assert refused.status_code == 422
    assert "does not match declared type" in refused.json()["detail"]

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SENT_FOR_SIGNATURE.value
    assert row.signature_status == SignatureStatus.SENT_FOR_SIGNATURE.value
    registered = list(
        (await session.scalars(select(Document).where(Document.tenant_id == place.tenant.id)))
        .all()
    )
    assert [doc.kind for doc in registered] == [DocumentKind.GENERATED_DRAFT.value]
    # The refusal wrote nothing: the chain is exactly what the release left.
    assert await audit_actions(session, place.tenant.id) == actions_before


# --- The review/approval gate (scope §2.11, P6-2) ------------------------------

SIGNED_BYTES = b"%PDF-1.4\n%signed-by-the-client\n"


async def drive_to_signature_received(place: Deployment, draft_id: str) -> str:
    """generate → edit → release → the client-signed upload: the gate's entry
    state (``signature_received``), driven through the real routes. Returns
    the transitions URL."""
    url = f"/api/v1/drafts/{draft_id}/transitions"
    edit = place.client.post(
        url,
        json={
            "action": "edit",
            "filing_metadata": {"member_particulars_delivery": "schedule_1"},
        },
    )
    assert edit.status_code == 200, edit.text
    released = place.client.post(url, json={"action": "send_for_signature"})
    assert released.status_code == 200, released.text
    uploaded = place.client.post(
        f"/api/v1/drafts/{draft_id}/signature",
        content=SIGNED_BYTES,
        headers={"Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    return url


async def test_the_review_and_approval_gate_records_the_stamps(
    session: AsyncSession,
) -> None:
    """The gate end to end: mark_reviewed → approve (with the item-16
    confirmation), the stamp pairs written with the moves, the statement
    refusal landing before anything is written, and the chain verifying.

    One account reviews and approves — four-eyes is permissive per scope
    §2.11, and this lifecycle is the same-actor case the plan pins."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])

    # At `signature_received` the reviewer's two rows are offered — the
    # rejection rationale-required — in §4.2's order.
    offered = place.client.get(url).json()["actions"]
    assert [item["action"] for item in offered] == ["mark_reviewed", "reject"]
    assert offered[1]["rationale_required"] is True

    reviewed = place.client.post(url, json={"action": "mark_reviewed"})
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["status"] == "reviewed"
    assert body["reviewed_by"] == str(place.admin.id)
    assert body["reviewed_at"] is not None
    assert body["approved_by"] is None
    assert body["statement_confirmed_by"] is None

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REVIEWED.value
    assert row.reviewed_by == place.admin.id
    assert row.reviewed_at is not None
    assert row.approved_by is None
    assert row.approved_at is None
    assert row.statement_confirmed_by is None

    # At `reviewed` the approver's two rows are offered.
    offered = place.client.get(url).json()["actions"]
    assert [item["action"] for item in offered] == ["approve", "reject"]
    assert offered[1]["rationale_required"] is True

    # The statement gate (ruling R2): the NAR1 makes the item-16 declaration,
    # so approval without the confirmation refuses by name and writes nothing.
    refused = place.client.post(url, json={"action": "approve"})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "statement_confirmation_required"
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REVIEWED.value
    assert row.approved_by is None
    assert row.statement_confirmed_by is None

    approved = place.client.post(
        url, json={"action": "approve", "confirm_statement": True}
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(place.admin.id)
    assert body["approved_at"] is not None
    assert body["statement_confirmed_by"] == str(place.admin.id)
    assert body["statement_confirmed_at"] is not None

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.APPROVED.value
    assert row.approved_by == place.admin.id
    assert row.approved_at is not None
    assert row.statement_confirmed_by == place.admin.id
    assert row.statement_confirmed_at is not None

    # Export is the one row P6-4 wired behind the approval: an approver at
    # `approved` is offered exactly it (the transitions read is the action
    # buttons' sole authority).
    offered = place.client.get(url).json()["actions"]
    assert offered == [
        {"action": "export", "to_status": "exported", "rationale_required": False}
    ]

    # The chain verifies; each transition entry names its action and the
    # approval carries no rationale (none was given).
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    transitions = [
        entry
        for entry in entries
        if entry.action == AuditAction.DRAFT_TRANSITION_RECORDED.value
    ]
    assert [entry.details["action"] for entry in transitions] == [
        "edit",
        "send_for_signature",
        "upload_signed",
        "mark_reviewed",
        "approve",
    ]
    approve_entry = transitions[-1]
    assert approve_entry.actor_user_id == place.admin.id
    assert "rationale" not in approve_entry.details


async def test_a_rejection_from_reviewed_clears_the_stamps(
    session: AsyncSession,
) -> None:
    """The stamp convention's clearing half: a rejection lands on `rejected`,
    which carries no stamp, so the review pair set a moment earlier is
    cleared with the move — the row must not keep telling a story the status
    has overruled. The rationale is the §2.11 record of the decision, on the
    row and in the chain."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])
    reviewed = place.client.post(url, json={"action": "mark_reviewed"})
    assert reviewed.status_code == 200, reviewed.text

    # The rationale is required at both legs: without it the refusal is 422
    # and the draft — review stamp included — stays exactly where it stood.
    without_reason = place.client.post(url, json={"action": "reject"})
    assert without_reason.status_code == 422
    assert without_reason.json()["detail"] == "rationale_required"
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REVIEWED.value
    assert row.reviewed_by == place.admin.id

    rejected = place.client.post(
        url,
        json={"action": "reject", "rationale": "the client signed the wrong page"},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["rationale"] == "the client signed the wrong page"

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REJECTED.value
    assert row.reviewed_by is None
    assert row.reviewed_at is None
    assert row.approved_by is None
    assert row.approved_at is None
    assert row.statement_confirmed_by is None
    assert row.statement_confirmed_at is None
    assert row.rationale == "the client signed the wrong page"

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    reject_entry = next(
        entry
        for entry in entries
        if entry.action == AuditAction.DRAFT_TRANSITION_RECORDED.value
        and entry.details.get("action") == "reject"
    )
    assert reject_entry.details["rationale"] == "the client signed the wrong page"


async def test_rejects_two_legs_are_enforced_by_flag(session: AsyncSession) -> None:
    """Review Focus 3: ``reject`` from ``signature_received`` is a reviewer's
    row, from ``reviewed`` an approver's — the wrong flag refuses 403 (the
    actor's authority), never 409 (the table's legality)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])

    reviewer_user = User(
        tenant_id=place.tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Reviewer",
        role="general",
        review_drafts=True,
    )
    approver_user = User(
        tenant_id=place.tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Approver",
        role="general",
        approve_drafts=True,
    )
    session.add_all([reviewer_user, approver_user])
    await session.flush()
    session.add_all(
        [
            CompanyAccessGrant(
                user_id=reviewer_user.id,
                company_id=UUID(place.company_id),
                tenant_id=place.tenant.id,
            ),
            CompanyAccessGrant(
                user_id=approver_user.id,
                company_id=UUID(place.company_id),
                tenant_id=place.tenant.id,
            ),
        ]
    )
    await session.flush()
    reviewer = make_client(
        session, ActorRef(user_id=reviewer_user.id, tenant_id=place.tenant.id)
    )
    approver = make_client(
        session, ActorRef(user_id=approver_user.id, tenant_id=place.tenant.id)
    )

    # The approver-only account holds the wrong flag for the reviewer's row:
    # 403, and the draft has not moved.
    refused = approver.post(url, json={"action": "reject", "rationale": "not mine"})
    assert refused.status_code == 403
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.SIGNATURE_RECEIVED.value

    # The reviewer performs their leg (rationale required, then accepted)…
    missing = reviewer.post(url, json={"action": "reject"})
    assert missing.status_code == 422
    assert missing.json()["detail"] == "rationale_required"
    done = reviewer.post(
        url, json={"action": "reject", "rationale": "signature illegible"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "rejected"

    # …and a second draft walks the other leg: the reviewer cannot approve
    # (their flag is wrong at `reviewed` — the same 403), the approver can.
    second = generate(place.client, place.company_id)
    second_url = await drive_to_signature_received(place, second["id"])
    marked = reviewer.post(second_url, json={"action": "mark_reviewed"})
    assert marked.status_code == 200, marked.text
    wrong_flag = reviewer.post(
        second_url, json={"action": "reject", "rationale": "still not mine"}
    )
    assert wrong_flag.status_code == 403
    by_approver = approver.post(
        second_url, json={"action": "reject", "rationale": "dates disagree"}
    )
    assert by_approver.status_code == 200, by_approver.text
    assert by_approver.json()["status"] == "rejected"

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []


async def test_a_statement_confirmation_on_a_non_statement_draft_is_refused(
    session: AsyncSession,
) -> None:
    """ND2A makes no item-16 statement (its flag is null), so a confirmation
    refuses `statement_not_made` and plain approval — no confirmation at all —
    is exactly what the gate expects. The export gate is unaffected (Review
    Focus 2's ND2A leg)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id, form_type="nd2a")
    assert draft["private_company_statement"] is None
    url = await drive_to_signature_received(place, draft["id"])
    reviewed = place.client.post(url, json={"action": "mark_reviewed"})
    assert reviewed.status_code == 200, reviewed.text

    refused = place.client.post(
        url, json={"action": "approve", "confirm_statement": True}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "statement_not_made"
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REVIEWED.value
    assert row.statement_confirmed_by is None

    approved = place.client.post(url, json={"action": "approve"})
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(place.admin.id)
    assert body["statement_confirmed_by"] is None


async def test_an_invalidated_draft_clears_the_approval_stamps(
    session: AsyncSession,
) -> None:
    """The stamp convention's other clearing path: an approved draft whose
    applied event is withdrawn is invalidated by the system leg, and the
    invalidation — a landing outside every carrying set — takes the review,
    approval and statement pairs with it."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])
    place.client.post(url, json={"action": "mark_reviewed"})
    approved = place.client.post(
        url, json={"action": "approve", "confirm_statement": True}
    )
    assert approved.status_code == 200, approved.text
    row = await current_draft(session, draft["id"])
    assert row.statement_confirmed_by == place.admin.id

    withdrawn = place.client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "revert"}
    )
    assert withdrawn.status_code == 200, withdrawn.text

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.INVALIDATED.value
    assert row.reviewed_by is None
    assert row.reviewed_at is None
    assert row.approved_by is None
    assert row.approved_at is None
    assert row.statement_confirmed_by is None
    assert row.statement_confirmed_at is None

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []


# --- Regeneration from `rejected` (P6-3) ---------------------------------------


async def test_a_rejected_lineage_regenerates_into_the_next_version(
    session: AsyncSession,
) -> None:
    """P6-3 end to end: a rejection leaves the lineage recoverable — the
    ``regenerate`` row appends ``version_no + 1`` to the same lineage, built
    from the *current* master data and the still-approved events, clean of
    every stamp by construction, with the generation's own audit story (one
    ``draft_generated`` entry naming the superseded version — no separate
    transition entry)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])
    place.client.post(url, json={"action": "mark_reviewed"})

    rejected = place.client.post(
        url,
        json={"action": "reject", "rationale": "the client signed the wrong page"},
    )
    assert rejected.status_code == 200, rejected.text

    # Between the rejection and the regeneration, master data moves: the
    # company is renamed, effective before the target date. The fresh
    # version must resolve the world as it stands now, not replay v1.
    await company_master.record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=place.tenant.id,
        company_id=UUID(place.company_id),
        effective_date=date(2026, 6, 15),
        name_en="LANTERN REBORN LIMITED",
        name_zh="明燈有限公司",
        business_name=None,
        created_by=None,
    )
    await session.flush()

    # At `rejected` the reviewer's one row is `regenerate` — no rationale
    # (the rejection carried it) — and the approver is offered nothing.
    offered = place.client.get(url).json()["actions"]
    assert [item["action"] for item in offered] == ["regenerate"]
    assert offered[0]["to_status"] == "generated"
    assert offered[0]["rationale_required"] is False

    # The row is rationale-free: a stated rationale is the body's shape for
    # `reject`/`edit` alone — the confirm_statement pattern — refused by
    # name before anything is built, and nothing is written.
    actions_before = await audit_actions(session, place.tenant.id)
    carrying = place.client.post(
        url, json={"action": "regenerate", "rationale": "client clarified"}
    )
    assert carrying.status_code == 422
    assert carrying.json()["detail"] == "regenerate: carries rationale"
    assert await audit_actions(session, place.tenant.id) == actions_before
    rows_now = list((await session.scalars(select(Draft))).all())
    assert [(row.version_no, row.status) for row in rows_now] == [(1, "rejected")]

    regenerated = place.client.post(url, json={"action": "regenerate"})
    assert regenerated.status_code == 200, regenerated.text
    body = regenerated.json()

    # The new version: the next number of the same lineage, `generated`,
    # every stamp clean by construction, the signature lifecycle reset.
    assert body["id"] == draft["id"]
    assert body["version_no"] == 2
    assert body["status"] == "generated"
    assert body["target_date"] == "2026-09-30"
    assert body["signature_status"] == "not_sent"
    assert body["reviewed_by"] is None
    assert body["reviewed_at"] is None
    assert body["approved_by"] is None
    assert body["approved_at"] is None
    assert body["statement_confirmed_by"] is None
    assert body["statement_confirmed_at"] is None
    assert body["rationale"] is None
    # The history shows both versions, newest first.
    assert [(entry["version_no"], entry["status"]) for entry in body["history"]] == [
        (1, "rejected")
    ]

    # The resolution reflects the current master data: the rename landed
    # after v1 and is what v2 states (v1 still holds the old name).
    assert body["resolved_state"]["company"]["name"]["name_en"] == "LANTERN REBORN LIMITED"
    first = await session.scalar(
        select(Draft).where(Draft.id == UUID(draft["id"]), Draft.version_no == 1)
    )
    assert first is not None
    assert first.status == DraftStatus.REJECTED.value
    assert first.rationale == "the client signed the wrong page"
    assert first.resolved_state["company"]["name"]["name_en"] == "BRIGHT LANTERN LIMITED"

    # The still-approved event is what the resolution actually applied —
    # the same rule as generation (it was `applied_to_draft` already, and
    # stays there: linking is not a second application).
    assert body["applied_event_ids"] == [event["id"]]
    assert [change["event_id"] for change in body["resolved_state"]["changes"]] == [event["id"]]
    listed = {
        item["id"]: item["status"]
        for item in place.client.get(f"/api/v1/companies/{place.company_id}/change-events").json()
    }
    assert listed[event["id"]] == ChangeEventStatus.APPLIED_TO_DRAFT.value

    # One audit story per mutation: the regeneration appends exactly one
    # `draft_generated` entry — naming the superseded version, with no
    # `draft_transition_recorded` duplication — and the chain verifies.
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []
    generated = [
        entry for entry in entries if entry.action == AuditAction.DRAFT_GENERATED.value
    ]
    assert len(generated) == 2
    regeneration_entry = generated[-1]
    assert regeneration_entry.details["version_no"] == 2
    assert regeneration_entry.details["regenerated"] is True
    assert regeneration_entry.details["regenerated_from_version"] == 1
    assert regeneration_entry.details["applied_event_ids"] == [event["id"]]
    transition_actions = [
        entry.details.get("action")
        for entry in entries
        if entry.action == AuditAction.DRAFT_TRANSITION_RECORDED.value
    ]
    assert "regenerate" not in transition_actions
    # Only the two generations wrote `draft_generated`; the refusal-free
    # path appended nothing else beyond the gate-checked build.
    assert len(entries) == len(actions_before) + 1


async def test_regenerate_refuses_when_the_gate_denies_and_writes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R3 / Review Focus 5: regeneration is gated exactly like
    generation. A tier that no longer licenses ``automated_prefill`` refuses
    403 with the gate's own reason and writes **nothing** — the lineage
    stays rejected at version 1, no version row appends, the audit chain
    gains no entry."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])
    place.client.post(url, json={"action": "mark_reviewed"})
    rejected = place.client.post(
        url,
        json={"action": "reject", "rationale": "figures did not agree"},
    )
    assert rejected.status_code == 200, rejected.text

    # The licence lapses to Free after the rejection: the tier's capability
    # leg is what the gate consults (the change-events/generation precedent).
    monkeypatch.setattr(settings, "license_tier", "free")
    actions_before = await audit_actions(session, place.tenant.id)

    refused = place.client.post(url, json={"action": "regenerate"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "automation_denied: tier_capability_missing"

    rows = list((await session.scalars(select(Draft))).all())
    assert [(row.version_no, row.status) for row in rows] == [(1, "rejected")]
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.REJECTED.value
    assert row.signature_status == SignatureStatus.SIGNATURE_RECEIVED.value
    # The refusal wrote nothing at all — not even an audit entry.
    assert await audit_actions(session, place.tenant.id) == actions_before
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == place.tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert verify_chain(entries) == []


# --- The export: FilingPackage, artifacts, submit, audit (P6-4) -------------


async def drive_to_approved(place: Deployment, draft_id: str) -> str:
    """``signature_received`` → ``reviewed`` → ``approved`` with the item-16
    confirmation (ruling R2) — the export's entry state, driven through the
    real routes. Returns the transitions URL."""
    url = await drive_to_signature_received(place, draft_id)
    reviewed = place.client.post(url, json={"action": "mark_reviewed"})
    assert reviewed.status_code == 200, reviewed.text
    approved = place.client.post(url, json={"action": "approve", "confirm_statement": True})
    assert approved.status_code == 200, approved.text
    return url


async def stored_event(session: AsyncSession, event_id: str) -> ChangeEvent:
    row: ChangeEvent | None = await session.scalar(
        select(ChangeEvent)
        .where(ChangeEvent.id == UUID(event_id))
        .order_by(ChangeEvent.version_no.desc())
        .limit(1)
    )
    assert row is not None
    return row


async def filing_packages(session: AsyncSession, draft_id: str) -> list[FilingPackage]:
    rows = await session.scalars(
        select(FilingPackage).where(FilingPackage.draft_id == UUID(draft_id))
    )
    return list(rows.all())


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def test_the_export_lifecycle_stores_the_paper_package_and_submits_the_events(
    session: AsyncSession,
) -> None:
    """The filing-affecting core end to end: generate → release → upload →
    review → approve (with the item-16 confirmation) → export (paper) — one
    FilingPackage, the flattened PDF and the checklist stored as WORM
    ``export_package`` documents, the applied event ``submitted``, the audit
    chain verifying (``draft_exported`` beside the transition), and download
    URLs issuing for both package documents."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    event = enter_event(place.client, place.company_id)
    create_shareholding(place.client, place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_approved(place, draft["id"])

    # At `approved` the transitions read offers exactly the export row.
    assert place.client.get(url).json()["actions"] == [
        {"action": "export", "to_status": "exported", "rationale_required": False}
    ]

    exported = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "paper"}
    )
    assert exported.status_code == 201, exported.text
    package = exported.json()
    assert package["draft_id"] == draft["id"]
    assert package["draft_version_no"] == 1
    assert package["mode"] == "paper"
    assert package["exported_by"] == str(place.admin.id)
    # The member fills one Schedule 1 sheet: the derived roster counts it,
    # and its footer pair is the one number the flat PDF actually prints.
    assert package["envelope"]["sheet_counts"] == {"s1": 1}
    assert package["envelope"]["page_pairs"] == [[1, 1]]
    assert package["envelope"]["helper_document_id"] is None
    assert package["envelope"]["checklist_document_id"] is not None

    # The draft moved; the approval and statement stamps ride to `exported`.
    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.EXPORTED.value
    assert row.approved_by == place.admin.id
    assert row.statement_confirmed_by == place.admin.id

    # The package documents: the flat primary and the markdown checklist,
    # both WORM `export_package` rows stored through the registry's path.
    documents = list(
        (
            await session.scalars(
                select(Document).where(
                    Document.company_id == UUID(place.company_id),
                    Document.kind == DocumentKind.EXPORT_PACKAGE.value,
                )
            )
        ).all()
    )
    assert {str(document.id) for document in documents} == {
        package["document_id"],
        package["envelope"]["checklist_document_id"],
    }
    primary = next(
        document for document in documents if str(document.id) == package["document_id"]
    )
    assert primary.content_type == "application/pdf"
    assert primary.origin == DocumentOrigin.GENERATED.value
    assert primary.scan_status == ScanStatus.CLEAN.value
    checklist = next(
        document
        for document in documents
        if str(document.id) == package["envelope"]["checklist_document_id"]
    )
    assert checklist.content_type == "text/markdown"
    checklist_bytes = place.store.objects[checklist.storage_key]
    assert b"12345678" in checklist_bytes  # the BRN block
    assert b"NAR1" in checklist_bytes
    assert b"105" in checklist_bytes  # the on-time fee figure
    assert b"verify against the CR's current fee table before filing" in checklist_bytes

    # The primary artifact is flat: no live form field survives the export.
    flat = PdfReader(io.BytesIO(place.store.objects[primary.storage_key]))
    assert flat.get_fields() in (None, {})

    # The event the draft applied is filed: `submitted`, exactly once.
    assert (await stored_event(session, event["id"])).status == ChangeEventStatus.SUBMITTED.value

    # The audit chain verifies; the export carries its own entry beside the
    # transition's, naming the package, the documents and the envelope.
    entries = await audit_entries(session, place.tenant.id)
    assert verify_chain(entries) == []
    export_entry = next(
        entry for entry in entries if entry.action == AuditAction.DRAFT_EXPORTED.value
    )
    assert export_entry.details["mode"] == "paper"
    assert export_entry.details["filing_package_id"] == package["id"]
    assert export_entry.details["document_id"] == package["document_id"]
    assert export_entry.details["envelope"] == package["envelope"]
    assert export_entry.details["submitted_event_ids"] == [event["id"]]
    transition_entry = next(
        entry
        for entry in entries
        if entry.action == AuditAction.DRAFT_TRANSITION_RECORDED.value
        and entry.details.get("action") == "export"
    )
    assert transition_entry.details["after"] == DraftStatus.EXPORTED.value
    assert transition_entry.details["filing_package_id"] == package["id"]

    # Both package documents download through the registry's own path.
    for document in (primary, checklist):
        download = place.client.get(
            f"/api/v1/companies/{place.company_id}/documents/{document.id}/download"
        )
        assert download.status_code == 200, download.text
        assert download.json()["url"].startswith("https://storage.invalid/")


async def test_the_export_statement_gate_refuses_and_writes_nothing(
    session: AsyncSession,
) -> None:
    """§2.11's statement leg on the way out: with the confirmation pair
    cleared (a data-repair state the approval flow itself cannot produce —
    ``approve`` with the flag true always records the pair — so the row is
    written directly, the way this suite seeds state), the export refuses 409
    ``statement_not_confirmed`` before anything is written: the draft stands,
    the store stays empty, no package row exists, and the chain is
    byte-identical to the chain before the attempt."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])
    row = await current_draft(session, draft["id"])
    row.statement_confirmed_by = None
    row.statement_confirmed_at = None
    await session.flush()

    before_entries = await audit_entries(session, place.tenant.id)
    before_objects = dict(place.store.objects)

    refused = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "paper"}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "statement_not_confirmed"

    # The transitions door answers the same gate the same way.
    via_transitions = place.client.post(
        f"/api/v1/drafts/{draft['id']}/transitions",
        json={"action": "export", "mode": "paper"},
    )
    assert via_transitions.status_code == 409
    assert via_transitions.json()["detail"] == "statement_not_confirmed"

    row = await current_draft(session, draft["id"])
    assert row.status == DraftStatus.APPROVED.value
    assert place.store.objects == before_objects
    assert await filing_packages(session, draft["id"]) == []
    assert await audit_entries(session, place.tenant.id) == before_entries


async def test_a_second_export_refuses_and_writes_nothing(session: AsyncSession) -> None:
    """Review Focus 4: one export per draft. The second lands on ``exported``,
    which admits nothing — 409 ``transition_not_allowed``, and the chain, the
    store and the package table are exactly what the first export left."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])

    first = place.client.post(f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"})
    assert first.status_code == 201, first.text
    before_entries = await audit_entries(session, place.tenant.id)
    before_objects = dict(place.store.objects)

    second = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"}
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "transition_not_allowed"
    # Same door through the transitions POST: the guard refuses first.
    via_transitions = place.client.post(
        f"/api/v1/drafts/{draft['id']}/transitions",
        json={"action": "export", "mode": "api_ready"},
    )
    assert via_transitions.status_code == 409
    assert via_transitions.json()["detail"] == "transition_not_allowed"

    assert len(await filing_packages(session, draft["id"])) == 1
    assert place.store.objects == before_objects
    assert await audit_entries(session, place.tenant.id) == before_entries


async def test_the_api_ready_mode_stores_the_deterministic_json(session: AsyncSession) -> None:
    """The ``api_ready`` package: one JSON document, byte-deterministic,
    carrying the snapshot, the filing-metadata document, the provenance, the
    QC flags and the registry pin — with no companion (the envelope names
    none)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])

    exported = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"}
    )
    assert exported.status_code == 201, exported.text
    package = exported.json()
    assert package["mode"] == "api_ready"
    assert package["envelope"]["helper_document_id"] is None
    assert package["envelope"]["checklist_document_id"] is None

    document = await session.scalar(
        select(Document).where(Document.id == UUID(package["document_id"]))
    )
    assert document is not None
    assert document.content_type == "application/json"
    payload = json.loads(place.store.objects[document.storage_key])
    row = await current_draft(session, draft["id"])
    assert payload == {
        "form_type": "nar1",
        "form_version": row.form_version,
        "target_date": row.target_date.isoformat(),
        "filing_metadata": row.filing_metadata,
        "resolved_state": row.resolved_state,
        "field_provenance": row.field_provenance,
        "qc_flags": row.qc_flags,
        "draft": {"id": draft["id"], "version_no": 1},
    }


async def test_the_ered_prep_mode_stores_the_flat_pdf_and_the_helper(
    session: AsyncSession,
) -> None:
    """The ``e_registry_prep`` package: the flattened PDF as the primary
    artifact (no live form fields), plus the step-by-step helper as the
    companion — guidance a human performs, never a submission."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])

    exported = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "e_registry_prep"}
    )
    assert exported.status_code == 201, exported.text
    package = exported.json()
    helper_id = package["envelope"]["helper_document_id"]
    assert helper_id is not None
    assert package["envelope"]["checklist_document_id"] is None

    helper = await session.scalar(select(Document).where(Document.id == UUID(helper_id)))
    assert helper is not None
    primary_document = await session.scalar(
        select(Document).where(Document.id == UUID(package["document_id"]))
    )
    assert primary_document is not None
    flat = PdfReader(io.BytesIO(place.store.objects[primary_document.storage_key]))
    assert flat.get_fields() in (None, {})
    helper_bytes = place.store.objects[helper.storage_key]
    assert b"e-Registry" in helper_bytes
    assert b"12345678" in helper_bytes


async def test_an_nd2a_draft_exports_without_any_statement_gate(
    session: AsyncSession,
) -> None:
    """Review Focus 2's other half: ND2A makes no item-16 statement, so its
    export asks nothing of one — approve needs no ``confirm_statement``, and
    the export answers 201 with the api-ready JSON."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    enter_event(place.client, place.company_id)
    draft = generate(place.client, place.company_id, form_type="nd2a")
    assert draft["private_company_statement"] is None
    url = await drive_to_signature_received(place, draft["id"])
    assert place.client.post(url, json={"action": "mark_reviewed"}).status_code == 200
    approved = place.client.post(url, json={"action": "approve"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["statement_confirmed_by"] is None

    exported = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"}
    )
    assert exported.status_code == 201, exported.text
    assert exported.json()["mode"] == "api_ready"
    assert (await current_draft(session, draft["id"])).status == DraftStatus.EXPORTED.value


async def test_the_transitions_door_performs_the_export_with_its_mode(
    session: AsyncSession,
) -> None:
    """Ruling R4: one executor, two doors. The transitions POST performs the
    export when the body carries the mode (the response is the moved draft),
    and refuses the body's shapes by name: no mode (422
    ``export_mode_required``), an unknown mode (422 ``export_mode_invalid``),
    and a mode on another action (422 ``{action}: carries mode``)."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    url = await drive_to_signature_received(place, draft["id"])
    carried = place.client.post(
        url, json={"action": "mark_reviewed", "mode": "paper"}
    )
    assert carried.status_code == 422
    assert carried.json()["detail"] == "mark_reviewed: carries mode"
    assert place.client.post(url, json={"action": "mark_reviewed"}).status_code == 200
    approved = place.client.post(url, json={"action": "approve", "confirm_statement": True})
    assert approved.status_code == 200, approved.text

    url = f"/api/v1/drafts/{draft['id']}/transitions"
    missing = place.client.post(url, json={"action": "export"})
    assert missing.status_code == 422
    assert missing.json()["detail"] == "export_mode_required"
    unknown = place.client.post(url, json={"action": "export", "mode": "fax"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "export_mode_invalid"
    assert (await current_draft(session, draft["id"])).status == DraftStatus.APPROVED.value

    performed = place.client.post(url, json={"action": "export", "mode": "api_ready"})
    assert performed.status_code == 200, performed.text
    assert performed.json()["status"] == "exported"
    packages = await filing_packages(session, draft["id"])
    assert len(packages) == 1
    assert packages[0].mode == "api_ready"


async def test_a_rationale_on_export_refuses_and_writes_nothing(
    session: AsyncSession,
) -> None:
    """The ``export`` row is rationale-free, the ``regenerate`` precedent: a
    stated note refuses 422 ``export: carries rationale`` before anything is
    written — the draft stands ``approved``, the store stays empty, no
    package row exists, and the chain is byte-identical to the chain before
    the attempt."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])

    before_entries = await audit_entries(session, place.tenant.id)
    before_objects = dict(place.store.objects)
    refused = place.client.post(
        f"/api/v1/drafts/{draft['id']}/transitions",
        json={"action": "export", "mode": "api_ready", "rationale": "partner asked"},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "export: carries rationale"

    assert (await current_draft(session, draft["id"])).status == DraftStatus.APPROVED.value
    assert place.store.objects == before_objects
    assert await filing_packages(session, draft["id"]) == []
    assert await audit_entries(session, place.tenant.id) == before_entries


async def test_export_submits_each_applied_event_once_and_only_the_applied(
    session: AsyncSession,
) -> None:
    """Review Focus 4's second half: the export files exactly the events the
    draft applied — each moved ``applied_to_draft → submitted`` once, and the
    submit path itself idempotent: a second pass over the same ids (and over
    ids that left the applied set) moves nothing and writes nothing. A
    withdrawn applied event never reaches this path at all — §4.1 invalidates
    its draft the moment it leaves the set — which is what makes the skip
    branch the defensive seam rather than a reachable state."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    applied_event = enter_event(place.client, place.company_id)
    second_event = enter_event(
        place.client,
        place.company_id,
        type="secretary_appointment",
        effective_date="2026-07-01",
        payload={
            "appointment": {
                "officer": {
                    "role": "company_secretary",
                    "name_en": "LEE Siu Ming",
                    "surname_en": "LEE",
                    "other_names_en": "Siu Ming",
                    "capacity": ["company_secretary"],
                    "appointment_date": "2026-07-01",
                }
            }
        },
    )
    draft = generate(place.client, place.company_id)
    assert set(draft["applied_event_ids"]) == {
        applied_event["id"],
        second_event["id"],
    }
    url = await drive_to_approved(place, draft["id"])

    exported = place.client.post(url, json={"action": "export", "mode": "api_ready"})
    assert exported.status_code == 200, exported.text
    assert (await stored_event(session, applied_event["id"])).status == (
        ChangeEventStatus.SUBMITTED.value
    )
    assert (await stored_event(session, second_event["id"])).status == (
        ChangeEventStatus.SUBMITTED.value
    )

    # The submit seam itself is idempotent per event: a second pass — the ids
    # it filed, and ids that were never in the applied set — moves nothing.
    before_entries = await audit_entries(session, place.tenant.id)
    submitted = await change_events.submit_applied_events(
        session,
        tenant_id=place.tenant.id,
        applied_event_ids=[UUID(applied_event["id"]), UUID(second_event["id"])],
    )
    assert submitted == []
    assert await audit_entries(session, place.tenant.id) == before_entries

    # And a filed event is out of reach of every later move: the §4.1 legs
    # that would touch it (withdraw, edit, delete, revert) refuse.
    for action in ("client_withdraw", "edit", "delete", "revert"):
        body = {"action": action}
        if action == "client_withdraw":
            body["rationale"] = "the client changed their mind"
        response = place.client.post(
            f"/api/v1/change-events/{applied_event['id']}/transitions", json=body
        )
        assert response.status_code == 409, action
        assert response.json()["detail"] == "transition_not_allowed"
    assert (await stored_event(session, applied_event["id"])).status == (
        ChangeEventStatus.SUBMITTED.value
    )


async def test_the_filing_package_row_is_tenant_pinned_and_mode_checked(
    session: AsyncSession,
) -> None:
    """Migration 0030's backstops at the database: the mode CHECK refuses the
    vocabulary's outside, and the composite draft linkage refuses a package
    that names another tenant's draft — a cross-tenant export is uninsertable,
    not merely unaudited."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    await drive_to_approved(place, draft["id"])
    exported = place.client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"}
    )
    assert exported.status_code == 201, exported.text
    package = (await filing_packages(session, draft["id"]))[0]
    # Captured before the rollback below expires the instance.
    package_draft_id = package.draft_id
    package_version_no = package.draft_version_no
    package_document_id = package.document_id

    with pytest.raises(IntegrityError):
        session.add(
            FilingPackage(
                tenant_id=place.tenant.id,
                draft_id=package_draft_id,
                draft_version_no=package_version_no,
                mode="fax",
                document_id=package_document_id,
                envelope={},
                exported_by=place.admin.id,
                exported_at=datetime.now(UTC),
            )
        )
        await session.flush()
    # The failed statement poisons the transaction; the savepoint rollback
    # (the session fixture's join mode) makes it usable for the FK check.
    await session.rollback()
    other_tenant, _ = await seed_tenant(session)
    with pytest.raises(IntegrityError):
        session.add(
            FilingPackage(
                tenant_id=other_tenant.id,
                draft_id=package_draft_id,
                draft_version_no=package_version_no,
                mode="paper",
                document_id=package_document_id,
                envelope={},
                exported_by=place.admin.id,
                exported_at=datetime.now(UTC),
            )
        )
        await session.flush()


# --- Tenancy -----------------------------------------------------------------


async def test_a_cross_tenant_draft_is_invisible(session: AsyncSession) -> None:
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)

    other_tenant, other_admin = await seed_tenant(session)
    other = make_client(session, ActorRef(user_id=other_admin.id, tenant_id=other_tenant.id))
    assert other.get(f"/api/v1/drafts/{draft['id']}").status_code == 404
    assert other.get(f"/api/v1/drafts/{draft['id']}/transitions").status_code == 404
    assert other.get(f"/api/v1/companies/{place.company_id}/drafts").status_code == 404
    assert (
        other.post(f"/api/v1/drafts/{draft['id']}/transitions", json=MARK).status_code == 404
    )
    assert (
        other.post(
            f"/api/v1/drafts/{draft['id']}/signature",
            content=b"%PDF-1.4",
            headers={"Content-Type": "application/pdf"},
        ).status_code
        == 404
    )


async def test_a_cross_tenant_draft_row_is_uninsertable(session: AsyncSession) -> None:
    place = await deployment(session)
    other_tenant, _ = await seed_tenant(session)
    with pytest.raises(IntegrityError):
        session.add(
            Draft(
                id=uuid4(),
                version_no=1,
                tenant_id=other_tenant.id,
                company_id=UUID(place.company_id),
                form_type="nar1",
                target_date=TARGET_DATE,
                status="generated",
                resolved_state={},
                field_provenance={},
                qc_flags=[],
                filing_metadata={},
                applied_event_ids=[],
                form_version={"specification_no": "x", "template_sha256": "y"},
                created_by=None,
            )
        )
        await session.flush()


async def test_a_draft_row_names_the_company_it_resolved(session: AsyncSession) -> None:
    """The composite ``(company_id, tenant_id)`` key is what makes the row's
    company the row's own — a foreign key, not a convention."""
    place = await deployment(session)
    await seed_company_facts(session, tenant=place.tenant, company_id=place.company_id)
    draft = generate(place.client, place.company_id)
    company = await session.scalar(select(Company).where(Company.id == UUID(place.company_id)))
    assert company is not None
    assert company.tenant_id == place.tenant.id
    assert draft["company_id"] == str(company.id)
