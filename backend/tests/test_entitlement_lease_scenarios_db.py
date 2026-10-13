"""DB-backed lease-client scenarios (opt-in; needs a migrated PostgreSQL).

The CP1b slice end to end against a real database and a faithful fake of the
vendor service (``tests/fake_control_plane.py``, no socket): activation
writing a 0600 credential file and caching the first lease; the callback's
refusals; refresh updating the cache and nothing else; a *failing* refresh
leaving the cached lease — and the gate — working; a lease ageing through
``grace`` into ``free`` with no successful refresh at all (the outage story);
a malformed payload refused rather than believed; deactivation returning the
deployment to configuration; and the precedence rule when both sources exist.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (``test_rbac_scenarios_db.py`` has the setup recipe). Every test runs
inside a transaction that is rolled back, so the review database stays clean.
The credential file is per-test (``conftest.py``), so no test can see — or
leave behind — a real credential.
"""

import json
import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.control_plane_client import ControlPlaneClient
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import lease, service_packages
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app
from tests.fake_control_plane import FakeControlPlane, UnreachableTransport

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

CONTROL_PLANE_URL = "http://control-plane.test"
AS_OF = date(2026, 6, 30)
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
ALL_CAPABILITIES = sorted(member.value for member in AutomationType)
ENTITLEMENT_URL = "/api/v1/entitlement"
ACTIVATION_URL = "/api/v1/entitlement/activation"
USER_PAYLOAD = {
    "email": "staff@tcsp.example",
    "display_name": "Staff User",
    "role": "general",
    "password": "staff-password-1",
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- Harness -------------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def provision_configuration(
    monkeypatch: pytest.MonkeyPatch, *, tier: str, expires_at: datetime | None = None
) -> None:
    """Provision the deployment configuration (the pre-activation source)."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


def use_control_plane(monkeypatch: pytest.MonkeyPatch, control_plane: FakeControlPlane) -> None:
    """Point the lease seam at the fake vendor service, in-process."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL,
            timeout=1.0,
            transport=httpx.ASGITransport(app=control_plane.app),
        ),
    )


def use_down_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every call to the vendor service fail like a dead host."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL, timeout=0.1, transport=UnreachableTransport()
        ),
    )


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


async def make_company(session: AsyncSession, tenant: Tenant) -> Company:
    company = Company(
        tenant_id=tenant.id,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2020, 1, 2),
        anniversary_date=date(2026, 1, 2),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


async def cover(
    session: AsyncSession, tenant: Tenant, company_id: UUID, *, services: list[str]
) -> None:
    """A package covering ``services``, assigned to the company in coverage."""
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=services,
        covered_forms=["NAR1", "ND2A"],
    )
    await service_packages.create_assignment(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        package_id=package.id,
        actor_user_id=None,
        coverage_start=COVERAGE_START,
        coverage_end=COVERAGE_END,
    )


async def gate(
    session: AsyncSession, tenant: Tenant, company_id: UUID, automation_type: AutomationType
):
    return await service_packages.check_automation(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        automation_type=automation_type,
        as_of=AS_OF,
    )


async def cached_lease(session: AsyncSession) -> LeaseCache | None:
    row: LeaseCache | None = await session.scalar(select(LeaseCache).limit(1))
    return row


async def audit_actions(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(AuditLog.action).order_by(AuditLog.seq))).all())


async def age_cache(
    session: AsyncSession, *, fetched_ago: timedelta, attempted_ago: timedelta | None = None
) -> None:
    """Backdate the cache so the refresh windows open (no clock is mocked)."""
    row = await cached_lease(session)
    assert row is not None
    now = datetime.now(UTC)
    row.fetched_at = now - fetched_ago if row.fetched_at is not None else None
    row.last_attempt_at = now - (
        attempted_ago if attempted_ago is not None else fetched_ago
    )
    await session.flush()


async def expire_cached_lease(session: AsyncSession, *, expired_ago: timedelta) -> None:
    """Make the cached lease read as expired — the vendor's own lapsed-lease case."""
    row = await cached_lease(session)
    assert row is not None
    row.license_expires_at = datetime.now(UTC) - expired_ago
    await session.flush()


def start_activation(client: TestClient) -> dict[str, object]:
    response = client.post(ACTIVATION_URL)
    assert response.status_code == 201, response.text
    return dict(response.json())


def complete_activation(
    client: TestClient,
    control_plane: FakeControlPlane,
    started: dict[str, object],
    *,
    code: str | None = None,
) -> httpx.Response:
    """Play the browser's return leg with the code the vendor would have issued."""
    issued = code if code is not None else control_plane.complete_handshake(
        UUID(str(started["request_id"]))
    )
    return client.get(
        f"{ACTIVATION_URL}/callback?code={issued}&state=stub-state", follow_redirects=False
    )


def credential_file() -> Path:
    return settings.cp_activation_token_file


def stored_token() -> str:
    return str(json.loads(credential_file().read_text(encoding="utf-8"))["credential"]["token"])


# --- Activation end to end ----------------------------------------------


async def test_activation_caches_the_first_lease_and_writes_a_0600_credential(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="standard", entitlement_version=2)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    started = start_activation(client)
    assert started["authorization_url"] == (
        f"{CONTROL_PLANE_URL}/api/v1/authorize?request_id={started['request_id']}"
    )

    completed = complete_activation(client, control_plane, started)
    assert completed.status_code == 200, completed.text
    assert "Activation complete" in completed.text

    # The credential is in the file, owner-only, and nowhere else.
    assert stat.S_IMODE(credential_file().stat().st_mode) == 0o600
    assert stored_token() in control_plane.tokens
    document = json.loads(credential_file().read_text(encoding="utf-8"))
    assert document["deployment_fingerprint"]

    # The first lease is cached.
    row = await cached_lease(session)
    assert row is not None
    assert row.control_plane_tenant_id == control_plane.tenant_id
    assert (row.tier, row.entitlement_version) == ("standard", 2)
    assert row.license_expires_at is not None
    assert row.fetched_at is not None
    assert row.last_error is None

    # The read path serves the lease, and reports which source it came from.
    view = client.get(ENTITLEMENT_URL).json()
    assert view["source"] == "lease"
    assert view["tier"] == "standard"
    assert view["license_state"] == "active"
    assert view["capabilities"] == ALL_CAPABILITIES
    assert view["seat_cap"] is None  # per subscription, from the lease

    # Neither the credential nor its token reached the database.
    token = stored_token()
    for column in LeaseCache.__table__.columns:
        assert token not in str(getattr(row, column.name))
    entries = list((await session.scalars(select(AuditLog))).all())
    assert entries, "the activation is audited"
    assert token not in json.dumps([entry.details for entry in entries])


async def test_activation_is_audited_against_the_administrator_who_started_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    started = start_activation(client)
    complete_activation(client, control_plane, started)

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.CONTROL_PLANE_ACTIVATED.value)
    )
    assert entry is not None
    assert entry.tenant_id == tenant.id
    assert entry.actor_user_id == admin.id
    assert entry.entity_id == control_plane.tenant_id


async def test_a_first_lease_failure_still_activates_the_deployment(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.lease_body = {"tier": "standard"}  # missing every other field
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    completed = complete_activation(client, control_plane, start_activation(client))

    assert completed.status_code == 200, completed.text
    assert "will be retried automatically" in completed.text
    # The credential is stored — the code was consumed, and it is valid.
    assert stored_token() in control_plane.tokens
    # Fail closed: activated, no usable lease, so Free — not the tier
    # deployment configuration still names (which is standard here).
    provision_configuration(monkeypatch, tier="standard")
    view = client.get(ENTITLEMENT_URL).json()
    assert view["source"] == "lease"
    assert view["tier"] == "free"
    assert view["capabilities"] == []
    row = await cached_lease(session)
    assert row is not None
    assert row.tier is None
    assert row.last_error == "invalid_payload:missing_field:license_expires_at"


# --- The callback's refusals --------------------------------------------


async def test_the_callback_refuses_without_a_pending_attempt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_control_plane(monkeypatch, FakeControlPlane())
    client = make_client(session)

    response = client.get(f"{ACTIVATION_URL}/callback?code=made-up&state=stub")

    assert response.status_code == 400
    assert "no_pending_activation" in response.text
    assert not credential_file().exists()
    assert await cached_lease(session) is None


async def test_the_callback_refuses_an_expired_attempt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(request_ttl_seconds=-1)  # already past at registration
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    started = start_activation(client)
    response = complete_activation(client, control_plane, started)

    assert response.status_code == 400
    assert "activation_expired" in response.text
    assert control_plane.token_exchanges == 0
    # Nothing was stored, and the dead attempt is gone so a restart is clean.
    document = json.loads(credential_file().read_text(encoding="utf-8"))
    assert "credential" not in document
    assert "pending" not in document
    assert await cached_lease(session) is None


async def test_the_callback_refuses_a_replayed_code(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    started = start_activation(client)
    code = control_plane.complete_handshake(UUID(str(started["request_id"])))
    assert complete_activation(client, control_plane, started, code=code).status_code == 200
    first_token = stored_token()

    replayed = complete_activation(client, control_plane, started, code=code)

    assert replayed.status_code == 400
    assert "no_pending_activation" in replayed.text
    assert stored_token() == first_token  # the credential in force is untouched


# --- Refresh: what it updates, what it must not ---------------------------


async def test_a_due_refresh_updates_the_cache_and_bumps_nothing_else(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="standard", entitlement_version=1)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))
    audits_before = await audit_actions(session)

    # The vendor moves the tenant up a tier; a day passes.
    control_plane.tier = "enterprise_offline"
    control_plane.entitlement_version = 2
    control_plane.company_cap = 250
    await age_cache(session, fetched_ago=timedelta(hours=25))

    view = client.get(ENTITLEMENT_URL).json()

    assert control_plane.lease_fetches == 2
    assert view["tier"] == "enterprise_offline"
    assert view["company_cap"] == 250
    assert view["entitlement_version"] == 2
    row = await cached_lease(session)
    assert row is not None and row.last_error is None
    # A refresh is licence bookkeeping, not a change to the tenant's data:
    # no audit entry, and only the one cache row.
    assert await audit_actions(session) == audits_before
    assert len((await session.scalars(select(LeaseCache))).all()) == 1


async def test_a_read_inside_the_backoff_does_not_call_the_control_plane(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))
    assert control_plane.lease_fetches == 1

    # The lease is old, but an attempt was made a minute ago: the backoff
    # window holds the next one off, so a burst of reads costs one call.
    await age_cache(
        session, fetched_ago=timedelta(days=30), attempted_ago=timedelta(minutes=1)
    )
    for _ in range(3):
        assert client.get(ENTITLEMENT_URL).status_code == 200

    assert control_plane.lease_fetches == 1


async def test_a_failing_refresh_keeps_the_cached_lease_and_the_gate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    company = await make_company(session, tenant)
    await cover(session, tenant, company.id, services=[AutomationType.DOCUMENT_EXTRACTION.value])
    control_plane = FakeControlPlane(tier="standard")
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    # The control plane goes away, and the lease ages past the interval.
    use_down_control_plane(monkeypatch)
    await age_cache(session, fetched_ago=timedelta(hours=25))
    row = await cached_lease(session)
    assert row is not None
    fetched_at = row.fetched_at
    view = client.get(ENTITLEMENT_URL).json()

    # The read still answers, from the cached lease.
    assert view["tier"] == "standard"
    assert view["license_state"] == "active"
    assert view["capabilities"] == ALL_CAPABILITIES
    row = await cached_lease(session)
    assert row is not None
    assert row.fetched_at == fetched_at  # a failed attempt does not move it
    assert row.last_error == "control_plane_unreachable"
    assert row.last_attempt_at is not None
    # Gated automation still works: the licence has not lapsed.
    decision = await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    assert decision.allowed is True


async def test_the_lease_ages_through_grace_into_free_with_no_successful_refresh(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    company = await make_company(session, tenant)
    await cover(session, tenant, company.id, services=[AutomationType.DOCUMENT_EXTRACTION.value])
    control_plane = FakeControlPlane(tier="standard")
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    # The vendor is unreachable from here on: no refresh ever succeeds again.
    use_down_control_plane(monkeypatch)

    # Inside the 7-day grace window the tier keeps its capabilities.
    await expire_cached_lease(session, expired_ago=timedelta(days=3))
    await age_cache(session, fetched_ago=timedelta(days=33))
    in_grace = client.get(ENTITLEMENT_URL).json()
    assert in_grace["tier"] == "standard"
    assert in_grace["license_state"] == "grace"
    assert in_grace["capabilities"] == ALL_CAPABILITIES
    assert (
        await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    ).allowed is True

    # Past it, the same cached lease degrades the deployment to Free.
    await expire_cached_lease(session, expired_ago=timedelta(days=8))
    await age_cache(session, fetched_ago=timedelta(days=38))
    degraded = client.get(ENTITLEMENT_URL).json()
    assert degraded["tier"] == "standard"  # the leased tier is still on record
    assert degraded["license_state"] == "free"
    assert degraded["capabilities"] == []
    assert degraded["source"] == "lease"
    decision = await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    assert decision.allowed is False
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING

    # The ungated safety line never depended on any of this.
    assert client.get("/api/v1/companies").status_code == 200


async def test_a_malformed_refresh_leaves_the_cached_lease_in_force(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="standard", entitlement_version=1)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    control_plane.lease_body = {"tier": "premium"}  # a tier nobody defines
    control_plane.entitlement_version = 2
    await age_cache(session, fetched_ago=timedelta(hours=25))

    view = client.get(ENTITLEMENT_URL).json()
    assert view["tier"] == "standard"  # the cached lease stands
    assert view["entitlement_version"] == 1
    row = await cached_lease(session)
    assert row is not None
    assert row.last_error == "invalid_payload:unknown_tier"

    # The explicit refresh reports the same refusal without changing anything.
    report = client.post(f"{ENTITLEMENT_URL}/refresh").json()
    assert report["refreshed"] is False
    assert report["reason"] == "invalid_payload:unknown_tier"
    assert report["tier"] == "standard"


async def test_an_older_lease_version_does_not_overwrite_a_newer_cache(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="standard", entitlement_version=5)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    # §2.2 gives the version one job: telling a stale lease from a fresh one.
    control_plane.tier = "free"
    control_plane.entitlement_version = 4
    await age_cache(session, fetched_ago=timedelta(hours=25))

    view = client.get(ENTITLEMENT_URL).json()

    assert view["tier"] == "standard"
    assert view["entitlement_version"] == 5
    row = await cached_lease(session)
    assert row is not None
    assert row.last_error == "stale_entitlement_version"


# --- Precedence between the two sources ----------------------------------


async def test_configuration_is_the_source_until_the_deployment_activates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    provision_configuration(monkeypatch, tier="standard")
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    view = client.get(ENTITLEMENT_URL).json()

    assert view["source"] == "configuration"
    assert view["tier"] == "standard"
    assert view["capabilities"] == ALL_CAPABILITIES
    assert await cached_lease(session) is None  # nothing was fetched, nothing stored


async def test_the_lease_wins_over_configuration_in_both_directions(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    # Configuration says standard; the lease says Free. The lease wins.
    provision_configuration(monkeypatch, tier="standard")
    control_plane = FakeControlPlane(tier="free")
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    leased = client.get(ENTITLEMENT_URL).json()
    assert leased["source"] == "lease"
    assert leased["tier"] == "free"
    assert leased["capabilities"] == []
    assert leased["seat_cap"] == 2  # the Free caps the lease tier carries

    # Configuration says Free; the lease says Standard. The lease still wins.
    provision_configuration(monkeypatch, tier="free")
    control_plane.tier = "standard"
    control_plane.entitlement_version = 2
    await age_cache(session, fetched_ago=timedelta(hours=25))

    refreshed = client.get(ENTITLEMENT_URL).json()
    assert refreshed["source"] == "lease"
    assert refreshed["tier"] == "standard"
    assert refreshed["capabilities"] == ALL_CAPABILITIES


# --- Deactivation --------------------------------------------------------


async def test_deactivation_returns_the_deployment_to_configuration(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    provision_configuration(monkeypatch, tier="standard")
    control_plane = FakeControlPlane(tier="free")
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))
    fingerprint = json.loads(credential_file().read_text(encoding="utf-8"))[
        "deployment_fingerprint"
    ]

    deactivated = client.delete(ACTIVATION_URL)

    assert deactivated.status_code == 200, deactivated.text
    body = deactivated.json()
    assert body["activated"] is False
    assert body["source"] == "configuration"
    assert body["control_plane_tenant_id"] is None
    assert body["cache"] is None
    # The credential is forgotten; the deployment id survives, so
    # re-activating lands on the same vendor tenant.
    document = json.loads(credential_file().read_text(encoding="utf-8"))
    assert "credential" not in document
    assert document["deployment_fingerprint"] == fingerprint
    assert await cached_lease(session) is None

    # Back on the configuration source.
    view = client.get(ENTITLEMENT_URL).json()
    assert view["source"] == "configuration"
    assert view["tier"] == "standard"
    assert (
        AuditAction.CONTROL_PLANE_DEACTIVATED.value in await audit_actions(session)
    ) is True


async def test_deactivation_is_idempotent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    assert client.delete(ACTIVATION_URL).status_code == 200
    second = client.delete(ACTIVATION_URL)

    assert second.status_code == 200
    assert second.json()["activated"] is False
    # One state change, one audit entry: the second call changed nothing.
    assert (await audit_actions(session)).count(
        AuditAction.CONTROL_PLANE_DEACTIVATED.value
    ) == 1


# --- Caps and the gating legs follow the lease ---------------------------


async def test_the_leased_tier_drives_the_creation_caps(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    provision_configuration(monkeypatch, tier="standard")  # configuration is permissive
    control_plane = FakeControlPlane(tier="free")  # the lease is not
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    # The bootstrap Administrator takes the first of the Free tier's two
    # seats (the lease's caps, not the permissive configuration): the second
    # account is allowed, the third is refused.
    second = client.post("/api/v1/users", json={**USER_PAYLOAD, "email": "second@tcsp.example"})
    assert second.status_code == 201, second.text
    refused = client.post("/api/v1/users", json=USER_PAYLOAD)

    assert refused.status_code == 409, refused.text
    assert "seat_cap_exceeded" in refused.json()["detail"]


# --- The explicit refresh endpoint --------------------------------------


async def test_the_explicit_refresh_reports_when_there_is_nothing_to_refresh(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    provision_configuration(monkeypatch, tier="standard")
    use_down_control_plane(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = client.post(f"{ENTITLEMENT_URL}/refresh").json()

    assert report["refreshed"] is False
    assert report["reason"] == "not_activated"
    assert report["source"] == "configuration"
    assert report["tier"] == "standard"


async def test_the_explicit_refresh_ignores_the_schedule(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="standard", entitlement_version=1)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    complete_activation(client, control_plane, start_activation(client))

    # No ageing at all: the schedule would say "not due", the endpoint says now.
    control_plane.entitlement_version = 2
    report = client.post(f"{ENTITLEMENT_URL}/refresh").json()

    assert report["refreshed"] is True
    assert report["reason"] is None
    assert report["entitlement_version"] == 2
    assert control_plane.lease_fetches == 2


# --- The status endpoint -------------------------------------------------


async def test_status_reports_the_pending_attempt_then_the_cached_lease(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="enterprise_offline", entitlement_version=3)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    started = start_activation(client)
    pending = client.get(ACTIVATION_URL).json()
    assert pending["activated"] is False
    assert pending["source"] == "configuration"
    assert pending["pending_expires_at"] is not None
    assert pending["authorization_url"] == started["authorization_url"]
    assert pending["cache"] is None

    complete_activation(client, control_plane, started)
    activated = client.get(ACTIVATION_URL).json()

    assert activated["activated"] is True
    assert activated["source"] == "lease"
    assert activated["control_plane_tenant_id"] == str(control_plane.tenant_id)
    assert activated["token_expires_at"] is not None
    assert activated["authorization_url"] is None  # the attempt is consumed
    assert activated["pending_expires_at"] is None
    assert activated["cache"]["tier"] == "enterprise_offline"
    assert activated["cache"]["entitlement_version"] == 3
    assert activated["cache"]["fetched_at"] is not None
    assert activated["cache"]["last_error"] is None
