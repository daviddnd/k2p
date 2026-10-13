"""DB-backed confirmation-request scenarios (opt-in; needs a migrated
PostgreSQL).

The M3 generator end to end against a real database: a company with master
data and a registered client contact, a package covering
``confirmation_generation``, and one trigger producing the §5.1.8 artifacts —
a stored bilingual ``message/rfc822`` draft (keyed from the tenant's
``generated`` template) and a ``confirmation_requests`` row carrying the
checklist. The suite drives ``app.application.confirmation.generate`` — the
one write path — and the setup runs the real endpoints (the same
``cover()``-style harness the ingestion suite uses).

What the custody test asserts is the slice's sharpest claim, so it is
asserted against the whole table rather than against a shape: a marker planted
in the master data reaches the request row's ``checklist`` and the stored
message, and the tenant's audit chain is searched entry by entry for it and
for the question phrasing. The marker *is* in the chain — in the
``company_fact_recorded`` entries the master-data write itself has always
written (it records the fact payload, ADR-0007/ADR-0009, and that predates
this slice) — so the assertion names exactly which entries may carry it and
proves that none of the generator's do. The question phrasing is in the
stored message and the checklist and nowhere in the chain at all.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

# This file quotes the bilingual copy and the client-facing subject it
# checks, so it carries the same deliberate, file-wide RUF001 suppression as
# the module that writes them.
# ruff: noqa: RUF001

import json
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import confirmation
from app.application.company_master import CompanyNotFoundError
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.company_master import CompanyType
from app.domain.compliance import NAR1_FILING_WINDOW_DAYS, DueDateRuleNotEstablishedError
from app.domain.confirmation import (
    FACTS,
    CompanyNameUnavailableError,
    NoClientReplyWindowError,
    RequestStatus,
    UnsupportedFormError,
)
from app.domain.documents import DocumentKind, DocumentOrigin, ScanStatus
from app.domain.milestones import (
    DEFAULT_CLIENT_REPLY_DAYS_BEFORE,
    DEFAULT_MINIMUM_REPLY_WINDOW_DAYS,
)
from app.domain.prior_filing import FormCode
from app.domain.service_packages import GatingReason
from app.main import create_app
from tests.fake_scanning import FakeScanner

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
# The period the suite works in: the anniversary still ahead of today. The
# panel's reply-by is counted back from the anniversary and the generator
# refuses a request whose reply-by would fall after CR's deadline (the
# founder's ruling of 2026-09-24), so a period fixed in the past would make
# every drafting scenario impossible. Everything below derives from it, and
# nothing else in the suite depends on the wall clock.
ANNIVERSARY = date(date.today().year + 1, 3, 1)
TARGET_DATE = ANNIVERSARY.isoformat()
# The anniversary falling in the target date's year, plus the statutory 42-day
# window: the guard the reply-by is checked against, stored on the row.
STATUTORY_DUE = ANNIVERSARY + timedelta(days=NAR1_FILING_WINDOW_DAYS)
# The anniversary minus the shipped client-reply offset (20 days): the date a
# tenant with no stored milestone block gives its client.
REPLY_BY = ANNIVERSARY - timedelta(days=DEFAULT_CLIENT_REPLY_DAYS_BEFORE)
# A period whose statutory deadline has passed, for the route-level refusal:
# the anniversary is set far enough back that CR's date is behind today.
CLOSED_ANNIVERSARY = date.today() - timedelta(days=100)
COVERAGE = {"start": f"{ANNIVERSARY.year - 1}-01-01", "end": f"{ANNIVERSARY.year}-12-31"}
LAPSED_COVERAGE = {
    "start": f"{ANNIVERSARY.year - 2}-01-01",
    "end": f"{ANNIVERSARY.year - 2}-12-31",
}
COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": ANNIVERSARY.isoformat(),
    "type": "private",
}


def as_printed(value: date) -> str:
    """The English form of a date as the copy prints it (``9 Feb 2027``).

    The wording's own suite pins the format verbatim; this suite only needs to
    find the row's derived date in the stored letter.
    """
    return f"{value.day} {value.strftime('%b')} {value.year}"


NAME_EN = "Bright Lantern Design Limited"
NAME_ZH = "明燈設計有限公司"
# Planted in the master data: it reaches the checklist and the stored message,
# and the audit search below is what proves where it does and does not go.
NATURE_MARKER = "ZED-MARKER-4471"
NATURE_DESCRIPTION = f"Design of printed matter ({NATURE_MARKER})"
# A phrase from the rendered question itself: the words the client reads, never
# a value the master data holds.
QUESTION_PHRASE = "Is the company's business nature still recorded as"


def nature_value(description: str) -> str:
    """How the business-nature fact renders: the code, then the description."""
    return f"74990 \u2014 {description}"


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


# --- Harness -------------------------------------------------------------


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_tenant_with_admin(session: AsyncSession) -> tuple[Tenant, User]:
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


def provision(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)


def configure(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    """Provision the mailbox address the message is written from (§2.5)."""
    monkeypatch.setattr(settings, "mail_address", address)


def hold_sends_for_review(client: TestClient) -> None:
    """Set the tenant's client send mode to ``review_required``.

    These suites are about **drafting** (and, in the renewals suite, about the
    gate a release meets); the shipped default for the client destination is
    ``automatic`` (the founder's ruling, contract §5.1.11), which would release
    every draft the moment it is written and make "drafted" and "sent" the same
    assertion here. Holding sends for review is one real `PUT /tenant-config`
    and keeps each suite's subject its own — the sending path's own tests are
    ``test_mail_send_scenarios_db.py``.
    """
    current = client.get("/api/v1/tenant-config").json()
    response = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
            "send_modes": {"client": "review_required", "government": "review_required"},
        },
    )
    assert response.status_code == 200, response.text


def make_company(client: TestClient, **overrides: object) -> str:
    response = client.post("/api/v1/companies", json={**COMPANY, **overrides})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def name_company(
    client: TestClient, company_id: str, *, name_en: str = NAME_EN, name_zh: str | None = NAME_ZH
) -> None:
    """Record a registered name through the effective-dated fact path."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": name_zh}},
    )
    assert response.status_code == 200, response.text


def record_business_nature(
    client: TestClient,
    company_id: str,
    *,
    description: str = NATURE_DESCRIPTION,
    effective_date: str = "2026-01-01",
) -> None:
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "effective_date": effective_date,
            "business_nature": {
                "business_nature_code": "74990",
                "business_nature_description": description,
            },
        },
    )
    assert response.status_code == 200, response.text


def add_registered_contact(client: TestClient, company_id: str, *, email: str = CONTACT) -> str:
    """A person with an email channel, associated with the company."""
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={"channel_type": "email", "identifier": email},
    )
    assert channel.status_code == 201, channel.text
    associate(client, company_id, person_id)
    return person_id


def associate(client: TestClient, company_id: str, person_id: str) -> None:
    response = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    assert response.status_code in (200, 201), response.text


def cover(
    client: TestClient,
    company_id: str,
    *,
    services: Sequence[str] | None = None,
    coverage: dict[str, str] = COVERAGE,
) -> dict:
    """Assign a package covering ``services`` through the real endpoints.

    Returns the assignment, so a caller can end it (the gate's
    ``assignment_ended`` leg).
    """
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": (
                list(services) if services is not None else ["confirmation_generation"]
            ),
            "covered_forms": ["NAR1", "ND2A"],
        },
    )
    assert package.status_code == 201, package.text
    assignment = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": coverage["start"],
            "coverage_end": coverage["end"],
        },
    )
    assert assignment.status_code == 201, assignment.text
    return assignment.json()


def record_continuation(
    client: TestClient, company_id: str, *, year: int, status: str = "confirmed"
) -> dict:
    """Record the period's continuation the way staff do, through the routes.

    ``confirmed`` (the default) is what a deployment that is ready to draft has
    on the record: §5.1.8's trigger refuses a period the client has not
    continued, so this is part of setting up a draftable deployment rather than
    an extra step in the test. ``pending`` and ``declined`` are the states the
    gate tests need, and ``paid`` walks one step further for the release gate.
    """
    created = client.post(
        f"/api/v1/companies/{company_id}/service-renewals", json={"anniversary_year": year}
    )
    assert created.status_code == 200, created.text
    if status == "pending":
        return created.json()
    decision = client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{year}/decision",
        json={
            "decision": "declined" if status == "declined" else "confirmed",
            "note": "moving provider" if status == "declined" else "client confirmed",
        },
    )
    assert decision.status_code == 200, decision.text
    if status != "paid":
        return decision.json()
    paid = client.post(f"/api/v1/companies/{company_id}/service-renewals/{year}/payment", json={})
    assert paid.status_code == 200, paid.text
    return paid.json()


async def draftable_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tier: str = "standard",
    covered: bool = True,
    services: Sequence[str] | None = None,
    coverage: dict[str, str] = COVERAGE,
    register_contact: bool = True,
    mailbox: str = MAILBOX,
    named: bool = True,
    nature: bool = True,
    ended: bool = False,
    anniversary: str | None = None,
    continuation: str | None = None,
) -> tuple[TestClient, Tenant, User, str]:
    """A tenant, a company with master data, and a package that covers drafting.

    ``continuation`` records the period's renewal through the routes; the
    default leaves the period unrecorded, because since the founder's revision
    of 2026-09-24 the trigger does not read the record at all — a deployment
    with nothing recorded drafts exactly as one the client has confirmed.
    ``confirmed``/``pending``/``declined``/``paid`` are there for the scenarios
    that need a period's state for another reason (a download's release gate).
    The year is the company's own anniversary year, which is the period the
    trigger's ``target_date`` selects.
    """
    tenant, admin = await seed_tenant_with_admin(session)
    provision(monkeypatch, tier)
    configure(monkeypatch, address=mailbox)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    hold_sends_for_review(client)
    company_id = make_company(
        client, **({} if anniversary is None else {"anniversary_date": anniversary})
    )
    if named:
        name_company(client, company_id)
    if nature:
        record_business_nature(client, company_id)
    if register_contact:
        add_registered_contact(client, company_id)
    if covered:
        assignment = cover(client, company_id, services=services, coverage=coverage)
        if ended:
            ended_assignment = client.patch(
                f"/api/v1/companies/{company_id}/service-assignments/{assignment['id']}",
                json={"status": "ended"},
            )
            assert ended_assignment.status_code == 200, ended_assignment.text
    if continuation is not None:
        record_continuation(client, company_id, year=period_year(anniversary), status=continuation)
    return client, tenant, admin, company_id


def period_year(anniversary: str | None) -> int:
    """The anniversary year a deployment's period is keyed by."""
    return (ANNIVERSARY if anniversary is None else date.fromisoformat(anniversary)).year


async def draft(
    session: AsyncSession,
    store: FakeStore,
    scanner: "FakeScanner",
    *,
    tenant: Tenant,
    admin: User,
    company_id: str,
    target_date: str = TARGET_DATE,
    as_of: date | None = None,
    contact_person_id: UUID | None = None,
    form_type: FormCode | str = FormCode.NAR1,
) -> ConfirmationRequest:
    """One request through the slice's own write path."""
    return await confirmation.generate(
        session,
        store,
        scanner,
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        actor_user_id=admin.id,
        form_type=form_type,
        target_date=date.fromisoformat(target_date),
        as_of=as_of if as_of is not None else date.today(),
        contact_person_id=contact_person_id,
    )


async def stored_documents(session: AsyncSession) -> list[Document]:
    return list((await session.scalars(select(Document))).all())


async def stored_requests(session: AsyncSession) -> list[ConfirmationRequest]:
    return list(
        await session.scalars(select(ConfirmationRequest).order_by(ConfirmationRequest.version_no))
    )


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


def entry_text(entry: AuditLog) -> str:
    """One audit entry as the searchable text of its whole row."""
    return json.dumps(
        {
            "action": entry.action,
            "details": entry.details,
            "llm_call": entry.llm_call,
            "entity_type": entry.entity_type,
            "entity_id": str(entry.entity_id),
        }
    )


def stored_message(store: FakeStore, document: Document):
    """The stored ``.eml``, parsed the way a mail client reads it."""
    return message_from_bytes(store.objects[document.storage_key], policy=default_policy)


# --- The draft -----------------------------------------------------------


async def test_a_draft_becomes_a_stored_message_a_checklist_and_a_row(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The whole artifact: the row, the stored message, the audit entry.

    The message is the thing staff review and (until the sending slice) send
    by hand, so it has to be byte-complete and reachable through the existing
    download route; the row is what the reply parser joins on.
    """
    # The period is paid as well as confirmed: this scenario follows the draft
    # through to the download a staff member makes before sending it by hand,
    # and that download is the release gate's to refuse until the fee is
    # recorded (ADR-0017).
    client, tenant, admin, company_id = await draftable_deployment(
        session, store, monkeypatch, continuation="paid"
    )

    row = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )

    assert row.form_type == FormCode.NAR1.value
    assert row.target_date == ANNIVERSARY
    assert row.reply_by == REPLY_BY
    # The statutory date is recorded beside the printed one: the guard that
    # verified "the client is not asked to reply after CR's deadline" is on the
    # row itself, not only in the code that ran.
    assert row.statutory_due == STATUTORY_DUE
    assert row.reply_by < row.statutory_due
    assert row.status == RequestStatus.DRAFT.value
    assert row.version_no == 1
    assert row.created_by == admin.id
    assert row.company_id == UUID(company_id)
    # One recipient: the company's associated person, at their email channel.
    assert len(row.recipients) == 1
    assert row.recipients[0]["address"] == CONTACT
    assert UUID(row.recipients[0]["person_id"]) == UUID(
        client.get(f"/api/v1/companies/{company_id}/contacts").json()[0]["id"]
    )

    # One stored document per draft: the message, keyed from the tenant's
    # ``generated`` template with the version, named by the type's extension.
    rows = await stored_documents(session)
    assert len(rows) == 1
    document = rows[0]
    assert document.id == row.document_id
    assert document.kind == DocumentKind.GENERATED_DRAFT.value
    assert document.origin == DocumentOrigin.GENERATED.value
    assert document.content_type == "message/rfc822"
    assert document.retention_class == "client_communication"
    assert document.scan_status == ScanStatus.CLEAN.value
    assert document.uploaded_by == admin.id
    today = datetime.now(UTC).date()
    assert document.storage_key == (
        f"1234567/{today.year}/NAR1_draft_1234567_{today.strftime('%Y%m%d')}_v1.eml"
    )
    assert document.supersedes_document_id is None

    # The stored bytes are the message: subject, both languages, the numbered
    # questions, and the reply-by date the tenant's milestone panel derived.
    message = stored_message(store, document)
    subject = str(message["Subject"])
    assert NAME_EN in subject
    assert NAME_ZH in subject
    assert "CR 1234567" in subject
    assert "Annual Return Confirmation" in subject
    assert f"Reply by {as_printed(REPLY_BY)}" in subject
    # The founder's ruling, end to end: the letter never prints CR's own last
    # day — the anniversary minus the configured offset is what the client is
    # asked for, and the 42 days stay as margin.
    assert as_printed(STATUTORY_DUE) not in subject
    assert message["From"] == MAILBOX
    assert message["To"] == CONTACT
    body = message.get_content()
    assert "敬啟者" in body
    assert "NO CHANGES" in body
    chinese = f"{REPLY_BY.year}年{REPLY_BY.month}月{REPLY_BY.day}日"
    assert f"回覆期限：{as_printed(REPLY_BY)} / {chinese}" in body
    # The numbered items follow the checklist's order.
    position = [item["fact_id"] for item in row.checklist].index("company.business_nature") + 1
    assert f"{position}. {QUESTION_PHRASE}" in body
    assert NATURE_MARKER in body
    assert NAME_EN in body and NAME_ZH in body

    # The checklist is the request's own: one item per applicable fact, in the
    # §3 order, each naming the row its value came from. Every fact applies to
    # this company — it has no share capital in force, so NAR1 item 10's
    # member-count question belongs in the checklist.
    assert [item["fact_id"] for item in row.checklist] == [spec.fact_id for spec in FACTS]
    nature_item = next(
        item for item in row.checklist if item["fact_id"] == "company.business_nature"
    )
    assert nature_item["expected_value"] == nature_value(NATURE_DESCRIPTION)
    assert nature_item["question"].startswith(QUESTION_PHRASE)
    assert nature_item["source_record_id"] is not None
    # A fact the product holds nothing for is asked as an open question.
    directors = next(item for item in row.checklist if item["fact_id"] == "officers.directors")
    assert directors["expected_value"] is None
    assert directors["source_record_id"] is None

    # Staff review it in the registry, download it, and send it by hand from
    # the TCSP's own mailbox (scope §2.5) until the sending slice lands.
    download = client.get(f"/api/v1/companies/{company_id}/documents/{document.id}/download")
    assert download.status_code == 200, download.text
    assert document.storage_key in download.json()["url"]
    assert download.json()["expires_in"] > 0

    # Two entries: the registry's own, and the request's.
    entries = await audit_entries(session, tenant.id)
    actions = [entry.action for entry in entries]
    assert actions[-2:] == [
        AuditAction.DOCUMENT_REGISTERED.value,
        AuditAction.CONFIRMATION_REQUEST_DRAFTED.value,
    ]
    drafted = entries[-1]
    assert drafted.entity_type == "confirmation_request"
    assert drafted.entity_id == row.id
    assert drafted.company_id == UUID(company_id)
    assert drafted.actor_user_id == admin.id
    assert drafted.details["document_id"] == str(document.id)
    assert drafted.details["form_type"] == "nar1"
    assert drafted.details["target_date"] == TARGET_DATE
    assert drafted.details["reply_by"] == REPLY_BY.isoformat()
    assert drafted.details["version_no"] == 1
    # Addresses are the inbound path's precedent; the questions' text is not
    # here (which the custody test below proves against the whole chain).
    assert drafted.details["recipients"] == [CONTACT]
    assert drafted.details["facts"] == [item["fact_id"] for item in row.checklist]


# --- The reply-by date: the milestone panel's anchor ----------------------


async def test_a_late_trigger_floors_the_clients_reply_window(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The configured offset has passed, so the client gets their minimum
    window measured from the generation day — while CR's deadline allows it."""
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    as_of = STATUTORY_DUE - timedelta(days=10)

    row = await draft(
        session,
        store,
        scanner_seam,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        as_of=as_of,
    )

    # The nominal milestone (the anniversary minus the offset) is already behind
    # us; what the client is asked for is the floor, and it is still inside the
    # statutory window.
    assert row.reply_by == as_of + timedelta(days=DEFAULT_MINIMUM_REPLY_WINDOW_DAYS)
    assert row.reply_by > REPLY_BY
    assert row.reply_by < STATUTORY_DUE
    assert row.statutory_due == STATUTORY_DUE


async def test_a_window_that_no_longer_fits_refuses_and_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """Fewer than the minimum reply window left before CR's deadline: the
    generator refuses rather than asking a client to reply late, and the
    refusal writes nothing at all."""
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    before = await audit_entries(session, tenant.id)
    # The statutory window closes three days from now; the floor needs seven.
    as_of = STATUTORY_DUE - timedelta(days=3)

    with pytest.raises(NoClientReplyWindowError) as caught:
        await draft(
            session,
            store,
            scanner_seam,
            tenant=tenant,
            admin=admin,
            company_id=company_id,
            as_of=as_of,
        )

    assert caught.value.statutory_due == STATUTORY_DUE
    assert await stored_documents(session) == []
    assert await stored_requests(session) == []
    assert await audit_entries(session, tenant.id) == before


async def test_the_route_refuses_a_closed_reply_window(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The same refusal on the wire, over a real database: a period whose
    statutory deadline has passed answers `422 no_client_reply_window` and
    leaves no stored message, no request row, and no audit entry."""
    client, tenant, _, company_id = await draftable_deployment(
        session, store, monkeypatch, anniversary=CLOSED_ANNIVERSARY.isoformat()
    )
    before = await audit_entries(session, tenant.id)

    refused = client.post(
        requests_url(company_id),
        json={"form_type": "nar1", "target_date": CLOSED_ANNIVERSARY.isoformat()},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "no_client_reply_window"
    assert await stored_documents(session) == []
    assert await stored_requests(session) == []
    assert await audit_entries(session, tenant.id) == before


async def test_the_checklist_states_the_values_in_force_on_the_generation_day(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The snapshot *is* the as-of read: a later record is not what is asked.

    A supersession mid-flight leaves the client being asked about the value
    that was in force on the generation day, not the newest one — the whole
    point of resolving master data as of a date.
    """
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    record_business_nature(
        client, company_id, description="Design of printed matter", effective_date="2026-06-01"
    )

    early = await draft(
        session,
        store,
        scanner_seam,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        as_of=date(2026, 3, 1),
    )
    late = await draft(
        session,
        store,
        scanner_seam,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        as_of=date(2026, 7, 1),
    )

    def nature(checklist: list[dict[str, object]]) -> dict[str, object]:
        return next(item for item in checklist if item["fact_id"] == "company.business_nature")

    # The March read sees January's record; the July read sees June's.
    assert nature(early.checklist)["expected_value"] == nature_value(NATURE_DESCRIPTION)
    assert nature(late.checklist)["expected_value"] == nature_value("Design of printed matter")


async def test_the_member_count_question_is_dropped_once_a_share_capital_is_in_force(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """NAR1 item 10 is the companies *without* a share capital.

    The applicability rule is the domain's; this is the master-data wiring
    under it — a share capital in force at the read's date is what drops the
    question.
    """
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    recorded = client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "effective_date": "2026-01-01",
            "share_capital_classes": {
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
        },
    )
    assert recorded.status_code == 200, recorded.text

    row = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )

    fact_ids = [item["fact_id"] for item in row.checklist]
    assert "company.member_count" not in fact_ids
    assert "company.share_capital" in fact_ids
    share_item = next(item for item in row.checklist if item["fact_id"] == "company.share_capital")
    assert share_item["expected_value"] == "Ordinary — 1000 shares, HKD 1000.00"


async def test_the_subject_falls_back_to_the_brn_when_no_cr_number_is_held(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """A company the master data holds no CR number for is named by its BRN.

    The same CR-else-BRN rule the storage key and the inbound resolver use —
    a reply must be attributable whichever identifier the sheet carries.
    """
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    company = client.get(f"/api/v1/companies/{company_id}").json()
    assert company["cr_number"] == "1234567"
    corrected = client.patch(f"/api/v1/companies/{company_id}/statics", json={"cr_number": None})
    assert corrected.status_code == 200, corrected.text

    row = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )

    document = (await stored_documents(session))[0]
    subject = str(stored_message(store, document)["Subject"])
    assert "BRN 12345678" in subject
    assert "CR " not in subject
    assert row.reply_by == REPLY_BY


async def test_regeneration_supersedes_the_open_draft_and_versions_the_lineage(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """One live draft per company and form: v2 replaces v1 and v1 is kept.

    The superseded row and its message are not deleted — they are the record
    of what the client was asked before — and the partial unique index is the
    backstop under "which draft is live?".
    """
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)

    first = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )
    second = await draft(
        session,
        store,
        scanner_seam,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        # A different made-up date in the same year: the reply-by is derived
        # from the anniversary that year, so it does not move.
        target_date=(ANNIVERSARY + timedelta(days=31)).isoformat(),
    )

    assert (first.version_no, second.version_no) == (1, 2)
    assert first.status == RequestStatus.SUPERSEDED.value
    assert second.status == RequestStatus.DRAFT.value
    assert second.reply_by == REPLY_BY
    first_document = await session.get(Document, first.document_id)
    second_document = await session.get(Document, second.document_id)
    assert second_document.supersedes_document_id == first_document.id
    assert len(await stored_documents(session)) == 2
    assert len(await stored_requests(session)) == 2

    # The registry's entry for the second document names the first: the drafts
    # are a chain, not a set of unrelated files.
    entries = await audit_entries(session, tenant.id)
    drafted = [e for e in entries if e.action == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value]
    assert [e.details["version_no"] for e in drafted] == [1, 2]
    assert "superseded_request_id" not in drafted[0].details
    assert drafted[1].details["superseded_request_id"] == str(first.id)

    # The backstop: a second open draft for the same company and form cannot
    # be inserted at all, whatever a caller does.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ConfirmationRequest(
                    tenant_id=tenant.id,
                    company_id=UUID(company_id),
                    form_type="nar1",
                    target_date=ANNIVERSARY + timedelta(days=61),
                    reply_by=REPLY_BY,
                    status="draft",
                    version_no=99,
                    document_id=second.document_id,
                    checklist=[],
                    recipients=[],
                )
            )
            await session.flush()


async def test_no_question_text_and_no_master_data_value_reaches_an_audit_entry(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The custody claim, searched against the whole chain and the whole schema.

    A marker planted in the master data must reach the request row's
    ``checklist`` and the stored message (that is what the client is asked
    about) and must not reach an audit entry the **generator** writes. The
    chain is searched entry by entry, and the one carrier it is allowed to
    have is named: the ``company_fact_recorded`` entries the master-data write
    itself has always written (it records the fact payload — ADR-0007/0009,
    predating this slice). The question phrasing, which is the product's own
    words rather than any stored value, appears nowhere in the chain at all.
    """
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)

    row = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )

    # Present where the client is asked about it: the checklist and the letter.
    assert any(
        item["expected_value"] == nature_value(NATURE_DESCRIPTION)
        and QUESTION_PHRASE in item["question"]
        and NATURE_MARKER in item["question"]
        for item in row.checklist
    )
    document = await session.get(Document, row.document_id)
    body = stored_message(store, document).get_content()
    assert NATURE_MARKER in body
    assert QUESTION_PHRASE in body

    # And absent from the audit chain — except in the master-data write's own
    # entry, which is where the value has always been recorded.
    entries = await audit_entries(session, tenant.id)
    carrying = [entry for entry in entries if NATURE_MARKER in entry_text(entry)]
    assert [entry.action for entry in carrying] == [AuditAction.COMPANY_FACT_RECORDED.value]
    assert AuditAction.CONFIRMATION_REQUEST_DRAFTED.value not in {
        entry.action for entry in entries if NATURE_MARKER in entry_text(entry)
    }
    assert QUESTION_PHRASE not in "\n".join(entry_text(entry) for entry in entries)

    # The same claim against the table rather than the rows a test happens to
    # know about: the marker's only home is the fact table and the request.
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert QUESTION_PHRASE not in audit_text
    assert NATURE_MARKER in audit_text  # the fact write's payload, named above


# --- The gate ------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "covered", "services", "coverage", "ended", "expected"),
    [
        ("free", True, None, COVERAGE, False, GatingReason.TIER_CAPABILITY_MISSING),
        ("standard", False, None, COVERAGE, False, GatingReason.NO_ASSIGNMENT),
        (
            "standard",
            True,
            ["email_ingestion"],
            COVERAGE,
            False,
            GatingReason.SERVICE_NOT_INCLUDED,
        ),
        (
            "standard",
            True,
            ["confirmation_generation"],
            LAPSED_COVERAGE,
            False,
            GatingReason.OUT_OF_COVERAGE,
        ),
        (
            "standard",
            True,
            ["confirmation_generation"],
            COVERAGE,
            True,
            GatingReason.ASSIGNMENT_ENDED,
        ),
    ],
    ids=[
        "free-tier",
        "no-assignment",
        "service-not-included",
        "out-of-coverage",
        "assignment-ended",
    ],
)
async def test_the_gate_refusal_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
    tier: str,
    covered: bool,
    services: Sequence[str] | None,
    coverage: dict[str, str],
    ended: bool,
    expected: GatingReason,
) -> None:
    """Every reachable leg of the §5.1 gate denies, and a denial writes nothing.

    All five: the tier's capability, no assignment at all, an ended
    assignment, coverage that has lapsed, and a package that omits the
    service. No stored message, no request row, and no audit entry — the
    trigger is what is gated, so a denied company must look untouched
    afterwards, and nothing reaches the scanner either.
    """
    _, tenant, admin, company_id = await draftable_deployment(
        session,
        store,
        monkeypatch,
        tier=tier,
        covered=covered,
        services=services,
        coverage=coverage,
        ended=ended,
    )
    before = await audit_entries(session, tenant.id)
    assert await stored_documents(session) == []

    with pytest.raises(confirmation.AutomationDeniedError) as refusal:
        await draft(session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id)

    assert refusal.value.reason is expected
    assert await stored_documents(session) == []
    assert await stored_requests(session) == []
    assert await audit_entries(session, tenant.id) == before
    # Nothing was even offered to the scanner: the denial is before the write
    # path, not inside it.
    assert scanner_seam.scanned == []


# --- The refusals --------------------------------------------------------


async def test_a_company_with_no_email_contact_is_refused_and_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """No recipient, no draft: an unaddressed letter is not a draft."""
    _, tenant, admin, company_id = await draftable_deployment(
        session, store, monkeypatch, register_contact=False
    )

    with pytest.raises(confirmation.NoClientContactError):
        await draft(session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id)

    assert await stored_documents(session) == []
    assert await stored_requests(session) == []


async def test_a_named_contact_who_cannot_receive_is_refused(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """A person who is not an email recipient of the company is named or nothing.

    Two ways to be that: not associated with the company at all, or
    associated with no email channel.
    """
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    stranger = client.post("/api/v1/persons", json={"name_en": "LEE SIU MING"}).json()
    with_email = add_registered_contact(client, company_id, email="chair@brightlantern.example.hk")
    phoneless = client.post("/api/v1/persons", json={"name_en": "WONG KA YAN"}).json()
    client.post(
        f"/api/v1/persons/{phoneless['id']}/contact-channels",
        json={"channel_type": "phone", "identifier": "21234567"},
    )
    associate(client, company_id, str(phoneless["id"]))

    for named in (stranger["id"], str(phoneless["id"])):
        with pytest.raises(confirmation.ClientContactNotFoundError):
            await draft(
                session,
                store,
                scanner_seam,
                tenant=tenant,
                admin=admin,
                company_id=company_id,
                contact_person_id=UUID(named),
            )

    assert await stored_documents(session) == []

    # And the happy half: naming a recipient who can receive drafts to them
    # alone.
    row = await draft(
        session,
        store,
        scanner_seam,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        contact_person_id=UUID(with_email),
    )
    assert [recipient["address"] for recipient in row.recipients] == [
        "chair@brightlantern.example.hk"
    ]


async def test_a_deployment_without_a_mailbox_address_is_refused(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """No mailbox address, no draft: §2.5 has it come from the TCSP's own domain."""
    _, tenant, admin, company_id = await draftable_deployment(
        session, store, monkeypatch, mailbox=""
    )

    with pytest.raises(confirmation.MailNotConfiguredError):
        await draft(session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id)

    assert await stored_documents(session) == []
    assert await stored_requests(session) == []


async def test_a_form_this_slice_cannot_confirm_is_refused(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """ND2A fails closed: every ND2A target is a change event, not master data."""
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)

    with pytest.raises(UnsupportedFormError):
        await draft(
            session,
            store,
            scanner_seam,
            tenant=tenant,
            admin=admin,
            company_id=company_id,
            form_type=FormCode.ND2A,
        )

    assert await stored_documents(session) == []
    assert await stored_requests(session) == []


async def test_a_company_with_no_name_in_force_is_refused_and_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """An unnamed company cannot be identified, so no message is written.

    Scope §7's guardrail: every client-facing message identifies the company
    by name and CR number — and the subject is also where a reply's
    attribution begins.
    """
    _, tenant, admin, company_id = await draftable_deployment(
        session, store, monkeypatch, named=False
    )

    with pytest.raises(CompanyNameUnavailableError):
        await draft(session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id)

    assert await stored_documents(session) == []
    assert await stored_requests(session) == []


async def test_a_company_type_without_an_established_rule_is_refused_and_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """No reply-by date means no request: the rule is never guessed.

    A public company's NAR1 deadline rule is not established in this product,
    so the generator refuses rather than writing a link the client cannot act
    on.
    """
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    corrected = client.patch(
        f"/api/v1/companies/{company_id}/statics", json={"type": CompanyType.PUBLIC.value}
    )
    assert corrected.status_code == 200, corrected.text

    with pytest.raises(DueDateRuleNotEstablishedError):
        await draft(session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id)

    assert await stored_documents(session) == []
    assert await stored_requests(session) == []


async def test_an_unknown_or_cross_tenant_company_is_not_found(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    _, tenant, admin, _ = await draftable_deployment(session, store, monkeypatch)

    with pytest.raises(CompanyNotFoundError):
        await draft(
            session, store, scanner_seam, tenant=tenant, admin=admin, company_id=str(uuid4())
        )


async def test_the_periods_renewal_is_not_consulted_at_all(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The reverted ruling, as a scenario: drafting is not gated.

    Three periods — one with no renewal row whatsoever, one the client has not
    answered, and one the client declined — all produce the same draft, because
    the founder's revision of 2026-09-24 moved the gate to the moment a
    document goes *to the client*. What the record can still refuse is the
    release of that stored letter (the download route, whose own suite covers
    it), never the writing of it.
    """
    for continuation in (None, "pending", "declined"):
        _, tenant, admin, company_id = await draftable_deployment(
            session, store, monkeypatch, continuation=continuation
        )
        before = len(await stored_requests(session))

        row = await draft(
            session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
        )

        assert row.status == RequestStatus.DRAFT.value
        assert len(await stored_requests(session)) == before + 1
        assert AuditAction.CONFIRMATION_REQUEST_DRAFTED.value in [
            entry.action for entry in await audit_entries(session, tenant.id)
        ]
        assert AuditAction.DOCUMENT_RELEASE_REFUSED.value not in [
            entry.action for entry in await audit_entries(session, tenant.id)
        ]


async def test_a_period_with_no_record_at_all_is_draftable(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The most common case — nothing has been recorded about the period — and
    the one the reverted precondition refused: the letter is written anyway,
    and no renewal row is created as a side effect of asking."""
    client, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)

    assert client.get(f"/api/v1/companies/{company_id}/service-renewals").json() == []

    row = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )

    assert row.status == RequestStatus.DRAFT.value
    assert client.get(f"/api/v1/companies/{company_id}/service-renewals").json() == []


async def test_the_route_drafts_for_a_period_nothing_has_answered(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim on the wire: 201, one stored message, one request row —
    the refusal that used to be here is gone rather than merely unreachable."""
    client, _, _, company_id = await draftable_deployment(session, store, monkeypatch)

    drafted = client.post(
        requests_url(company_id), json={"form_type": "nar1", "target_date": TARGET_DATE}
    )

    assert drafted.status_code == 201, drafted.text
    assert drafted.json()["status"] == "draft"
    assert len(await stored_documents(session)) == 1
    assert len(await stored_requests(session)) == 1


# --- The route -----------------------------------------------------------


def requests_url(company_id: str) -> str:
    return f"/api/v1/companies/{company_id}/confirmation-requests"


async def test_the_route_drafts_a_request_end_to_end(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The wire, over the real write path: POST drafts, GET reads it back.

    The service-level scenarios above cover the behaviour; this is the route
    shell around it against a real database — the request body the caller
    sends, the 201 they get, the row and message that exist afterwards, and
    the read that returns the same request.
    """
    client, tenant, _, company_id = await draftable_deployment(session, store, monkeypatch)

    created = client.post(
        requests_url(company_id), json={"form_type": "nar1", "target_date": TARGET_DATE}
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == RequestStatus.DRAFT.value
    assert body["form_type"] == "nar1"
    assert body["target_date"] == TARGET_DATE
    assert body["reply_by"] == REPLY_BY.isoformat()
    # The statutory date travels with the request: a reader can see both what
    # the client was asked for and what that was guarded against.
    assert body["statutory_due"] == STATUTORY_DUE.isoformat()
    assert body["version_no"] == 1
    assert body["recipients"] == [
        {"person_id": body["recipients"][0]["person_id"], "address": CONTACT}
    ]
    assert [item["fact_id"] for item in body["checklist"]] == [spec.fact_id for spec in FACTS]
    assert body["created_by"] is not None

    rows = await stored_requests(session)
    assert len(rows) == 1
    document = await session.get(Document, rows[0].document_id)
    assert str(document.id) == body["document_id"]
    assert document.storage_key.endswith("_v1.eml")
    assert NATURE_MARKER in stored_message(store, document).get_content()
    # The response is the request — its parameters and the checklist it will
    # be answered against — and not the rendered letter: the salutation, the
    # close and the signature block are the stored message's alone.
    assert "敬啟者" not in created.text
    assert "NO CHANGES" not in created.text
    assert "Yours faithfully" not in created.text
    actions = [entry.action for entry in await audit_entries(session, tenant.id)]
    assert actions[-1] == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value

    listed = client.get(requests_url(company_id))

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [body["id"]]


async def test_a_concurrent_generation_is_a_typed_conflict(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The lost half of an overlapping regeneration is a refusal, not a 500.

    Two generations that both read "no open draft" compute the same next
    version and race to insert a live draft; the partial unique index lets
    exactly one commit, so the loser meets `IntegrityError` at the insert and
    is answered `409 draft_conflict` — re-read, retry — instead of a server
    error. **No two live drafts can ever commit**: the index is the guarantee,
    and this refusal is the availability half.

    **How the race is reached:** a true two-connection overlap is impractical
    inside this suite's single rolled-back transaction (a second connection
    cannot see uncommitted seeded rows, and the first holds their locks), so
    the sibling's draft row is inserted directly and the generation's own
    open-draft read is made to return nothing — which is precisely what a
    racer's read sees before its sibling commits. Everything after that point
    is the real path: the real insert, the real index, the real route.

    On a real request edge the `409` rolls the whole transaction back, so
    nothing of the loser — not even the message it had composed and stored —
    commits; in this shared session the transaction is still open, which is
    why the assertions below are about the request row and the response.
    """
    client, tenant, _, company_id = await draftable_deployment(session, store, monkeypatch)
    # Any registered document will do: it is the row's presence the index sees.
    uploaded = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=NAR1",
        content=b"%PDF-1.7 sibling",
        headers={"Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 201, uploaded.text
    session.add(
        ConfirmationRequest(
            tenant_id=tenant.id,
            company_id=UUID(company_id),
            form_type="nar1",
            target_date=ANNIVERSARY,
            reply_by=REPLY_BY,
            status=RequestStatus.DRAFT.value,
            version_no=1,
            document_id=UUID(uploaded.json()["id"]),
            checklist=[],
            recipients=[],
        )
    )
    await session.flush()

    async def stale_read(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(confirmation, "_open_draft", stale_read)

    refused = client.post(
        requests_url(company_id), json={"form_type": "nar1", "target_date": TARGET_DATE}
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "draft_conflict"
    # The sibling is still the one live draft, and the loser added no row of
    # its own and wrote no drafting audit entry.
    rows = await stored_requests(session)
    assert len(rows) == 1
    assert rows[0].version_no == 1
    assert rows[0].status == RequestStatus.DRAFT.value
    assert [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value
    ] == []
    assert [
        row.id
        for row in await confirmation.list_requests(
            session, tenant_id=tenant.id, company_id=UUID(company_id)
        )
    ] == [rows[0].id]


async def test_the_route_refuses_a_denied_company_and_writes_nothing(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The gate's refusal on the wire, over a real database.

    The status, the reason the dashboard reads, and the proof that nothing was
    written: no stored message, no request row, no audit entry.
    """
    client, tenant, _, company_id = await draftable_deployment(
        session, store, monkeypatch, covered=False
    )
    before = await audit_entries(session, tenant.id)

    refused = client.post(
        requests_url(company_id), json={"form_type": "nar1", "target_date": TARGET_DATE}
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "automation_denied: no_assignment"
    assert await stored_documents(session) == []
    assert await stored_requests(session) == []
    assert await audit_entries(session, tenant.id) == before


async def test_the_route_refuses_each_refusal_with_its_own_code(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The 4xx codes a caller can rely on, over a real database."""
    client, _, _, company_id = await draftable_deployment(session, store, monkeypatch)

    unsupported = client.post(
        requests_url(company_id), json={"form_type": "nd2a", "target_date": TARGET_DATE}
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"] == "unsupported_form"

    unnamed = make_company(client, br_number="87654321", cr_number="7654321")
    add_registered_contact(client, unnamed, email="other@brightlantern.example.hk")
    cover(client, unnamed)
    missing_name = client.post(
        requests_url(unnamed), json={"form_type": "nar1", "target_date": TARGET_DATE}
    )
    assert missing_name.status_code == 422
    assert missing_name.json()["detail"] == "company_name_unavailable"

    assert client.get(f"{requests_url(uuid4())}").status_code == 404
    assert (
        client.post(
            f"{requests_url(uuid4())}", json={"form_type": "nar1", "target_date": TARGET_DATE}
        ).status_code
        == 404
    )


# --- The read ------------------------------------------------------------


async def test_the_list_is_the_companys_requests_newest_first_and_is_ungated(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """The read survives a lapse (ADR-0012) and answers newest draft first."""
    _, tenant, admin, company_id = await draftable_deployment(session, store, monkeypatch)
    first = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )
    second = await draft(
        session, store, scanner_seam, tenant=tenant, admin=admin, company_id=company_id
    )
    # A lapsed licence: the trigger is denied, the customer's own rows are not.
    provision(monkeypatch, "free")

    rows = await confirmation.list_requests(
        session, tenant_id=tenant.id, company_id=UUID(company_id)
    )

    assert [row.id for row in rows] == [second.id, first.id]
    assert [row.status for row in rows] == [
        RequestStatus.DRAFT.value,
        RequestStatus.SUPERSEDED.value,
    ]


async def test_a_row_written_under_the_old_rule_reads_a_null_statutory_due(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    """Migration ``0019`` adds the column with no backfill, deliberately.

    Historical rows were written when the reply-by *was* CR's date: the column
    stays NULL on them rather than claiming a guard that was never run, and the
    read answers ``null`` instead of inventing one.
    """
    client, tenant, _, company_id = await draftable_deployment(session, store, monkeypatch)
    uploaded = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=NAR1",
        content=b"%PDF-1.7 historical",
        headers={"Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 201, uploaded.text
    session.add(
        ConfirmationRequest(
            tenant_id=tenant.id,
            company_id=UUID(company_id),
            form_type="nar1",
            target_date=ANNIVERSARY,
            reply_by=STATUTORY_DUE,  # the old rule: the reply-by *was* CR's date
            status=RequestStatus.SUPERSEDED.value,
            version_no=1,
            document_id=UUID(uploaded.json()["id"]),
            checklist=[],
            recipients=[],
        )
    )
    await session.flush()

    listed = client.get(requests_url(company_id))

    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["statutory_due"] is None
    assert listed.json()[0]["reply_by"] == STATUTORY_DUE.isoformat()
