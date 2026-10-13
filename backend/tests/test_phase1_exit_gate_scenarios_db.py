"""Phase 1 exit-gate acceptance walks (opt-in; needs a migrated PostgreSQL).

The Phase 1 plan's exit-gate verification
(``.agents/plans/2026-08-19-phase-1-foundation.md``) as three rerunnable
demonstrations driven through the real endpoint stack against the real
database:

1. DoD §9 item 1 — the Administrator bootstraps the tenant, logs in for a
   real Bearer session, bulk-imports a portfolio by CSV, configures the
   client contact channels, sets the naming template, and reads the
   portfolio back.
2. DoD §9 item 2 — staff ingest one company's prior NAR1 in both shapes:
   the digital filing is uploaded, extracted into the review queue, and its
   proposals are confirmed into master data; the scanned filing runs the
   same path through the OCR reader where the system Tesseract binary is
   installed (``README.md``), and says so where it is not.
3. The exit gate's degradation item — a valid Standard lease allows gated
   extraction; past the 7-day grace the capability set collapses to Free,
   the trigger is refused with the gate's reason, and the paths that are
   never gated keep working: master-data reads, document upload, review
   decisions, and the audit trail.

Each walk prints a transcript (run with ``-s``) and asserts its outcome.
Every test runs inside a transaction that is rolled back, so the review
database stays clean. Skipped unless ``AICS_TEST_DATABASE_URL`` points at
a database migrated to head.
"""

import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import AuthSession, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import auth
from app.core.config import settings
from app.domain import mfa
from app.domain.audit import AuditAction, verify_chain
from app.domain.auth import session_token_digest
from app.domain.milestones import default_milestones
from app.domain.naming_templates import DEFAULT_DOCUMENT_TEMPLATE
from app.domain.prior_filing import AUTO_ACCEPT_THRESHOLD, value_is_validated
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app
from tests.pdf_fixtures import acroform_pdf
from tests.pdf_scan_fixtures import scan_pdf

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

TENANT_NAME = "Exit Gate TCSP"
ADMIN_EMAIL = f"admin-{uuid4().hex[:8]}@tcsp.example"
ADMIN_PASSWORD = "exit-gate-secret-1"

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}
# Item 2's company starts with the wrong anchor type on purpose: the filing
# carries the checkbox that corrects it, so confirming the batch exercises
# the Administrator-only statics path too.
COMPANY_BEFORE_FILING = COMPANY | {"type": "company_limited_by_guarantee"}

MADE_UP_DATE = date(2025, 3, 31)
AS_OF = date(2026, 6, 30)
DATA_AS_OF = date(2026, 6, 30)
COVERAGE = {"start": date(2026, 1, 1), "end": date(2026, 12, 31)}

PORTFOLIO_CSV = (
    "br_number,cr_number,name_en,date_of_incorporation,type,status,email\n"
    "5432 1097,2210971,HARBOUR VIEW TRADING LIMITED,2014-06-18,private,active,"
    "info@harbourview.example\n"
    "54321888,,PEAK CONSULTANCY (HK) LIMITED,2018-02-05,private,dormant,"
    "admin@peakconsultancy.example\n"
)
REIMPORT_CSV = (
    "br_number,cr_number,name_en,date_of_incorporation,type,status,email\n"
    "5432 1097,2210971,HARBOUR VIEW TRADING (HK) LIMITED,2014-06-18,private,active,"
    "info@harbourview.example\n"
    "54321888,,PEAK CONSULTANCY (HK) LIMITED,2018-02-05,private,active,"
    "admin@peakconsultancy.example\n"
)

CUSTOM_TEMPLATES = {
    "document": "{CompanyCode}/{BRN}/{Form}_{DocType}_{YYYYMMDD}.pdf",
    "generated": "{CompanyCode}/{BRN}/{Form}_{DocType}_{YYYYMMDD}_v{Version}.pdf",
}

# A filing with one value per supported target family: the company's own
# data (name, address, contacts, figures, type), the repeated header BRN,
# and the made-up date the records inherit.
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


def make_client(
    session: AsyncSession, *, store: FakeStore | None = None, actor: ActorRef | None = None
) -> TestClient:
    """The real app, overridden only at the seams a walk does not exercise.

    Item 1 goes through the real login and needs no override; items 2 and 3
    run as a seeded actor, and the blob store is the in-memory fake (the
    boto3 adapter has its own opt-in MinIO round trip).
    """
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if store is not None:
        app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def announce(title: str) -> None:
    """The banner of one walk or step, so ``pytest -s`` reads as a demo."""
    print(f"\n=== {title} ===")


def say(text: str) -> None:
    print(f"    {text}")


async def seed_tenant(
    session: AsyncSession, *, role: str = "administrator"
) -> tuple[Tenant, User]:
    """A committed-shape tenant with one account — items 2/3's fixture."""
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


def provision(
    monkeypatch: pytest.MonkeyPatch, tier: str, *, expires_at: datetime | None = None
) -> None:
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


def post_csv(client: TestClient, csv_text: str) -> Response:
    """POST the raw CSV body as ``text/csv``."""
    return client.post(
        f"/api/v1/companies/import?data_as_of={DATA_AS_OF.isoformat()}",
        content=csv_text.encode(),
        headers={"Content-Type": "text/csv"},
    )


def create_company(client: TestClient, payload: dict = COMPANY) -> dict:
    response = client.post("/api/v1/companies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


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
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=nar1",
        content=data,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def trigger_extraction(client: TestClient, company_id: str, document_id: str) -> Response:
    return client.post(
        f"/api/v1/companies/{company_id}/import-batches",
        json={"document_id": document_id, "form": "nar1"},
    )


def extract(client: TestClient, company_id: str, document_id: str) -> dict:
    response = trigger_extraction(client, company_id, document_id)
    assert response.status_code == 201, response.text
    return response.json()


def decide(
    client: TestClient,
    company_id: str,
    batch_id: str,
    record_id: str,
    decision: str,
    *,
    effective_date: date | None = None,
) -> Response:
    """One staff review decision through the real endpoint."""
    body = None if effective_date is None else {"effective_date": effective_date.isoformat()}
    return client.post(
        f"/api/v1/companies/{company_id}/import-batches/{batch_id}"
        f"/records/{record_id}/{decision}",
        json=body,
    )


def records_by_target(batch: dict) -> dict[str, dict]:
    return {record["target"]: record for record in batch["records"]}


def master_data(client: TestClient, company_id: str, as_of: date) -> dict:
    response = client.get(
        f"/api/v1/companies/{company_id}/master-data?as_of={as_of.isoformat()}"
    )
    assert response.status_code == 200, response.text
    return response.json()


async def audit_entries(session: AsyncSession, tenant: Tenant) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return list(rows.all())


# --- DoD §9 item 1: tenant → CSV portfolio → contact channels → template ---


async def test_item1_admin_onboards_a_portfolio(session: AsyncSession) -> None:
    announce("Phase 1 exit gate — DoD §9 item 1: the admin onboards a portfolio")
    client = make_client(session)

    bootstrap = client.post(
        "/api/v1/tenants",
        json={
            "tenant_name": TENANT_NAME,
            "admin_email": ADMIN_EMAIL,
            "admin_display_name": "Root Admin",
            "admin_password": ADMIN_PASSWORD,
        },
    )
    assert bootstrap.status_code == 201, bootstrap.text
    say(
        f"POST /api/v1/tenants → 201; tenant {bootstrap.json()['tenant_id']} "
        f"with administrator {ADMIN_EMAIL}"
    )

    assert client.get("/api/v1/companies").status_code == 401
    say("GET /api/v1/companies before login → 401 (the session is what authorizes)")
    login = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200, login.text
    say(
        f"POST /api/v1/auth/login → 200; {login.json()['token_type']} session "
        f"expires {login.json()['expires_at']}"
    )
    assert login.json()["mfa_required"] is True
    assert login.json()["enrollment_state"] == "pending"
    say(
        "the password alone leaves an Administrator unverified "
        "(mfa_required, enrollment_state 'pending') — MFA enrollment first"
    )
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)
    now = datetime.now(UTC)
    enrolled = mfa.totp_at(secret, mfa.current_step(now.timestamp()))
    await auth.complete_mfa_enrollment(
        session, user=admin, secret=secret, code=enrolled, now=now
    )
    challenge_login = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert challenge_login.status_code == 200, challenge_login.text
    assert challenge_login.json()["enrollment_state"] == "enrolled"
    row = await session.scalar(
        select(AuthSession).where(
            AuthSession.token_digest
            == session_token_digest(challenge_login.json()["access_token"])
        )
    )
    assert row is not None
    challenge = mfa.totp_at(secret, mfa.current_step(now.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=admin, code=challenge, now=now, auth_session=row
    )
    client.headers["Authorization"] = (
        f"Bearer {challenge_login.json()['access_token']}"
    )
    say("TOTP enrollment + challenge → the session verifies; the surface opens")

    entitlement = client.get("/api/v1/entitlement")
    assert entitlement.status_code == 200, entitlement.text
    assert entitlement.json()["tier"] == "free"
    say(
        "GET /api/v1/entitlement → tier 'free' (the unprovisioned default): "
        "onboarding is not licensed automation"
    )

    first = post_csv(client, PORTFOLIO_CSV)
    assert first.status_code == 200, first.text
    report = first.json()
    assert (report["created"], report["updated"], report["rejected"]) == (2, 0, 0)
    for row in report["rows"]:
        assert (row["outcome"], row["reason"]) == ("created", None)
        assert row["company_id"] is not None
        say(f"  row {row['row']}: BRN {row['br_number']} → {row['outcome']} ({row['company_id']})")
    created_ids = {row["br_number"]: row["company_id"] for row in report["rows"]}

    second = post_csv(client, REIMPORT_CSV)
    assert second.status_code == 200, second.text
    repeat = second.json()
    assert (repeat["created"], repeat["updated"], repeat["rejected"]) == (0, 2, 0)
    assert {row["br_number"]: row["company_id"] for row in repeat["rows"]} == created_ids
    say("re-import of the same BRNs → 0 created / 2 updated, same company ids (match-or-create)")
    portfolio = client.get("/api/v1/companies")
    assert portfolio.status_code == 200 and len(portfolio.json()) == 2
    say("GET /api/v1/companies → the two imported companies, no duplicates")

    harbour = created_ids["5432 1097"]
    peak = created_ids["54321888"]
    wong = client.post(
        "/api/v1/persons",
        json={"name_en": "Wong Tai Man", "email": "wong.taiman@client.example"},
    )
    assert wong.status_code == 201, wong.text
    wong_email = client.post(
        f"/api/v1/persons/{wong.json()['id']}/contact-channels",
        json={"channel_type": "email", "identifier": "wong.taiman@client.example"},
    )
    assert wong_email.status_code == 201, wong_email.text
    say(
        f"person {wong.json()['id']} (Wong Tai Man) with email channel "
        f"{wong_email.json()['identifier']!r}"
    )
    chan = client.post("/api/v1/persons", json={"name_zh": "陳大文"})
    assert chan.status_code == 201, chan.text
    chan_whatsapp = client.post(
        f"/api/v1/persons/{chan.json()['id']}/contact-channels",
        json={"channel_type": "whatsapp", "identifier": "85298765432"},
    )
    assert chan_whatsapp.status_code == 201, chan_whatsapp.text
    say(
        f"person {chan.json()['id']} (陳大文) with whatsapp channel "
        f"{chan_whatsapp.json()['identifier']!r}"
    )

    for person_id, company_id in ((wong.json()["id"], harbour), (chan.json()["id"], peak)):
        link = client.post(
            f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
        )
        assert link.status_code == 200, link.text
        assert (link.json()["company_id"], link.json()["person_id"]) == (company_id, person_id)
        say(f"associated {person_id} with company {company_id}")
    contacts = client.get(f"/api/v1/companies/{harbour}/contacts")
    assert contacts.status_code == 200, contacts.text
    assert [row["id"] for row in contacts.json()] == [wong.json()["id"]]
    assert contacts.json()[0]["channels"][0]["identifier"] == "wong.taiman@client.example"
    say("the Harbour company lists Wong with his channel — and not the other client")

    shipped = client.get("/api/v1/tenant-config")
    assert shipped.status_code == 200
    assert shipped.json()["naming_templates"]["document"] == DEFAULT_DOCUMENT_TEMPLATE
    say(
        "GET /api/v1/tenant-config before first PUT → shipped default "
        f"{DEFAULT_DOCUMENT_TEMPLATE!r}"
    )
    put = client.put(
        "/api/v1/tenant-config",
        # The whole configuration is one PUT: the milestone panel travels with
        # the templates (2026-09-24). Its defaults are what this deployment
        # keeps, so the shipped block is what is written back.
        json={
            "naming_templates": CUSTOM_TEMPLATES,
            "milestones": default_milestones().as_dict(),
        },
    )
    assert put.status_code == 200, put.text
    stored = client.get("/api/v1/tenant-config")
    assert stored.status_code == 200
    assert stored.json()["naming_templates"] == CUSTOM_TEMPLATES
    assert stored.json()["updated_at"] is not None
    say(f"PUT + GET /api/v1/tenant-config → {CUSTOM_TEMPLATES['document']!r} round-trips")

    snapshot = master_data(client, harbour, DATA_AS_OF)
    assert snapshot["name"]["name_en"] == "HARBOUR VIEW TRADING (HK) LIMITED"
    assert snapshot["status"]["status"] == "active"
    assert snapshot["contact"]["email"] == "info@harbourview.example"
    statics = client.get(f"/api/v1/companies/{harbour}")
    assert statics.status_code == 200, statics.text
    assert statics.json()["br_number"] == "54321097"  # normalized on import
    assert statics.json()["cr_number"] == "2210971"
    say(
        f"master data {DATA_AS_OF}: {snapshot['name']['name_en']!r}, "
        f"status {snapshot['status']['status']!r}, "
        f"email {snapshot['contact']['email']!r}"
    )
    assert master_data(client, peak, DATA_AS_OF)["status"]["status"] == "active"
    say("the re-import's status change superseded the original at the same data-as-of date")


# --- DoD §9 item 2: prior filing → extraction → review → master data ---


async def test_item2_staff_ingests_prior_filings_both_shapes(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 1 exit gate — DoD §9 item 2: prior filings, digital and scanned")
    tenant, admin = await seed_tenant(session)
    actor = ActorRef(user_id=admin.id, tenant_id=tenant.id)
    client = make_client(session, store=store, actor=actor)
    provision(monkeypatch, "standard")
    company = create_company(client, COMPANY_BEFORE_FILING)
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    say(
        f"company {company['id']} (BRN {company['br_number']}, type "
        f"{company['type']!r}) licensed for document_extraction"
    )

    announce("the digital shape")
    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    say(f"uploaded NAR1 → document {document['id']} at {document['storage_key']!r}")
    batch = extract(client, company["id"], document["id"])
    assessment = batch["assessment"]
    assert batch["shape"] == "acroform"
    assert assessment["model"] == "pdf-geometry/v1"
    assert assessment["routing"] == "staff_review"
    assert assessment["overall_score"] == 0.8
    assert {record["status"] for record in batch["records"]} == {"pending"}
    for record in batch["records"]:
        say(f"  {record['target']:<50} = {record['value']!r:<42} {record['confidence']:.2f}")
    review = client.get(f"/api/v1/companies/{company['id']}/import-batches/{batch['id']}")
    assert review.status_code == 200, review.text
    assert review.json()["assessment"]["id"] == assessment["id"]
    assert {row["id"] for row in review.json()["records"]} == {
        row["id"] for row in batch["records"]
    }
    say("GET .../import-batches/{id} → the review-queue read returns the same batch")
    untouched = master_data(client, company["id"], AS_OF)
    assert untouched["name"] is None and untouched["contact"] is None
    say("extraction alone wrote nothing to master data — every record awaits a decision")

    records = records_by_target(batch)
    rejected = decide(
        client, company["id"], batch["id"], records["company.contact_phone_hk"]["id"], "reject"
    )
    assert rejected.status_code == 200, rejected.text
    say(f"reject  company.contact_phone_hk = {records['company.contact_phone_hk']['value']!r}")
    for target, record in records.items():
        if target == "company.contact_phone_hk":
            continue
        confirmed = decide(client, company["id"], batch["id"], record["id"], "confirm")
        assert confirmed.status_code == 200, f"{target}: {confirmed.text}"
        assert confirmed.json()["status"] == "confirmed"
        say(f"confirm {target} = {record['value']!r}")

    master = master_data(client, company["id"], AS_OF)
    assert master["name"]["name_en"] == "SYNTHETIC HARBOUR LIMITED"
    assert master["name"]["name_zh"] == "合成海港有限公司"
    assert master["name"]["business_name"] == "SYNTHETIC BIZ"
    assert master["contact"] == {
        "effective_date": MADE_UP_DATE.isoformat(),
        "email": "staff@tcsp.example",
        "contact_phone_hk": None,  # the rejected proposal wrote no column
    }
    office = master["registered_office"]
    assert (office["flat"], office["building"], office["street"], office["district"]) == (
        "FLAT 12",
        "HARBOUR BUILDING",
        "1 QUEEN'S ROAD",
        "CENTRAL",
    )
    assert master["business_nature"]["business_nature_code"] == "045"
    assert master["filing"]["member_count"] == 1024
    assert Decimal(str(master["filing"]["mortgages_and_charges_total"])) == Decimal("1000.00")
    statics = client.get(f"/api/v1/companies/{company['id']}")
    assert statics.status_code == 200, statics.text
    assert statics.json()["type"] == "private"  # the confirmed static correction
    say(
        f"master data {AS_OF}: name/en+zh, contact email, registered office, "
        f"nature 045, filing 1024 shares — type corrected to 'private'"
    )

    announce("the scanned shape")
    if shutil.which("tesseract") is None:
        say(
            "system Tesseract is not installed on this host — the scanned half is "
            "skipped here; the deployment image ships it (README.md)"
        )
        return
    scanned = upload_prior_filing(client, company["id"], scan_pdf())
    scanned_batch = extract(client, company["id"], scanned["id"])
    scanned_assessment = scanned_batch["assessment"]
    assert scanned_batch["shape"] == "needs_ocr"
    assert scanned_assessment["model"].startswith("tesseract/")
    say(
        f"scanned NAR1 read by {scanned_assessment['model']}: "
        f"{len(scanned_batch['records'])} records, routing {scanned_assessment['routing']}"
    )
    for record in scanned_batch["records"]:
        say(f"  {record['target']:<50} = {record['value']!r:<42} {record['confidence']:.2f}")
    # The OCR safety property: nothing a machine check cannot confirm may
    # reach the auto-accept band, however sure the engine was.
    unvalidated_auto = [
        record["target"]
        for record in scanned_batch["records"]
        if record["confidence"] >= AUTO_ACCEPT_THRESHOLD
        and not value_is_validated(record["target"], record["value"])
    ]
    assert unvalidated_auto == []
    member_count = records_by_target(scanned_batch)["company.member_count"]
    confirmed = decide(
        client,
        company["id"],
        scanned_batch["id"],
        member_count["id"],
        "confirm",
        effective_date=MADE_UP_DATE,
    )
    assert confirmed.status_code == 200, confirmed.text
    say("confirmed the scan's member count with the reviewer-supplied effective date")
    assert master_data(client, company["id"], AS_OF)["filing"]["member_count"] == 1024


# --- the exit gate's degradation item: Standard → Free ---


async def test_degradation_standard_to_free_keeps_the_manual_path_open(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 1 exit gate — Standard → Free degradation")
    tenant, admin = await seed_tenant(session)
    actor = ActorRef(user_id=admin.id, tenant_id=tenant.id)
    client = make_client(session, store=store, actor=actor)
    company = create_company(client)
    cover(client, company["id"], services=[AutomationType.DOCUMENT_EXTRACTION.value])
    now = datetime.now(UTC)
    provision(monkeypatch, "standard", expires_at=now + timedelta(days=30))
    licensed = client.get("/api/v1/entitlement")
    assert licensed.status_code == 200, licensed.text
    assert licensed.json()["license_state"] == "active"
    assert AutomationType.DOCUMENT_EXTRACTION.value in licensed.json()["capabilities"]
    say(
        f"lease active (expires {licensed.json()['license_expires_at']}): tier "
        f"{licensed.json()['tier']!r}, document_extraction licensed"
    )

    document = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    batch = extract(client, company["id"], document["id"])
    say(f"gated extraction under the valid lease → 201, batch {batch['id']} ({batch['shape']})")

    provision(monkeypatch, "standard", expires_at=now - timedelta(days=8))
    degraded = client.get("/api/v1/entitlement").json()
    assert degraded["tier"] == "standard"  # still provisioned
    assert degraded["license_state"] == "free"
    assert degraded["capabilities"] == []
    say("lease 8 days past expiry: state 'free', capabilities collapse to the Free set")

    refused = trigger_extraction(client, company["id"], document["id"])
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == (
        f"automation_denied: {GatingReason.TIER_CAPABILITY_MISSING.value}"
    )
    say(f"extraction trigger → 403 {refused.json()['detail']!r}")

    reading = client.get(f"/api/v1/companies/{company['id']}/master-data?as_of={AS_OF}")
    assert reading.status_code == 200, reading.text
    say("master-data read → 200 (reads are never gated)")
    reupload = upload_prior_filing(client, company["id"], acroform_pdf(FILING))
    assert reupload["storage_key"] != document["storage_key"]
    say(f"document upload → 201, WORM suffix {reupload['storage_key']!r}")

    records = records_by_target(batch)
    rejected = decide(
        client, company["id"], batch["id"], records["company.contact_phone_hk"]["id"], "reject"
    )
    assert rejected.status_code == 200, rejected.text
    confirmed = decide(
        client, company["id"], batch["id"], records["company.email"]["id"], "confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    assert master_data(client, company["id"], AS_OF)["contact"]["email"] == "staff@tcsp.example"
    say("confirm + reject on the already-proposed records → 200; the email is in master data")

    entries = await audit_entries(session, tenant)
    actions = {entry.action for entry in entries}
    for action in (
        AuditAction.DOCUMENT_REGISTERED,
        AuditAction.PRIOR_FILING_EXTRACTED,
        AuditAction.IMPORT_RECORD_CONFIRMED,
        AuditAction.IMPORT_RECORD_REJECTED,
    ):
        assert action.value in actions
    assert verify_chain(entries) == []
    say(f"audit trail: {len(entries)} entries, hash chain intact")
