"""DB-backed prior-filing ingestion and review-queue scenarios (opt-in; needs
a migrated PostgreSQL).

These verify migration ``0010_import_batches`` and the ingestion slice end to
end against a real database: the gated extraction trigger, the batch and
proposals it persists, the §2.7 assessment, confirmation writing master data
through the single write path, rejection writing nothing, the reviewer-supplied
effective date, the Administrator-only statics path, the ADR-0012 rule that a
lapsed licence stops automation but never the customer's own data work, and
cross-tenant invisibility. Skipped unless ``AICS_TEST_DATABASE_URL`` points at
a database migrated to head.

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.company_master import (
    CompanyContactFact,
    CompanyNameRecord,
    RegisteredOfficeRecord,
)
from app.adapters.persistence.models import CompanyAccessGrant, Tenant, User
from app.adapters.persistence.prior_filing import ImportBatchRecord
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app
from tests.pdf_fixtures import acroform_pdf, scan_shaped_pdf

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}
MADE_UP_DATE = date(2025, 3, 31)
AS_OF = date(2026, 6, 30)
COVERAGE = {"start": date(2026, 1, 1), "end": date(2026, 12, 31)}

# A filing with one value per supported target family: the company's own
# data (name, address, contacts, figures, type), the repeated header BRN,
# and the made-up-to date the records inherit.
FILING = {
    "fill_2_P.1": "SYNTHETIC HARBOUR LIMITED 合成海港有限公司",
    "fill_3_P.1": "SYNTHETIC BIZ",
    "fill_4_P.1": "045",
    "fill_5_P.1": "IMPORT AND EXPORT TRADE",
    "fill_6_P.1": "31",
    "fill_7_P.1": "03",
    "fill_8_P.1": "2025",
    "fill_15_P.1": "FLAT 12",
    "fill_16_P.1": "HARBOUR BUILDING",
    "fill_17_P.1": "1 QUEEN'S ROAD",
    "fill_18_P.1": "CENTRAL",
    "fill_2_P.2": "staff@tcsp.example",
    "fill_3_P.2": "2345 6789",
    "fill_4_P.2": "HK$1,000.00",
    "fill_5_P.2": "1,024",
    "cb_1_P.1": "On",
    **{f"fill_1_P.{page}": "12345678" for page in range(1, 9)},
}


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes, deterministic URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_tenant(session: AsyncSession, *, role: str = "administrator") -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role=role,
    )
    session.add(user)
    await session.flush()
    return tenant, user


def provision(monkeypatch: pytest.MonkeyPatch, tier: str, *, expires_at: datetime | None = None):
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


def cover(client: TestClient, company_id: str, *, services: list[str]) -> None:
    """Assign a package covering ``services`` through the real endpoints."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": services,
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


def upload_prior_filing(client: TestClient, company_id: str, data: bytes) -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=NAR1",
        content=data,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def extract(client: TestClient, company_id: str, document_id: str, form: str = "nar1") -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/import-batches",
        json={"document_id": document_id, "form": form},
    )
    assert response.status_code == 201, response.text
    return response.json()


def records_by_target(batch: dict) -> dict[str, dict]:
    return {record["target"]: record for record in batch["records"]}


async def audit_actions(session: AsyncSession, tenant: Tenant) -> list[str]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return [row.action for row in rows.all()]


async def master_data(client: TestClient, company_id: str, as_of: date) -> dict:
    response = client.get(
        f"/api/v1/companies/{company_id}/master-data?as_of={as_of.isoformat()}"
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- The extraction trigger and the batch it persists ---


async def test_extraction_creates_a_pending_review_batch(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))

    batch = extract(client, company["id"], document["id"])

    assert (batch["shape"], batch["form"]) == ("acroform", "nar1")
    assert batch["document_id"] == document["id"]
    records = records_by_target(batch)
    assert set(records) == {
        "company.br_number",
        "company.name_en",
        "company.name_zh",
        "company.business_name",
        "company.business_nature_code",
        "company.business_nature_description",
        "company.email",
        "company.contact_phone_hk",
        "company.mortgages_and_charges_total",
        "company.member_count",
        "company.registered_office.flat",
        "company.registered_office.building",
        "company.registered_office.street",
        "company.registered_office.district",
        "company.type",
    }
    assert {record["status"] for record in records.values()} == {"pending"}
    assert records["company.email"]["value"] == "staff@tcsp.example"
    assert records["company.member_count"]["value"] == "1024"
    assert records["company.mortgages_and_charges_total"]["value"] == "1000.00"
    assert records["company.type"]["value"] == "private"
    # Effective-dated facts inherit the filing's made-up date; the anchor
    # statics are not time-bound.
    assert records["company.email"]["effective_date"] == MADE_UP_DATE.isoformat()
    assert records["company.br_number"]["effective_date"] is None

    assessment = batch["assessment"]
    assert assessment["component"] == "document_extractor"
    assert assessment["model"] == "pdf-geometry/v1"
    assert assessment["signal_log"]["shape"] == "acroform"
    # The weakest field routes the run: the bilingual name box is split by
    # script, so nothing reaches auto-accept on this filing.
    assert assessment["overall_score"] == 0.8
    assert assessment["routing"] == "staff_review"
    assert assessment["signal_log"]["fields"]["company.name_zh"] == [
        "exact_field_value",
        "bilingual_box_split",
    ]
    assert AuditAction.PRIOR_FILING_EXTRACTED.value in await audit_actions(session, tenant)


async def test_needs_ocr_document_records_the_outcome_without_guessing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], scan_shaped_pdf())

    batch = extract(client, company["id"], document["id"])

    assert (batch["shape"], batch["records"]) == ("needs_ocr", [])
    # The scan shape, then why the page was not read: it is not the form.
    assert batch["assessment"]["signal_log"]["document"] == [
        "needs_ocr",
        "form_frame_not_found",
    ]
    assert batch["assessment"]["routing"] is None
    assert batch["assessment"]["overall_score"] is None


async def test_re_extraction_accumulates_assessments_per_document(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.7: one subject may carry several assessments; the view shows the newest."""
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(
        client, company["id"], acroform_pdf({"fill_2_P.2": "a@b.example"})
    )

    first = extract(client, company["id"], document["id"])
    second = extract(client, company["id"], document["id"])

    assert second["id"] != first["id"]
    assert second["assessment"]["id"] != first["assessment"]["id"]
    re_read = client.get(f"/api/v1/companies/{company['id']}/import-batches/{second['id']}")
    assert re_read.json()["assessment"]["id"] == second["assessment"]["id"]


# --- The company view's batch list ---


async def test_batch_list_carries_every_run_and_its_decision_counts(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list says which runs exist and how much of each waits on staff.

    A run that proposed nothing is a row with zero counts — the batch exists,
    and hiding it would leave the reviewer wondering where the upload went.
    """
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    filing = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    scan = upload_prior_filing(client, company["id"], scan_shaped_pdf())
    url = f"/api/v1/companies/{company['id']}/import-batches"

    read = extract(client, company["id"], filing["id"])
    unread = extract(client, company["id"], scan["id"])

    response = client.get(url)

    assert response.status_code == 200, response.text
    listed = {item["id"]: item for item in response.json()}
    assert set(listed) == {read["id"], unread["id"]}
    assert listed[unread["id"]]["shape"] == "needs_ocr"
    assert listed[unread["id"]]["records_total"] == 0
    assert listed[unread["id"]]["records_pending"] == 0
    assert listed[read["id"]]["records_total"] == len(read["records"])
    assert listed[read["id"]]["records_pending"] == len(read["records"])
    # The list is summaries only: no record bodies, no assessment.
    assert "records" not in listed[read["id"]]

    email = records_by_target(read)["company.email"]
    phone = records_by_target(read)["company.contact_phone_hk"]
    base = f"{url}/{read['id']}/records"
    assert client.post(f"{base}/{email['id']}/confirm").status_code == 200
    assert client.post(f"{base}/{phone['id']}/reject").status_code == 200

    after = {item["id"]: item for item in client.get(url).json()}[read["id"]]
    assert after["records_total"] == len(read["records"])
    assert after["records_confirmed"] == 1
    assert after["records_rejected"] == 1
    assert after["records_pending"] == len(read["records"]) - 2


async def test_batch_list_is_unknown_across_companies_and_tenants(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company with no runs lists empty; another tenant's company 404s."""
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    other = client.post(
        "/api/v1/companies", json={**COMPANY, "br_number": "87654321"}
    ).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    cover(client, other["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    extract(client, company["id"], document["id"])

    mine = client.get(f"/api/v1/companies/{company['id']}/import-batches")
    assert mine.status_code == 200
    assert len(mine.json()) == 1
    untouched = client.get(f"/api/v1/companies/{other['id']}/import-batches")
    assert untouched.status_code == 200
    assert untouched.json() == []
    assert client.get(f"/api/v1/companies/{uuid4()}/import-batches").status_code == 404

    stranger, stranger_admin = await seed_tenant(session)
    stranger_client = make_client(
        session, store, ActorRef(user_id=stranger_admin.id, tenant_id=stranger.id)
    )
    assert (
        stranger_client.get(f"/api/v1/companies/{company['id']}/import-batches").status_code
        == 404
    )


# --- The gated trigger (ADR-0013 / scope §2.15) ---


async def test_extraction_is_denied_without_coverage_and_without_the_service(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    url = f"/api/v1/companies/{company['id']}/import-batches"

    unassigned = client.post(url, json={"document_id": document["id"], "form": "nar1"})
    assert unassigned.status_code == 403
    assert unassigned.json()["detail"] == f"automation_denied: {GatingReason.NO_ASSIGNMENT.value}"

    cover(client, company["id"], services=[AutomationType.AI_REPLY_PARSING.value])
    not_included = client.post(url, json={"document_id": document["id"], "form": "nar1"})
    assert not_included.status_code == 403
    assert (
        not_included.json()["detail"]
        == f"automation_denied: {GatingReason.SERVICE_NOT_INCLUDED.value}"
    )

    # Storage is never gated: the upload above succeeded in both cases.
    listing = client.get(f"/api/v1/companies/{company['id']}/documents")
    assert listing.status_code == 200


async def test_lapsed_licence_denies_extraction_but_not_the_review_queue(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    email = records_by_target(batch)["company.email"]
    phone = records_by_target(batch)["company.contact_phone_hk"]

    # Past the grace window: gated automation fails closed (ADR-0012 §4.3)…
    provision(monkeypatch, "standard", expires_at=datetime.now(UTC) - timedelta(days=8))
    denied = client.post(
        f"/api/v1/companies/{company['id']}/import-batches",
        json={"document_id": document["id"], "form": "nar1"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == (
        f"automation_denied: {GatingReason.TIER_CAPABILITY_MISSING.value}"
    )

    # …while the customer's own data stays fully usable: confirming and
    # rejecting proposals already extracted is not automation.
    base = f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}/records"
    confirmed = client.post(f"{base}/{email['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    rejected = client.post(f"{base}/{phone['id']}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"


# --- Confirmation writes master data through the single write path ---


async def test_confirm_writes_the_fact_group_and_flips_the_status(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    records = records_by_target(batch)
    base = f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}/records"

    response = client.post(f"{base}/{records['company.email']['id']}/confirm")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["decided_by"] == str(admin.id)
    assert response.json()["effective_date"] == MADE_UP_DATE.isoformat()
    # The contact group is written whole from the batch's own records.
    master = await master_data(client, company["id"], MADE_UP_DATE)
    assert master["contact"] == {
        "effective_date": MADE_UP_DATE.isoformat(),
        "email": "staff@tcsp.example",
        "contact_phone_hk": "2345 6789",
    }
    actions = await audit_actions(session, tenant)
    assert AuditAction.IMPORT_RECORD_CONFIRMED.value in actions
    assert AuditAction.COMPANY_FACT_RECORDED.value in actions
    # The decision is final: a second confirm is refused.
    assert client.post(f"{base}/{records['company.email']['id']}/confirm").status_code == 409


async def test_confirm_of_a_name_record_carries_the_whole_name_group(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    name_en = records_by_target(batch)["company.name_en"]

    response = client.post(
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{name_en['id']}/confirm"
    )
    assert response.status_code == 200, response.text

    master = await master_data(client, company["id"], MADE_UP_DATE)
    assert master["name"]["name_en"] == "SYNTHETIC HARBOUR LIMITED"
    assert master["name"]["name_zh"] == "合成海港有限公司"
    assert master["name"]["business_name"] == "SYNTHETIC BIZ"


async def test_rejected_siblings_are_left_out_of_the_fact_group(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    records = records_by_target(batch)
    base = f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}/records"

    phone = client.post(f"{base}/{records['company.contact_phone_hk']['id']}/reject")
    email = client.post(f"{base}/{records['company.email']['id']}/confirm")
    assert (phone.status_code, email.status_code) == (200, 200)

    contact = await session.scalar(select(CompanyContactFact))
    assert (contact.email, contact.contact_phone_hk) == ("staff@tcsp.example", None)
    assert AuditAction.IMPORT_RECORD_REJECTED.value in await audit_actions(session, tenant)


async def test_rejection_writes_nothing_to_master_data(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    record = records_by_target(batch)["company.name_en"]

    response = client.post(
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{record['id']}/reject"
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert response.json()["decided_at"] is not None
    assert await session.scalar(select(CompanyNameRecord)) is None
    # The proposal itself stays in the batch as the record of the decision.
    stored = await session.get(ImportBatchRecord, UUID(record["id"]))
    assert stored.status == "rejected"


async def test_reviewer_supplies_the_effective_date_the_filing_omits(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(
        client, company["id"], acroform_pdf({"fill_2_P.2": "a@b.example"})
    )
    batch = extract(client, company["id"], document["id"])
    record = records_by_target(batch)["company.email"]
    assert record["effective_date"] is None
    url = (
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{record['id']}/confirm"
    )

    missing = client.post(url)
    assert missing.status_code == 422
    assert "effective date" in missing.json()["detail"]
    assert await session.scalar(select(CompanyContactFact)) is None

    supplied = client.post(url, json={"effective_date": "2025-01-15"})
    assert supplied.status_code == 200, supplied.text
    assert supplied.json()["effective_date"] == "2025-01-15"
    master = await master_data(client, company["id"], date(2025, 1, 15))
    assert master["contact"]["effective_date"] == "2025-01-15"


async def test_an_incomplete_fact_group_is_refused_not_padded(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = client.post("/api/v1/companies", json=COMPANY).json()
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(
        client,
        company["id"],
        # Three of the four registered-office boxes; the fourth is blank.
        # The date boxes are filled so the group check, not the date check,
        # is what refuses the confirmation.
        acroform_pdf(
            {
                "fill_6_P.1": "31",
                "fill_7_P.1": "03",
                "fill_8_P.1": "2025",
                "fill_15_P.1": "FLAT 12",
                "fill_16_P.1": "HARBOUR",
                "fill_17_P.1": "1 ROAD",
            }
        ),
    )
    batch = extract(client, company["id"], document["id"])
    flat = records_by_target(batch)["company.registered_office.flat"]

    response = client.post(
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{flat['id']}/confirm"
    )

    assert response.status_code == 422
    assert "district" in response.json()["detail"]
    # Nothing was written and the proposal is still awaiting a decision.
    assert await session.scalar(select(RegisteredOfficeRecord)) is None
    stored = await session.get(ImportBatchRecord, UUID(flat["id"]))
    assert stored.status == "pending"


# --- Anchor statics stay an administrative path (option (b), 2026-08-24) ---


async def test_static_confirmation_requires_an_administrator(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    staff = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
    )
    session.add(staff)
    await session.flush()
    admin_client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = admin_client.post("/api/v1/companies", json=COMPANY).json()
    cover(admin_client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(
        admin_client, company["id"], acroform_pdf({"fill_1_P.1": "99999999"})
    )
    batch = extract(admin_client, company["id"], document["id"])
    brn = records_by_target(batch)["company.br_number"]
    url = (
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{brn['id']}/confirm"
    )

    session.add(
        CompanyAccessGrant(
            user_id=staff.id, company_id=UUID(company["id"]), tenant_id=tenant.id
        )
    )
    await session.flush()
    staff_client = make_client(session, store, ActorRef(user_id=staff.id, tenant_id=tenant.id))

    refused = staff_client.post(url)
    assert refused.status_code == 403
    assert "administrator" in refused.json()["detail"]

    # The Administrator corrects the static through the statics path.
    corrected = admin_client.post(url)
    assert corrected.status_code == 200, corrected.text
    listing = admin_client.get("/api/v1/companies")
    updated = next(row for row in listing.json() if row["id"] == company["id"])
    assert updated["br_number"] == "99999999"
    assert AuditAction.COMPANY_STATICS_CORRECTED.value in await audit_actions(session, tenant)


# --- Tenant isolation ---


async def test_cross_tenant_batch_and_records_are_invisible(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, admin_a = await seed_tenant(session)
    tenant_b, admin_b = await seed_tenant(session)
    client_a = make_client(session, store, ActorRef(user_id=admin_a.id, tenant_id=tenant_a.id))
    client_b = make_client(session, store, ActorRef(user_id=admin_b.id, tenant_id=tenant_b.id))
    company = client_a.post("/api/v1/companies", json=COMPANY).json()
    cover(client_a, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    provision(monkeypatch, "standard")
    document = upload_prior_filing(client_a, company["id"], acroform_pdf(FILING))
    batch = extract(client_a, company["id"], document["id"])
    record = records_by_target(batch)["company.email"]

    assert client_b.get(
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
    ).status_code == 404
    assert client_b.post(
        f"/api/v1/companies/{company['id']}/import-batches",
        json={"document_id": document["id"], "form": "nar1"},
    ).status_code == 404
    assert client_b.post(
        f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}"
        f"/records/{record['id']}/confirm"
    ).status_code == 404
