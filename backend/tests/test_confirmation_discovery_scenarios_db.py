"""The discovery cycle's exit-gate walk (opt-in; needs a migrated PostgreSQL).

The slice's acceptance evidence for the due-company sweep, driven through the
real endpoint stack against the real database, one walk per scenario:

1. **the due company walks from sweep to panel-done.** A Standard-tier tenant
   with a mailbox address, a covered, named, contactable company whose
   anniversary sits ``data_confirmation_days_before`` out — so the ask date is
   today, the day the walk runs — and last year's period already asked (the
   realistic shape: the cycle ran last year; without that history the sweep
   attempts last year's period first and the trigger refuses it
   ``no_client_reply_window``). ``POST /api/v1/mail/confirmation-discovery``
   answers one ``generated`` outcome: the request is ``queued`` under the
   tenant's ``automatic`` client mode, its client outbox row pends, one
   ``confirmation_request_drafted`` entry lands on the hash chain — and the
   client-actions panel's data-confirmation milestone for the period reads
   ``done``. A second sweep the same day is the idempotency proof: the period
   is ``skipped: already_requested``, no second request, no second outbox row,
   no second audit entry.
2. **a ``review_required`` tenant holds the draft.** The same shape with the
   tenant's client send mode set to ``review_required``: the walk still
   generates, but the request stays a held ``draft`` and **no** outbox row
   exists for it — nothing is queued behind the walk's back.
3. **a company with no client contact is refused and writes nothing.** Named
   and covered but no associated contact: the outcome is the trigger's own
   ``no_client_contact``, with no request row and no audit entry for the
   company — the refusal is a report entry, never a write.
4. **one vocabulary for "was the client asked?" (R2), through the real panel
   route.** A mixed seed walks both reads over every panel state — asked last
   year (superseded history), asked this year (sent), never asked — and pins
   what the route can see on any calendar day: the sweep's skips are exactly
   the earliest-arrived periods holding a request row (whatever its status),
   and no sweep outcome ever names a period the panel marks ``done``. (The
   literal set-equality the application suite pins with a mid-January ``as_of``
   is calendar-gated: the sweep's skip is always keyed on the earliest arrived
   candidate year — ``as_of.year - 1`` — while the panel route only lists
   periods with a milestone inside its ``[today-30, today+90]`` window, which
   never includes ``as_of.year - 1`` outside early January. The application
   suite owns that pinned-as_of equality; this walk owns the route-observable
   half.)

Across the walks the audit hash chain verifies after every scenario — the
chain records the trigger's own ``confirmation_request_drafted`` entry on a
generation and nothing for a refusal or a skip.

**The clock.** Every business fact enters through a real route; the one thing
no route can do is choose the day. The panel and the discovery route both read
``date.today()`` for ``as_of``, so the walks move the *anchor* instead (the
family convention): the company's ``anniversary_date`` is placed so the
configured offsets land where the walk needs them — thirty days out for the
due-today walk (the shipped default offset), thirteen days back for the R2
seed (whose period is both arrived and inside the panel's window on the day).

**The harness** mirrors ``tests/test_confirmation_discovery_application.py``
(the rollback session, the in-memory store, the seeded tenant and
Administrator, the route helpers, the prior-year seed) rather than inventing a
new one — but it is restated here, not imported, because this family's
convention is that no test module imports another's fixtures or helpers.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import documents, scanning
from app.core.config import settings
from app.domain.audit import AuditAction, verify_chain
from app.domain.compliance import anniversary_in_year
from app.domain.confirmation import RequestStatus
from app.domain.documents import MESSAGE_CONTENT_TYPE
from app.domain.mail_send import OutboxKind, OutboxStatus
from app.domain.milestones import DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

DISCOVERY_URL = "/api/v1/mail/confirmation-discovery"
PANEL_URL = "/api/v1/client-actions"

MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
NAME_EN = "Bright Lantern Design Limited"

# The walk's clock travel, both anchors moved rather than the wall clock (see
# the module docstring). The due company: the configured offset counted forward
# from today puts its data-confirmation ask date on the day the walk runs.
DUE_ANNIVERSARY = date.today() + timedelta(days=DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE)
# The R2 seed: thirteen days back, the period's signature posting (offset 14)
# sits inside the panel's 30-day lookback on any run date, so the period is
# both arrived (its ask date is 43 days past) and listed.
R2_ANCHOR = date.today() - timedelta(days=13)

COVERAGE = {"start": "2020-01-01", "end": "2030-12-31"}


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes."""

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


@pytest.fixture(autouse=True)
def standard_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard tier and a mailbox address: the cycle's letter has a From."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)


@pytest.fixture
async def actor(session: AsyncSession) -> ActorRef:
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
    return ActorRef(user_id=admin.id, tenant_id=tenant.id)


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


# --- Harness -------------------------------------------------------------


def make_company(client: TestClient, *, anniversary: date = DUE_ANNIVERSARY) -> str:
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": f"{uuid4().int % 100000000:08d}",
            "cr_number": f"{uuid4().int % 10000000:07d}",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": anniversary.isoformat(),
            "type": "private",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def name_company(client: TestClient, company_id: str, *, name_en: str = NAME_EN) -> None:
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": None}},
    )
    assert response.status_code == 200, response.text


def add_registered_contact(client: TestClient, company_id: str) -> None:
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={"channel_type": "email", "identifier": f"{uuid4()}@brightlantern.example.hk"},
    )
    assert channel.status_code == 201, channel.text
    associated = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    assert associated.status_code in (200, 201), associated.text


def cover(client: TestClient, company_id: str) -> None:
    """An assignment whose package covers the automation these paths gate."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": ["confirmation_generation", "email_ingestion"],
            "covered_forms": ["NAR1"],
        },
    )
    assert package.status_code == 201, package.text
    assignment = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": COVERAGE["start"],
            "coverage_end": COVERAGE["end"],
        },
    )
    assert assignment.status_code == 201, assignment.text


def covered_company(client: TestClient, *, anniversary: date = DUE_ANNIVERSARY) -> str:
    company_id = make_company(client, anniversary=anniversary)
    name_company(client, company_id)
    add_registered_contact(client, company_id)
    cover(client, company_id)
    return company_id


def letter_bytes(message_id: str) -> bytes:
    """A minimal stored letter carrying its own Message-ID."""
    return (
        f"From: {MAILBOX}\r\n"
        f"To: {CONTACT}\r\n"
        f"Subject: [Bright Lantern — CR 1234567] Annual Return Confirmation\r\n"
        f"Message-ID: {message_id}\r\n"
        f"\r\n"
        f"Please confirm. / 請確認。\r\n"
    ).encode()


async def seed_request(
    session: AsyncSession,
    store: FakeStore,
    *,
    tenant_id: UUID,
    company_id: UUID,
    target_date: date,
    status: str,
) -> None:
    """One confirmation-request row for (company, target year), any status.

    The R2 done-signal counts any row, whatever its status: a superseded row
    is the realistic shape of "asked in the past" — the cycle ran last year —
    and seeds the sweep's "attempt this year" path by closing last year's
    period. Seeded directly because the trigger's own reply-window guard
    refuses a generation for a period whose window has closed.
    """
    document = await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=company_id,
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=letter_bytes(f"<hist-{uuid4()}@tcsp.example>"),
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=target_date,
        reply_by=target_date - timedelta(days=20),
        statutory_due=None,
        status=status,
        version_no=1,
        document_id=document.id,
        checklist=[],
        recipients=[{"person_id": str(uuid4()), "address": CONTACT}],
        created_by=None,
    )
    session.add(request)
    await session.flush()


async def request_rows(session: AsyncSession, *, company_id: UUID) -> list[ConfirmationRequest]:
    rows = await session.scalars(
        select(ConfirmationRequest)
        .where(ConfirmationRequest.company_id == company_id)
        .order_by(ConfirmationRequest.target_date)
    )
    return list(rows.all())


async def outbox_rows(session: AsyncSession, *, request_id: UUID) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox).where(MailOutbox.confirmation_request_id == request_id)
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


async def drafted_entries(session: AsyncSession, *, company_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(
            AuditLog.action == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value,
            AuditLog.company_id == company_id,
        )
    )
    return list(rows.all())


def panel_periods(client: TestClient, *, company_id: str) -> list[dict[str, object]]:
    response = client.get(PANEL_URL)
    assert response.status_code == 200, response.text
    return [
        period
        for period in response.json()["periods"]
        if period["company_id"] == company_id
    ]


def data_confirmation_milestone(period: dict[str, object]) -> dict[str, object]:
    (milestone,) = [
        item for item in period["milestones"] if item["kind"] == "data_confirmation"
    ]
    return milestone


def panel_done_set(client: TestClient) -> set[tuple[UUID, int]]:
    """Every (company, anniversary year) the panel marks data-confirmation done."""
    response = client.get(PANEL_URL)
    assert response.status_code == 200, response.text
    return {
        (UUID(period["company_id"]), period["anniversary_year"])
        for period in response.json()["periods"]
        for milestone in period["milestones"]
        if milestone["kind"] == "data_confirmation" and milestone["state"] == "done"
    }


# --- 1. the due company walks from sweep to panel-done --------------------


async def test_the_due_company_is_generated_and_the_panel_marks_it_done(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    year = DUE_ANNIVERSARY.year
    # Last year's period was asked (and since superseded): the cycle ran last
    # year. Without the history the sweep attempts last year's period first —
    # its ask date is always the earliest arrived — and the trigger refuses it
    # ``no_client_reply_window``; with it, this year's arrived ask date is the
    # period the sweep attempts.
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(DUE_ANNIVERSARY, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )

    # The panel before the cycle: the period is listed (its continuation ask
    # falls on the window's first day) and the data-confirmation milestone is
    # due today — the ask date has arrived, nothing has been generated.
    (period,) = panel_periods(client, company_id=company_id)
    assert period["anniversary_year"] == year
    milestone = data_confirmation_milestone(period)
    assert milestone["date"] == (
        DUE_ANNIVERSARY - timedelta(days=DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE)
    ).isoformat()
    assert milestone["state"] == "due"
    assert milestone["days_remaining"] == 0

    response = client.post(DISCOVERY_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["generated"], body["refused"], body["skipped"]) == (1, 1, 0, 0)
    (outcome,) = body["outcomes"]
    assert outcome["company_id"] == company_id
    assert outcome["year"] == year
    assert outcome["outcome"] == "generated"
    assert outcome["request_id"] is not None
    assert outcome["reason"] is None
    assert outcome["skip"] is None

    # Under the default ``automatic`` client mode the generation is the
    # release: the request is queued and its client outbox row pends.
    rows = await request_rows(session, company_id=UUID(company_id))
    (row,) = [item for item in rows if item.id == UUID(outcome["request_id"])]
    assert row.target_date == DUE_ANNIVERSARY
    assert row.status == RequestStatus.QUEUED.value
    (job,) = await outbox_rows(session, request_id=row.id)
    assert job.kind == OutboxKind.CONFIRMATION_REQUEST.value
    assert job.destination == "client"
    assert job.status == OutboxStatus.PENDING.value

    # One entry through the trigger's own write path, as the system actor.
    (entry,) = await drafted_entries(session, company_id=UUID(company_id))
    assert entry.entity_id == row.id
    assert entry.actor_user_id is None
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []

    # The panel after the cycle: the same milestone the sweep released now
    # reads done — the request row is the panel's own done-signal.
    (period,) = panel_periods(client, company_id=company_id)
    assert data_confirmation_milestone(period)["state"] == "done"

    # The same-day second sweep is the idempotency proof: the period —
    # whatever row's year the skip names — is already_requested, and nothing
    # is written a second time.
    entries_before = len(await audit_entries(session, actor.tenant_id))
    second = client.post(DISCOVERY_URL)
    assert second.status_code == 200, second.text
    again = second.json()
    assert (again["examined"], again["generated"], again["refused"], again["skipped"]) == (
        1,
        0,
        0,
        1,
    )
    (skip_outcome,) = again["outcomes"]
    assert skip_outcome["outcome"] == "skipped"
    assert skip_outcome["skip"] == "already_requested"
    assert skip_outcome["request_id"] is None
    # No second request (the history row plus the generated one), no second
    # outbox row, no second audit entry.
    assert len(await request_rows(session, company_id=UUID(company_id))) == 2
    assert len(await outbox_rows(session, request_id=row.id)) == 1
    assert len(await drafted_entries(session, company_id=UUID(company_id))) == 1
    assert len(await audit_entries(session, actor.tenant_id)) == entries_before
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []


# --- 2. a review_required tenant holds the draft ----------------------------


async def test_under_review_required_the_walk_holds_the_draft(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    current = client.get("/api/v1/tenant-config").json()
    put = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
            "send_modes": {"client": "review_required", "government": "review_required"},
        },
    )
    assert put.status_code == 200, put.text
    company_id = covered_company(client)
    year = DUE_ANNIVERSARY.year
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(DUE_ANNIVERSARY, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )

    response = client.post(DISCOVERY_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["generated"], body["refused"]) == (1, 1, 0)
    (outcome,) = body["outcomes"]
    assert outcome["outcome"] == "generated"
    row = await session.get(ConfirmationRequest, UUID(outcome["request_id"]))
    assert row is not None
    assert row.status == RequestStatus.DRAFT.value
    # Held: nothing is queued for it.
    assert await outbox_rows(session, request_id=row.id) == []
    assert len(await drafted_entries(session, company_id=UUID(company_id))) == 1
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []


# --- 3. a company with no client contact is refused and writes nothing ------


async def test_a_company_without_a_client_contact_is_refused_and_writes_nothing(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = make_company(client)
    name_company(client, company_id)
    cover(client, company_id)
    # Named and covered, but no associated contact with an email address.
    entries_before = len(await audit_entries(session, actor.tenant_id))

    response = client.post(DISCOVERY_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["generated"], body["refused"]) == (1, 0, 1)
    (outcome,) = body["outcomes"]
    assert outcome["company_id"] == company_id
    assert outcome["outcome"] == "refused"
    assert outcome["reason"] == "no_client_contact"
    assert outcome["request_id"] is None
    # A refusal writes nothing: no request, and no audit entry for the company.
    assert await request_rows(session, company_id=UUID(company_id)) == []
    assert await drafted_entries(session, company_id=UUID(company_id)) == []
    assert len(await audit_entries(session, actor.tenant_id)) == entries_before
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []


# --- 4. one vocabulary for "was the client asked?" (R2) ----------------------


async def test_the_sweeps_skips_are_the_asked_periods_and_it_never_reasks_a_done_one(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    """R2 through the real panel route. A mixed seed walks both reads over
    every panel state — asked for both arrived periods (last year's
    superseded history and this year's sent letter), asked for the listed
    period only, never asked — on a period that is both arrived and inside
    the panel's window (the anchor sits 13 days back; see the module
    docstring).

    The route-observable invariant: the sweep's skips are exactly the
    earliest-arrived periods already holding a request row — any status, the
    panel's own done-signal — and no sweep outcome ever names a period the
    panel marks ``done``. The literal set-equality is calendar-gated to early
    January (the skip is keyed on ``as_of.year - 1``, which the panel's
    30-day lookback only lists in the year's first days); the application
    suite pins that equality with a pinned ``as_of``.
    """
    year = R2_ANCHOR.year
    client = make_client(session, store, actor)
    asked_both = covered_company(client, anniversary=R2_ANCHOR)
    asked_current = covered_company(client, anniversary=R2_ANCHOR)
    never_asked = covered_company(client, anniversary=R2_ANCHOR)
    # Asked for the earliest arrived period (last year's cycle, since
    # superseded) and for the listed period: nothing is due, so the sweep
    # must skip, not generate — the history row still answers "asked".
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(asked_both),
        target_date=anniversary_in_year(R2_ANCHOR, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(asked_both),
        target_date=anniversary_in_year(R2_ANCHOR, year),
        status=RequestStatus.SENT.value,
    )
    # Asked for the listed period only: last year owes an attempt, which the
    # trigger's own window guard refuses — the one attempt per company never
    # reaches the asked period.
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(asked_current),
        target_date=anniversary_in_year(R2_ANCHOR, year),
        status=RequestStatus.SENT.value,
    )

    # Before the cycle: the panel's done set is exactly the companies holding
    # a row for the listed period — the row is the criterion, not its status.
    before = panel_done_set(client)
    assert before == {(UUID(asked_both), year), (UUID(asked_current), year)}

    response = client.post(DISCOVERY_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 3
    assert body["generated"] == 0
    assert body["skipped"] == 1
    assert body["refused"] == 2
    by_company = {UUID(item["company_id"]): item for item in body["outcomes"]}

    # The superseded history still answers "asked": the sweep skips the
    # earliest arrived period, naming it and no other.
    skipped = by_company[UUID(asked_both)]
    assert skipped["outcome"] == "skipped"
    assert skipped["skip"] == "already_requested"
    assert skipped["year"] == year - 1
    assert skipped["request_id"] is None
    # The skip set is exactly the earliest-arrived period holding a row.
    skip_set = {
        (company_id, item["year"])
        for company_id, item in by_company.items()
        if item["outcome"] == "skipped"
    }
    assert skip_set == {(UUID(asked_both), year - 1)}
    assert len(await request_rows(session, company_id=UUID(asked_both))) == 2
    # No sweep outcome names a period the panel marks done: the sweep never
    # re-asks an asked period, whichever year the panel lists it under.
    for company_id, item in by_company.items():
        assert (company_id, item["year"]) not in before

    # The two companies not asked for the earliest arrived period meet the
    # trigger's own window guard on it — reported, never worked around, and
    # nothing is written.
    for company_id in (asked_current, never_asked):
        outcome = by_company[UUID(company_id)]
        assert outcome["outcome"] == "refused"
        assert outcome["reason"] == "no_client_reply_window"
        assert outcome["request_id"] is None
    assert len(await request_rows(session, company_id=UUID(asked_current))) == 1
    assert await request_rows(session, company_id=UUID(never_asked)) == []

    # After the cycle the panel's done set is unchanged: a refusal and a skip
    # are report entries, never writes.
    assert panel_done_set(client) == before
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []
