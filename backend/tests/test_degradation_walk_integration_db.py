"""The Standard → Free degradation walk with packs, across a real disconnect (opt-in).

Phase 2's exit gate carried one item: Standard → Free degradation across a
**real control-plane disconnect**, not a local lease expiry, with packs in the
picture (``.agents/plans/2026-09-21-phase-2-visibility-and-control-plane.md``).
Each leg is already proven in isolation — the CP1b outage walk
(``test_entitlement_lease_scenarios_db.py``), the §4.3 pack freeze
(``test_pack_cache_scenarios_db.py``), delivery of a licensed release across
the two deployables (``test_pack_delivery_integration_db.py``). This is the
composition: one deployment, one continuous walk, the real vendor service on
the other side of it.

In order:

1. the deployment is **activated against the real control plane** (the vendor
   app served by a subprocess on loopback, the shape the other
   cross-deployable suites use), marked ``standard`` by the vendor's back
   office, and installs a licensed ``form_mapping`` release through the real
   refresh → verify → install path — so the digest the vendor computed is the
   digest this deployment verified;
2. the control plane becomes **unreachable**: the client seam is swapped for
   a transport that refuses to connect (the CP1b pattern), so the outage
   touches nothing at all, not even loopback;
3. the cached lease keeps answering — the refresh fails without raising, the
   entitlement read still serves ``standard`` and its capabilities, and
   gated automation still passes the §5.1 gate — then ages through ``grace``
   into ``free`` (§4.3) as the paid period is backdated past the 7-day window;
4. on ``free`` the pack cache **freezes**: refresh is refused with the gate's
   own ``license_state_free``, the cached release is retained and still
   hashes to its digest, and the ungated paths (the portfolio read, master
   data, the documents list) never notice;
5. the control plane returns; an explicit refresh recovers the lease and
   unfreezes the cache, and the next pack refresh installs the version the
   vendor published while the deployment was frozen.

What this deliberately does not do: re-assert each leg's detail (the per-leg
suites own those), touch the network beyond loopback, or age the lease by
waiting — the cached expiry is backdated, the mechanism the lease and pack
suites already use.

Skipped unless both databases are reachable and migrated: the deployment's is
named by ``AICS_TEST_DATABASE_URL`` and the vendor's derived from it
(``tests/control_plane_integration.py``). The deployment's rows live in a
transaction that is rolled back; the vendor service commits for real (it is a
separate process), so the vendor rows this run created are removed again at
the end.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
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
from app.adapters.persistence.packs import PackCache
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import lease, service_packages
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app
from tests.control_plane_integration import redeem_activation_code
from tests.fake_control_plane import UnreachableTransport
from tests.vendor_harness import VendorService, remove_vendor_rows, vendor_headers

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ACTIVATION_URL = "/api/v1/entitlement/activation"
ENTITLEMENT_URL = "/api/v1/entitlement"
PACKS_URL = "/api/v1/packs"
PACKS_REFRESH_URL = "/api/v1/packs/refresh"
# The transport is what goes dead, so this URL is never dialled during the
# outage; it only has to be a URL the client will accept.
CONTROL_PLANE_URL = "http://control-plane.test"
# The one credential this test hands the vendor service — through the
# environment, so no secret is printed or parsed out of a subprocess.
VENDOR_ADMIN_KEY = "degradation-walk-vendor-key"

# The §5.1 gate is asked about one automation as of a fixed date inside the
# package's coverage (the lease suite's own constants).
AS_OF = date(2026, 6, 30)
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
ALL_CAPABILITIES = sorted(member.value for member in AutomationType)

# Two releases of one pack type: the one installed before the outage, and the
# one the vendor publishes while the deployment is frozen. Small on purpose —
# the walk is about the licence lifecycle, and the delivery suite already
# exercises a real registry payload byte for byte.
FORM_MAPPING_V1: dict[str, Any] = {
    "forms": {"nar1": {"manifest": {"form_code": "NAR1", "specification_no": "2/2025"}}}
}
FORM_MAPPING_V2: dict[str, Any] = {
    "forms": {"nar1": {"manifest": {"form_code": "NAR1", "specification_no": "3/2025"}}}
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after the test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- The deployment -------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def point_at_vendor(monkeypatch: pytest.MonkeyPatch, vendor: VendorService) -> None:
    """Point the deployment's control-plane calls at the real vendor service."""
    monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
    monkeypatch.setattr(
        lease,
        "client",
        lambda: ControlPlaneClient(base_url=vendor.base_url, timeout=5.0),
    )


def set_tier(vendor: VendorService, vendor_tenant_id: str, tier: str) -> None:
    """Mark the tenant paid (or not) through the vendor's back office."""
    with httpx.Client(timeout=10.0) as http:
        response = http.put(
            f"{vendor.base_url}/api/v1/tenants/{vendor_tenant_id}/entitlement",
            json={"tier": tier},
            headers=vendor_headers(vendor),
        )
    assert response.status_code == 200, response.text


def publish_form_mapping(vendor: VendorService, payload: dict[str, Any]) -> str:
    """Publish one release through the vendor's own back office; returns its digest."""
    with httpx.Client(timeout=10.0) as http:
        response = http.post(
            f"{vendor.base_url}/api/v1/packs",
            json={"pack_type": "form_mapping", "payload": payload, "published_by": "vendor_ops"},
            headers=vendor_headers(vendor),
        )
    assert response.status_code == 201, response.text
    return str(response.json()["sha256"])


def use_down_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every call to the vendor service fail like a dead host.

    The CP1b pattern: the seam is swapped for a client whose transport refuses
    to connect, so the outage is staged without a socket and without touching
    the vendor process that is still running.
    """
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


def activate_deployment(client: TestClient, vendor: VendorService) -> str:
    """Run the real activation handshake; returns the vendor tenant id."""
    started = client.post(ACTIVATION_URL)
    assert started.status_code == 201, started.text
    authorization_url = started.json()["authorization_url"]
    assert authorization_url.startswith(f"{vendor.base_url}/api/v1/authorize")

    with httpx.Client(follow_redirects=False, timeout=10.0) as browser:
        to_google = browser.get(authorization_url)
        assert to_google.status_code == 302, to_google.text
        google = browser.get(to_google.headers["location"])
        assert google.status_code == 302, google.text
        back_to_deployment = browser.get(google.headers["location"])
        assert back_to_deployment.status_code == 302, back_to_deployment.text
        query = parse_qs(urlsplit(back_to_deployment.headers["location"]).query)
        code, state = query["code"][0], query["state"][0]

    redeem_activation_code(client, code, state)
    status = client.get(ACTIVATION_URL).json()
    assert status["activated"] is True
    return str(status["control_plane_tenant_id"])


# --- The clock the walk needs --------------------------------------------


async def cached_lease(session: AsyncSession) -> LeaseCache:
    row = await session.scalar(select(LeaseCache).limit(1))
    assert row is not None
    return row


async def age_lease(session: AsyncSession, *, fetched_ago: timedelta) -> None:
    """Backdate the cache so the refresh windows open (no clock is mocked)."""
    row = await cached_lease(session)
    row.fetched_at = datetime.now(UTC) - fetched_ago
    row.last_attempt_at = datetime.now(UTC) - fetched_ago
    await session.flush()


async def expire_lease(session: AsyncSession, *, days_ago: int) -> None:
    """Move the cached paid-period end into (or past) the §4.3 grace window."""
    row = await cached_lease(session)
    row.license_expires_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()


async def gate(session: AsyncSession, tenant: Tenant, company_id: UUID):
    return await service_packages.check_automation(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        automation_type=AutomationType.DOCUMENT_EXTRACTION,
        as_of=AS_OF,
    )


async def audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.seq))
    return list(rows.all())


def outcomes(response: httpx.Response) -> dict[str, dict[str, Any]]:
    return {outcome["pack_type"]: outcome for outcome in response.json()["outcomes"]}


# --- The walk -------------------------------------------------------------


async def test_standard_degrades_to_free_across_a_disconnect_and_recovers_with_packs(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            company = await make_company(session, tenant)
            await cover(
                session, tenant, company.id, services=[AutomationType.DOCUMENT_EXTRACTION.value]
            )
            point_at_vendor(monkeypatch, vendor)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

            # 1. Standard, activated for real, with a licensed pack installed.

            vendor_tenant_id = activate_deployment(client, vendor)
            set_tier(vendor, vendor_tenant_id, "standard")
            licensed = client.post(f"{ENTITLEMENT_URL}/refresh").json()
            assert (licensed["tier"], licensed["license_state"]) == ("standard", "active")
            assert licensed["capabilities"] == ALL_CAPABILITIES

            digest_v1 = publish_form_mapping(vendor, FORM_MAPPING_V1)
            installed = client.post(PACKS_REFRESH_URL)
            assert installed.status_code == 200, installed.text
            assert outcomes(installed)["form_mapping"]["action"] == "installed"
            # The digest the vendor computed when it published is the digest
            # this deployment recomputed and installed under.
            assert outcomes(installed)["form_mapping"]["sha256"] == digest_v1
            cached = await session.scalar(select(PackCache))
            assert cached is not None and cached.sha256 == digest_v1

            # 2. The control plane goes away — at the transport, so the outage
            # is real to the deployment and touches nothing else.

            use_down_control_plane(monkeypatch)

            # 3. Nothing degrades yet: the paid period still holds, so the
            # cached lease keeps serving reads and gated automation while the
            # failed refresh is booked — never raised.

            await age_lease(session, fetched_ago=timedelta(hours=25))
            still_licensed = client.get(ENTITLEMENT_URL).json()
            assert still_licensed["license_state"] == "active"
            assert still_licensed["tier"] == "standard"
            assert still_licensed["capabilities"] == ALL_CAPABILITIES
            row = await cached_lease(session)
            assert row.last_error == "control_plane_unreachable"
            assert (await gate(session, tenant, company.id)).allowed is True

            forced = client.post(f"{ENTITLEMENT_URL}/refresh").json()
            assert forced["refreshed"] is False
            assert forced["reason"] == "control_plane_unreachable"

            # The paid period passes: the grace window opens with every
            # capability retained, and only the state moves.
            await expire_lease(session, days_ago=3)
            await age_lease(session, fetched_ago=timedelta(hours=25))
            in_grace = client.get(ENTITLEMENT_URL).json()
            assert in_grace["license_state"] == "grace"
            assert in_grace["tier"] == "standard"
            assert in_grace["capabilities"] == ALL_CAPABILITIES
            assert (await gate(session, tenant, company.id)).allowed is True

            # 4. Past the window the same cached lease degrades the deployment.

            await expire_lease(session, days_ago=8)
            await age_lease(session, fetched_ago=timedelta(days=38))

            degraded = client.get(ENTITLEMENT_URL).json()
            assert degraded["tier"] == "standard"  # the leased tier stays on record
            assert degraded["license_state"] == "free"
            assert degraded["capabilities"] == []
            denied = await gate(session, tenant, company.id)
            assert denied.allowed is False
            assert denied.reason is GatingReason.TIER_CAPABILITY_MISSING

            # The pack cache freezes; the refusal is the licence's own reason.
            frozen = client.get(PACKS_URL).json()
            assert frozen["license_state"] == "free"
            assert frozen["frozen"] is True
            (entry,) = frozen["packs"]
            assert entry["version"] == 1
            assert entry["sha256"] == digest_v1
            assert entry["frozen"] is True and entry["frozen_at"] is not None
            # Retained and still readable: the stored copy still hashes to the
            # digest it was installed under, and the content is still there.
            assert entry["payload_verified"] is True
            retained = await session.scalar(select(PackCache))
            assert retained is not None and retained.payload == FORM_MAPPING_V1

            refused = client.post(PACKS_REFRESH_URL)
            assert refused.status_code == 403, refused.text
            assert refused.json() == {"code": "packs_not_licensed", "reason": "license_state_free"}

            # The vendor moves on while the deployment cannot see it: a new
            # version is published, and the frozen cache still refuses to
            # download it.
            digest_v2 = publish_form_mapping(vendor, FORM_MAPPING_V2)
            assert digest_v2 != digest_v1
            assert client.post(PACKS_REFRESH_URL).status_code == 403

            # The ungated paths never consulted any of this.
            portfolio = client.get("/api/v1/companies")
            assert portfolio.status_code == 200
            assert [item["br_number"] for item in portfolio.json()] == [company.br_number]
            assert client.get(f"/api/v1/companies/{company.id}/master-data").status_code == 200
            listed = client.get(f"/api/v1/companies/{company.id}/documents")
            assert listed.status_code == 200 and listed.json() == []

            # 5. The control plane returns.

            point_at_vendor(monkeypatch, vendor)

            recovered = client.post(f"{ENTITLEMENT_URL}/refresh").json()
            assert recovered["refreshed"] is True
            assert (recovered["tier"], recovered["license_state"]) == ("standard", "active")
            assert recovered["capabilities"] == ALL_CAPABILITIES

            unfrozen = client.get(PACKS_URL).json()
            assert unfrozen["frozen"] is False
            assert [(item["frozen"], item["frozen_at"]) for item in unfrozen["packs"]] == [
                (False, None)
            ]

            # Refresh resumes, and installs what the vendor shipped while the
            # cache was frozen.
            resumed = client.post(PACKS_REFRESH_URL)
            assert resumed.status_code == 200, resumed.text
            assert outcomes(resumed)["form_mapping"]["action"] == "installed"
            assert outcomes(resumed)["form_mapping"]["version"] == 2
            assert outcomes(resumed)["form_mapping"]["sha256"] == digest_v2
            rows = await session.scalars(select(PackCache).order_by(PackCache.version))
            assert [(row.version, row.frozen) for row in rows.all()] == [(1, False), (2, False)]

            # The whole lifecycle is on the append-only audit chain, written
            # through the real paths: one freeze, one unfreeze, two installs,
            # and the refusals carrying the gate's own reason.
            actions = await audit_actions(session)
            assert actions.count(AuditAction.PACKS_FROZEN.value) == 1
            assert actions.count(AuditAction.PACKS_UNFROZEN.value) == 1
            assert actions.count(AuditAction.PACK_INSTALLED.value) == 2
            refusals = (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.PACK_REFRESH_REFUSED.value
                    )
                )
            ).all()
            assert {entry.details["reason"] for entry in refusals} == {"license_state_free"}
        finally:
            remove_vendor_rows(
                control_plane_database,
                since=run_started_at,
                vendor_level=("DELETE FROM pack_releases WHERE published_at >= :since",),
            )
