"""DB-backed calendar-feed scenarios (opt-in; needs a migrated PostgreSQL).

These verify the subscription feed against a real database and the real
stack: the credential's lifecycle (created once, stored as a digest,
revoked, replaced), that the plaintext is returned exactly once and is
nowhere else — not in the row, not in the listing, not in the audit chain —
that revocation stops the feed on the next fetch, the §5.1 gate's per-tier
and per-company decisions (Free, a lapsed licence, a package that omits
``calendar_sync``, one company covered among several), tenant isolation
between two tenants' tokens, and that dormant exclusion and the
``not_generated`` refusal behave exactly as the in-app read model does.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the
review database stays clean.
"""

import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.calendar_sync import CalendarFeedCredential
from app.adapters.persistence.company_master import CompanyNameRecord, CompanyStatusRecord
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import calendar_sync, company_master
from app.core.config import settings
from app.domain.calendar_sync import UID_DOMAIN, hash_feed_token
from app.domain.compliance import NAR1_FILING_WINDOW_DAYS, nar1_due_date
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

CREDENTIALS_URL = "/api/v1/calendar/feed-credentials"
FEED_URL = "/api/v1/calendar/feed.ics"
TODAY = date.today()

# The application-level window scenario drives an explicit date; the HTTP
# scenarios use the real clock, since ``GET /calendar/feed.ics`` takes no
# as-of parameter.
WINDOW_AS_OF = date(2026, 6, 1)


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


def anchor_for_due_date(due: date) -> date:
    """An anniversary date whose NAR1 deadline lands on ``due``.

    The rule is "42 days after the anniversary in that year", so the
    anniversary's month/day is the due date's minus the window. The year is
    only a carrier — the read model resolves the month/day per year — and
    2000 is used because it can express February 29.
    """
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
    due_date: date | None = None,
    anniversary: date | None = None,
    type: str = "private",
) -> Company:
    """A company whose next NAR1 deadline is ``due_date`` (or an explicit anchor)."""
    anchor = anniversary if anniversary is not None else anchor_for_due_date(due_date)  # type: ignore[arg-type]
    company = await company_master.create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=incorporation_date(anchor),
        anniversary_date=anchor,
        type=type,
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


def create_feed(client: TestClient) -> dict:
    """Create the tenant's feed credential; returns the 201 body."""
    response = client.post(CREDENTIALS_URL)
    assert response.status_code == 201, response.text
    return response.json()


def token_of(issued: dict) -> str:
    return issued["subscription_url"].split("token=", 1)[1]


def fetch_feed(client: TestClient, token: str):
    return client.get(f"{FEED_URL}?token={token}")


async def audit_actions(session: AsyncSession, tenant: Tenant) -> list[str]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return [row.action for row in rows.all()]


async def stored_credentials(
    session: AsyncSession, tenant: Tenant
) -> list[CalendarFeedCredential]:
    rows = await session.scalars(
        select(CalendarFeedCredential)
        .where(CalendarFeedCredential.tenant_id == tenant.id)
        .order_by(CalendarFeedCredential.created_at)
    )
    return list(rows.all())


def unfold(body: str) -> str:
    """The body with its folds rejoined — UIDs and refusals fold past 75 octets."""
    return body.replace("\r\n ", "")


def answered_companies(body: str) -> set[UUID]:
    """The company ids the feed carries an entry for (read from its UIDs)."""
    pattern = re.compile(r"^UID:nar1_due-([0-9a-fA-F-]{36})-\d+@", re.MULTILINE)
    return {UUID(match) for match in pattern.findall(unfold(body))}


# --- The credential's lifecycle ---


async def test_the_token_is_returned_once_and_stored_only_as_a_digest(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    issued = create_feed(client)
    token = token_of(issued)

    stored = await stored_credentials(session, tenant)
    assert len(stored) == 1
    assert stored[0].token_hash == hash_feed_token(token)
    assert token not in stored[0].token_hash
    assert stored[0].created_by == admin.id
    assert stored[0].revoked_at is None

    listing = client.get(CREDENTIALS_URL)
    assert listing.status_code == 200
    assert token not in listing.text
    assert stored[0].token_hash not in listing.text
    assert set(listing.json()[0]) == {"id", "created_by", "created_at", "revoked_at"}


async def test_one_active_credential_per_tenant_until_it_is_revoked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    first = create_feed(client)
    assert client.post(CREDENTIALS_URL).status_code == 409

    revoked = client.post(f"{CREDENTIALS_URL}/{first['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    second = create_feed(client)
    assert second["id"] != first["id"]
    stored = await stored_credentials(session, tenant)
    assert [row.revoked_at is None for row in stored] == [False, True]


async def test_both_management_operations_are_audited(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    issued = create_feed(client)
    assert await audit_actions(session, tenant) == ["calendar_feed_created"]
    token = token_of(issued)
    # The one-time secret never reaches the chain, which is a read surface.
    entries = await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant.id))
    assert token not in json.dumps([entry.details for entry in entries.all()])
    assert token not in json.dumps([str(entry.entity_id) for entry in entries.all()])

    client.post(f"{CREDENTIALS_URL}/{issued['id']}/revoke")
    assert await audit_actions(session, tenant) == [
        "calendar_feed_created",
        "calendar_feed_revoked",
    ]
    revoked = (await stored_credentials(session, tenant))[0]
    assert revoked.revoked_at is not None

    # Idempotent: a second revoke changes nothing and writes no second event.
    client.post(f"{CREDENTIALS_URL}/{issued['id']}/revoke")
    assert await audit_actions(session, tenant) == [
        "calendar_feed_created",
        "calendar_feed_revoked",
    ]


async def test_revocation_stops_the_feed_immediately(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])
    issued = create_feed(client)
    token = token_of(issued)

    assert fetch_feed(client, token).status_code == 200

    client.post(f"{CREDENTIALS_URL}/{issued['id']}/revoke")

    revoked = fetch_feed(client, token)
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "unknown or revoked feed token"


# --- Gating ---


async def test_an_unlicensed_deployment_is_denied_with_the_gate_reason(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "free")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 403
    assert response.json()["detail"] == f"automation_denied: {GatingReason.TIER_CAPABILITY_MISSING}"


async def test_a_lapsed_licence_stops_the_feed_like_an_unlicensed_one(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    # Standard provisioned, but the licence expired past the 7-day grace.
    provision(monkeypatch, "standard", expires_at=datetime.now(UTC) - timedelta(days=8))
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 403
    assert response.json()["detail"] == f"automation_denied: {GatingReason.TIER_CAPABILITY_MISSING}"


async def test_a_package_that_omits_calendar_sync_denies_the_feed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    company = await make_company(
        session, tenant, name="Acme Limited", due_date=TODAY + timedelta(days=100)
    )
    cover(client, str(company.id), services=[AutomationType.AI_REPLY_PARSING.value])

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 403
    assert response.json()["detail"] == f"automation_denied: {GatingReason.SERVICE_NOT_INCLUDED}"


# --- The served calendar ---


async def test_a_covered_company_is_served_with_its_deadline(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    due = TODAY + timedelta(days=100)
    company = await make_company(session, tenant, name="Acme Limited", due_date=due)
    cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    body = response.text
    expected_due = nar1_due_date(
        company.anniversary_date, (due - timedelta(days=42)).year, company_type=company.type
    )
    assert f"DTSTART;VALUE=DATE:{expected_due:%Y%m%d}" in body
    assert (
        f"UID:nar1_due-{company.id}-{(due - timedelta(days=42)).year}@{UID_DOMAIN}"
        in unfold(body)
    )
    assert "SUMMARY:NAR1 annual return due: Acme Limited" in unfold(body)
    assert body.count("BEGIN:VEVENT") == 1
    assert body.count("BEGIN:VALARM") == 3


async def test_only_covered_companies_are_in_the_feed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    covered = await make_company(
        session, tenant, name="Covered Limited", due_date=TODAY + timedelta(days=100)
    )
    lapsed = await make_company(
        session, tenant, name="Lapsed Limited", due_date=TODAY + timedelta(days=120)
    )
    cover(client, str(covered.id), services=[AutomationType.CALENDAR_SYNC.value])
    cover(
        client,
        str(lapsed.id),
        services=[AutomationType.CALENDAR_SYNC.value],
        end=TODAY - timedelta(days=1),
    )

    body = fetch_feed(client, token_of(create_feed(client))).text

    assert answered_companies(body) == {covered.id}
    assert "Covered Limited" in body
    assert "Lapsed Limited" not in body


async def test_a_tenants_token_never_serves_another_tenants_portfolio(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch, "standard")
    first_tenant, first_admin = await seed_tenant(session)
    second_tenant, second_admin = await seed_tenant(session)
    first_client = make_client(session, ActorRef(user_id=first_admin.id, tenant_id=first_tenant.id))
    second_client = make_client(
        session, ActorRef(user_id=second_admin.id, tenant_id=second_tenant.id)
    )
    first_company = await make_company(
        session, first_tenant, name="Alpha Limited", due_date=TODAY + timedelta(days=100)
    )
    second_company = await make_company(
        session, second_tenant, name="Beta Limited", due_date=TODAY + timedelta(days=100)
    )
    cover(first_client, str(first_company.id), services=[AutomationType.CALENDAR_SYNC.value])
    cover(second_client, str(second_company.id), services=[AutomationType.CALENDAR_SYNC.value])

    first_body = fetch_feed(first_client, token_of(create_feed(first_client))).text
    second_body = fetch_feed(second_client, token_of(create_feed(second_client))).text

    assert answered_companies(first_body) == {first_company.id}
    assert answered_companies(second_body) == {second_company.id}
    assert "Beta Limited" not in first_body
    assert "Alpha Limited" not in second_body


async def test_dormant_and_refused_companies_follow_the_read_model(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    dormant = await make_company(
        session, tenant, name="Dormant Limited", due_date=TODAY + timedelta(days=100)
    )
    public = await make_company(
        session, tenant, name="Public Limited", due_date=TODAY + timedelta(days=100), type="public"
    )
    served = await make_company(
        session, tenant, name="Served Limited", due_date=TODAY + timedelta(days=110)
    )
    await set_status(
        session, tenant, dormant, status="dormant", effective=TODAY - timedelta(days=10)
    )
    for company in (dormant, public, served):
        cover(client, str(company.id), services=[AutomationType.CALENDAR_SYNC.value])

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 200, response.text
    body = response.text
    assert answered_companies(body) == {served.id}
    assert "Dormant Limited" not in body
    assert "Public Limited" not in body
    # The refusal is carried, not hidden and not guessed into a date.
    assert f"X-AICS-NOT-GENERATED:{public.id}:nar1_rule_not_established" in unfold(body)


async def test_the_feed_serves_the_next_obligation_and_only_the_next_year(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Application-level, with an explicit as-of: one entry per company, none
    in the past, none beyond a year."""
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")
    passed = await make_company(
        session, tenant, name="Passed Limited", anniversary=date(2000, 3, 1)
    )
    upcoming = await make_company(
        session, tenant, name="Upcoming Limited", anniversary=date(2000, 9, 1)
    )
    for company in (passed, upcoming):
        cover(
            client,
            str(company.id),
            services=[AutomationType.CALENDAR_SYNC.value],
            start=WINDOW_AS_OF - timedelta(days=30),
            end=WINDOW_AS_OF + timedelta(days=400),
        )
    issued = create_feed(client)

    feed = await calendar_sync.build_feed(
        session, token=token_of(issued), as_of=WINDOW_AS_OF
    )

    assert (feed.window_from, feed.window_to) == (
        WINDOW_AS_OF,
        WINDOW_AS_OF + timedelta(days=365),
    )
    # "Passed" has its 2026 anniversary deadline behind it and the 2027 one
    # ahead; "Upcoming" has 2026 inside the window and 2027 outside it.
    expected = [
        (nar1_due_date(date(2000, 9, 1), 2026, company_type="private"), "2026"),
        (nar1_due_date(date(2000, 3, 1), 2027, company_type="private"), "2027"),
    ]
    assert [
        (event.date, event.uid.split("-")[-1].split("@")[0]) for event in feed.events
    ] == expected
    assert all(event.date >= WINDOW_AS_OF for event in feed.events)
    assert all(event.date <= feed.window_to for event in feed.events)
    assert feed.refusals == ()

    rendered = calendar_sync.render_feed(feed, dtstamp=datetime.now(UTC))
    assert rendered.count("BEGIN:VEVENT") == 2
    assert "Passed Limited" in rendered and "Upcoming Limited" in rendered


async def test_a_tenant_with_no_companies_gets_an_empty_calendar(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to gate and nothing to leak: a valid empty feed, not a refusal."""
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    response = fetch_feed(client, token_of(create_feed(client)))

    assert response.status_code == 200
    assert "BEGIN:VEVENT" not in response.text
    assert response.text.startswith("BEGIN:VCALENDAR\r\n")
