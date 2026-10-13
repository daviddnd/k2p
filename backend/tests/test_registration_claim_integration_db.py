"""The registration → claim → handshake chain, across the two deployables.

CP4c's acceptance test, in the same shape as the command channel's exit gate
(``test_command_envelope_integration_db.py``): the vendor service is the
**real** ``control_plane`` application served by a subprocess on a loopback
socket (``tests/control_plane_server.py`` — both deployables answer to the
package name ``app``, so the side that is not under test leaves the process),
and the deployment activates against it through its own endpoints, with the
browser and Google played by the test over that same socket.

What is asserted is the whole chain the registration code exists for:

1. a prospective customer registers on the control plane
   (``POST /api/v1/registrations``) and is delivered a one-time code;
2. the deployment's administrator starts activation presenting that code
   (``POST /api/v1/entitlement/activation`` with ``registration_code``);
3. the handshake completes for real — PKCE, the fake Google identity
   provider, the one-time code redemption — and the first lease is cached;
4. the lease **pins** the free plan's caps (2 seats / 100 companies) in its
   raw payload — the plan ladder made the pinned number the source of truth, so
   the deployment's effective view (also asserted) is read off the lease, not
   derived locally — and the registration on the control plane reads
   ``claimed`` against the very tenant the deployment activated as.

Then the two refusals that make the code a pin rather than a hint: a code
the vendor never issued is refused by the deployment's activation start as
the deployment's own typed failure (``control_plane_refused:registration_
code_invalid``), and a code that was already claimed is refused from any
fingerprint but the one that claimed it — while re-presenting it from the
claiming deployment stays an idempotent success, so a lost response can be
retried.

Skipped unless both databases are reachable and migrated; see
``tests/control_plane_integration.py``. The vendor service commits for real
(it is a separate process), so its rows are removed again at the end.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneRefused,
)
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.core.config import settings
from app.main import create_app
from tests.control_plane_integration import redeem_activation_code
from tests.vendor_harness import VendorService, remove_vendor_rows, vendor_headers

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ACTIVATION_URL = "/api/v1/entitlement/activation"
ENTITLEMENT_URL = "/api/v1/entitlement"
REDIRECT_URI = "http://localhost:8000/api/v1/entitlement/activation/callback"
# The one credential this test hands the vendor service — through the
# environment, so no secret is printed or parsed out of a subprocess.
VENDOR_ADMIN_KEY = "registration-claim-vendor-key"


@pytest.fixture
async def session() -> Iterator[AsyncSession]:  # type: ignore[misc]
    """A session inside an outer transaction, rolled back after the test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- The vendor's own record ----------------------------------------------


def register_lead(vendor: VendorService, *, contact_email: str) -> tuple[str, str]:
    """Sign up on the real control plane; returns ``(registration_id, code)``."""
    with httpx.Client(timeout=10.0) as http:
        response = http.post(
            f"{vendor.base_url}/api/v1/registrations",
            json={
                "legal_name": "Example Secretarial Limited",
                "contact_email": contact_email,
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    return str(body["registration_id"]), str(body["registration_code"])


def read_registration(vendor: VendorService, registration_id: str) -> dict[str, object]:
    """The lead as the vendor's follow-up read reports it."""
    with httpx.Client(timeout=10.0) as http:
        response = http.get(
            f"{vendor.base_url}/api/v1/registrations", headers=vendor_headers(vendor)
        )
    assert response.status_code == 200, response.text
    entries = response.json()["registrations"]
    entry = next(item for item in entries if item["registration_id"] == registration_id)
    return dict(entry)


# --- The deployment -------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> Iterator[AsyncSession]:  # type: ignore[misc]
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


def activate_with_code(client: TestClient, vendor: VendorService, code: str) -> str:
    """Run the real activation handshake presenting ``code``; returns the
    vendor tenant id.

    Identical to the command-channel suite's browser leg except for the one
    field this slice added: the activation start carries the registration
    code, and everything after it — the redirect chain, the callback, the
    PKCE redemption — is the unchanged handshake.
    """
    started = client.post(ACTIVATION_URL, json={"registration_code": code})
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
        code_from_vendor, state = query["code"][0], query["state"][0]

    redeem_activation_code(client, code_from_vendor, state)

    status = client.get(ACTIVATION_URL).json()
    assert status["activated"] is True
    assert status["cache"]["tier"] == "free"  # a fresh tenant starts Free
    return str(status["control_plane_tenant_id"])


# --- The acceptance chain ---------------------------------------------------


async def test_a_registration_code_binds_the_deployment_to_its_registration(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            registration_id, code = register_lead(
                vendor, contact_email=f"ops-{uuid4()}@tcsp.example"
            )
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

            vendor_tenant_id = activate_with_code(client, vendor, code)

            # The raw lease the vendor serves pins the free plan's caps —
            # read straight off the vendor with the deployment's own
            # credential, so the assertion is on the pinned number, not on
            # anything the deployment derived.
            state = ActivationStore(settings.cp_activation_token_file).load()
            assert state.credential is not None
            with httpx.Client(timeout=10.0) as http:
                lease = http.get(
                    f"{vendor.base_url}/api/v1/entitlement",
                    headers={"Authorization": f"Bearer {state.credential.token}"},
                )
            assert lease.status_code == 200, lease.text
            assert lease.json()["tier"] == "free"
            assert lease.json()["seat_cap"] == 2
            assert lease.json()["company_cap"] == 100

            # The lease the handshake cached puts the deployment on the free
            # plan: the free plan's own caps are the ones in force.
            view = client.get(ENTITLEMENT_URL).json()
            assert view["source"] == "lease"
            assert view["tier"] == "free"
            assert view["seat_cap"] == 2
            assert view["company_cap"] == 100

            # And the vendor's own record agrees: the registration is claimed
            # against the very tenant this deployment activated as.
            entry = read_registration(vendor, registration_id)
            assert entry["status"] == "claimed"
            assert entry["tenant_id"] == vendor_tenant_id
            assert entry["claimed_at"] is not None
        finally:
            remove_vendor_rows(
                control_plane_database,
                since=run_started_at,
                leading=("DELETE FROM registrations WHERE created_at >= :since",),
            )


async def test_a_made_up_registration_code_is_refused_as_the_deployments_own_failure(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No registration is created in this test, so there is nothing on the
    # vendor side to clean up: a refused claim creates no request and no row.
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        tenant, admin = await seed_tenant_with_admin(session)
        monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
        client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

        refused = client.post(
            ACTIVATION_URL, json={"registration_code": "a-code-the-vendor-never-issued"}
        )

        assert refused.status_code == 502, refused.text
        assert refused.json()["detail"] == "control_plane_refused:registration_code_invalid"
        # The refusal aborted the start: no attempt is left pending.
        status = client.get(ACTIVATION_URL).json()
        assert status["activated"] is False
        assert status["authorization_url"] is None


async def test_a_used_code_is_refused_from_any_fingerprint_but_the_claimants(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            registration_id, code = register_lead(
                vendor, contact_email=f"ops-{uuid4()}@tcsp.example"
            )
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

            vendor_tenant_id = activate_with_code(client, vendor, code)

            # From the deployment that claimed it, the used code is an
            # idempotent success — a lost response can be retried.
            retried = client.post(ACTIVATION_URL, json={"registration_code": code})
            assert retried.status_code == 201, retried.text

            # From any other fingerprint, the same code is refused: that is
            # the pin. Presented through the real client, the way a different
            # deployment's activation start would send it.
            direct = ControlPlaneClient(base_url=vendor.base_url, timeout=10.0)
            with pytest.raises(ControlPlaneRefused) as refusal:
                await direct.register_activation(
                    redirect_uri=REDIRECT_URI,
                    code_challenge="x" * 43,  # a well-formed S256 challenge
                    deployment_fingerprint="a-different-deployment",
                    registration_code=code,
                )
            assert refusal.value.reason == "registration_code_used"

            # And the binding never moved.
            entry = read_registration(vendor, registration_id)
            assert entry["status"] == "claimed"
            assert entry["tenant_id"] == vendor_tenant_id
        finally:
            remove_vendor_rows(
                control_plane_database,
                since=run_started_at,
                leading=("DELETE FROM registrations WHERE created_at >= :since",),
            )
