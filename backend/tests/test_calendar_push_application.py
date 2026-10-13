"""DB-backed calendar-push scenarios (opt-in; needs a migrated PostgreSQL).

These verify the API-push half of one-way calendar sync against a real
database and the real stack: the ADR-0014 credential store (save/read/revoke,
the file at mode 0600), and the push sweep over a mixed portfolio — a covered
company's obligation is created, a corrected due date updates in place, an
obligation that disappears is deleted from the provider and forgotten, a
gate-denied company is omitted with the §5.1 gate's own reason and nothing
written for it, a lapsed gate stops pushes but never deletes the provider
event (ADR-0012), an idempotent re-sweep proposes nothing, and the sweep
flushes but never commits.

The provider is a fake: Task 2 of the slice supplies the real Google and
Microsoft adapters behind ``app.application.calendar_sync.CalendarProvider``,
and no suite may reach a real provider endpoint (the slice's global
constraint 2).

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the
review database stays clean.
"""

import os
import stat
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.calendar.credential_store import CalendarCredentialStore
from app.adapters.persistence.calendar_push import CalendarPushEntry
from app.adapters.persistence.company_master import CompanyNameRecord, CompanyStatusRecord
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import calendar_sync, company_master
from app.core.config import settings
from app.domain.calendar_push import (
    CalendarProviderName,
    ProviderCredential,
    SyncMode,
    content_hash,
)
from app.domain.compliance import NAR1_FILING_WINDOW_DAYS
from app.domain.ics import IcsEvent
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

TODAY = date.today()


class FakeCalendarProvider:
    """The ``CalendarProvider`` protocol's structural stand-in: deterministic
    provider event ids, and a call log the tests assert against."""

    def __init__(self) -> None:
        self.events: dict[str, IcsEvent] = {}
        self.upserted: list[str] = []
        self.deleted: list[str] = []

    async def upsert_event(self, event: IcsEvent) -> str:
        event_id = f"fake-{event.uid}"
        self.events[event_id] = event
        self.upserted.append(event.uid)
        return event_id

    async def delete_event(self, provider_event_id: str) -> None:
        self.deleted.append(provider_event_id)
        self.events.pop(provider_event_id, None)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role="administrator",
    )
    session.add(user)
    await session.flush()
    return tenant, user


def anchor_for_due_date(due: date) -> date:
    """An anniversary date whose NAR1 deadline lands on ``due``."""
    anniversary = due - timedelta(days=NAR1_FILING_WINDOW_DAYS)
    return date(2000, anniversary.month, anniversary.day)


def incorporation_date(anchor: date) -> date:
    """A plausible made-up date carrying the anchor's month/day."""
    leap = (anchor.month, anchor.day) == (2, 29)
    return date(2012 if leap else 2010, anchor.month, anchor.day)


async def make_company(
    session: AsyncSession,
    tenant: Tenant,
    *,
    name: str,
    due_date: date,
) -> Company:
    """A company whose next NAR1 deadline is ``due_date``."""
    anchor = anchor_for_due_date(due_date)
    company = await company_master.create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=incorporation_date(anchor),
        anniversary_date=anchor,
        type="private",
    )
    await company_master.record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2020, 1, 1),
        created_by=None,
        name_en=name,
    )
    return company


async def set_status(
    session: AsyncSession, tenant: Tenant, company: Company, *, status: str, effective: date
) -> None:
    await company_master.record_fact(
        session,
        model=CompanyStatusRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=effective,
        created_by=None,
        status=status,
    )


def provision(monkeypatch: pytest.MonkeyPatch, tier: str, *, expires_at: datetime | None = None):
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


def cover(
    client: TestClient,
    company_id: str,
    *,
    services: list[str],
    start: date = TODAY - timedelta(days=30),
    end: date = TODAY + timedelta(days=300),
) -> None:
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
            "coverage_start": start.isoformat(),
            "coverage_end": end.isoformat(),
        },
    )
    assert assignment.status_code == 201, assignment.text


def connect_calendar(tenant: Tenant, *, probe_passed: bool = True) -> None:
    """Store the tenant's Google provider credential through the real seam."""
    CalendarCredentialStore(settings.calendar_credential_dir).save(
        tenant.id,
        ProviderCredential(
            provider=CalendarProviderName.GOOGLE,
            client_id="google-client",
            client_secret="google-secret",
            refresh_token="google-refresh",
            calendar_id="primary",
            probe_passed=probe_passed,
        ),
    )


def lapse(client: TestClient, company_id: str) -> None:
    """Move the company's active assignment's coverage end into the past."""
    assignments = client.get(f"/api/v1/companies/{company_id}/service-assignments")
    assert assignments.status_code == 200, assignments.text
    active = next(a for a in assignments.json() if a["status"] == "active")
    response = client.patch(
        f"/api/v1/companies/{company_id}/service-assignments/{active['id']}",
        json={"coverage_end": (TODAY - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 200, response.text


def company_ids_of(uids: tuple[str, ...] | list[str]) -> set[str]:
    """The company ids an obligation UID names (``{kind}-{id}-{period}@…``)."""
    return {uid.split("@")[0].rsplit("-", 1)[0].removeprefix("nar1_due-") for uid in uids}


async def push_rows(session: AsyncSession, tenant: Tenant) -> list[CalendarPushEntry]:
    rows = await session.scalars(
        select(CalendarPushEntry)
        .where(CalendarPushEntry.tenant_id == tenant.id)
        .order_by(CalendarPushEntry.uid)
    )
    return list(rows.all())


# --- The credential store (ADR-0014 custody) ---


def test_the_store_round_trips_a_credential_at_0600() -> None:
    store = CalendarCredentialStore(settings.calendar_credential_dir)
    tenant_id = uuid4()
    credential = ProviderCredential(
        provider=CalendarProviderName.MICROSOFT,
        client_id="microsoft-client",
        client_secret="microsoft-secret",
        refresh_token="microsoft-refresh",
        calendar_id="events-calendar",
        directory_tenant_id="microsoft-directory",
        probe_passed=True,
    )

    store.save(tenant_id, credential)

    path = store.path_for(tenant_id)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = store.load(tenant_id)
    assert loaded == credential
    assert loaded is not None and loaded.directory_tenant_id == "microsoft-directory"

    # Revoke removes the file outright; reading it back is "no credential".
    store.clear(tenant_id)
    assert not path.exists()
    assert store.load(tenant_id) is None


def test_a_missing_or_damaged_file_reads_as_no_credential() -> None:
    store = CalendarCredentialStore(settings.calendar_credential_dir)
    tenant_id = uuid4()
    assert store.load(tenant_id) is None

    store.save(
        tenant_id,
        ProviderCredential(
            provider=CalendarProviderName.GOOGLE,
            client_id="c",
            client_secret="s",
            refresh_token="r",
            calendar_id="primary",
        ),
    )
    store.path_for(tenant_id).write_text("not json", encoding="utf-8")
    assert store.load(tenant_id) is None


def test_a_partial_or_shapeless_document_is_not_a_credential() -> None:
    store = CalendarCredentialStore(settings.calendar_credential_dir)
    tenant_id = uuid4()
    store.save(
        tenant_id,
        ProviderCredential(
            provider=CalendarProviderName.GOOGLE,
            client_id="c",
            client_secret="s",
            refresh_token="r",
            calendar_id="primary",
        ),
    )
    path = store.path_for(tenant_id)
    path.write_text('{"provider": "google", "client_id": "only-one-field"}', encoding="utf-8")
    assert store.load(tenant_id) is None


# --- The sweep over a mixed portfolio ---


async def test_without_a_credential_the_sweep_stays_on_the_feed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])
    provider = FakeCalendarProvider()

    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )

    assert report.mode is SyncMode.FEED
    assert report.created == () and report.updated == ()
    assert report.unchanged == () and report.deleted == ()
    assert report.omissions == ()
    assert provider.upserted == [] and provider.deleted == []
    assert await push_rows(session, tenant) == []


async def test_a_credential_whose_probe_never_passed_pushes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])
    connect_calendar(tenant, probe_passed=False)
    provider = FakeCalendarProvider()

    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )

    assert report.mode is SyncMode.PUSH
    assert report.created == () and report.updated == ()
    assert report.unchanged == () and report.deleted == ()
    assert provider.upserted == []
    assert await push_rows(session, tenant) == []


async def test_the_sweep_pushes_a_mixed_portfolio_and_is_idempotent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    provision(monkeypatch, "standard")
    steady = await make_company(
        session, tenant, name="Steady Limited", due_date=TODAY + timedelta(days=100)
    )
    moving = await make_company(
        session, tenant, name="Moving Limited", due_date=TODAY + timedelta(days=120)
    )
    denied = await make_company(
        session, tenant, name="Denied Limited", due_date=TODAY + timedelta(days=130)
    )
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    cover(client, str(steady.id), services=[AutomationType.CALENDAR_SYNC.value])
    cover(client, str(moving.id), services=[AutomationType.CALENDAR_SYNC.value])
    cover(client, str(denied.id), services=[AutomationType.AI_REPLY_PARSING.value])
    connect_calendar(tenant)
    provider = FakeCalendarProvider()

    # First sweep: both covered obligations are created; the gate-denied
    # company is omitted with the gate's own reason and nothing is written
    # for it — no row, no provider event.
    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    assert report.mode is SyncMode.PUSH
    assert report.created == tuple(sorted(report.created))
    assert company_ids_of(report.created) == {str(steady.id), str(moving.id)}
    assert report.updated == () and report.unchanged == () and report.deleted == ()
    assert report.omissions == (
        calendar_sync.PushOmission(company_id=denied.id, reason=GatingReason.SERVICE_NOT_INCLUDED),
    )
    assert sorted(provider.upserted) == sorted(report.created)
    assert "Denied Limited" not in "\n".join(event.summary for event in provider.events.values())

    rows = await push_rows(session, tenant)
    assert {row.uid for row in rows} == set(report.created)
    for row in rows:
        assert row.provider_event_id == f"fake-{row.uid}"
        assert row.content_hash == content_hash(provider.events[row.provider_event_id])
    steady_uid = next(row.uid for row in rows if f"{steady.id}" in row.uid)
    moving_uid = next(row.uid for row in rows if f"{moving.id}" in row.uid)

    # A corrected anniversary moves the due date: the entry updates in place
    # (same UID, same obligation period), the untouched one is reported
    # unchanged. The correction moves the date *earlier*: moving it past
    # today's month/day would roll the anniversary year and legitimately mint
    # a new UID — a new obligation instance, not an update.
    corrected_due = TODAY + timedelta(days=90)
    await company_master.correct_statics(
        session,
        tenant_id=tenant.id,
        company_id=moving.id,
        actor_user_id=None,
        changes={"anniversary_date": anchor_for_due_date(corrected_due)},
    )
    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    assert report.created == () and report.deleted == ()
    assert report.unchanged == (steady_uid,)
    assert report.updated == (moving_uid,)
    assert provider.events[f"fake-{moving_uid}"].date == corrected_due
    assert report.omissions == (
        calendar_sync.PushOmission(company_id=denied.id, reason=GatingReason.SERVICE_NOT_INCLUDED),
    )
    assert {row.uid for row in await push_rows(session, tenant)} == {steady_uid, moving_uid}

    # The obligation disappears (the company goes dormant): the provider event
    # is deleted once and the row forgotten; the other entry persists.
    await set_status(session, tenant, moving, status="dormant", effective=TODAY)
    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    assert report.deleted == (moving_uid,)
    assert provider.deleted == [f"fake-{moving_uid}"]
    assert set(provider.events) == {f"fake-{steady_uid}"}
    assert {row.uid for row in await push_rows(session, tenant)} == {steady_uid}

    # An immediate re-sweep proposes nothing: same projection, same hashes.
    upserts_before = list(provider.upserted)
    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    assert report.created == () and report.updated == ()
    assert report.unchanged == (steady_uid,) and report.deleted == ()
    assert provider.upserted == upserts_before
    assert provider.deleted == [f"fake-{moving_uid}"]


async def test_a_lapsed_gate_stops_pushes_but_never_deletes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Lapsed Limited", due_date=TODAY + timedelta(days=100)
    )
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])
    connect_calendar(tenant)
    provider = FakeCalendarProvider()

    first = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    uid = first.created[0]
    assert set(provider.events) == {f"fake-{uid}"}

    # The coverage ends: the assignment is patched to lapse yesterday (a
    # second active assignment would be refused), the company leaves the
    # covered set, and the gate's reason names why — but the provider event
    # and the row persist (ADR-0012's "already-synced entries persist": a
    # lapsed gate never deletes).
    lapse(client, str(company.id))
    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )
    assert report.created == () and report.updated == () and report.deleted == ()
    assert report.omissions == (
        calendar_sync.PushOmission(company_id=company.id, reason=GatingReason.OUT_OF_COVERAGE),
    )
    assert provider.upserted == [uid] and provider.deleted == []
    assert set(provider.events) == {f"fake-{uid}"}
    assert {row.uid for row in await push_rows(session, tenant)} == {uid}


async def test_the_sweep_flushes_but_never_commits(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])
    connect_calendar(tenant)
    provider = FakeCalendarProvider()

    commits = 0
    original_commit = session.commit

    async def spy_commit() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    monkeypatch.setattr(session, "commit", spy_commit)

    report = await calendar_sync.sweep_pushes(
        session, tenant_id=tenant.id, as_of=TODAY, provider=provider
    )

    assert commits == 0
    # The rows are visible without any autoflush: the sweep flushed them itself.
    with session.no_autoflush:
        rows = (
            await session.scalars(
                select(CalendarPushEntry).where(CalendarPushEntry.tenant_id == tenant.id)
            )
        ).all()
    assert {row.uid for row in rows} == set(report.created)
