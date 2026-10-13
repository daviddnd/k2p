"""Phase 5 exit-gate acceptance walk (opt-in; needs a migrated PostgreSQL).

The Phase 5 plan's exit-gate verification
(``.agents/plans/2026-09-28-phase-5-form-auto-drafter.md`` → Exit gate) as one
rerunnable demonstration driven through the real endpoint stack against the
real database:

**DoD §9 item 13** — a draft generated from master entity data + approved change
events (using effective dates), with changed fields highlighted and traceable to
source. The walk is the whole manual path:

1. a company registered through its own route, its last NAR1 ingested (upload →
   extract → staff confirm) into master data, and a corrected name entered
   through the fact-append write path — the unexplained drift the cross-check
   exists for;
2. the office-holder and member records entered through F1's routes (one
   appointment, one shareholding);
3. a registered-office change and a director appointment entered by hand and
   walked to ``confirmed`` through §4.1's own action route, each with its
   effective date;
4. ``POST /companies/{id}/drafts`` — the gated generation;
5. the resolved snapshot (the event's new address and the event's new director
   beside the master facts), the applied events moved ``applied_to_draft`` and
   linked in ``applied_event_ids``, the per-registry-item ``field_provenance``
   (the office box traced to its change event, the email box to the filing's
   stored document, the corrected name to its fact row), the prior-filing
   cross-check's ``unexpected_change`` flag on the unexplained drift and on
   nothing else, the §4.2 transitions read, and the identical re-read of
   ``GET /drafts/{id}``.

**The walk's boundary:** the walk stops at the draft's resolution and
transitions; the PDF half of item 13 (the filled form) is F4b's, whose own
``test_drafts_scenarios_db.py`` release test walks ``send_for_signature`` end to
end. What this walk produces is the document those fillers expand, and its
provenance is the source map they read.

Everything that has a route is seeded through it. The one fixture is the prior
filing's bytes: ``tests/pdf_fixtures.py`` writes synthetic values into the
pinned NAR1 template, so no filing binary is committed and the read side meets
the template's real widget structure (the header BRN repeated on every page,
the filing's own made-up date printed in its boxes).

Each walk prints a transcript (run with ``-s``) and asserts its outcome. Every
test runs inside a transaction that is rolled back, so the review database stays
clean. Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated
to head.

The fixtures and helpers follow the two sibling gate files
(``test_phase1_exit_gate_scenarios_db.py``,
``test_phase2_exit_gate_scenarios_db.py``). That family's convention is that each
gate file is self-contained — no test module in this suite imports another's
fixtures or helpers — so the small generic pieces (the rollback session, the
in-memory blob store, the seeded actor, the fact/event/generation calls) are
restated here in the phase-2 file's own shape rather than shared by import.
"""

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.forms.registry import load_form_registry
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.core.config import settings
from app.domain.audit import AuditAction, verify_chain
from app.domain.prior_filing import FormCode
from app.main import create_app
from tests.pdf_fixtures import acroform_pdf

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

# --- The walk's facts, one home each ---

COMPANY = {
    "br_number": "20265001",
    "cr_number": "2650001",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}

# The draft's target date is fixed, so the run does not depend on the wall
# clock: every effective date below is stated against it, and the NAR1's
# made-up date is the latest anniversary on or before it.
TARGET_DATE = date(2026, 9, 30)
STATUS_SINCE = date(2020, 1, 1)
NAME_CORRECTED = date(2026, 1, 15)
OFFICE_MOVED = date(2026, 5, 1)
APPOINTED = date(2026, 6, 1)

NAR1_MADE_UP_DATE = date(2026, 3, 1)
# When the prior return was made up: printed in the filing's own boxes, so the
# records the extraction proposes inherit it as their effective date.
FILING_MADE_UP_DATE = date(2025, 3, 31)

FILING_NAME_EN = "WOODFIELD TRADING LIMITED"
CARRIED_NAME_EN = "WOODFIELD TRADING (HK) LIMITED"
FILING_NAME_ZH = "活田貿易有限公司"

FILING_EMAIL = "staff@woodfield.example"
FILING_PHONE = "2345 6789"
FILING_MORTGAGES = "1000.00"

# The registered office the last return stated, and the one the client moved to
# — the change event's address.
PRIOR_OFFICE = {
    "flat": "12A",
    "building": "BRIGHT TOWER",
    "street": "1 QUEEN'S ROAD CENTRAL",
    "district": "CENTRAL",
}
MOVED_OFFICE = {
    "flat": "5B",
    "building": "HARBOUR HOUSE",
    "street": "2 CONNAUGHT ROAD",
    "district": "SHEUNG WAN",
}
OFFICER_ADDRESS = {
    "flat": "8C",
    "building": "Larch Court",
    "street": "15 Bonham Road",
    "district": "Western",
}

# The NAR1 box the highlighting and the traceability are read from: the
# registered office, whose four parts the registry names as one item.
OFFICE_ITEM_TARGET = (
    "company.registered_office.flat + company.registered_office.building + "
    "company.registered_office.street + company.registered_office.district"
)

# The prior NAR1: one value per supported target family the walk asserts on —
# the company's name, its registered office, its contacts and its charges — with
# the header BRN repeated on every page like the real form. The business-nature
# and share-capital boxes are left as the walk seeds them (the fact-append path
# and F1's routes); nothing the fillers will read is invented here.
FILING = {
    "fill_2_P.1": f"{FILING_NAME_EN} {FILING_NAME_ZH}",
    "fill_6_P.1": "31",
    "fill_7_P.1": "03",
    "fill_8_P.1": "2025",
    "fill_15_P.1": PRIOR_OFFICE["flat"],
    "fill_16_P.1": PRIOR_OFFICE["building"],
    "fill_17_P.1": PRIOR_OFFICE["street"],
    "fill_18_P.1": PRIOR_OFFICE["district"],
    "fill_2_P.2": FILING_EMAIL,
    "fill_3_P.2": FILING_PHONE,
    "fill_4_P.2": "HK$1,000.00",
    **{f"fill_1_P.{page}": COMPANY["br_number"] for page in range(1, 9)},
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


def make_client(
    session: AsyncSession, *, actor: ActorRef, store: FakeStore | None = None
) -> TestClient:
    """The real app, overridden only at the seams a walk does not exercise.

    The actor is seeded (this walk is about the manual path, not about login),
    the RBAC context loads from the database through the real dependency chain,
    and the blob store is the in-memory fake the other scenario modules use (the
    boto3 adapter has its own opt-in MinIO round trip).
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    if store is not None:
        app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def announce(title: str) -> None:
    """The banner of one step, so ``pytest -s`` reads as a demo."""
    print(f"\n=== {title} ===")


def say(text: str) -> None:
    print(f"    {text}")


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    """A tenant with one Administrator account."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


def provision(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    """Provision the deployment configuration the entitlement seam reads.

    No activation credential exists in a test run (``conftest`` points the
    credential file at a fresh temporary path), so configuration is the source —
    the precedence rule a deployment runs on before its lease arrives.
    """
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)


def create_company(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/v1/companies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def patch_facts(
    client: TestClient, company_id: str, *, effective: date, **groups: Any
) -> dict[str, Any]:
    """One effective-dated fact append through the master-data write path."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": effective.isoformat(), **groups},
    )
    assert response.status_code == 200, response.text
    return response.json()


def master_data(client: TestClient, company_id: str, as_of: date) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/companies/{company_id}/master-data?as_of={as_of.isoformat()}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def cover(client: TestClient, company_id: str, *, services: list[str]) -> None:
    """Assign a package covering ``services`` through the real endpoints.

    ``automated_prefill`` is what the §5.1 gate asks before doc generation;
    ``document_extraction`` is what it asks before the ingestion trigger.
    """
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": services,
            "covered_forms": ["NAR1", "ND2A"],
        },
    )
    assert package.status_code == 201, package.text
    today = date.today()
    assignment = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": (today - timedelta(days=365)).isoformat(),
            "coverage_end": (today + timedelta(days=365)).isoformat(),
        },
    )
    assert assignment.status_code == 201, assignment.text


def upload_prior_filing(client: TestClient, company_id: str, data: bytes) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=nar1",
        content=data,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def extract(client: TestClient, company_id: str, document_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/{company_id}/import-batches",
        json={"document_id": document_id, "form": "nar1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm_record(client: TestClient, company_id: str, batch_id: str, record_id: str) -> None:
    """One staff confirmation through the real review decision route.

    No effective date is supplied: the filing printed its own made-up date, and
    the records inherited it.
    """
    response = client.post(
        f"/api/v1/companies/{company_id}/import-batches/{batch_id}/records/{record_id}/confirm",
        json=None,
    )
    assert response.status_code == 200, response.text


def records_by_target(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["target"]: record for record in batch["records"]}


def create_officer(client: TestClient, company_id: str) -> dict[str, Any]:
    """One officer appointment through F1's route (an inline new person)."""
    response = client.post(
        f"/api/v1/companies/{company_id}/officers",
        json={
            "person": {
                "person_type": "natural_person",
                "name_zh": "陳大文",
                "name_en": "CHAN Tai Man",
                "surname_en": "CHAN",
                "other_names_en": "Tai Man",
                "email": "chan@woodfield.example",
                "hkid_partial": "A1234",
            },
            "role": "director",
            "capacity": ["director"],
            "valid_from": "2020-01-01",
            "correspondence_address": OFFICER_ADDRESS,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_shareholding(
    client: TestClient, company_id: str, *, person_id: str
) -> dict[str, Any]:
    """One member position through F1's route, for the person already recorded."""
    response = client.post(
        f"/api/v1/companies/{company_id}/shareholdings",
        json={
            "person_id": person_id,
            "class_name": "Ordinary",
            "shares_held": 1000,
            "percentage_held": "100.00",
            "allotment_date": "2020-01-01",
            "amount_paid": "1000.00",
            "amount_unpaid": "0.00",
            "address": OFFICER_ADDRESS,
            "valid_from": "2020-01-01",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def enter_change_event(
    client: TestClient,
    company_id: str,
    *,
    type: str,
    effective: date,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """One change event entered by hand through the real route (scope §2.7)."""
    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={"type": type, "effective_date": effective.isoformat(), "payload": payload},
    )
    assert response.status_code == 201, response.text
    return response.json()


def decide_event(client: TestClient, event_id: str, *actions: str) -> dict[str, Any]:
    """Walk one event through §4.1's actions, failing loudly on a refusal."""
    body: dict[str, Any] = {}
    for action in actions:
        response = client.post(
            f"/api/v1/change-events/{event_id}/transitions", json={"action": action}
        )
        assert response.status_code == 200, response.text
        body = response.json()
    return body


def confirm_event(client: TestClient, event_id: str) -> dict[str, Any]:
    return decide_event(client, event_id, "mark_under_review", "mark_reviewed", "confirm")


def change_event_statuses(client: TestClient, company_id: str) -> dict[str, str]:
    response = client.get(f"/api/v1/companies/{company_id}/change-events")
    assert response.status_code == 200, response.text
    return {item["id"]: item["status"] for item in response.json()}


def generate_draft(
    client: TestClient, company_id: str, *, target: date = TARGET_DATE
) -> dict[str, Any]:
    """One draft generation through the real route (the gated automation)."""
    response = client.post(
        f"/api/v1/companies/{company_id}/drafts",
        json={"form_type": "nar1", "target_date": target.isoformat()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def registry_sha256(form: str) -> str:
    return load_form_registry(FormCode(form)).template_sha256


async def audit_entries(session: AsyncSession, tenant_id: Any) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return list(rows.all())


# --- DoD §9 item 13: master data + approved events → the draft ---


async def test_item13_draft_from_master_data_and_approved_change_events(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 5 exit gate — DoD §9 item 13: the draft the auto-drafter produces")
    tenant, admin = await seed_tenant(session)
    actor = ActorRef(user_id=admin.id, tenant_id=tenant.id)
    client = make_client(session, store=store, actor=actor)
    provision(monkeypatch, "standard")
    say(
        "every fact below enters through its own route: registration, the ingestion "
        "path, the fact-append write path, F1's officer/member routes, §4.1's action route"
    )

    # 1. The company, and the coverage the generation's gate asks for.
    company = create_company(client, COMPANY)
    company_id = company["id"]
    cover(client, company_id, services=["document_extraction", "automated_prefill"])
    say(
        f"POST /api/v1/companies → 201: {company['br_number']} "
        f"({company['date_of_incorporation']} incorporation, {company['type']}), "
        "licensed for document_extraction and automated_prefill"
    )

    # 2. The last return, ingested: upload → extract → staff confirm.
    announce("the prior NAR1, read into master data")
    document = upload_prior_filing(client, company_id, acroform_pdf(FILING))
    batch = extract(client, company_id, document["id"])
    assert batch["shape"] == "acroform"
    assert {record["status"] for record in batch["records"]} == {"pending"}
    records = records_by_target(batch)
    # The dict is keyed by target: a duplicated proposal target would collapse
    # two records into one and leave one unconfirmed while the counts below
    # still balanced, so the walk pins the two lengths together first.
    assert len(records) == len(batch["records"])
    # The header BRN is an anchor static — it carries no effective date; every
    # fact the return states inherits the made-up date it printed.
    assert records["company.br_number"]["effective_date"] is None
    assert {
        record["effective_date"]
        for target, record in records.items()
        if target != "company.br_number"
    } == {FILING_MADE_UP_DATE.isoformat()}
    for target, record in sorted(records.items()):
        confirm_record(client, company_id, batch["id"], record["id"])
        say(f"  {target:<42} = {record['value']!r}")
    say(
        f"document {document['id']} → batch {batch['id']} ({batch['shape']}): "
        f"{len(records)} proposals, all confirmed with the return's own made-up date "
        f"{FILING_MADE_UP_DATE}; nothing is in master data before a human decides it"
    )

    master = master_data(client, company_id, TARGET_DATE)
    assert master["name"] == {
        "effective_date": FILING_MADE_UP_DATE.isoformat(),
        "name_en": FILING_NAME_EN,
        "name_zh": FILING_NAME_ZH,
        "business_name": None,
    }
    assert master["registered_office"] == {
        "effective_date": FILING_MADE_UP_DATE.isoformat(),
        **PRIOR_OFFICE,
        "country": None,
    }
    assert master["contact"] == {
        "effective_date": FILING_MADE_UP_DATE.isoformat(),
        "email": FILING_EMAIL,
        "contact_phone_hk": FILING_PHONE,
    }
    # NAR1 item 9: the charges total the return printed, normalised to money —
    # the master read's filing fact.
    assert master["filing"]["effective_date"] == FILING_MADE_UP_DATE.isoformat()
    assert Decimal(str(master["filing"]["mortgages_and_charges_total"])) == Decimal(
        FILING_MORTGAGES
    )
    say(
        f"master data at {TARGET_DATE}: the filing's name, office, email/phone and "
        f"charges are in force from {FILING_MADE_UP_DATE} — every one of them backfilled "
        "from the stored document, which is what their provenance will say"
    )

    # 3. The facts the filing did not state: the corrected name (the drift), the
    # status and the share capital — through the master-data write path.
    patch_facts(
        client,
        company_id,
        effective=STATUS_SINCE,
        status={"status": "active"},
    )
    patch_facts(
        client,
        company_id,
        effective=STATUS_SINCE,
        share_capital_classes={
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
    )
    patch_facts(
        client,
        company_id,
        effective=NAME_CORRECTED,
        name={"name_en": CARRIED_NAME_EN, "name_zh": FILING_NAME_ZH},
    )
    say(
        f"PATCH /api/v1/companies/{company_id} → 200 three times: status active from "
        f"{STATUS_SINCE}, one Ordinary class, and the English name corrected to "
        f"{CARRIED_NAME_EN!r} from {NAME_CORRECTED} — no event carries a name change, so "
        "this correction is exactly the drift the cross-check must flag"
    )
    assert master_data(client, company_id, TARGET_DATE)["name"] == {
        "effective_date": NAME_CORRECTED.isoformat(),
        "name_en": CARRIED_NAME_EN,
        "name_zh": FILING_NAME_ZH,
        "business_name": None,
    }

    # 4. The office-holder and member records (F1's routes).
    director = create_officer(client, company_id)
    member = create_shareholding(client, company_id, person_id=director["person"]["id"])
    assert member["person_id"] == director["person_id"]
    say(
        f"POST .../officers → 201: director {director['person']['name_en']} "
        f"(person {director['person_id']}) in office from {director['valid_from']}; "
        f"POST .../shareholdings → 201: 1000 Ordinary shares for the same person — "
        "one Person row, two records, the §2.4 shape the draft resolves"
    )

    # 5. The client's two changes, entered by hand and approved.
    announce("two changes, approved through §4.1's own actions")
    office_event = enter_change_event(
        client,
        company_id,
        type="registered_office_change",
        effective=OFFICE_MOVED,
        payload={"registered_office": {"address": MOVED_OFFICE}},
    )
    appointment_event = enter_change_event(
        client,
        company_id,
        type="director_appointment",
        effective=APPOINTED,
        payload={
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_zh": "黃少峰",
                    "surname_en": "WONG",
                    "other_names_en": "Siu Fung",
                    "capacity": ["director"],
                    "appointment_date": APPOINTED.isoformat(),
                }
            }
        },
    )
    for event in (office_event, appointment_event):
        final = confirm_event(client, event["id"])
        assert final["status"] == "confirmed"
        say(
            f"{event['type']} effective {event['effective_date']} → pending → "
            "mark_under_review → mark_reviewed → confirmed"
        )

    # 6. Generation.
    announce("the generation")
    draft = generate_draft(client, company_id)
    assert draft["company_id"] == company_id
    assert (draft["form_type"], draft["version_no"], draft["status"]) == ("nar1", 1, "generated")
    assert draft["history"] == []
    assert draft["signature_status"] == "not_sent"
    assert draft["private_company_statement"] is True
    assert draft["form_version"] == {
        "specification_no": "2/2025 (May 2025)",
        "template_sha256": registry_sha256("nar1"),
    }
    say(
        f"POST /api/v1/companies/{company_id}/drafts → 201: {draft['form_type']} v"
        f"{draft['version_no']} targeting {draft['target_date']}, status "
        f"{draft['status']}, pinned to the registry's {draft['form_version']['specification_no']} "
        "(ADR-0010's form_version)"
    )

    # 7. The resolved snapshot: the events' values beside the master facts.
    state = draft["resolved_state"]
    filing_metadata = draft["filing_metadata"]
    assert filing_metadata["return"]["made_up_date"] == NAR1_MADE_UP_DATE.isoformat()
    say(
        f"filing_metadata.return.made_up_date = {NAR1_MADE_UP_DATE} — the latest anniversary "
        f"({COMPANY['anniversary_date']}) on or before the target {TARGET_DATE}"
    )

    assert state["company"]["name"] == {
        "name_en": CARRIED_NAME_EN,
        "name_zh": FILING_NAME_ZH,
        "business_name": None,
    }
    assert state["company"]["registered_office"] == MOVED_OFFICE
    assert state["company"]["contact"] == {
        "email": FILING_EMAIL,
        "contact_phone_hk": FILING_PHONE,
    }
    assert state["company"]["status"] == "active"
    assert state["company"]["br_number"] == COMPANY["br_number"]
    say(
        "the company block resolves the master facts (BRN, type, contact, the corrected "
        "name) and the event's address as of the target date"
    )
    # The overlay is the draft's, not a write: master data still states the
    # filing's office, and the event stays the explanation for the difference.
    assert master_data(client, company_id, TARGET_DATE)["registered_office"] == {
        "effective_date": FILING_MADE_UP_DATE.isoformat(),
        **PRIOR_OFFICE,
        "country": None,
    }
    say(
        "master data still states the filing's office — the event is the draft's overlay "
        "for the target date, not a fact the generation wrote"
    )

    assert [
        (
            entry["name_zh"],
            entry["name_en"],
            entry["role"],
            entry["capacity"],
            entry["valid_from"],
            entry["valid_to"],
            entry["source"],
        )
        for entry in state["officers"]
    ] == [
        (
            "陳大文",
            "CHAN Tai Man",
            "director",
            ["director"],
            "2020-01-01",
            None,
            {"type": "master_record_id", "id": director["id"]},
        ),
        (
            "黃少峰",
            None,
            "director",
            ["director"],
            APPOINTED.isoformat(),
            None,
            {"type": "change_event_id", "id": appointment_event["id"]},
        ),
    ]
    assert [
        (
            entry["name_en"],
            entry["class_name"],
            entry["shares_held"],
            entry["percentage_held"],
            entry["valid_from"],
            entry["source"],
        )
        for entry in state["shareholders"]
    ] == [
        (
            "CHAN Tai Man",
            "Ordinary",
            1000,
            "100.00",
            "2020-01-01",
            {"type": "master_record_id", "id": member["id"]},
        )
    ]
    say(
        "officers: the master-record director beside the director the appointment event "
        "added (valid from its effective date), each entry carrying its own source; "
        "shareholders: the member position F1's route recorded"
    )
    assert state["company"]["share_capital"] == {
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
    }
    assert [change["type"] for change in state["changes"]] == [
        "registered_office_change",
        "director_appointment",
    ]
    assert [change["event_id"] for change in state["changes"]] == [
        office_event["id"],
        appointment_event["id"],
    ]
    assert [change["effective_date"] for change in state["changes"]] == [
        OFFICE_MOVED.isoformat(),
        APPOINTED.isoformat(),
    ]
    say(
        "resolved_state.changes lists both applied events in effective-date order — the "
        "snapshot carries the change list a reviewer reads the draft against"
    )

    # 8. The §4.1 linkage: the applied events moved, and are named on the draft.
    assert draft["applied_event_ids"] == [office_event["id"], appointment_event["id"]]
    statuses = change_event_statuses(client, company_id)
    assert statuses[office_event["id"]] == "applied_to_draft"
    assert statuses[appointment_event["id"]] == "applied_to_draft"
    assert [flag for flag in draft["qc_flags"] if flag["kind"] == "unapplied_event"] == []
    say(
        "both events read back applied_to_draft and both ids are in the draft's "
        "applied_event_ids — §4.1's apply, performed by the generation itself"
    )

    # 9. Provenance: traceable to source, and the changed items highlighted.
    announce("provenance, highlighting and the prior-filing cross-check")
    provenance = draft["field_provenance"]
    assert provenance["6"] == {
        "target": OFFICE_ITEM_TARGET,
        "source_type": "change_event_id",
        "source_id": office_event["id"],
        "changed": True,
    }
    assert provenance["1"]["target"] == "company.name_en + company.name_zh"
    assert provenance["1"]["source_type"] == "master_record_id"
    # The fact row the correction wrote, not the company anchor it is filed on.
    assert provenance["1"]["source_id"] != company_id
    assert provenance["1"]["changed"] is True
    assert provenance["7"] == {
        "target": "company.email",
        "source_type": "backfill_document_id",
        "source_id": document["id"],
        "changed": False,
    }
    assert provenance["12A.name"] == {
        "target": (
            "officers[].name_zh + officers[].surname_en + officers[].other_names_en"
        ),
        "source_type": "change_event_id",
        "source_id": appointment_event["id"],
        "changed": False,
    }
    assert sorted(item for item, entry in provenance.items() if entry["changed"]) == ["1", "6"]
    say("per-item provenance (item ← source, CHANGED = differs from the prior filing):")
    for item in ("HDR-BRN", "1", "6", "7", "8", "9", "12A.name"):
        entry = provenance[item]
        say(
            f"  item {item:<9} {entry['source_type']:<21} "
            f"{str(entry['source_id'])[:8]:<9} {'CHANGED' if entry['changed'] else ''}"
        )
    say(
        "the office box traces to the event that moved it, the email box to the document "
        "the filing was read from, the corrected name to its fact row, and the officer box "
        "to the appointment event — only items 1 and 6 are highlighted"
    )

    assert draft["qc_flags"] == [
        {
            "kind": "unexpected_change",
            "target": "company.name_en",
            "detail": f"{FILING_NAME_EN} -> {CARRIED_NAME_EN}",
        }
    ]
    say(
        f"qc_flags: {draft['qc_flags'][0]['detail']!r} — the one difference from the last "
        "return no event explains; the office change differs too and raises nothing, "
        "because the event that caused it is the explanation"
    )

    # 10. The reading surface: the §4.2 transitions the actor may perform now.
    transitions = client.get(f"/api/v1/drafts/{draft['id']}/transitions")
    assert transitions.status_code == 200, transitions.text
    assert transitions.json() == {
        "draft_id": draft["id"],
        "company_id": company_id,
        "version_no": 1,
        "status": "generated",
        "actions": [
            {"action": "edit", "to_status": "under_review", "rationale_required": False},
            {
                "action": "mark_under_review",
                "to_status": "under_review",
                "rationale_required": False,
            },
        ],
    }
    say(
        "GET /drafts/{id}/transitions → edit and mark_under_review, and nothing else: "
        "the release (send_for_signature) is wired since F4b but admitted from "
        "under_review alone, the rows behind it and Phase 6's are unwired "
        "(upload_signed, recall, mark_reviewed, reject, approve, export, regenerate), "
        "and no system row (invalidate) is offered"
    )

    # 11. The re-read is the walk's own evidence.
    read_back = client.get(f"/api/v1/drafts/{draft['id']}")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json() == draft
    say("GET /drafts/{id} re-reads byte for byte identical to the generation's response")

    # 12. Everything the walk did is on one verifiable chain.
    entries = await audit_entries(session, tenant.id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.DRAFT_GENERATED.value) == 1
    # Three actions per event to confirm, plus the apply each generation made.
    assert actions.count(AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value) == 8
    assert actions.count(AuditAction.IMPORT_RECORD_CONFIRMED.value) == len(records)
    assert verify_chain(entries) == []
    say(
        f"audit: {len(entries)} entries, hash chain intact — the {len(records)} "
        "confirmations, the six event transitions, the two applies and the generation"
    )
