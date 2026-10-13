"""DB-backed confirmation-discovery sweep scenarios (opt-in; needs a migrated PostgreSQL).

The discovery cycle against a real database: the arrived-and-unasked company is
generated (queued under ``automatic``, held ``draft`` under ``review_required``),
any request row for the period — whatever its status — is the panel's
``already_requested`` skip, the trigger's typed refusals become report entries
that write nothing, an unreadable milestone block stops the cycle cleanly, the
sweep's skip set stays pinned to the client-actions panel's own done set (R2),
and two overlapping cycles serialize on the per-tenant advisory lock.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean (the one concurrency test commits its fixture rows and
purges them — its own fixture says why).
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.base import Base
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import (
    confirmation,
    confirmation_discovery,
    documents,
    scanning,
    service_renewals,
)
from app.application.company_master import CompanyNotFoundError
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.compliance import anniversary_in_year
from app.domain.confirmation import RequestStatus
from app.domain.confirmation_discovery import DiscoveryOutcome, DiscoverySkip
from app.domain.documents import MESSAGE_CONTENT_TYPE
from app.domain.mail_send import OutboxStatus
from app.domain.milestones import MilestoneKind
from app.domain.prior_filing import FormCode
from app.domain.rbac import ActorContext, Role
from app.domain.service_renewals import MilestoneState
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
NAME_EN = "Bright Lantern Design Limited"

NOW = datetime.now(UTC)
TODAY = NOW.date()

# The sweep's eligibility reads the data-confirmation milestone (the anniversary
# minus its configured offset): an anniversary ten days out puts this year's
# ask date twenty days in the past (arrived), forty days out puts it ten days
# in the future (not arrived), forty days back makes last year's reply window
# long closed.
ANNIVERSARY = TODAY + timedelta(days=10)
FAR_ANNIVERSARY = TODAY + timedelta(days=40)
PAST_ANNIVERSARY = TODAY - timedelta(days=40)

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
    """Standard tier and a mailbox address: the sweep's letter has a From."""
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


def make_company(
    client: TestClient, *, anniversary: date = ANNIVERSARY, **overrides: object
) -> str:
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": f"{uuid4().int % 100000000:08d}",
            "cr_number": f"{uuid4().int % 10000000:07d}",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": anniversary.isoformat(),
            "type": "private",
            **overrides,
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


def add_registered_contact(
    client: TestClient, company_id: str, *, email: str | None = None
) -> None:
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={
            "channel_type": "email",
            "identifier": email or f"{uuid4()}@brightlantern.example.hk",
        },
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


def covered_company(client: TestClient, *, anniversary: date = ANNIVERSARY) -> str:
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
) -> ConfirmationRequest:
    """One confirmation-request row for (company, target year), any status.

    The R2 done-signal counts any row, whatever its status: a superseded row
    is the realistic shape of "asked in the past", a draft of "asked, held".
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
    return request


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


async def audit_count(session: AsyncSession) -> int:
    return len((await session.scalars(select(AuditLog))).all())


async def drafted_entries(session: AsyncSession, *, company_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(
            AuditLog.action == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value,
            AuditLog.company_id == company_id,
        )
    )
    return list(rows.all())


def spy_on_generate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Count and capture the trigger's calls without changing its behaviour."""
    calls: list[dict[str, object]] = []
    original = confirmation.generate

    async def spy(s: AsyncSession, st: object, sc: object, **kwargs: object) -> ConfirmationRequest:
        calls.append(kwargs)
        return await original(s, st, sc, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(confirmation_discovery.confirmation, "generate", spy)
    return calls


# --- The cycle ------------------------------------------------------------


async def test_an_arrived_unasked_year_generates_once_and_releases_under_automatic(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    year = ANNIVERSARY.year
    # Last year's period was asked (and since superseded): this year's ask
    # date has arrived, so the cycle must attempt this year.
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(ANNIVERSARY, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )
    calls = spy_on_generate(monkeypatch)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    assert report.config_error is None
    assert report.examined == 1
    assert report.generated == 1
    assert report.refused == 0
    assert report.skipped == 0
    (outcome,) = report.outcomes
    assert outcome.company_id == UUID(company_id)
    assert outcome.outcome is DiscoveryOutcome.GENERATED
    assert outcome.year == year
    assert outcome.skip is None
    assert outcome.reason is None
    # The trigger was called exactly once, as the system actor, for this
    # year's anniversary as the made-up date.
    assert len(calls) == 1
    (call,) = calls
    assert call["tenant_id"] == actor.tenant_id
    assert call["company_id"] == UUID(company_id)
    assert call["actor_user_id"] is None
    assert call["form_type"] == FormCode.NAR1
    assert call["target_date"] == ANNIVERSARY
    assert call["as_of"] == TODAY
    # Under the default automatic client mode the generation is the release:
    # the request is queued and its outbox row pends.
    assert outcome.request_id is not None
    rows = await request_rows(session, company_id=UUID(company_id))
    (row,) = [r for r in rows if r.id == outcome.request_id]
    assert row.target_date == ANNIVERSARY
    assert row.status == RequestStatus.QUEUED.value
    (job,) = await outbox_rows(session, request_id=row.id)
    assert job.status == OutboxStatus.PENDING.value
    assert job.destination == "client"
    # One audit entry through the trigger's own write path.
    (entry,) = await drafted_entries(session, company_id=UUID(company_id))
    assert entry.entity_id == row.id
    assert entry.actor_user_id is None


async def test_under_review_required_the_request_is_held_as_a_draft(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
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
    year = ANNIVERSARY.year
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(ANNIVERSARY, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.GENERATED
    assert outcome.request_id is not None
    row = await session.get(ConfirmationRequest, outcome.request_id)
    assert row is not None
    assert row.status == RequestStatus.DRAFT.value
    # Held: nothing is queued for it.
    assert await outbox_rows(session, request_id=row.id) == []
    assert len(await drafted_entries(session, company_id=UUID(company_id))) == 1


async def test_any_request_row_for_the_arrived_year_is_already_requested(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client, anniversary=FAR_ANNIVERSARY)
    # Forty days out, only last year's ask date has arrived.
    last_year = FAR_ANNIVERSARY.year - 1
    existing = await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(FAR_ANNIVERSARY, last_year),
        status=RequestStatus.DRAFT.value,
    )
    calls = spy_on_generate(monkeypatch)
    before = await request_rows(session, company_id=UUID(company_id))

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.SKIPPED
    assert outcome.skip is DiscoverySkip.ALREADY_REQUESTED
    assert outcome.year == last_year
    assert outcome.request_id is None
    assert outcome.reason is None
    assert calls == []
    # Nothing was written: the seeded row is the only one.
    assert await request_rows(session, company_id=UUID(company_id)) == before
    assert [row.id for row in before] == [existing.id]


async def test_a_gate_denied_company_is_refused_and_nothing_is_written(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    denied = covered_company(client, anniversary=FAR_ANNIVERSARY)
    monkeypatch.setattr(settings, "license_tier", "free")
    requests_before = len(await request_rows(session, company_id=UUID(company_id)))
    requests_denied_before = len(await request_rows(session, company_id=UUID(denied)))
    outbox_before = len((await session.scalars(select(MailOutbox))).all())
    audit_before = await audit_count(session)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    by_company = {outcome.company_id: outcome for outcome in report.outcomes}
    outcome = by_company[UUID(company_id)]
    assert outcome.outcome is DiscoveryOutcome.REFUSED
    assert outcome.reason == "automation_denied: tier_capability_missing"
    denied_outcome = by_company[UUID(denied)]
    assert denied_outcome.outcome is DiscoveryOutcome.REFUSED
    assert denied_outcome.reason == "automation_denied: tier_capability_missing"
    assert report.generated == 0
    # A refusal writes nothing: no request, no outbox row, no audit entry.
    assert len(await request_rows(session, company_id=UUID(company_id))) == requests_before
    assert len(await request_rows(session, company_id=UUID(denied))) == requests_denied_before
    assert len((await session.scalars(select(MailOutbox))).all()) == outbox_before
    assert await audit_count(session) == audit_before


async def test_a_company_without_an_assignment_is_refused_with_the_gates_reason(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = make_company(client)
    name_company(client, company_id)
    add_registered_contact(client, company_id)
    # No service assignment: the gate's no_assignment leg refuses.
    audit_before = await audit_count(session)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.REFUSED
    assert outcome.reason == "automation_denied: no_assignment"
    assert await request_rows(session, company_id=UUID(company_id)) == []
    assert await audit_count(session) == audit_before


async def test_a_company_without_a_client_contact_is_refused(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = make_company(client)
    name_company(client, company_id)
    cover(client, company_id)
    # Covered but no associated contact with an email address.

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.REFUSED
    assert outcome.reason == "no_client_contact"
    assert await request_rows(session, company_id=UUID(company_id)) == []


async def test_a_long_past_ask_date_is_refused_with_no_client_reply_window(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    # Forty days past the anniversary, last year's CR deadline is long gone:
    # the trigger's own window guard refuses — the sweep reports, never works
    # around it.
    company_id = covered_company(client, anniversary=PAST_ANNIVERSARY)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.REFUSED
    assert outcome.reason == "no_client_reply_window"
    assert outcome.year == PAST_ANNIVERSARY.year - 1
    assert await request_rows(session, company_id=UUID(company_id)) == []


async def test_a_company_vanishing_before_the_attempt_is_refused_not_raised(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The population read and the attempt share one transaction, but a
    concurrent commit can retire a company in the moment between them: the
    trigger raises ``CompanyNotFoundError``, and the cycle answers the
    contract's "never a 500 for one company" with the refusal vocabulary —
    a named outcome, nothing written.
    """
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    requests_before = len(await request_rows(session, company_id=UUID(company_id)))
    outbox_before = len((await session.scalars(select(MailOutbox))).all())
    audit_before = await audit_count(session)

    async def vanished(s: AsyncSession, st: object, sc: object, **kwargs: object) -> None:
        raise CompanyNotFoundError

    monkeypatch.setattr(confirmation_discovery.confirmation, "generate", vanished)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is DiscoveryOutcome.REFUSED
    assert outcome.reason == "company_not_found"
    assert outcome.request_id is None
    assert len(await request_rows(session, company_id=UUID(company_id))) == requests_before
    assert len((await session.scalars(select(MailOutbox))).all()) == outbox_before
    assert await audit_count(session) == audit_before


async def test_an_unreadable_milestone_block_stops_the_cycle_cleanly(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    session.add(
        TenantConfig(
            tenant_id=actor.tenant_id,
            naming_templates={
                "document": "{CompanyCode}-{Form}",
                "generated": "{CompanyCode}-{Form}-{Version}",
            },
            milestones={"reminder_cadence_days": "not-a-list"},
            send_modes={"client": "automatic", "government": "review_required"},
        )
    )
    await session.flush()
    requests_before = len(await request_rows(session, company_id=UUID(company_id)))
    audit_before = await audit_count(session)

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
    )

    assert report.config_error is not None
    assert report.outcomes == ()
    assert report.examined == 0
    assert len(await request_rows(session, company_id=UUID(company_id))) == requests_before
    assert await audit_count(session) == audit_before


# --- R2: one vocabulary for "was the client asked?" ------------------------


async def test_the_sweeps_skip_set_is_the_panels_done_set(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    """R2/R9: the sweep's skip rule and the panel's done-signal are two reads
    of one fact. A mixed seed walks both over every panel state — never asked,
    asked for the arrived period (live and superseded history), asked for
    another year — and pins the two sets identical, before and after a cycle.

    Both reads are pinned to one as_of (mid-January, so the arrived period is
    also a period the panel lists): the sweep through its ``now``, the panel
    through its own ``pending_actions`` read with an Administrator context —
    the same scope the sweep's population read has.
    """
    as_of = date(2026, 1, 15)
    at = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    # A late-December anniversary: the 2025 period's signature posting falls
    # inside the panel's window (so the period is listed), its ask date is
    # before as_of (so it is the arrived period), and CR's reply window for
    # the period is still open (so a generation succeeds).
    panel_anniversary = date(2010, 12, 31)
    arrived_year = 2025

    client = make_client(session, store, actor)
    never_asked = covered_company(client, anniversary=panel_anniversary)
    asked = covered_company(client, anniversary=panel_anniversary)
    superseded_history = covered_company(client, anniversary=panel_anniversary)
    asked_other_year = covered_company(client, anniversary=panel_anniversary)
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(asked),
        target_date=date(arrived_year, 12, 31),
        status=RequestStatus.SENT.value,
    )
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(superseded_history),
        target_date=date(arrived_year, 12, 31),
        status=RequestStatus.SUPERSEDED.value,
    )
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(asked_other_year),
        target_date=date(arrived_year - 1, 12, 31),
        status=RequestStatus.SENT.value,
    )

    async def panel_done_set() -> set[tuple[UUID, int]]:
        context = ActorContext(
            is_active=True,
            role=Role.ADMINISTRATOR,
            permissions=frozenset(),
            company_ids=frozenset(),
        )
        panel = await service_renewals.pending_actions(
            session, tenant_id=actor.tenant_id, context=context, as_of=as_of
        )
        return {
            (period.company_id, period.anniversary_year)
            for period in panel.periods
            for milestone in period.milestones
            if milestone.kind is MilestoneKind.DATA_CONFIRMATION
            and milestone.state is MilestoneState.DONE
        }

    def sweep_skip_set(
        report: confirmation_discovery.DiscoveryCycleReport,
    ) -> set[tuple[UUID, int]]:
        return {
            (outcome.company_id, outcome.year)
            for outcome in report.outcomes
            if outcome.outcome is DiscoveryOutcome.SKIPPED
        }

    # Before the cycle: the panel's done set is exactly the two companies
    # asked for the arrived period — whatever their request's status.
    before = await panel_done_set()
    assert before == {(UUID(asked), arrived_year), (UUID(superseded_history), arrived_year)}

    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=at
    )

    # The sweep's skip set equals the panel read's done set.
    assert sweep_skip_set(report) == before
    # The two unasked companies were generated, not skipped — for the same
    # arrived period the panel lists.
    generated = {
        (outcome.company_id, outcome.year)
        for outcome in report.outcomes
        if outcome.outcome is DiscoveryOutcome.GENERATED
    }
    assert generated == {(UUID(never_asked), arrived_year), (UUID(asked_other_year), arrived_year)}

    # After the cycle, the panel's done set is the skip set plus the periods
    # the cycle generated: one vocabulary, no drift, end to end.
    after = await panel_done_set()
    assert after == before | generated


# --- Concurrency: two overlapping cycles -----------------------------------


# The review database's cleanliness comes from the rollback harness above, but
# a concurrency test needs one session to COMMIT — the other side can only
# pass the lock and prove the serialization once the winner's transaction is
# durable. That session's rows are removed here, after the test's own session
# has closed (declared before ``session`` in each test's signature so this
# teardown runs last).
ALL_TABLES = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)


@pytest.fixture
async def purge_committed_rows() -> AsyncIterator[None]:
    yield
    async with engine.connect() as connection:
        # Fail loudly after a few seconds rather than hang forever: a leaked
        # lock here means a racing session was not closed, and that is the
        # bug the test should report, not mask.
        await connection.execute(text("SET lock_timeout = '5s'"))
        await connection.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        await connection.commit()


async def test_overlapping_cycles_serialize_one_request_per_company(
    purge_committed_rows: None,
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping cycles decide as one: a single request, a single entry.

    Without the per-tenant advisory lock both cycles read "last year asked,
    this year due" (the same facts), both call the trigger, and the loser's
    generation supersedes the winner's just-committed draft — two rows, two
    audit entries, a letter orphaned by the loser. With the lock the second
    cycle blocks before any read and re-derives from the winner's committed
    transaction: this year is now asked, so it skips.
    """
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    year = ANNIVERSARY.year
    await seed_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        target_date=anniversary_in_year(ANNIVERSARY, year - 1),
        status=RequestStatus.SUPERSEDED.value,
    )
    # The racing connections can only see durable rows: commit the fixture
    # transaction itself (a session commit would release only its savepoint),
    # and let the purge fixture remove the rows after the test.
    connection = await session.connection()
    await connection.commit()

    # Cycle A on its own connection: it takes the advisory lock and holds its
    # transaction open across cycle B, the way a slow generation does.
    conn_a = await engine.connect()
    try:
        await conn_a.begin()
        async with AsyncSession(bind=conn_a, expire_on_commit=False) as session_a:
            report_a = await confirmation_discovery.sweep(
                session_a, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
            )
        (outcome_a,) = report_a.outcomes
        assert outcome_a.outcome is DiscoveryOutcome.GENERATED

        # Gate cycle B's trigger call: if B reaches it while A is uncommitted,
        # the interleave is the duplicated generation. Under the lock B is
        # blocked before any read, so the first wait timing out IS the fixed
        # behaviour — and B must never call the trigger at all.
        entered = asyncio.Event()
        original_generate = confirmation.generate

        async def gated_generate(
            s: AsyncSession, st: object, sc: object, **kwargs: object
        ) -> ConfirmationRequest:
            entered.set()
            return await original_generate(s, st, sc, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(confirmation_discovery.confirmation, "generate", gated_generate)

        async def cycle_b() -> tuple[
            confirmation_discovery.DiscoveryCycleReport, AsyncSession, AsyncConnection
        ]:
            conn_b = await engine.connect()
            await conn_b.begin()
            session_b = AsyncSession(bind=conn_b, expire_on_commit=False)
            try:
                report = await confirmation_discovery.sweep(
                    session_b, store, scanning.scanner(), tenant_id=actor.tenant_id, now=NOW
                )
            except BaseException:
                # A failed cycle must not leak its connection — an open
                # transaction here would deadlock the cleanup truncate.
                await session_b.close()
                await conn_b.close()
                raise
            return report, session_b, conn_b

        task_b = asyncio.create_task(cycle_b())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(entered.wait(), timeout=2.0)
        await conn_a.commit()
        report_b, session_b, conn_b = await task_b
    finally:
        # Closing rolls back the open transaction and releases the advisory
        # lock even when an assertion above fails.
        await conn_a.close()

    try:
        # The gate never fired: cycle B never reached the trigger.
        assert not entered.is_set()
        (outcome_b,) = report_b.outcomes
        assert outcome_b.outcome is DiscoveryOutcome.SKIPPED
        assert outcome_b.skip is DiscoverySkip.ALREADY_REQUESTED
        # Exactly one request for the arrived period — the history row plus
        # cycle A's — and one audit entry naming it. Read through cycle B's
        # own session: an unserialized loser commits nothing but still writes
        # its entry inside its transaction, and that is the fact that must
        # not exist.
        rows = [
            row
            for row in (
                await session_b.scalars(
                    select(ConfirmationRequest).where(
                        ConfirmationRequest.company_id == UUID(company_id)
                    )
                )
            ).all()
            if row.target_date.year == year
        ]
        assert len(rows) == 1
        entries = await session_b.scalars(
            select(AuditLog).where(
                AuditLog.action == AuditAction.CONFIRMATION_REQUEST_DRAFTED.value,
                AuditLog.company_id == UUID(company_id),
            )
        )
        assert len(entries.all()) == 1
    finally:
        # Closing rolls back whatever an unserialized loser wrote — session
        # first, then the connection itself: the session was bound to an
        # externally-begun transaction, which only the connection's close
        # releases (and its locks with it).
        await session_b.close()
        await conn_b.close()
