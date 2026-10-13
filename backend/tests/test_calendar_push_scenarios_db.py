"""The push slice's exit-gate walk (opt-in; needs a migrated PostgreSQL).

The slice's acceptance evidence for the API-push half of calendar sync,
driven through the real endpoint stack against the real database and the
real Google and Microsoft adapters — pointed at the Task 2 loopback fakes,
so the wire shape is exercised end to end without a network. One walk, in
the plan's order:

1. **the provider credential is registered through the Administrator API.**
   ``POST /api/v1/calendar/provider-credentials`` stores the credential in
   the ADR-0014 file store and audits the custody: one
   ``calendar_provider_connected`` entry naming the credential's id and
   provider metadata — never the client secret, never the refresh token.
2. **the honest probe goes green over the real route.** ``POST
   /api/v1/calendar/probe`` refreshes the token against the fake, lists the
   target calendar, writes a probe event and deletes it — the four legs all
   pass, the credential's ``probe_passed`` flag flips, and no probe event
   lingers on the fake's calendar.
3. **one push sweep creates one event per covered company.** The app
   exposes no operator-triggered sweep route: the reconciliation loop is
   the trigger (ruling R4), woken by the write seams or its interval, so
   the walk drives the worker's cycle body —
   ``calendar_sync.sweep_tenant_pushes`` — directly, stating so. The fake
   then holds one event per covered company keyed by the obligation UID
   (the ``iCalUID``), and ``calendar_push_entries`` maps each UID to the
   provider event id the upsert returned.
4. **a due-date correction updates the event in place.** A
   ``PATCH /api/v1/companies/{id}/statics`` anniversary correction (the
   seam that also wakes the loop, a no-op while no loop runs) moves the
   due date; the next sweep PATCHes the same provider event — the event id
   the push state holds does not move — with the new content.
5. **a deleted obligation removes its event.** The company goes dormant
   over the real fact route (the family's convention for removing the
   underlying state); the next sweep deletes the provider event once and
   forgets the row, leaving the surviving entry untouched.
6. **a gate-denied company is omitted with the feed's own refusal reason.**
   A company whose package does not cover ``calendar_sync`` is reported in
   the sweep's omissions with ``service_not_included`` — the §5.1 gate's
   own vocabulary, the feed's rule — and nothing is ever written for it:
   no push-state row, no provider event.
7. **the audit chain verifies clean over the whole walk** (the family's
   integrity convention: ``verify_chain`` over every entry the tenant
   accumulated answers no broken links).
8. **an idempotent re-sweep the same day writes nothing**: the projection
   and the stored content hashes agree, so no provider call is made and no
   audit entry lands — the hash discipline, end to end.

A second walk covers the provider split on the Google leg — register,
probe, and sweep-create against ``tests/fake_google_calendar.py`` over the
same routes, so both adapters are walked, not just the Microsoft one.

**The clock.** The walk moves the anchor rather than the wall clock (the
family convention): each company's ``anniversary_date`` is placed so its
NAR1 deadline — the anniversary plus the 42-day filing window — lands
where the walk needs it, and the sweep runs at ``date.today()``.

**The harness** mirrors ``tests/test_calendar_push_application.py`` and
``tests/test_confirmation_discovery_scenarios_db.py`` (the rollback
session, the seeded tenant and Administrator, the provisioned Standard
tier, the route helpers, the anchor arithmetic) — but it is restated here,
not imported, because this family's convention is that no test module
imports another's fixtures or helpers.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the
review database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.calendar.google_client import GoogleCalendarClient
from app.adapters.calendar.microsoft_client import MicrosoftGraphClient
from app.adapters.calendar.probe import (
    CALENDAR_LIST,
    PROBE_DELETE,
    PROBE_WRITE,
    TOKEN_REFRESH,
)
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.calendar_push import CalendarPushEntry
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import calendar_sync
from app.core.config import settings
from app.domain.audit import AuditAction, verify_chain
from app.domain.calendar_push import CalendarProviderName, SyncMode
from app.domain.compliance import NAR1_FILING_WINDOW_DAYS
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app
from tests.fake_google_calendar import FakeGoogleCalendar
from tests.fake_microsoft_graph import FakeMicrosoftGraph

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

TODAY = date.today()

CREDENTIALS_URL = "/api/v1/calendar/provider-credentials"
PROBE_URL = "/api/v1/calendar/probe"

# The walk's obligations: two covered companies and one gate-denied one,
# then the corrected date for the moving company. All inside the feed
# window (a 365-day horizon) on any run date.
STEADY_DUE = TODAY + timedelta(days=100)
MOVING_DUE = TODAY + timedelta(days=120)
DENIED_DUE = TODAY + timedelta(days=130)
CORRECTED_DUE = TODAY + timedelta(days=90)

REFRESH_TOKEN = "the-refresh-token"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture(autouse=True)
def standard_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Standard tier the walk's tenant is provisioned with."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)


@pytest.fixture
async def actor(session: AsyncSession) -> ActorRef:
    """A fresh tenant and its Administrator, the actor every route sees."""
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


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with the auth seam and session overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def wire_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    graph: FakeMicrosoftGraph | None = None,
    google: FakeGoogleCalendar | None = None,
) -> None:
    """Point the app's single adapter-construction seam at the loopback fake.

    The real ``build_provider`` gets a transport argument only in tests, so
    the patch constructs the real adapter with an ASGI transport against
    the fake: every hop the walk makes — the token exchange, the probe's
    four legs, the sweep's writes — is the production client's own code.
    """
    timeout = settings.calendar_request_timeout_seconds

    def build(credential: calendar_sync.ProviderCredential) -> object:
        if credential.provider is CalendarProviderName.MICROSOFT:
            assert graph is not None
            return MicrosoftGraphClient(
                credential, timeout=timeout, transport=httpx.ASGITransport(app=graph.app)
            )
        assert google is not None
        return GoogleCalendarClient(
            credential, timeout=timeout, transport=httpx.ASGITransport(app=google.app)
        )

    monkeypatch.setattr(calendar_sync, "build_provider", build)


# --- Harness -------------------------------------------------------------


def anchor_for_due_date(due: date) -> date:
    """An anniversary date whose NAR1 deadline lands on ``due``."""
    anniversary = due - timedelta(days=NAR1_FILING_WINDOW_DAYS)
    return date(2000, anniversary.month, anniversary.day)


def incorporation_date(anchor: date) -> date:
    """A plausible made-up date carrying the anchor's month/day."""
    leap = (anchor.month, anchor.day) == (2, 29)
    return date(2012 if leap else 2010, anchor.month, anchor.day)


def make_company(client: TestClient, *, due_date: date, name_en: str) -> str:
    """A named company whose next NAR1 deadline is ``due_date``, via the routes."""
    anchor = anchor_for_due_date(due_date)
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": f"{uuid4().int % 10**8:08d}",
            "cr_number": f"{uuid4().int % 10**7:07d}",
            "date_of_incorporation": incorporation_date(anchor).isoformat(),
            "anniversary_date": anchor.isoformat(),
            "type": "private",
        },
    )
    assert response.status_code == 201, response.text
    company_id = str(response.json()["id"])
    named = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2020-01-01", "name": {"name_en": name_en, "name_zh": None}},
    )
    assert named.status_code == 200, named.text
    return company_id


def cover(client: TestClient, company_id: str, *, services: list[str]) -> None:
    """An assignment whose package covers ``services`` through the real endpoints."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": services,
            "covered_forms": ["NAR1"],
        },
    )
    assert package.status_code == 201, package.text
    assignment = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": (TODAY - timedelta(days=30)).isoformat(),
            "coverage_end": (TODAY + timedelta(days=300)).isoformat(),
        },
    )
    assert assignment.status_code == 201, assignment.text


def correct_anniversary(client: TestClient, company_id: str, *, due_date: date) -> None:
    """Move the company's NAR1 deadline to ``due_date`` over the statics route."""
    response = client.patch(
        f"/api/v1/companies/{company_id}/statics",
        json={"anniversary_date": anchor_for_due_date(due_date).isoformat()},
    )
    assert response.status_code == 200, response.text


def mark_dormant(client: TestClient, company_id: str) -> None:
    """The family's way of removing the underlying obligation: dormant today."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": TODAY.isoformat(), "status": {"status": "dormant"}},
    )
    assert response.status_code == 200, response.text


def microsoft_payload(graph: FakeMicrosoftGraph) -> dict[str, str]:
    """The Administrator's paste, matching what the fake grants."""
    return {
        "provider": "microsoft",
        "client_id": graph.client_id,
        "client_secret": graph.client_secret,
        "refresh_token": REFRESH_TOKEN,
        "calendar_id": graph.calendar_id,
        "directory_tenant_id": graph.directory_tenant_id,
    }


def google_payload(google: FakeGoogleCalendar) -> dict[str, str]:
    return {
        "provider": "google",
        "client_id": google.client_id,
        "client_secret": google.client_secret,
        "refresh_token": REFRESH_TOKEN,
        "calendar_id": google.calendar_id,
    }


async def push_rows(session: AsyncSession, tenant_id: UUID) -> list[CalendarPushEntry]:
    rows = await session.scalars(
        select(CalendarPushEntry)
        .where(CalendarPushEntry.tenant_id == tenant_id)
        .order_by(CalendarPushEntry.uid)
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


async def connected_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.CALENDAR_PROVIDER_CONNECTED.value,
        )
    )
    return list(rows.all())


def company_ids_of(uids: tuple[str, ...] | list[str]) -> set[str]:
    """The company ids an obligation UID names (``nar1_due-{id}-{period}@…``)."""
    return {uid.split("@")[0].rsplit("-", 1)[0].removeprefix("nar1_due-") for uid in uids}


def uid_for(company_id: str, rows: list[CalendarPushEntry]) -> str:
    """The obligation UID the push state holds for ``company_id``."""
    return next(row.uid for row in rows if company_id in row.uid)


# --- The walk: credential to push, update, delete, and the idempotent day ---


async def test_the_push_walk_from_credential_to_delete_and_the_idempotent_resweep(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, actor: ActorRef
) -> None:
    graph = FakeMicrosoftGraph()
    wire_provider(monkeypatch, graph=graph)
    client = make_client(session, actor)
    steady = make_company(client, due_date=STEADY_DUE, name_en="Steady Limited")
    moving = make_company(client, due_date=MOVING_DUE, name_en="Moving Limited")
    denied = make_company(client, due_date=DENIED_DUE, name_en="Denied Limited")
    cover(client, steady, services=[AutomationType.CALENDAR_SYNC.value])
    cover(client, moving, services=[AutomationType.CALENDAR_SYNC.value])
    cover(client, denied, services=[AutomationType.AI_REPLY_PARSING.value])
    denied_omission = calendar_sync.PushOmission(
        company_id=UUID(denied), reason=GatingReason.SERVICE_NOT_INCLUDED
    )

    # 1. Register the provider credential through the Administrator API: the
    # file store holds it, the response carries metadata only, and the custody
    # is audited with an id-only payload — the credential's id and provider
    # metadata, never the tokens.
    created = client.post(CREDENTIALS_URL, json=microsoft_payload(graph))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["connected"] is True
    assert body["provider"] == "microsoft"
    assert body["calendar_id"] == graph.calendar_id
    assert body["probe_passed"] is False
    credential_id = UUID(body["credential_id"])
    assert graph.client_secret not in created.text
    assert REFRESH_TOKEN not in created.text
    (entry,) = await connected_entries(session, actor.tenant_id)
    assert entry.entity_id == credential_id
    assert entry.actor_user_id == actor.user_id
    assert entry.details == {"provider": "microsoft", "calendar_id": graph.calendar_id}
    assert graph.marker not in str(entry.details)

    # 2. Probe green over the real route: every leg passes, the flag flips,
    # and the probe leaves nothing on the calendar.
    probed = client.post(PROBE_URL)
    assert probed.status_code == 200, probed.text
    report = probed.json()
    assert report["passed"] is True
    assert [(leg["name"], leg["ok"], leg["detail"]) for leg in report["legs"]] == [
        (TOKEN_REFRESH, True, None),
        (CALENDAR_LIST, True, None),
        (PROBE_WRITE, True, None),
        (PROBE_DELETE, True, None),
    ]
    reread = client.get(CREDENTIALS_URL)
    assert reread.status_code == 200, reread.text
    assert reread.json()["probe_passed"] is True
    assert graph.events == {}
    writes_after_probe = (graph.event_creates, graph.event_updates, graph.event_deletes)

    # 3. One push sweep — the worker's cycle body, driven directly (the app
    # exposes no operator-triggered sweep route): one event per covered
    # company on the fake, keyed by the obligation UID, and the push-state
    # rows map UIDs to the provider event ids.
    sweep = await calendar_sync.sweep_tenant_pushes(
        session, tenant_id=actor.tenant_id, as_of=TODAY
    )
    assert sweep.mode is SyncMode.PUSH
    assert company_ids_of(sweep.created) == {steady, moving}
    assert sweep.updated == () and sweep.unchanged == () and sweep.deleted == ()
    assert sweep.omissions == (denied_omission,)
    assert (graph.event_creates, graph.event_updates, graph.event_deletes) == (
        writes_after_probe[0] + 2,
        writes_after_probe[1],
        writes_after_probe[2],
    )
    assert {stored["iCalUID"] for stored in graph.events.values()} == set(sweep.created)
    rows = await push_rows(session, actor.tenant_id)
    assert {row.uid for row in rows} == set(sweep.created)
    assert {row.provider_event_id for row in rows} == set(graph.events)
    steady_uid = uid_for(steady, rows)
    moving_uid = uid_for(moving, rows)
    steady_event_id = next(row.provider_event_id for row in rows if row.uid == steady_uid)
    moving_event_id = next(row.provider_event_id for row in rows if row.uid == moving_uid)
    # The events carry the obligation's due date and the company's name.
    assert graph.events[steady_event_id]["start"] == {
        "dateTime": f"{STEADY_DUE.isoformat()}T00:00:00Z"
    }
    assert "Steady Limited" in graph.events[steady_event_id]["subject"]
    # 6 (create sweep): the gate-denied company — no row, no event.
    assert all(denied not in row.uid for row in rows)
    assert all(denied not in stored["iCalUID"] for stored in graph.events.values())

    # 4. A due-date correction over the real statics route; the next sweep
    # updates the event in place: the provider event id does not move, the
    # content does (one PATCH on the wire, no create).
    correct_anniversary(client, moving, due_date=CORRECTED_DUE)
    sweep = await calendar_sync.sweep_tenant_pushes(
        session, tenant_id=actor.tenant_id, as_of=TODAY
    )
    assert sweep.created == () and sweep.deleted == ()
    assert sweep.unchanged == (steady_uid,)
    assert sweep.updated == (moving_uid,)
    assert sweep.omissions == (denied_omission,)
    rows = await push_rows(session, actor.tenant_id)
    corrected_row = next(row for row in rows if row.uid == moving_uid)
    assert corrected_row.provider_event_id == moving_event_id
    assert graph.events[moving_event_id]["iCalUID"] == moving_uid
    assert graph.events[moving_event_id]["start"] == {
        "dateTime": f"{CORRECTED_DUE.isoformat()}T00:00:00Z"
    }
    assert graph.event_updates == writes_after_probe[1] + 1
    assert graph.event_creates == writes_after_probe[0] + 2

    # 5. The obligation disappears (the company goes dormant); the next sweep
    # deletes the provider event once and forgets the row; the steady entry
    # is untouched.
    mark_dormant(client, moving)
    sweep = await calendar_sync.sweep_tenant_pushes(
        session, tenant_id=actor.tenant_id, as_of=TODAY
    )
    assert sweep.created == () and sweep.updated == () and sweep.unchanged == (steady_uid,)
    assert sweep.deleted == (moving_uid,)
    assert sweep.omissions == (denied_omission,)
    assert moving_event_id not in graph.events
    assert graph.events[steady_event_id]["start"] == {
        "dateTime": f"{STEADY_DUE.isoformat()}T00:00:00Z"
    }
    assert {row.uid for row in await push_rows(session, actor.tenant_id)} == {steady_uid}

    # 8. An idempotent re-sweep the same day: the projection and the stored
    # hashes agree, so no provider call is made and no audit entry lands.
    writes_before = (
        graph.event_creates,
        graph.event_updates,
        graph.event_deletes,
        len(graph.uid_lookups),
    )
    audit_before = len(await audit_entries(session, actor.tenant_id))
    sweep = await calendar_sync.sweep_tenant_pushes(
        session, tenant_id=actor.tenant_id, as_of=TODAY
    )
    assert sweep.created == () and sweep.updated == () and sweep.deleted == ()
    assert sweep.unchanged == (steady_uid,)
    assert sweep.omissions == (denied_omission,)
    assert (
        graph.event_creates,
        graph.event_updates,
        graph.event_deletes,
        len(graph.uid_lookups),
    ) == writes_before
    assert len(await audit_entries(session, actor.tenant_id)) == audit_before
    assert all(denied not in stored["iCalUID"] for stored in graph.events.values())

    # 7. The audit chain verifies clean over the whole walk.
    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []


# --- The provider split: the Google leg over the same routes ---


async def test_the_google_leg_registers_probes_and_pushes_over_the_real_routes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, actor: ActorRef
) -> None:
    google = FakeGoogleCalendar()
    wire_provider(monkeypatch, google=google)
    client = make_client(session, actor)
    company = make_company(
        client, due_date=STEADY_DUE, name_en="Harbour Bright Limited"
    )
    cover(client, company, services=[AutomationType.CALENDAR_SYNC.value])

    created = client.post(CREDENTIALS_URL, json=google_payload(google))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["provider"] == "google"
    assert body["calendar_id"] == google.calendar_id
    assert body["probe_passed"] is False
    assert google.client_secret not in created.text
    (entry,) = await connected_entries(session, actor.tenant_id)
    assert entry.entity_id == UUID(body["credential_id"])
    assert entry.details == {"provider": "google", "calendar_id": google.calendar_id}
    assert google.marker not in str(entry.details)

    probed = client.post(PROBE_URL)
    assert probed.status_code == 200, probed.text
    report = probed.json()
    assert report["passed"] is True
    assert [leg["ok"] for leg in report["legs"]] == [True, True, True, True]
    assert client.get(CREDENTIALS_URL).json()["probe_passed"] is True
    assert google.events == {}

    sweep = await calendar_sync.sweep_tenant_pushes(
        session, tenant_id=actor.tenant_id, as_of=TODAY
    )
    assert sweep.mode is SyncMode.PUSH
    assert sweep.omissions == ()
    assert company_ids_of(sweep.created) == {company}
    (uid,) = sweep.created
    assert len(google.events) == 1
    (stored,) = google.events.values()
    assert stored["iCalUID"] == uid
    assert stored["start"] == {"date": STEADY_DUE.isoformat()}
    assert "Harbour Bright Limited" in stored["summary"]
    (row,) = await push_rows(session, actor.tenant_id)
    assert row.uid == uid
    assert row.provider_event_id in google.events

    assert verify_chain(await audit_entries(session, actor.tenant_id)) == []
