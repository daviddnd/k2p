"""DB-backed service-renewal scenarios (opt-in; needs a migrated PostgreSQL).

The M3b record end to end against a real database: the period's row and its
unique index, the CHECK that keeps status and timestamps telling one story, the
guarded transitions through the real routes, the locked read, and the §5.1.9
panel read. The two gates are exercised here as well — the continuation gate on
the confirmation trigger (whose own suite is
``test_confirmation_scenarios_db.py``) and the release gate on the download
route — including the assertion the release gate lives or dies by: **a refused
download leaves an entry in the chain, a permitted one leaves none.**

**What the session override does, and what it does not.** The override is a
plain ``yield`` (the other DB suites' shape): it neither commits on a clean
return nor rolls back on an error. What the custody scenario therefore does is
explicit — it commits the requests it makes through the routes and then calls
``session.rollback()`` itself, which takes back any *uncommitted* write and
leaves the route's committed refusal entry standing. That is the property the
route's own commit provides, and removing that commit is what makes this
suite fail. **The deployment's rollback of a refused request is FastAPI's
dependency-teardown behaviour and this suite does not exercise it** — the
framework's own session dependency is replaced here, as in every other DB
suite, so a deployment's rollback is assumed rather than reproduced (the plan
records the same limit).

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.service_renewals import ServiceRenewal
from app.adapters.persistence.session import get_session
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import service_packages, service_renewals
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.milestones import MilestoneKind, default_milestones, milestone_dates
from app.domain.naming_templates import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DEFAULT_GENERATED_TEMPLATE,
)
from app.domain.rbac import ActorContext, Role
from app.domain.service_packages import AutomationType
from app.domain.service_renewals import RenewalStatus
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

# The period this suite works in: the anniversary still ahead of today, so the
# panel's milestones are in front of the read rather than behind it. The
# milestones themselves are derived from the shipped configuration, never
# hard-coded: one run in January is a run in which the client's reply-by is
# already last year.
ANNIVERSARY = date(date.today().year + 1, 3, 1)
PERIOD = ANNIVERSARY.year
MILESTONES = default_milestones()
MILESTONE_DATES = milestone_dates(ANNIVERSARY, PERIOD, milestones=MILESTONES)
COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": ANNIVERSARY.isoformat(),
    "type": "private",
}
MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
COVERAGE = {"start": f"{PERIOD - 1}-01-01", "end": f"{PERIOD}-12-31"}


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
    """A session inside an outer transaction, rolled back after each test.

    ``join_transaction_mode="create_savepoint"`` is not the default, and it is
    here on purpose: the deployment's own session dependency commits at the
    request edge, and the release gate's custody claim is *about* that commit —
    the refusal's audit entry has to outlive the request that wrote it. With a
    session merely joined to the outer transaction, a session-level rollback
    would take back even what a route committed, and "the entry survived" would
    be an assertion that cannot fail. A savepoint makes the same statement true
    in the suite as in a deployment: a commit releases it, a rollback takes back
    what was not committed.
    """
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


# --- Harness -------------------------------------------------------------


def make_client(
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
    context: ActorContext | None = None,
) -> TestClient:
    """The real app over the test session, with the deployment's session rules."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
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


def provision(monkeypatch: pytest.MonkeyPatch, tier: str = "standard") -> None:
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)


def configure_mailbox(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    monkeypatch.setattr(settings, "mail_address", address)


async def seeded_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    drafting: bool = False,
    covered: bool = False,
) -> tuple[TestClient, Tenant, User, str]:
    """A tenant, an administrator, and one company.

    ``drafting=True`` also gives the company a registered name, a registered
    contact, a package covering ``confirmation_generation`` and a configured
    mailbox, so the confirmation trigger can be driven over the same client.
    ``covered=True`` — implied by ``drafting`` — gives it the package and its
    coverage period and nothing else, which is what the release gate's scope
    legs need: the gate reads the assignment and the package, not a mailbox.
    """
    tenant, admin = await seed_tenant_with_admin(session)
    provision(monkeypatch)
    if drafting:
        configure_mailbox(monkeypatch)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post("/api/v1/companies", json=COMPANY)
    assert response.status_code == 201, response.text
    company_id = str(response.json()["id"])
    if drafting:
        hold_sends_for_review(client)
        name_company(client, company_id)
        add_registered_contact(client, company_id)
    if drafting or covered:
        cover_confirmation_generation(client, company_id)
    return client, tenant, admin, company_id


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


def name_company(
    client: TestClient,
    company_id: str,
    *,
    name_en: str = "Bright Lantern Design Limited",
    name_zh: str | None = "明燈設計有限公司",
) -> None:
    """Record a registered name through the effective-dated fact path."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": name_zh}},
    )
    assert response.status_code == 200, response.text


def add_registered_contact(client: TestClient, company_id: str, *, email: str = CONTACT) -> str:
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={"channel_type": "email", "identifier": email},
    )
    assert channel.status_code == 201, channel.text
    associated = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    assert associated.status_code in (200, 201), associated.text
    return person_id


def cover_confirmation_generation(
    client: TestClient,
    company_id: str,
    *,
    services: Sequence[str] = ("confirmation_generation",),
    covered_forms: Sequence[str] = ("NAR1", "ND2A"),
    coverage: dict[str, str] = COVERAGE,
) -> dict:
    """Assign a package covering ``services`` for the work in ``covered_forms``.

    The two lists are the two axes, and the release gate reads them
    differently: ``services`` decides what automation may run (§5.1),
    ``covered_forms`` what work may go out to the client. A caller narrowing
    ``covered_forms`` is how the gate's form leg is exercised.
    """
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": list(services),
            "covered_forms": list(covered_forms),
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


def renewals_url(company_id: str) -> str:
    return f"/api/v1/companies/{company_id}/service-renewals"


def record_period(
    client: TestClient,
    company_id: str,
    *,
    year: int = PERIOD,
    status: str = "pending",
    note: str | None = None,
) -> dict:
    """Create the period's row and walk it to ``status`` through the routes.

    The walks the lifecycle admits, and only those: ``pending`` (created),
    ``confirmed``, ``declined`` (with the reason the API requires), and
    ``payment_received`` (confirmation, then the fee).
    """
    created = client.post(renewals_url(company_id), json={"anniversary_year": year})
    assert created.status_code == 200, created.text
    if status == "pending":
        return created.json()
    decision = client.post(
        f"{renewals_url(company_id)}/{year}/decision",
        json={
            "decision": "declined" if status == "declined" else "confirmed",
            "note": note or ("moving provider" if status == "declined" else "client confirmed"),
        },
    )
    assert decision.status_code == 200, decision.text
    if status != "payment_received":
        return decision.json()
    paid = client.post(f"{renewals_url(company_id)}/{year}/payment", json={})
    assert paid.status_code == 200, paid.text
    return paid.json()


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def stored_renewals(session: AsyncSession) -> list[ServiceRenewal]:
    return list((await session.scalars(select(ServiceRenewal))).all())


async def actions_of(session: AsyncSession, tenant_id: UUID) -> list[str]:
    return [entry.action for entry in await audit_entries(session, tenant_id)]


# The panel is driven directly with an explicit ``as_of`` where a test needs
# exact states: the route passes today's date, and every date-dependent
# expectation would otherwise be a test that passes in September and fails at
# the turn of the year.
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


async def panel(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    as_of: date,
    due_within: int = 365,
    context: ActorContext = ADMIN_CONTEXT,
) -> service_renewals.ClientActions:
    """The §5.1.9 read model as of a fixed day."""
    return await service_renewals.pending_actions(
        session, tenant_id=tenant_id, context=context, as_of=as_of, due_within=due_within
    )


def one_period(view: service_renewals.ClientActions, *, year: int) -> dict[str, str]:
    """One period's milestone states, by kind — the shape a test compares."""
    periods = [period for period in view.periods if period.anniversary_year == year]
    assert len(periods) == 1, [period.anniversary_year for period in view.periods]
    return {item.kind.value: item.state.value for item in periods[0].milestones}


def blocked_reason(
    view: service_renewals.ClientActions, *, year: int, kind: MilestoneKind
) -> str | None:
    """One milestone's blocked reason, or ``None`` — the leg the panel named."""
    periods = [period for period in view.periods if period.anniversary_year == year]
    assert len(periods) == 1, [period.anniversary_year for period in view.periods]
    milestone = next(item for item in periods[0].milestones if item.kind is kind)
    return None if milestone.blocked_reason is None else milestone.blocked_reason.value


async def store_letter(
    session: AsyncSession,
    *,
    tenant: Tenant,
    admin: User,
    company_id: str,
    target_date: date = ANNIVERSARY,
) -> UUID:
    """A stored confirmation letter and its request row, without the generator.

    Used where a scenario needs a client-bound document to exist while the
    period's renewal is in a state the trigger would refuse: the gate reads the
    artifact, and how it got there is the generator's business (whose own suite
    covers it end to end).
    """
    document = Document(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256=uuid4().hex + uuid4().hex,
        storage_key=f"letters/{uuid4()}.eml",
        byte_size=12,
        content_type="message/rfc822",
        scan_status="clean",
        uploaded_by=admin.id,
    )
    session.add(document)
    await session.flush()
    session.add(
        ConfirmationRequest(
            tenant_id=tenant.id,
            company_id=UUID(company_id),
            form_type="nar1",
            target_date=target_date,
            reply_by=target_date - timedelta(days=MILESTONES.client_reply_days_before),
            status="draft",
            version_no=1,
            document_id=document.id,
            checklist=[],
            recipients=[],
            created_by=admin.id,
        )
    )
    await session.flush()
    return document.id


# --- The record ----------------------------------------------------------


async def test_the_periods_row_is_created_once_and_reads_pending(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create-or-return, and the entry the creation leaves.

    A row that exists is returned unchanged — two answers about one period is
    what the unique index exists to prevent — and the creation is a change to
    tenant data, so it is in the chain with ``before: null`` (there was no
    row) and the actor's identity.
    """
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)

    first = record_period(client, company_id)
    second = record_period(client, company_id)

    assert first["id"] == second["id"]
    assert first["status"] == RenewalStatus.PENDING.value
    assert first["client_decided_at"] is None and first["payment_received_at"] is None
    rows = await stored_renewals(session)
    assert len(rows) == 1
    entries = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.SERVICE_RENEWAL_RECORDED.value
    ]
    assert len(entries) == 1
    assert entries[0].details == {"anniversary_year": PERIOD, "before": None, "after": "pending"}
    assert entries[0].actor_user_id == admin.id
    assert entries[0].company_id == UUID(company_id)


async def test_the_whole_walk_records_the_answer_the_fee_and_who_decided(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three writes of one period, and what each leaves behind.

    The statuses move through the guard, the stamps and the accounts land on
    the row, and the chain carries one entry per write with the status it moved
    from and to — including the decline-reason note, which is the record of why
    a period stopped.
    """
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)

    record_period(client, company_id, status="pending")
    confirmed = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/decision",
        json={"decision": "confirmed", "note": "client rang on the 12th"},
    )
    assert confirmed.status_code == 200, confirmed.text
    enough = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/payment", json={"note": "cheque cleared"}
    )

    assert enough.status_code == 200, enough.text
    body = enough.json()
    assert body["status"] == RenewalStatus.PAYMENT_RECEIVED.value
    assert body["decided_by"] == str(admin.id) and body["payment_by"] == str(admin.id)
    assert body["client_decided_at"] is not None and body["payment_received_at"] is not None
    # The payment's own note replaces the row's, and the confirmation's note is
    # still in the chain: the record keeps both answers.
    assert body["note"] == "cheque cleared"

    row = (await stored_renewals(session))[0]
    assert row.status == RenewalStatus.PAYMENT_RECEIVED.value
    entries = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.SERVICE_RENEWAL_RECORDED.value
    ]
    assert [(entry.details["before"], entry.details["after"]) for entry in entries] == [
        (None, "pending"),
        ("pending", "confirmed"),
        ("confirmed", "payment_received"),
    ]
    assert entries[1].details["note"] == "client rang on the 12th"
    assert entries[2].details["note"] == "cheque cleared"
    assert [entry.actor_user_id for entry in entries] == [admin.id, admin.id, admin.id]


async def test_a_second_row_for_the_same_period_cannot_exist(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique index is the "which row is this period's?" guarantee, whatever
    a caller does — including a second row that says something else."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ServiceRenewal(
                    tenant_id=tenant.id,
                    company_id=UUID(company_id),
                    anniversary_year=PERIOD,
                    status="confirmed",
                    client_decided_at=datetime.now(UTC),
                )
            )
            await session.flush()

    assert len(await stored_renewals(session)) == 1


@pytest.mark.parametrize(
    ("status", "decided", "paid"),
    [
        # A paid period with no payment day: the status would be lying about a
        # stamp that does not exist.
        ("payment_received", True, False),
        # A decided period with no day it was decided.
        ("confirmed", False, False),
        ("declined", False, False),
        # An open period carrying a stamp from another state.
        ("pending", True, False),
        # Payment without the client's answer — the ordering the founder asked
        # for, refused by the database rather than by convention.
        ("payment_received", False, True),
    ],
)
async def test_a_status_and_its_timestamps_must_agree(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    decided: bool,
    paid: bool,
) -> None:
    """The CHECK of migration ``0020``: every incoherent pair is uninsertable,
    so a status can never be read against a stamp belonging to another state."""
    _, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ServiceRenewal(
                    tenant_id=tenant.id,
                    company_id=UUID(company_id),
                    anniversary_year=PERIOD,
                    status=status,
                    client_decided_at=datetime.now(UTC) if decided else None,
                    payment_received_at=datetime.now(UTC) if paid else None,
                )
            )
            await session.flush()


async def test_a_cross_tenant_row_is_uninsertable(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The F1 pattern: the company and the deciding account are pinned to the
    row's tenant, so a renewal cannot describe another tenant's company or
    attribute a decision to another tenant's user."""
    _, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    other_tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    other_admin = User(
        tenant_id=other_tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Other",
        role="administrator",
    )
    session.add(other_admin)
    await session.flush()

    # Another tenant's company, in this tenant's row.
    other_company = Company(
        tenant_id=other_tenant.id,
        br_number="99999999",
        date_of_incorporation=date(2011, 6, 1),
        anniversary_date=ANNIVERSARY,
        type="private",
    )
    session.add(other_company)
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ServiceRenewal(
                    tenant_id=tenant.id,
                    company_id=other_company.id,
                    anniversary_year=PERIOD,
                    status="pending",
                )
            )
            await session.flush()

    # This tenant's company, but another tenant's user as the decider.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ServiceRenewal(
                    tenant_id=tenant.id,
                    company_id=UUID(company_id),
                    anniversary_year=PERIOD,
                    status="confirmed",
                    client_decided_at=datetime.now(UTC),
                    decided_by=other_admin.id,
                )
            )
            await session.flush()

    assert await stored_renewals(session) == []
    assert admin.id is not None  # the seeded administrator is the one that works


# --- The guard, through the routes ---------------------------------------


async def test_a_second_decision_is_already_closed_and_changes_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision stands: the refused write leaves the row in the state the
    first answer put it in, and the chain holds no entry for the attempt."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="confirmed")
    before = await audit_entries(session, tenant.id)

    response = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/decision",
        json={"decision": "declined", "note": "changed our mind"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "already_closed"
    assert (await stored_renewals(session))[0].status == RenewalStatus.CONFIRMED.value
    assert await audit_entries(session, tenant.id) == before


async def test_a_payment_before_confirmation_is_refused_and_changes_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The founder's ordering: the fee cannot be recorded before the client's
    answer is, and the refusal is the one that names the remedy."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id)
    before = await audit_entries(session, tenant.id)

    response = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "service_not_confirmed"
    row = (await stored_renewals(session))[0]
    assert row.status == RenewalStatus.PENDING.value
    assert row.payment_received_at is None and row.payment_by is None
    assert await audit_entries(session, tenant.id) == before


async def test_a_second_payment_is_already_closed(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="payment_received")
    before = await audit_entries(session, tenant.id)

    response = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "already_closed"
    assert await audit_entries(session, tenant.id) == before


async def test_the_payment_route_leaves_a_declined_period_alone(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declined period is not a period to pay for, and the refusal says the
    row is not confirmed rather than that it is closed."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="declined")
    before = await audit_entries(session, tenant.id)

    response = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_confirmed"
    assert await audit_entries(session, tenant.id) == before


# --- The correction ------------------------------------------------------


async def test_a_correction_puts_a_wrong_decision_right_and_audits_both_sides(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the route exists: a mis-clicked decision is fixable.

    The answer changes, the stamps move to the correcting act, the reason lands
    in the row's note, and the chain carries what the status moved from and to
    — so a reader sees a correction for what it is rather than a decision that
    silently says something else.
    """
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="confirmed", note="client rang on the 12th")

    corrected = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": "declined", "note": "recorded against the wrong company"},
    )

    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["status"] == RenewalStatus.DECLINED.value
    assert body["decided_by"] == str(admin.id)
    assert body["client_decided_at"] is not None
    assert body["payment_received_at"] is None
    assert body["note"] == "recorded against the wrong company"

    entries = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.SERVICE_RENEWAL_RECORDED.value
    ]
    assert [(entry.details["before"], entry.details["after"]) for entry in entries] == [
        (None, "pending"),
        ("pending", "confirmed"),
        ("confirmed", "declined"),
    ]
    assert entries[-1].details["note"] == "recorded against the wrong company"
    assert entries[-1].actor_user_id == admin.id

    # And back again: a correction is not a one-way door.
    again = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": "confirmed", "note": "the client is in fact continuing"},
    )

    assert again.status_code == 200, again.text
    assert again.json()["status"] == RenewalStatus.CONFIRMED.value


async def test_a_correction_of_a_payment_clears_it_and_the_fee_stays_recordable(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounced-cheque case, end to end.

    A payment recorded by mistake is corrected away: the status goes back to a
    decision and the stamps go with it — the database CHECK would refuse the
    row otherwise, so the correction is not merely allowed to clear them, it
    must. The period is then an ordinary confirmed one, and the fee is recorded
    again when it really arrives.
    """
    client, _, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="payment_received")
    paid = (await stored_renewals(session))[0]
    assert paid.payment_received_at is not None and paid.payment_by is not None

    corrected = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": "confirmed", "note": "the cheque bounced"},
    )

    assert corrected.status_code == 200, corrected.text
    row = (await stored_renewals(session))[0]
    assert row.status == RenewalStatus.CONFIRMED.value
    assert row.payment_received_at is None and row.payment_by is None
    assert row.client_decided_at is not None  # a decision still carries its stamp

    again = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={"note": "cleared"})

    assert again.status_code == 200, again.text
    assert again.json()["status"] == RenewalStatus.PAYMENT_RECEIVED.value
    assert again.json()["payment_received_at"] is not None


async def test_a_correction_of_an_undecided_period_is_refused(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing has been answered, so there is nothing to correct — and the
    refusal says which situation it is, so the remedy (the decision route) is
    the one the caller reaches for rather than "the record is closed"."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id)
    before = await audit_entries(session, tenant.id)

    response = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": "confirmed", "note": "no answer is on file"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "not_decided"
    assert (await stored_renewals(session))[0].status == RenewalStatus.PENDING.value
    assert await audit_entries(session, tenant.id) == before


@pytest.mark.parametrize(
    ("status", "decision"),
    [("confirmed", "confirmed"), ("declined", "declined")],
)
async def test_a_correction_that_changes_nothing_is_refused(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    decision: str,
) -> None:
    """A correction must change the decision: re-recording the answer the row
    already holds would put an entry in an append-only chain claiming a
    correction that corrected nothing."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status=status)
    before = await audit_entries(session, tenant.id)

    response = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": decision, "note": "same answer again"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "decision_unchanged"
    assert (await stored_renewals(session))[0].status == status
    assert await audit_entries(session, tenant.id) == before


async def test_the_correction_reads_the_row_under_a_lock(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write goes through the one executor, the correction included: the
    row is locked before the guard is asked, so two overlapping corrections
    cannot both read the same status and both write."""
    client, _, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id, status="confirmed")
    statements: list[str] = []

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = client.post(
            f"{renewals_url(company_id)}/{PERIOD}/correction",
            json={"decision": "declined", "note": "wrong company"},
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert response.status_code == 200, response.text
    locking = [
        statement
        for statement in statements
        if "service_renewals" in statement and "FOR UPDATE" in statement
    ]
    assert locking, statements


async def test_the_transition_reads_the_row_under_a_lock(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is the whole concurrency story, and it is asserted where it is
    observable: the row's own SELECT carries ``FOR UPDATE``, so two overlapping
    decisions cannot both read ``pending`` and both close the period — the
    loser waits on the lock and is then refused by the guard."""
    client, _, _, company_id = await seeded_deployment(session, store, monkeypatch)
    record_period(client, company_id)

    statements: list[str] = []

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = client.post(
            f"{renewals_url(company_id)}/{PERIOD}/decision", json={"decision": "confirmed"}
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert response.status_code == 200, response.text
    locking = [
        statement
        for statement in statements
        if "service_renewals" in statement and "FOR UPDATE" in statement
    ]
    assert locking, statements


async def test_the_decline_does_not_touch_the_service_assignment(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 2026-09-24: a decline means no renewed coverage and nothing else.

    The assignment is left exactly as it was — ending it is the TCSP's own
    commercial decision, made through the routes that exist for it — and the
    existing gate is what answers for the period: while the coverage holds it
    still allows automation, and once the period ends it answers
    ``out_of_coverage`` without this slice doing anything.
    """
    client, tenant, _, company_id = await seeded_deployment(
        session, store, monkeypatch, drafting=True
    )
    assignment = client.get(f"/api/v1/companies/{company_id}/service-assignments").json()[0]
    record_period(client, company_id, status="declined")

    still_there = client.get(f"/api/v1/companies/{company_id}/service-assignments").json()[0]

    assert still_there == assignment
    assert still_there["status"] == "active"
    allowed = await service_packages.check_automation(
        session,
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        automation_type=AutomationType.CONFIRMATION_GENERATION,
        as_of=date.today(),
    )
    assert allowed.allowed is True  # coverage, not the renewal, is what the gate reads


# --- The panel -----------------------------------------------------------


async def test_the_panel_lists_the_periods_in_the_window_with_their_states(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §5.1.9 read: every milestone of the period, with the state its record
    and its date produce.

    Read five days before the continuation ask is due, so the four states are
    pinned rather than date-dependent: the ask itself is still ``pending``
    (staff's own work, which nothing in the product makes yet), the data
    confirmation is ``pending`` too — nothing gates a draft any more (the
    founder's revision of 2026-09-24), so it reads by its date — the reply waits
    on a letter nobody has generated (``blocked``), and the signature posting is
    held by the release gate, which refuses for the scope legs as much as for
    the payment: this company has no service package at all, so the panel says
    so instead of showing a milestone nobody can complete.
    """
    _, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    as_of = MILESTONE_DATES.continuation_request - timedelta(days=5)

    view = await panel(session, tenant_id=tenant.id, as_of=as_of)

    assert one_period(view, year=PERIOD) == {
        "continuation_request": "pending",
        "data_confirmation": "pending",
        "client_reply": "blocked",
        "signature_post": "blocked",
    }
    # A blocked milestone names the leg that refused — the panel's answer to
    # "why", so staff are not left to guess at the remedy. The posting here is
    # a company with no package at all, and the reply's block is the missing
    # letter rather than a gate leg.
    assert blocked_reason(view, year=PERIOD, kind=MilestoneKind.SIGNATURE_POST) == "no_assignment"
    assert (
        blocked_reason(view, year=PERIOD, kind=MilestoneKind.CLIENT_REPLY)
        == "no_confirmation_request"
    )
    assert blocked_reason(view, year=PERIOD, kind=MilestoneKind.CONTINUATION_REQUEST) is None
    period = next(period for period in view.periods if period.anniversary_year == PERIOD)
    assert period.company_id == UUID(company_id)
    assert period.br_number == COMPANY["br_number"]
    assert period.anniversary_date == ANNIVERSARY
    assert period.renewal_status is None
    expected = [
        (MilestoneKind.CONTINUATION_REQUEST, MILESTONE_DATES.continuation_request),
        (MilestoneKind.DATA_CONFIRMATION, MILESTONE_DATES.data_confirmation),
        (MilestoneKind.CLIENT_REPLY, MILESTONE_DATES.client_reply),
        (MilestoneKind.SIGNATURE_POST, MILESTONE_DATES.signature_post),
    ]
    assert [(item.kind, item.date, item.days_remaining) for item in period.milestones] == [
        (kind, day, (day - as_of).days) for kind, day in expected
    ]
    assert view.as_of == as_of
    assert view.window_from == as_of - timedelta(days=30)
    assert view.window_to == as_of + timedelta(days=365)


async def test_the_panel_reads_the_answer_the_letter_and_the_fee(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What completing a milestone does to the panel: the client's answer makes
    the continuation ``done`` and lets the confirmation be drafted; a generated
    letter makes the data confirmation ``done`` and lets the reply be asked
    for; the fee unblocks the signature posting — which also needs the scope,
    so the company here is assigned to a package that covers the letter's work.
    Until the fee arrives that last one is ``blocked`` — the release gate, on
    the panel, before anyone tries."""
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    as_of = MILESTONE_DATES.continuation_request - timedelta(days=5)
    record_period(client, company_id, status="confirmed")

    assert one_period(await panel(session, tenant_id=tenant.id, as_of=as_of), year=PERIOD) == {
        "continuation_request": "done",
        "data_confirmation": "pending",
        "client_reply": "blocked",
        "signature_post": "blocked",
    }

    await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)

    blocked = await panel(session, tenant_id=tenant.id, as_of=as_of)
    assert one_period(blocked, year=PERIOD) == {
        "continuation_request": "done",
        "data_confirmation": "done",
        "client_reply": "pending",
        "signature_post": "blocked",
    }
    # The scope holds and the client has answered, so the leg that refuses is
    # the fee — the reason staff act on, named rather than implied by the
    # record's status.
    assert blocked_reason(blocked, year=PERIOD, kind=MilestoneKind.SIGNATURE_POST) == (
        "payment_not_received"
    )

    client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})

    released = await panel(session, tenant_id=tenant.id, as_of=as_of)
    assert one_period(released, year=PERIOD) == {
        "continuation_request": "done",
        "data_confirmation": "done",
        "client_reply": "pending",
        "signature_post": "pending",
    }
    assert blocked_reason(released, year=PERIOD, kind=MilestoneKind.SIGNATURE_POST) is None


async def test_the_panel_lists_only_the_periods_inside_its_window(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is real, and the dates decide it: the same company with the
    same (empty) record is listed on the day the period's milestones are in
    front of the read, and not two months after the anniversary, when every one
    of them is behind the 30-day lookback and the next period's are a year
    away. The response says what it looked at, so a caller can see why."""
    _, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)

    inside = await panel(
        session, tenant_id=tenant.id, as_of=MILESTONE_DATES.continuation_request, due_within=1
    )

    assert one_period(inside, year=PERIOD)
    assert inside.window_from == MILESTONE_DATES.continuation_request - timedelta(days=30)
    assert inside.window_to == MILESTONE_DATES.continuation_request + timedelta(days=1)

    after = await panel(
        session, tenant_id=tenant.id, as_of=ANNIVERSARY + timedelta(days=60), due_within=1
    )

    assert after.periods == []
    assert after.window_from == ANNIVERSARY + timedelta(days=30)
    assert after.window_to == ANNIVERSARY + timedelta(days=61)
    assert company_id is not None


async def test_the_panel_is_scoped_to_what_the_actor_may_see(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Least privilege at portfolio scale: a General account sees the periods of
    the companies its grants cover, and an account with no grants sees an empty
    panel rather than someone else's work — and the route scopes identically,
    because the rule lives in the application layer, once
    (``app.application.compliance``)."""
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    as_of = MILESTONE_DATES.continuation_request - timedelta(days=5)

    granted = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(),
        company_ids=frozenset({UUID(company_id)}),
    )
    ungranted = ActorContext(
        is_active=True, role=Role.GENERAL, permissions=frozenset(), company_ids=frozenset()
    )

    seen = await panel(session, tenant_id=tenant.id, as_of=as_of, context=granted)
    assert [period.company_id for period in seen.periods] == [UUID(company_id)]
    empty = await panel(session, tenant_id=tenant.id, as_of=as_of, context=ungranted)
    assert empty.periods == []

    response = client.get("/api/v1/client-actions", params={"due_within": 365})
    assert response.status_code == 200, response.text
    assert [period["company_id"] for period in response.json()["periods"]].count(company_id) == 1
    # The leg that refused travels on the wire, so the panel a staff member
    # reads in the browser can name the remedy the API named.
    posting = next(
        milestone
        for milestone in response.json()["periods"][0]["milestones"]
        if milestone["kind"] == "signature_post"
    )
    assert posting["state"] == "blocked"
    assert posting["blocked_reason"] == "no_assignment"
    assert admin.id is not None


# --- The release gate ----------------------------------------------------


async def test_the_client_bound_letter_is_refused_until_the_fee_is_recorded(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole gate, end to end, on the artifact that exists today.

    A confirmation request is generated — drafting is not gated, and the period
    needs no recorded answer for it — the client's answer is recorded, and then:
    the stored letter cannot be downloaded while the **confirmed** period is
    unpaid (its scope is fine: the assigned package covers NAR1 and its coverage
    holds), the refusal names the period, one ``document_release_refused`` entry
    lands in the chain **and survives the request's own rollback**, and
    recording the fee opens the download with no new entry at all — nothing
    changed on a permitted download (ADR-0009).
    """
    client, tenant, _, company_id = await seeded_deployment(
        session, store, monkeypatch, drafting=True
    )
    tenant_id = tenant.id  # captured: the rollback below expires the ORM object
    generated = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": ANNIVERSARY.isoformat()},
    )
    assert generated.status_code == 201, generated.text
    document_id = generated.json()["document_id"]
    download = f"/api/v1/companies/{company_id}/documents/{document_id}/download"
    # The client said yes: the period is confirmed and unpaid, which is the case
    # the payment refusal exists for.
    record_period(client, company_id, status="confirmed")
    # Each request above is its own transaction at the deployment's edge; the
    # test commits them so that what follows is what a refusal really is — a
    # request whose transaction ends without the rest of its work.
    await session.commit()

    refused = client.get(download)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == f"payment_not_received: {PERIOD}"
    refusals = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action == AuditAction.DOCUMENT_RELEASE_REFUSED.value
    ]
    assert len(refusals) == 1
    # The form in the entry is the registry's code — the same vocabulary the
    # package's covered_forms carries, because that is what the gate compared.
    assert refusals[0].details == {
        "reason": "payment_not_received",
        "anniversary_year": PERIOD,
        "form": "NAR1",
        "kind": "generated_draft",
    }
    assert refusals[0].company_id == UUID(company_id)
    assert refusals[0].entity_id == UUID(document_id)

    # The refusal is a record, and it outlives the request that produced it: the
    # entry is committed before the 409 is answered, so a rollback after the
    # fact cannot take it back. The rollback here stands in for the deployment's
    # own — the session dependency rolls a failed request back at teardown — so
    # this asserts exactly what that commit buys. Remove the commit and this is
    # where it goes: the entry would still be uncommitted and would go with it.
    await session.rollback()
    survived = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action == AuditAction.DOCUMENT_RELEASE_REFUSED.value
    ]
    assert len(survived) == 1

    # The remedy the refusal names: the fee, on the row the client already
    # confirmed (the decision route would refuse a second answer).
    paid = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})
    assert paid.status_code == 200, paid.text
    before = len(await audit_entries(session, tenant_id))

    permitted = client.get(download)

    assert permitted.status_code == 200, permitted.text
    assert permitted.json()["url"].startswith("https://storage.invalid/")
    assert len(await audit_entries(session, tenant_id)) == before


async def test_an_unanswered_period_is_refused_for_its_answer_not_for_money(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy defect this distinction fixes, over the routes.

    A client-bound letter for a period nobody has recorded an answer for — no
    row at all, or a ``pending`` one — answers ``continuation_not_recorded:
    <year>``, never ``payment_not_received``: staff are sent to ask the client
    and record the answer, which is the step that works. Recording the answer
    moves the very same download to the payment refusal, whose remedy — record
    the fee — is then the right one, and every denial is one entry in the
    chain with its own reason.
    """
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)
    download = f"/api/v1/companies/{company_id}/documents/{letter}/download"

    # Nothing on file for the period: the client's answer is what is missing.
    unrecorded = client.get(download)
    assert unrecorded.status_code == 409, unrecorded.text
    assert unrecorded.json()["detail"] == f"continuation_not_recorded: {PERIOD}"

    # The row exists and is `pending` — the client has not answered: the same
    # refusal, because the remedy is the same.
    record_period(client, company_id, status="pending")
    pending = client.get(download)
    assert pending.status_code == 409, pending.text
    assert pending.json()["detail"] == f"continuation_not_recorded: {PERIOD}"

    # The client has now said yes: the missing piece is the fee, and the
    # refusal says that instead.
    confirmed = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/decision", json={"decision": "confirmed"}
    )
    assert confirmed.status_code == 200, confirmed.text
    unpaid = client.get(download)
    assert unpaid.status_code == 409, unpaid.text
    assert unpaid.json()["detail"] == f"payment_not_received: {PERIOD}"

    reasons = [
        entry.details["reason"]
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.DOCUMENT_RELEASE_REFUSED.value
    ]
    assert reasons == [
        "continuation_not_recorded",
        "continuation_not_recorded",
        "payment_not_received",
    ]


async def test_the_decision_is_no_longer_a_gate_on_the_release(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reversal of the previous revision's ruling, at the gate.

    A period the client **declined** and a period with **no renewal row** are
    both refused by the one leg that is left — and never for what the decision
    says: a declined row answers the payment refusal (its remedy runs through
    the correction route first), and the unrecorded period answers the missing
    answer. The decision is a record, and (as the correction route exists to
    fix) a wrongly-recorded decline must not be able to hold a paid client's
    documents back.
    """
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    record_period(client, company_id, status="declined")
    declined_letter = await store_letter(
        session, tenant=tenant, admin=admin, company_id=company_id
    )

    declined = client.get(f"/api/v1/companies/{company_id}/documents/{declined_letter}/download")

    assert declined.status_code == 409, declined.text
    assert declined.json()["detail"] == f"payment_not_received: {PERIOD}"
    assert "declined" not in declined.json()["detail"]

    # A second company, covered, with a letter and no renewal row at all: the
    # refusal names the answer nobody recorded, because that is the remedy —
    # recording a fee nobody asked for would not open this download.
    other = client.post(
        "/api/v1/companies",
        json={**COMPANY, "br_number": "87654321", "anniversary_date": ANNIVERSARY.isoformat()},
    )
    assert other.status_code == 201, other.text
    other_id = str(other.json()["id"])
    cover_confirmation_generation(client, other_id)
    unrecorded_letter = await store_letter(
        session, tenant=tenant, admin=admin, company_id=other_id
    )

    unrecorded = client.get(
        f"/api/v1/companies/{other_id}/documents/{unrecorded_letter}/download"
    )

    assert unrecorded.status_code == 409, unrecorded.text
    assert unrecorded.json()["detail"] == f"continuation_not_recorded: {PERIOD}"

    # The remedy the refusal names is the record's, and it works: correct the
    # decision, record the fee, and the letter goes out.
    corrected = client.post(
        f"{renewals_url(company_id)}/{PERIOD}/correction",
        json={"decision": "confirmed", "note": "client rang to say they stay"},
    )
    assert corrected.status_code == 200, corrected.text
    paid = client.post(f"{renewals_url(company_id)}/{PERIOD}/payment", json={})
    assert paid.status_code == 200, paid.text

    permitted = client.get(f"/api/v1/companies/{company_id}/documents/{declined_letter}/download")

    assert permitted.status_code == 200, permitted.text
    assert (await actions_of(session, tenant.id)).count(
        AuditAction.DOCUMENT_RELEASE_REFUSED.value
    ) == 2


async def test_the_scope_legs_are_named_one_by_one(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every scope refusal, on the wire, with the artifact that exists today.

    Four companies, four reasons: never assigned, assigned then ended, assigned
    outside the coverage period, and assigned to a package that does not cover
    the letter's work. Each is a different remedy for the TCSP, which is why
    the reason is on the wire at all.
    """
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)

    def download_url(document_id: UUID) -> str:
        return f"/api/v1/companies/{company_id}/documents/{document_id}/download"

    # No assignment at all: nothing covers this company.
    unassigned = client.get(download_url(letter))
    assert unassigned.status_code == 409, unassigned.text
    assert unassigned.json()["detail"] == "service_not_in_scope: no_assignment"

    # Assigned, then ended (the coverage window still holds — the status is
    # what refuses).
    assignment = cover_confirmation_generation(client, company_id)
    ended = client.patch(
        f"/api/v1/companies/{company_id}/service-assignments/{assignment['id']}",
        json={"status": "ended"},
    )
    assert ended.status_code == 200, ended.text
    ended_response = client.get(download_url(letter))
    assert ended_response.status_code == 409, ended_response.text
    assert ended_response.json()["detail"] == "service_not_in_scope: assignment_ended"

    # The period's own year is outside the coverage window.
    client.patch(
        f"/api/v1/companies/{company_id}/service-assignments/{assignment['id']}",
        json={"status": "active", "coverage_start": "2019-01-01", "coverage_end": "2019-12-31"},
    )
    lapsed = client.get(download_url(letter))
    assert lapsed.status_code == 409, lapsed.text
    assert lapsed.json()["detail"] == "service_not_in_scope: out_of_coverage"

    # In scope again, but the package does not cover the letter's work.
    client.patch(
        f"/api/v1/companies/{company_id}/service-assignments/{assignment['id']}",
        json={"coverage_start": COVERAGE["start"], "coverage_end": COVERAGE["end"]},
    )
    narrowed = client.patch(
        f"/api/v1/service-packages/{assignment['package_id']}", json={"covered_forms": ["ND2A"]}
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["covered_forms"] == ["ND2A"]

    uncovered = client.get(download_url(letter))
    assert uncovered.status_code == 409, uncovered.text
    assert uncovered.json()["detail"] == "service_not_in_scope: form_not_covered"

    # And the entry records the reason the caller read — the same vocabulary.
    reasons = [
        entry.details["reason"]
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.DOCUMENT_RELEASE_REFUSED.value
    ]
    assert reasons == ["no_assignment", "assignment_ended", "out_of_coverage", "form_not_covered"]


async def test_a_staff_upload_is_never_gated(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 2026-09-24: the gate is the TCSP's own precondition on what goes
    *out to a client*, never on its staff's working copies.

    A file a staff member uploaded — even one labelled ``generated_draft`` — is
    not a client-bound artifact: the product did not produce it for a client,
    no request row points at it, and no renewal row exists here at all. The
    download is permitted, and writes nothing.
    """
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)

    uploaded = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=generated_draft&form=nar1",
        content=b"%PDF-1.4 working copy",
        headers={"content-type": "application/pdf"},
    )
    assert uploaded.status_code == 201, uploaded.text
    before = await audit_entries(session, tenant.id)

    permitted = client.get(
        f"/api/v1/companies/{company_id}/documents/{uploaded.json()['id']}/download"
    )

    assert permitted.status_code == 200, permitted.text
    assert await audit_entries(session, tenant.id) == before


async def test_an_unsafe_document_answers_the_scan_refusal_first(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Availability (ADR-0009) is decided before the release gate: a document
    nobody may be served is not a document the client's commercial state has
    anything to say about, and the two refusals stay distinguishable."""
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)
    document = await session.get(Document, letter)
    assert document is not None
    document.scan_status = "infected"
    await session.flush()

    response = client.get(f"/api/v1/companies/{company_id}/documents/{letter}/download")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "document_not_available: infected"


async def test_a_permitted_download_leaves_no_release_entry(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the custody claim, on its own: a paid period's letter
    downloads (its scope is the covering package), and the chain is
    byte-for-byte what it was — no ``document_release_refused``, and nothing
    else either (ADR-0009 records changes, and a permitted read of a
    non-sensitive document is not one)."""
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    record_period(client, company_id, status="payment_received")
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)
    before = await audit_entries(session, tenant.id)

    response = client.get(f"/api/v1/companies/{company_id}/documents/{letter}/download")

    assert response.status_code == 200, response.text
    assert await audit_entries(session, tenant.id) == before
    assert await actions_of(session, tenant.id) == [
        entry.action for entry in before
    ]  # the chain is the same chain, not merely the same length


async def test_the_gate_reads_the_period_the_letter_belongs_to(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is per period, not per company: a letter for next year's return
    is gated by next year's renewal, whatever this year's row says — and the
    refusal names the missing period, which is the year staff have to record."""
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    record_period(client, company_id, status="payment_received")
    next_period = PERIOD + 1
    letter = await store_letter(
        session,
        tenant=tenant,
        admin=admin,
        company_id=company_id,
        target_date=ANNIVERSARY.replace(year=next_period),
    )

    refused = client.get(f"/api/v1/companies/{company_id}/documents/{letter}/download")

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == f"continuation_not_recorded: {next_period}"


async def test_two_request_rows_on_one_document_gate_rather_than_crash(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classification is a lookup with one answer, and it must never raise.

    Two request rows pointing at one document is not a state the product can
    produce — each row is written with its own stored message — but a
    hand-edited database must not turn a download into a ``500``: a crash is
    neither a permit nor a refusal, and it would skip the gate on exactly the
    document the gate exists for. The read takes the oldest row deterministically
    (here the draft, for this period, whose answer is unrecorded), so the
    impossible state still answers the refusal a caller can act on."""
    client, tenant, admin, company_id = await seeded_deployment(
        session, store, monkeypatch, covered=True
    )
    record_period(client, company_id, status="pending")
    record_period(client, company_id, year=PERIOD + 1, status="payment_received")
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)
    # A second row for the same document, in the state the record keeps closed
    # rows in (the partial unique index allows one *open* draft per company and
    # form, and this row is not one).
    session.add(
        ConfirmationRequest(
            tenant_id=tenant.id,
            company_id=UUID(company_id),
            form_type="nar1",
            target_date=ANNIVERSARY.replace(year=PERIOD + 1),
            reply_by=ANNIVERSARY.replace(year=PERIOD + 1)
            - timedelta(days=MILESTONES.client_reply_days_before),
            status="superseded",
            version_no=2,
            document_id=letter,
            checklist=[],
            recipients=[],
            created_by=admin.id,
        )
    )
    await session.flush()

    response = client.get(f"/api/v1/companies/{company_id}/documents/{letter}/download")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == f"continuation_not_recorded: {PERIOD}"
    assert response.status_code != 500


async def test_generation_waits_for_nothing_on_the_record(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverted ruling 3, over the routes: drafting is not gated.

    A period with no row, a period the client has not answered and a period the
    client declined all produce the same draft — the founder's revision of
    2026-09-24 moved the gate to the moment a document goes *to the client*.
    Nothing is refused, and nothing about the record is consulted: the draft is
    written, stored and audited exactly as for a confirmed period. That a
    declined period is unaffected is the point a reader must not have to infer:
    the remedy for a wrong decision is the correction route, not a block on the
    product's work.
    """
    request_body = {"form_type": "nar1", "target_date": ANNIVERSARY.isoformat()}
    before_documents = len(list((await session.scalars(select(Document))).all()))
    statuses: list[str] = []

    for continuation in (None, "pending", "declined"):
        client, tenant, _, company_id = await seeded_deployment(
            session, store, monkeypatch, drafting=True
        )
        if continuation is not None:
            record_period(client, company_id, status=continuation)
        response = client.post(
            f"/api/v1/companies/{company_id}/confirmation-requests", json=request_body
        )

        assert response.status_code == 201, response.text
        statuses.append(response.json()["status"])
        assert AuditAction.CONFIRMATION_REQUEST_DRAFTED.value in await actions_of(
            session, tenant.id
        )
        refused_actions = await actions_of(session, tenant.id)
        assert AuditAction.DOCUMENT_RELEASE_REFUSED.value not in refused_actions

    assert statuses == ["draft", "draft", "draft"]
    assert len(list((await session.scalars(select(Document))).all())) == before_documents + 3


async def test_a_confirmed_period_drafts_exactly_as_an_unrecorded_one(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the answer buys nothing here: the draft is written whether the period
    is confirmed or silent, because what the payment gates is what is *sent* and
    the decision gates nothing at all."""
    client, _, _, company_id = await seeded_deployment(session, store, monkeypatch, drafting=True)
    record_period(client, company_id, status="confirmed")

    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": ANNIVERSARY.isoformat()},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"
    assert len(list((await session.scalars(select(ConfirmationRequest))).all())) == 1


async def test_the_period_with_no_row_is_drafted_like_any_other(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most common case — nothing recorded for the period — and the one the
    old gate refused. It is drafted, and asking does not create a renewal row:
    the record is written when staff learn something, not when the product
    wonders."""
    client, _, _, company_id = await seeded_deployment(session, store, monkeypatch, drafting=True)
    assert (await session.scalars(select(ServiceRenewal))).all() == []

    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": ANNIVERSARY.isoformat()},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"
    assert (await session.scalars(select(ServiceRenewal))).all() == []


async def test_the_renewal_routes_are_grant_gated_end_to_end(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A General account without the company's grant cannot read or write the
    period: the same rule the master-data routes apply, through the real RBAC
    loader (no context override here)."""
    _, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
    )
    session.add(general)
    await session.flush()
    client = make_client(session, store, ActorRef(user_id=general.id, tenant_id=tenant.id))

    assert client.get(renewals_url(company_id)).status_code == 403
    assert (
        client.post(renewals_url(company_id), json={"anniversary_year": PERIOD}).status_code == 403
    )
    assert admin.id is not None


async def test_a_corrupted_milestone_block_refuses_the_panel_and_the_config_read_repairs_it(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One hand-edited row must not take a read down — and must stay repairable.

    The two reads answer deliberately differently, and this is the pair that
    proves why. The **panel** derives dates from the block, so it cannot be
    computed from one nobody can read: it answers the typed refusal naming what
    failed (``409 tenant_config_invalid: <violations>``, contract §5.1.3). The
    **configuration read** is where the repair is assembled — a full-replace
    ``PUT`` carries the blocks it is assembled from — so it answers with the
    unreadable block's shipped defaults and says which block it could not read
    and why, giving the operator both the diagnosis and the values to put right.
    The repair itself is the documented one: a ``PUT`` validates its own request
    and replaces the blocks it carries without reading the stored row, so one
    request restores the panel.
    """
    client, tenant, _, _ = await seeded_deployment(session, store, monkeypatch)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones={"continuation_request_days_before": "sixty"},
        )
    )
    await session.flush()

    refused = client.get("/api/v1/client-actions")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith("tenant_config_invalid: ")
    assert "milestones." in refused.json()["detail"]

    config = client.get("/api/v1/tenant-config")
    assert config.status_code == 200, config.text
    body = config.json()
    assert body["milestones"] == default_milestones().as_dict()
    assert body["updated_at"] is not None  # the row exists; its timestamp stands
    assert [problem["block"] for problem in body["unreadable_blocks"]] == ["milestones"]
    violations = body["unreadable_blocks"][0]["violations"]
    assert violations and all(violation.startswith("milestones.") for violation in violations)

    repaired = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": body["naming_templates"],
            "milestones": default_milestones().as_dict(),
        },
    )

    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["unreadable_blocks"] == []
    assert client.get("/api/v1/tenant-config").status_code == 200
    assert client.get("/api/v1/client-actions").status_code == 200


async def test_the_panel_shows_the_scope_legs_but_cannot_judge_the_form_leg(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel's projection of the release gate, and its stated limit.

    A company with no package reads ``blocked`` on the signature posting: the
    scope is as much a gate as the payment, and a panel that showed ``due`` for
    work nobody may release would send staff to chase it.

    The one leg it cannot judge is the document's own form — a milestone names
    no form — so a package covering only ND2A reads as releasable on the panel
    while the NAR1 letter is refused at the download. That gap is the price of
    a period-shaped read; it is stated in the domain, the application and the
    contract, and this scenario is where a reader sees it happen.
    """
    client, tenant, admin, company_id = await seeded_deployment(session, store, monkeypatch)
    as_of = MILESTONE_DATES.continuation_request - timedelta(days=5)

    uncovered = await panel(session, tenant_id=tenant.id, as_of=as_of)
    assert one_period(uncovered, year=PERIOD)["signature_post"] == "blocked"
    assert blocked_reason(uncovered, year=PERIOD, kind=MilestoneKind.SIGNATURE_POST) == (
        "no_assignment"
    )

    cover_confirmation_generation(client, company_id, covered_forms=("ND2A",))
    record_period(client, company_id, status="payment_received")
    letter = await store_letter(session, tenant=tenant, admin=admin, company_id=company_id)

    # The panel: the assignment holds, the coverage holds, the fee is in — so
    # the milestone reads by its date.
    assert one_period(await panel(session, tenant_id=tenant.id, as_of=as_of), year=PERIOD) == {
        "continuation_request": "done",
        "data_confirmation": "done",
        "client_reply": "pending",
        "signature_post": "pending",
    }

    # The download: the same period, refused for the work the package omits.
    refused = client.get(f"/api/v1/companies/{company_id}/documents/{letter}/download")

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "service_not_in_scope: form_not_covered"


async def test_the_panel_does_not_write_anything(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is derived: it creates no renewal row for the companies it
    lists, and it appends nothing to the chain. A panel that recorded what it
    showed would be a second source of truth about which periods were
    ``pending``."""
    client, tenant, _, company_id = await seeded_deployment(session, store, monkeypatch)
    as_of = MILESTONE_DATES.continuation_request - timedelta(days=5)
    before = await audit_entries(session, tenant.id)

    view = await panel(session, tenant_id=tenant.id, as_of=as_of)
    route = client.get("/api/v1/client-actions", params={"due_within": 365})

    assert [period.company_id for period in view.periods] == [UUID(company_id)]
    assert route.status_code == 200
    assert await stored_renewals(session) == []
    assert await audit_entries(session, tenant.id) == before
