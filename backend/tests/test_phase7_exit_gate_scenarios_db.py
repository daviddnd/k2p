"""Phase 7 exit-gate acceptance walk (opt-in; needs a migrated PostgreSQL).

The Phase 7 plan's exit-gate verification
(``.agents/plans/2026-10-02-phase-7-hardening-and-launch.md`` → PH7-F1) as one
rerunnable demonstration driven through the real endpoint stack against the
real database, with every seam a walk does not exercise replaced by the
suite family's fakes:

**DoD §9 item 18** — the audit trail captures all client communications, event
lifecycle actions, data changes, AI suggestions and approvals: the walk
asserts, at each step, that the step's ``AuditAction`` entries exist in the
chain, and at the end that every one of DoD 18's five report areas (via the
live ``app.domain.audit_report.ACTION_AREA`` table PH7-A shipped) is
represented and that ``verify_chain`` over the tenant's full chain passes.

**DoD §9 item 21** — internal end-to-end validation on representative sample
data: one tenant bootstrapped the real way (including the E3 MFA reality — the
Administrator enrolls through the real application functions and passes the
verified-session gate; nothing overrides the authentication seam), a CSV
portfolio imported, coverage assigned (before the gated extraction, which the
§5.1 gate asks for the assignment first — the phase-1/5 precedent), the prior
NAR1 ingested in the digital shape and confirmed into master data, the
operator's risk acknowledgment confirmed (the 2026-10-04 pre-flight layer),
a confirmation request drafted and delivered over
the fake send transport, one synthetic inbound reply injected over the fake
IMAP leg and parsed by the stubbed gateway, the proposed event reviewed and
confirmed, and the NAR1 draft walked through release, signature, the
review/approval gate and export.

**DoD §9 item 22** — what F1 re-measures and what it cites. The deterministic
pre-fill leg is *cited*: ``tests/test_draft_fill_pdf.py``'s
``test_item22_deterministic_prefill_agreement`` measures 100% (476 fields, 482
widget instances) over six scenarios against the ≥99% bar, always-on, and F1
does not rebuild it; F1 *re-measures the data half* on its own walk — the
generated draft's resolved state is compared field by field against the truth
set that entered through the routes, and the agreement rate is asserted at
100% (≥99%). The reply-parsing leg is *cited*: the recorded live seed run's
macro 99.3% (bands 100/100/96.9) and the always-on replay harness
(``tests/test_reply_parsing_corpus.py``, faithful replay measured at 100%) pin
the Gate-B measurement; the ≥150-record blind launch corpus stays a pre-launch
gate F1 does not recreate. F1 *re-measures the routing half* on its own walk:
the stubbed parse of the walk's own reply lands, scores 1.0 field-level and
routes ``staff_review`` — ruling R-D's cap, because the reply proposes
appointing a person the company's appointments do not carry.

**The one-export-per-draft rule** (contract §4.2, P6-4): an exported draft
admits nothing further, so three exports need three drafts. The walk stages
three sequential versions of the NAR1 lineage — paper, ``e_registry_prep`` and
``api_ready`` — and then asserts the refusal (409 ``transition_not_allowed``)
on a second export of the third draft, with the chain byte-identical to the
chain before the attempt. Version 1, generated while the walk's change event
is still approved, carries the event into its resolution and is the subject of
the DoD-22 re-measurement; versions 2 and 3 demonstrate the remaining export
modes after the event has been filed (``submitted``) by version 1's export.

**The actor split** follows the contract: Administrator only where the
contract requires it (tenant bootstrap, user management, the CSV import, the
anchor-statics confirmation, service packages, the risk acknowledgment,
the tenant config, and the Administrator-only mail cycles); the filing work runs
as General Staff with the company grant and the four §2.9 permission flags.

Everything that has a route is seeded through it; the one fixture is the
prior filing's bytes (``tests/pdf_fixtures.py`` writes synthetic values into
the pinned NAR1 template, the digital shape — the scanned shape is Phase 1's
Tesseract leg, deliberately not part of this walk). The gateway is stubbed at
the transport seam (``ai_gateway.llm_client``, the M6 harness pattern), so
the real ``infer`` runs — the redaction, the audit booking, the grammar
checks — and no suite talks to a provider. No S3 (in-memory store), no
Tesseract, no external network: the two mail fakes are the standing suites'
own (``tests/fake_smtp.py`` on loopback, ``tests/fake_imap.py`` without a
socket).

Each step prints a transcript (run with ``-s``) and asserts its outcome.
Every test runs inside a transaction that is rolled back, so the review
database stays clean. Skipped unless ``AICS_TEST_DATABASE_URL`` points at a
database migrated to head.

The fixtures and helpers follow the sibling gate files
(``test_phase1_exit_gate_scenarios_db.py``, ``test_phase5_exit_gate_scenarios_db.py``):
each gate file is self-contained, so the small generic pieces are restated
here in this family's own shape rather than shared by import.
"""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.llm.port import TransportResult
from app.adapters.mail import imap_client, smtp_client
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.documents import Document
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import (
    AuthSession,
    CompanyAccessGrant,
    User,
)
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.reply_parsing import ReplyParse
from app.adapters.persistence.session import get_session
from app.api.deps import get_current_actor  # noqa: F401 — the real seam, never overridden
from app.api.documents import get_storage
from app.application import ai_gateway, auth
from app.core.config import settings
from app.domain import mfa
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.audit import AuditAction, verify_chain
from app.domain.audit_report import ACTION_AREA, ReportArea
from app.domain.auth import session_token_digest
from app.domain.change_events import ChangeEventStatus
from app.main import create_app
from tests.fake_imap import FakeImap
from tests.fake_smtp import FakeSmtpServer, trusting_context
from tests.pdf_fixtures import acroform_pdf

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

# --- The walk's facts, one home each ---

TENANT_NAME = "Phase 7 Exit Gate TCSP"
ADMIN_EMAIL = f"admin-{uuid4().hex[:8]}@tcsp.example"
ADMIN_PASSWORD = "phase-7-exit-gate-secret"
STAFF_EMAIL = f"staff-{uuid4().hex[:8]}@tcsp.example"
STAFF_PASSWORD = "phase-7-exit-gate-staff"
# The enrollment and the challenge verify at one pinned instant, so the walk
# does not depend on where in a TOTP window the wall clock happens to stand.
PINNED_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

DATA_AS_OF = date(2026, 10, 2)
TARGET_DATE = date(2027, 3, 1)  # the next made-up date: anniversary 2027-03-01
COVERAGE = {"start": date(2026, 1, 1), "end": date(2027, 12, 31)}

PORTFOLIO_CSV = (
    "br_number,cr_number,name_en,name_zh,date_of_incorporation,type,status,email\n"
    "76540001,7654001,HARBOUR LIGHT TRADING LIMITED,海港燈貿易有限公司,2016-03-01,"
    "private,active,info@harbourlight.example\n"
    "76540002,,PEAK HOLDINGS (HK) LIMITED,峰控股(香港)有限公司,2019-07-15,"
    "private,dormant,admin@peakholdings.example\n"
)

COMPANY_A_BRN = "76540001"
NAME_EN = "HARBOUR LIGHT TRADING LIMITED"
NAME_ZH = "海港燈貿易有限公司"
CONTACT_EMAIL = "info@harbourlight.example"
CLIENT_EMAIL = "director@harbourlight.example"

# The registered office the last return stated.
FILING_OFFICE = {
    "flat": "12A",
    "building": "HARBOUR BUILDING",
    "street": "1 QUEEN'S ROAD",
    "district": "CENTRAL",
}
FILING_MADE_UP_DATE = date(2026, 3, 1)

# The prior NAR1 (digital shape): one value per supported target family —
# name, office, business nature, charges, the made-up date it printed, and
# the header BRN repeated on every page like the real form. The type
# checkbox is deliberately absent: ticking it would land on the
# Administrator-only anchor-statics path, and the walk's review leg is the
# General Staff's.
FILING = {
    "fill_2_P.1": f"{NAME_EN} {NAME_ZH}",
    "fill_4_P.1": "045",
    "fill_5_P.1": "IMPORT AND EXPORT TRADE",
    "fill_6_P.1": "01",
    "fill_7_P.1": "03",
    "fill_8_P.1": "2026",
    "fill_15_P.1": FILING_OFFICE["flat"],
    "fill_16_P.1": FILING_OFFICE["building"],
    "fill_17_P.1": FILING_OFFICE["street"],
    "fill_18_P.1": FILING_OFFICE["district"],
    "fill_4_P.2": "HK$0.00",
    **{f"fill_1_P.{page}": COMPANY_A_BRN for page in range(1, 9)},
}

# The facts the filing did not state: share capital (patched through the
# master-data write path) and the baseline office-holder/member (F1's
# officer/member routes).
SHARE_CAPITAL = {
    "classes": [
        {
            "class_name": "Ordinary",
            "currency": "HKD",
            "total_shares": 1000,
            "total_amount": "1000.00",
            "total_paid_up": "1000.00",
        }
    ]
}

MAILBOX = "ai-filings@tcsp.example"
APP_PASSWORD = "phase7-exit-gate-app-password"
CONTACT_PERSON = "LEE Wai Man"
DIRECTOR = {"name_zh": "陳大文", "name_en": "CHAN Tai Man", "surname_en": "CHAN",
            "other_names_en": "Tai Man"}

# The synthetic inbound reply: one confirmation and one change proposal, every
# evidence span verbatim from the body.
REPLY_BODY = (
    "All confirmed — the director is unchanged. Please appoint "
    "Wong New Director as a director effective 1 November 2026."
)
EVENT_NAME = "Wong New Director"
EVENT_EVIDENCE = "appoint Wong New Director as a director effective 1 November 2026"
CONFIRM_EVIDENCE = "the director is unchanged"
EVENT_EFFECTIVE = "2026-11-01"

def signed_bytes(version_no: int) -> bytes:
    """One distinct signature artifact per draft version: the registry keys
    a signed draft by its content, and each version's print differs — the
    client signs the document they were sent, not a copy of version one's."""
    return f"%PDF-1.4\n%signed-by-the-client-v{version_no}\n".encode()


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


class StubTransport:
    """The replaced gateway transport seam (the M6 harness pattern): records
    the minimized snippet that would have left, then answers with the
    scripted envelope — the real ``infer`` runs around it, redaction, audit
    booking and grammar checks included, and no provider is contacted."""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.snippets: list[str] = []
        self._envelope = {
            "protocol_version": 1,
            "provider": "stub-provider",
            "task_type": "reply_parsing",
            "model_id": "stub-model",
            "prompt_name": "reply_parser",
            "prompt_version": 1,
            "result": dict(result),
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            "completed_at": "2026-10-02T10:00:00+00:00",
        }

    async def complete(self, *, task_type: str, language: str, snippet: str) -> TransportResult:
        self.snippets.append(snippet)
        return TransportResult(envelope=dict(self._envelope))


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


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provision the Standard tier (the §5.1 gate's capability leg), the
    mailbox identity, the Q1.3 gate value, and
    the SMTP TLS seam the fake server needs."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "ai_q13_evidence", "counsel review 2026-09-30")
    monkeypatch.setattr(smtp_client, "tls_context", trusting_context)


def make_app(session: AsyncSession, store: FakeStore) -> FastAPI:
    """The real app, overridden only at the storage and session seams.

    The authentication seam is deliberately NOT overridden: the walk goes
    through the real login and the E3 verified-session gate, the
    Administrator's session carrying the MFA challenge's verdict.
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def announce(title: str) -> None:
    """The banner of one step, so ``pytest -s`` reads as a demo."""
    print(f"\n=== {title} ===")


def say(text: str) -> None:
    print(f"    {text}")


def login(client: TestClient, email: str, password: str) -> dict[str, Any]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def verified_admin_session(
    session: AsyncSession, admin_client: TestClient
) -> tuple[User, UUID, str]:
    """Bootstrap → login → MFA enrollment → challenge: the real E3 flow.

    Enrollment runs through the application's own functions with a code from
    ``domain/mfa.totp_at`` at the pinned step; the returned token belongs to
    a session the verified-session gate admits. Returns (admin user, tenant
    id, access_token)."""
    bootstrap = admin_client.post(
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
        f"POST /api/v1/tenants → 201: tenant {bootstrap.json()['tenant_id']} "
        f"with administrator {ADMIN_EMAIL}"
    )

    first = login(admin_client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert first["mfa_required"] is True
    assert first["enrollment_state"] == "pending"
    say(
        "the password alone leaves an Administrator unverified "
        "(mfa_required, enrollment_state 'pending') — MFA enrollment first"
    )
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)
    enrolled_code = mfa.totp_at(secret, mfa.current_step(PINNED_NOW.timestamp()))
    await auth.complete_mfa_enrollment(
        session, user=admin, secret=secret, code=enrolled_code, now=PINNED_NOW
    )
    say(
        "begin_mfa_enrollment → complete_mfa_enrollment with totp_at(secret, "
        "current step at the pinned instant) — the register seals the secret, "
        "one mfa_enrolled entry"
    )

    second = login(admin_client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert second["mfa_required"] is True
    assert second["enrollment_state"] == "enrolled"
    row = await session.scalar(
        select(AuthSession).where(
            AuthSession.token_digest == session_token_digest(second["access_token"])
        )
    )
    assert row is not None
    challenge_code = mfa.totp_at(secret, mfa.current_step(PINNED_NOW.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=admin, code=challenge_code, now=PINNED_NOW, auth_session=row
    )
    say("second login → the TOTP challenge marks the session verified")
    admin_client.headers["Authorization"] = f"Bearer {second['access_token']}"
    return admin, admin.tenant_id, second["access_token"]


def create_staff(admin_client: TestClient) -> dict[str, Any]:
    """The General Staff account: the company grant's future holder, with the
    four §2.9 permission flags the contract names."""
    created = admin_client.post(
        "/api/v1/users",
        json={
            "email": STAFF_EMAIL,
            "display_name": "General Staff",
            "role": "general",
            "password": STAFF_PASSWORD,
            "review_events": True,
            "approve_events": True,
            "review_drafts": True,
            "approve_drafts": True,
        },
    )
    assert created.status_code == 201, created.text
    say(
        "POST /api/v1/users → 201: General Staff with review_events, "
        "approve_events, review_drafts and approve_drafts"
    )
    return created.json()


def post_csv(client: TestClient, csv_text: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/import?data_as_of={DATA_AS_OF.isoformat()}",
        content=csv_text.encode(),
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return list(rows.all())


def assert_new(
    entries: list[AuditLog], seen: set[str], *required: AuditAction
) -> set[str]:
    """The required actions joined the chain since the last snapshot; returns
    the refreshed snapshot."""
    actions = {entry.action for entry in entries}
    for action in required:
        assert action.value in actions, f"{action.value} missing from the chain"
        assert action.value not in seen, f"{action.value} already asserted"
    return actions


# --- DoD 22: the deterministic data-resolution re-measurement -------------


def resolution_accuracy(resolved: Mapping[str, Any]) -> tuple[int, int]:
    """The walk's own deterministic leg: the generated draft's resolved state
    against the truth set that entered through the routes, field by field.

    Returns (agreeing, compared). The ≥99% DoD bar itself is met by the cited
    six-scenario widget measurement (``test_draft_fill_pdf.py``); this is the
    same arithmetic over the walk's scenario, so a resolution regression in
    the end-to-end path cannot hide behind the unit suite's cover."""
    company = resolved["company"]
    expected: dict[str, Any] = {
        "company.name.name_en": NAME_EN,
        "company.name.name_zh": NAME_ZH,
        "company.name.business_name": None,
        "company.registered_office": FILING_OFFICE,
        "company.status": "active",
        "company.br_number": COMPANY_A_BRN,
        "company.cr_number": "7654001",
        "company.contact.email": CONTACT_EMAIL,
        "company.business_nature.business_nature_code": "045",
        "company.business_nature.business_nature_description": "IMPORT AND EXPORT TRADE",
        "company.filing.mortgages_and_charges_total": "0.00",
        "company.share_capital.classes": SHARE_CAPITAL["classes"],
        "company.share_capital.total": {
            "derived": True,
            "currency": "HKD",
            "total_shares": 1000,
            "total_amount": "1000.00",
            "total_paid_up": "1000.00",
        },
    }

    def dig(root: Mapping[str, Any], path: str) -> Any:
        node: Any = root
        for part in path.split("."):
            node = node[part]
        return node

    agreeing = 0
    for path, want in expected.items():
        got = dig({"company": company}, path)
        assert got == want, f"resolved {path}: {got!r} != {want!r}"
        agreeing += 1
    officers = resolved["officers"]
    assert [(o["name_en"], o["valid_from"]) for o in officers] == [
        (DIRECTOR["name_en"], "2024-01-01"),
        (EVENT_NAME, EVENT_EFFECTIVE),
    ]
    assert all(o["role"] == "director" for o in officers)
    agreeing += 3  # the two officers' identities plus the shared role
    shareholders = resolved["shareholders"]
    assert len(shareholders) == 1
    assert shareholders[0]["name_en"] == DIRECTOR["name_en"]
    assert shareholders[0]["class_name"] == "Ordinary"
    assert shareholders[0]["shares_held"] == 1000
    assert shareholders[0]["percentage_held"] == "100.00"
    agreeing += 5
    return agreeing, agreeing  # every comparison above is an asserted equality


# --- The walk ---------------------------------------------------------------


async def test_items_18_21_22_the_representative_sample_walk(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 7 exit gate — DoD §9 items 18/21/22: the internal end-to-end walk")
    app = make_app(session, store)
    admin_client = TestClient(app)
    staff_client = TestClient(app)

    # Step 1 — the tenant, the two accounts, the verified Administrator.
    announce("step 1 — bootstrap, MFA enrollment, the General Staff account")
    _admin, tenant_id, _admin_token = await verified_admin_session(session, admin_client)
    staff = create_staff(admin_client)
    staff_login = login(staff_client, STAFF_EMAIL, STAFF_PASSWORD)
    staff_client.headers["Authorization"] = f"Bearer {staff_login['access_token']}"
    say("General Staff login → the session verifies by role (no MFA surface)")
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        set(),
        AuditAction.TENANT_CREATED,
        AuditAction.USER_CREATED,
        AuditAction.STAFF_LOGIN,
        AuditAction.MFA_ENROLLED,
    )

    # Step 2 — the representative CSV portfolio (two companies, one dormant).
    announce("step 2 — the CSV portfolio import")
    report = post_csv(admin_client, PORTFOLIO_CSV)
    assert (report["created"], report["updated"], report["rejected"]) == (2, 0, 0)
    companies = {row["br_number"]: row["company_id"] for row in report["rows"]}
    assert set(companies) == {COMPANY_A_BRN, "76540002"}
    company_id = companies[COMPANY_A_BRN]
    portfolio = admin_client.get("/api/v1/companies")
    assert portfolio.status_code == 200 and len(portfolio.json()) == 2
    say(
        f"POST /api/v1/companies/import → 2 created / 0 updated / 0 rejected; "
        f"{COMPANY_A_BRN} active, 76540002 dormant"
    )
    # The contract's grant: the General actor may touch company A only.
    session.add(
        CompanyAccessGrant(
            user_id=UUID(staff["id"]),
            company_id=UUID(company_id),
            tenant_id=tenant_id,
        )
    )
    await session.flush()
    say(f"company access grant: General Staff ↔ {company_id}")
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries, seen, AuditAction.COMPANIES_IMPORTED, AuditAction.COMPANY_FACT_RECORDED
    )

    # Step 3 — the service package covering the walk's automation types.
    # Coverage comes before the ingestion batch on purpose: the extraction
    # trigger is §5.1-gated automation and asks for the document_extraction
    # service, so the assignment must stand first (the phase-1/5 precedent).
    announce("step 3 — coverage: the package and the assignment")
    package = admin_client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": [
                "document_extraction",
                "email_ingestion",
                "confirmation_generation",
                "automated_prefill",
                "ai_reply_parsing",
            ],
            "covered_forms": ["NAR1", "ND2A"],
        },
    )
    assert package.status_code == 201, package.text
    assignment = admin_client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": COVERAGE["start"].isoformat(),
            "coverage_end": COVERAGE["end"].isoformat(),
        },
    )
    assert assignment.status_code == 201, assignment.text
    say(
        "one package: document_extraction, email_ingestion, "
        "confirmation_generation, "
        "automated_prefill, ai_reply_parsing; assigned 2026-01-01..2027-12-31"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries, seen, AuditAction.SERVICE_PACKAGE_CREATED, AuditAction.SERVICE_ASSIGNMENT_CREATED
    )

    # Step 4 — the prior NAR1, read into master data (staff's own review leg).
    announce("step 4 — the prior NAR1: upload, extraction batch, staff confirm")
    document = staff_client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=nar1",
        content=acroform_pdf(FILING),
        headers={"Content-Type": "application/pdf"},
    )
    assert document.status_code == 201, document.text
    batch_response = staff_client.post(
        f"/api/v1/companies/{company_id}/import-batches",
        json={"document_id": document.json()["id"], "form": "nar1"},
    )
    assert batch_response.status_code == 201, batch_response.text
    batch = batch_response.json()
    assert batch["shape"] == "acroform"
    assert {record["status"] for record in batch["records"]} == {"pending"}
    say(
        f"the digital prior filing → batch {batch['id']} ({batch['shape']}): "
        f"{len(batch['records'])} proposals, all pending"
    )
    confirmed = 0
    for record in batch["records"]:
        if record["target"] == "company.br_number":
            continue  # the anchor static: the contract's Administrator leg
        response = staff_client.post(
            f"/api/v1/companies/{company_id}/import-batches/{batch['id']}"
            f"/records/{record['id']}/confirm",
            json=None,
        )
        assert response.status_code == 200, response.text
        confirmed += 1
    anchor = next(r for r in batch["records"] if r["target"] == "company.br_number")
    statics = admin_client.post(
        f"/api/v1/companies/{company_id}/import-batches/{batch['id']}"
        f"/records/{anchor['id']}/confirm",
        json=None,
    )
    assert statics.status_code == 200, statics.text
    say(
        f"the General Staff confirms {confirmed} fact proposals (name, office, "
        "business nature, charges — each inheriting the return's own made-up "
        f"date {FILING_MADE_UP_DATE}); the Administrator confirms the one "
        "anchor static (company.br_number), the contract's statics split"
    )
    # The facts the return did not state: share capital through the write path.
    patch = staff_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "effective_date": "2020-01-01",
            "share_capital_classes": SHARE_CAPITAL,
        },
    )
    assert patch.status_code == 200, patch.text
    officer = staff_client.post(
        f"/api/v1/companies/{company_id}/officers",
        json={
            "person": {
                "person_type": "natural_person",
                "name_zh": DIRECTOR["name_zh"],
                "name_en": DIRECTOR["name_en"],
                "surname_en": DIRECTOR["surname_en"],
                "other_names_en": DIRECTOR["other_names_en"],
                "email": "chan@harbourlight.example",
                "hkid_partial": "A1234",
            },
            "role": "director",
            "capacity": ["director"],
            "valid_from": "2024-01-01",
            "correspondence_address": FILING_OFFICE,
        },
    )
    assert officer.status_code == 201, officer.text
    shareholding = staff_client.post(
        f"/api/v1/companies/{company_id}/shareholdings",
        json={
            "person_id": officer.json()["person"]["id"],
            "class_name": "Ordinary",
            "shares_held": 1000,
            "percentage_held": "100.00",
            "allotment_date": "2020-01-01",
            "amount_paid": "1000.00",
            "amount_unpaid": "0.00",
            "address": FILING_OFFICE,
            "valid_from": "2020-01-01",
        },
    )
    assert shareholding.status_code == 201, shareholding.text
    say(
        "PATCH share capital (one Ordinary class, 2020-01-01); one baseline "
        "director and one member position through F1's routes"
    )
    master = staff_client.get(
        f"/api/v1/companies/{company_id}/master-data?as_of={TARGET_DATE.isoformat()}"
    )
    assert master.status_code == 200, master.text
    assert master.json()["name"]["name_en"] == NAME_EN
    assert master.json()["registered_office"] == {
        "effective_date": FILING_MADE_UP_DATE.isoformat(),
        **FILING_OFFICE,
        "country": None,
    }
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        seen,
        AuditAction.DOCUMENT_REGISTERED,
        AuditAction.PRIOR_FILING_EXTRACTED,
        AuditAction.IMPORT_RECORD_CONFIRMED,
        AuditAction.PERSON_CREATED,  # the officers route's inline person
        AuditAction.OFFICER_APPOINTMENT_CREATED,
        AuditAction.SHAREHOLDING_CREATED,
    )

    # Step 5 — the operator acknowledgment and the tenant's milestone panel.
    announce("step 5 — the acknowledgment and the tenant config defaults")
    # The inference channel's deployment legs, planted the M6 harness way: an
    # activation credential in the per-test credential file and the cached
    # Standard lease it entitles — the pre-flight reads both for real.
    now = datetime.now(UTC)
    credential = ActivationCredential(
        token="activation-token",
        token_id=uuid4(),
        control_plane_tenant_id=uuid4(),
        expires_at=now + timedelta(days=1),
    )
    ActivationStore(settings.cp_activation_token_file).save(
        ActivationState(credential=credential)
    )
    session.add(
        LeaseCache(
            control_plane_tenant_id=credential.control_plane_tenant_id,
            tier="standard",
            license_expires_at=now + timedelta(days=30),
            entitlement_version=1,
            fetched_at=now,
            last_attempt_at=now,
        )
    )
    await session.flush()
    say(
        "the deployment's activation credential and its cached Standard "
        "lease — the channel pre-flight's legs"
    )
    # The operator layer the 2026-10-04 rulings added to the pre-flight: the
    # Administrator confirms the risk-disclosure statement through the real
    # route, or every AI call below refuses ai_acknowledgment_missing.
    acknowledged = admin_client.post("/api/v1/risk-acknowledgment")
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["acknowledged"] is True
    say(
        "POST /risk-acknowledgment → 200 "
        "(the operator's confirmation, the pre-flight's operator leg)"
    )
    config = admin_client.get("/api/v1/tenant-config")
    assert config.status_code == 200, config.text
    milestones = config.json()["milestones"]
    assert milestones["reminder_cadence_days"]
    assert config.json()["send_modes"]["client"] == "automatic"
    say(
        "GET /tenant-config → the shipped milestone panel "
        f"(client reply {milestones['client_reply_days_before']} days before the "
        f"anniversary, reminders on {milestones['reminder_cadence_days']}) and the "
        "founder's default client send mode 'automatic'"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        seen,
        AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED,
    )

    # Step 6 — the confirmation request: drafted by staff, released by the
    # default send mode, delivered over the fake send transport.
    announce("step 6 — the confirmation request: draft, release, delivery")
    person = admin_client.post("/api/v1/persons", json={"name_en": CONTACT_PERSON})
    assert person.status_code == 201, person.text
    channel = admin_client.post(
        f"/api/v1/persons/{person.json()['id']}/contact-channels",
        json={"channel_type": "email", "identifier": CLIENT_EMAIL},
    )
    assert channel.status_code == 201, channel.text
    linked = admin_client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person.json()["id"]}
    )
    assert linked.status_code in (200, 201), linked.text
    say(f"the client contact: {CONTACT_PERSON} <{CLIENT_EMAIL}> associated with the company")
    # The release gate's period legs: the period's answer and its fee.
    renewal = staff_client.post(
        f"/api/v1/companies/{company_id}/service-renewals",
        json={"anniversary_year": TARGET_DATE.year},
    )
    assert renewal.status_code == 200, renewal.text
    decision = staff_client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{TARGET_DATE.year}/decision",
        json={"decision": "confirmed", "note": "client confirmed the renewal"},
    )
    assert decision.status_code == 200, decision.text
    payment = staff_client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{TARGET_DATE.year}/payment", json={}
    )
    assert payment.status_code == 200, payment.text
    say(
        f"the {TARGET_DATE.year} period: renewal confirmed and the fee recorded — "
        "the release gate's two period legs"
    )
    drafted = staff_client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
    )
    assert drafted.status_code == 201, drafted.text
    request = drafted.json()
    assert request["status"] == "queued"
    checklist_fact = request["checklist"][0]["fact_id"]
    say(
        f"POST /companies/{company_id}/confirmation-requests → 201: reply by "
        f"{request['reply_by']}, {len(request['checklist'])} checklist items; the "
        "'automatic' send mode releases it the moment it is written"
    )
    queued = list(
        (
            await session.scalars(
                select(MailOutbox).where(
                    MailOutbox.tenant_id == tenant_id,
                    MailOutbox.confirmation_request_id == UUID(request["id"]),
                )
            )
        ).all()
    )
    assert len(queued) == 1 and queued[0].status == "pending"
    say(f"the outbox row stands pending ({queued[0].id}) — the release path")
    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        monkeypatch.setattr(settings, "smtp_host", "localhost")
        monkeypatch.setattr(settings, "smtp_port", server.port)
        monkeypatch.setattr(settings, "mail_app_password", APP_PASSWORD)
        sent = staff_client.post(
            f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "sent"
        say("the send route releases and delivers: the SMTP fallback leg (the fake server)")
    assert len(server.messages) == 1
    assert server.messages[0].recipients == [CLIENT_EMAIL]
    outbound = (
        await session.scalar(
            select(Communication).where(
                Communication.tenant_id == tenant_id,
                Communication.confirmation_request_id == UUID(request["id"]),
            )
        )
    )
    assert outbound is not None and outbound.direction == "outbound"
    assert outbound.mailbox_message_id is not None
    say(
        f"one message leaves for {CLIENT_EMAIL}; the outbound Communication "
        f"carries the stamped message id {outbound.mailbox_message_id}"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        seen,
        AuditAction.CONTACT_CHANNEL_ADDED,
        AuditAction.COMPANY_CONTACT_LINKED,
        AuditAction.SERVICE_RENEWAL_RECORDED,
        AuditAction.CONFIRMATION_REQUEST_DRAFTED,
        AuditAction.CONFIRMATION_REQUEST_RELEASED,
        AuditAction.MAIL_MESSAGE_SENT,
    )

    # Step 7 — one synthetic inbound reply on the thread, parsed by the
    # stubbed gateway.
    announce("step 7 — the client's reply: ingest over the fake IMAP, parse by the stub")
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    reply = EmailMessage()
    reply["From"] = f"{CONTACT_PERSON} <{CLIENT_EMAIL}>"
    reply["To"] = MAILBOX
    reply["Subject"] = f"Re: [{NAME_EN} — CR 7654001] Annual Return Confirmation"
    reply["In-Reply-To"] = outbound.mailbox_message_id
    reply.set_content(REPLY_BODY)
    fake.add_message(raw=reply.as_bytes(policy=SMTP_POLICY))
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_app_password", APP_PASSWORD)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    ingested = admin_client.post("/api/v1/mail/ingest")
    assert ingested.status_code == 200, ingested.text
    assert ingested.json()["ingested"] == 1, ingested.text
    inbound = (
        await session.scalar(
            select(Communication).where(
                Communication.tenant_id == tenant_id, Communication.direction == "inbound"
            )
        )
    )
    assert inbound is not None
    assert inbound.confirmation_request_id == UUID(request["id"])
    say(
        "POST /mail/ingest → 1 ingested: the reply joins the request's thread "
        "by its In-Reply-To header (§5.1.12's deterministic join)"
    )
    scripted: dict[str, Any] = {
        "status": "changes_reported",
        "language": "en",
        "confirmations": [
            {
                "fact_id": checklist_fact,
                "answer": "confirmed",
                "evidence_span": CONFIRM_EVIDENCE,
                "uncertain": False,
            }
        ],
        "change_events": [
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": EVENT_NAME,
                    "name_en": EVENT_NAME,
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": EVENT_NAME,
                    "uncertain": False,
                },
                "effective_date": EVENT_EFFECTIVE,
                "effective_date_text": "1 November 2026",
                "evidence_span": EVENT_EVIDENCE,
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ],
        "multi_company_signals": {"company_mentions": []},
    }
    transport = StubTransport(scripted)
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)
    parsed = admin_client.post("/api/v1/mail/parse-replies")
    assert parsed.status_code == 200, parsed.text
    outcome = parsed.json()["outcomes"][0]
    assert outcome["outcome"] == "parsed"
    # Ruling R-D: the reply proposes appointing a director the company's
    # appointments do not carry yet, so a flawless 1.0 run is capped one band
    # below auto-accept — the F1 walk re-measures the routing rule, and this
    # is the rule's own answer for a new-person proposal.
    assert outcome["routing"] == "staff_review"
    assert len(outcome["event_ids"]) == 1
    say(
        "POST /mail/parse-replies → parsed, routing staff_review (R-D: the "
        "named person is not on the register): one change proposal, one "
        "gateway call through the stubbed transport"
    )
    event_id = outcome["event_ids"][0]
    event = await session.scalar(select(ChangeEvent).where(ChangeEvent.id == UUID(event_id)))
    assert event is not None
    assert event.status == ChangeEventStatus.PENDING.value
    assert event.source_type == "communication"
    assert event.source_id == inbound.id
    assert event.effective_date == date.fromisoformat(EVENT_EFFECTIVE)
    say(
        f"the proposal lands pending, source_type 'communication', the reply as "
        f"its source, effective {EVENT_EFFECTIVE}"
    )
    (parse_row,) = (
        await session.scalars(select(ReplyParse).where(ReplyParse.communication_id == inbound.id))
    ).all()
    assert parse_row.status == "changes_reported"
    assert parse_row.routing == "staff_review"
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == inbound.id,
            )
        )
    ).all()
    assert assessment.component == "reply_parser"
    assert assessment.routing == "staff_review"
    assert {k: float(v) for k, v in assessment.field_scores.items()} == {
        f"confirmations.{checklist_fact}": 1.0,
        "change_events.0": 1.0,
    }
    say(
        "the parse row and the §2.7 assessment land with the walk's own reply "
        "measured 1.0 field-level (the DoD-22 routing half, re-measured: the "
        "band is the register rule's, the fields are the model's)"
    )
    llm_entries = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action == AuditAction.LLM_CALL.value
    ]
    (llm_entry,) = llm_entries
    assert llm_entry.llm_call is not None
    assert llm_entry.llm_call["task_type"] == "reply_parsing"
    assert llm_entry.llm_call["provider"] == "stub-provider"
    assert llm_entry.llm_call["prompt_tokens"] == 12
    assert llm_entry.llm_call["snippet_sha256"] == hashlib.sha256(
        transport.snippets[0].encode("utf-8")
    ).hexdigest()
    say(
        "the llm_call entry books provider, model, token counts and the digest "
        "of the exact minimized bytes that left"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        seen,
        AuditAction.MAIL_MESSAGE_INGESTED,
        AuditAction.LLM_CALL,
        AuditAction.REPLY_PARSED,
        AuditAction.CHANGE_EVENT_RECORDED,
        AuditAction.RECONFIRMATION_ENQUEUED,
    )

    # Step 8 — the review queue: staff review and confirmation.
    announce("step 8 — the event through the review gate")
    queue = staff_client.get("/api/v1/change-events")
    assert queue.status_code == 200, queue.text
    assert event_id in {item["id"] for item in queue.json()}
    for action in ("mark_under_review", "mark_reviewed", "confirm"):
        moved = staff_client.post(
            f"/api/v1/change-events/{event_id}/transitions", json={"action": action}
        )
        assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "confirmed"
    say(
        "the review queue lists it; General Staff walks pending → "
        "under_review → reviewed → confirmed (review_events, review_events, "
        "approve_events — the contract's two flags)"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(entries, seen, AuditAction.CHANGE_EVENT_TRANSITION_RECORDED)

    # Step 9 — the NAR1 draft, the signature, the approval gate, three exports.
    announce("step 9 — the draft, the signature, the approval gate, the exports")
    generated = staff_client.post(
        f"/api/v1/companies/{company_id}/drafts",
        json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
    )
    assert generated.status_code == 201, generated.text
    draft = generated.json()
    assert (draft["version_no"], draft["status"]) == (1, "generated")
    assert draft["applied_event_ids"] == [event_id]
    assert draft["filing_metadata"]["return"]["made_up_date"] == TARGET_DATE.isoformat()
    say(
        f"POST /companies/{company_id}/drafts → 201: version 1, made-up date "
        f"{TARGET_DATE}, the confirmed event applied into the resolution"
    )
    agreeing, compared = resolution_accuracy(draft["resolved_state"])
    say(
        f"DoD 22 (re-measured on this walk): {agreeing}/{compared} resolved "
        "fields agree with the truth set that entered through the routes = "
        "100% (≥99%); the six-scenario widget measurement is cited, not rebuilt"
    )

    def release_sign_review(draft_id: str, version_no: int) -> str:
        url = f"/api/v1/drafts/{draft_id}/transitions"
        edit = staff_client.post(
            url,
            json={
                "action": "edit",
                "filing_metadata": {"member_particulars_delivery": "schedule_1"},
            },
        )
        assert edit.status_code == 200, edit.text
        released = staff_client.post(url, json={"action": "send_for_signature"})
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "sent_for_signature"
        package = released.json()["signature_package"]
        assert package is not None and package["escalations"]
        uploaded = staff_client.post(
            f"/api/v1/drafts/{draft_id}/signature",
            content=signed_bytes(version_no),
            headers={"Content-Type": "application/pdf"},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["status"] == "signature_received"
        reviewed = staff_client.post(url, json={"action": "mark_reviewed"})
        assert reviewed.status_code == 200, reviewed.text
        return url

    # Version 1 → paper.
    url = release_sign_review(draft["id"], 1)
    refused = staff_client.post(url, json={"action": "approve"})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "statement_confirmation_required"
    say(
        "edit → send_for_signature (the signature-ready package) → the signed "
        "upload → mark_reviewed; approve without the item-16 confirmation "
        "refuses 422 statement_confirmation_required"
    )
    approved = staff_client.post(url, json={"action": "approve", "confirm_statement": True})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    say("approve with confirm_statement → approved, the statement pair recorded")
    exported = staff_client.post(f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "paper"})
    assert exported.status_code == 201, exported.text
    package = exported.json()
    assert package["mode"] == "paper"
    assert package["envelope"]["sheet_counts"] == {"s1": 1}
    assert package["envelope"]["checklist_document_id"] is not None
    primary = await session.scalar(
        select(Document).where(Document.id == UUID(package["document_id"]))
    )
    assert primary is not None
    checklist = await session.scalar(
        select(Document).where(Document.id == UUID(package["envelope"]["checklist_document_id"]))
    )
    assert checklist is not None
    assert b"NAR1" in store.objects[checklist.storage_key]
    assert COMPANY_A_BRN.encode() in store.objects[checklist.storage_key]
    flat = PdfReader(BytesIO(store.objects[primary.storage_key]))
    assert flat.get_fields() in (None, {})
    filed = await session.scalar(select(ChangeEvent).where(ChangeEvent.id == UUID(event_id)))
    assert filed is not None and filed.status == ChangeEventStatus.SUBMITTED.value
    say(
        "export (paper) → 201: the flat PDF and the checklist stored, the "
        "applied event filed 'submitted'"
    )
    entries = await audit_entries(session, tenant_id)
    seen = assert_new(
        entries,
        seen,
        AuditAction.DRAFT_GENERATED,
        AuditAction.DRAFT_TRANSITION_RECORDED,
        AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED,
        AuditAction.DRAFT_EXPORTED,
    )

    # Versions 2 and 3 — the remaining export modes, one export per draft.
    for version_no, mode in ((2, "e_registry_prep"), (3, "api_ready")):
        regenerated = staff_client.post(
            f"/api/v1/companies/{company_id}/drafts",
            json={"form_type": "nar1", "target_date": TARGET_DATE.isoformat()},
        )
        assert regenerated.status_code == 201, regenerated.text
        body = regenerated.json()
        assert (body["id"], body["version_no"]) == (draft["id"], version_no)
        url = release_sign_review(draft["id"], version_no)
        approved = staff_client.post(url, json={"action": "approve", "confirm_statement": True})
        assert approved.status_code == 200, approved.text
        exported = staff_client.post(
            f"/api/v1/drafts/{draft['id']}/exports", json={"mode": mode}
        )
        assert exported.status_code == 201, exported.text
        envelope = exported.json()["envelope"]
        if mode == "e_registry_prep":
            assert envelope["helper_document_id"] is not None
        else:
            api_document = await session.scalar(
                select(Document).where(Document.id == UUID(exported.json()["document_id"]))
            )
            assert api_document is not None
            payload = json.loads(store.objects[api_document.storage_key])
            assert payload["form_type"] == "nar1"
            assert payload["draft"] == {"id": draft["id"], "version_no": 3}
            assert payload["resolved_state"] == body["resolved_state"]
        say(f"version {version_no} → export ({mode}) → 201")

    # The one-export-per-draft rule: the third draft is spent; a second export
    # refuses and writes nothing.
    before = await audit_entries(session, tenant_id)
    second = staff_client.post(
        f"/api/v1/drafts/{draft['id']}/exports", json={"mode": "api_ready"}
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "transition_not_allowed"
    assert await audit_entries(session, tenant_id) == before
    say(
        "the second export of the spent draft refuses 409 "
        "transition_not_allowed and the chain is byte-identical — the "
        "one-export-per-draft rule (§4.2, P6-4), asserted in the walk"
    )

    # Step 10 — DoD 18's completeness list, made executable, and the chain.
    announce("step 10 — the audit chain: DoD 18's five areas, then the verdict")
    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    areas = {ACTION_AREA[AuditAction(action)] for action in actions}
    assert areas == set(ReportArea), (
        f"DoD 18's areas not all represented: {set(ReportArea) - areas}"
    )
    say("every one of DoD 18's five report areas is represented on this one chain:")
    for area in ReportArea:
        members = sorted({a for a in actions if ACTION_AREA[AuditAction(a)] is area})
        say(f"  {area.value:<22} {', '.join(members)}")
    assert actions.count(AuditAction.DRAFT_GENERATED.value) == 3
    assert actions.count(AuditAction.DRAFT_EXPORTED.value) == 3
    assert actions.count(AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED.value) == 3
    assert actions.count(AuditAction.LLM_CALL.value) == 1
    assert actions.count(AuditAction.REPLY_PARSED.value) == 1
    assert actions.count(AuditAction.MAIL_MESSAGE_SENT.value) == 1
    assert actions.count(AuditAction.MAIL_MESSAGE_INGESTED.value) == 1
    # 3 staff review transitions + the generation's apply + the export's submit.
    assert actions.count(AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value) == 5
    assert actions.count(AuditAction.IMPORT_RECORD_CONFIRMED.value) == confirmed + 1
    say(
        f"{len(entries)} entries; the counts pin the walk's shape: 3 "
        "generations, 3 exports, 1 gateway call and its parse, 1 message sent "
        "and 1 ingested, 5 event transitions (3 staff, the generation's apply, "
        f"the export's submit), {confirmed + 1} record confirmations"
    )
    assert verify_chain(entries) == []
    say("verify_chain over the tenant's full chain → no violations (DoD 18/21)")
