"""Licensed pack delivery, end to end, across the two deployables (opt-in).

CP2's cross-deployable evidence, in the same shape as the command-channel's
exit-gate test: the vendor service is the **real** ``control_plane``
application served by a subprocess on loopback (``tests/control_plane_server.py``),
the deployment activates against it for real (ADR-0016's handshake), the pack
is published through the vendor's own back office, and the deployment
downloads it through its own refresh route — which is what makes the digest
the vendor computed the digest this deployment verified.

It walks the commercial gate in both directions: while the tenant's tier is
Free the vendor refuses the download (``entitlement_denied``), and while the
deployment's own licence is Free the refusal is local and the vendor is not
asked at all. Then, licensed, the pack installs, the audit chain records it,
and the cached payload builds the same form registry the image holds — the
one place a licensed mapping pack comes to matter (``app.adapters.forms``).

Skipped unless both databases are reachable and migrated. The vendor service
commits for real (it is a separate process), so its rows are removed again at
the end.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.forms.registry import forms_root, image_registry, load_form_registry
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.packs import PackCache
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.prior_filing import FormCode
from app.main import create_app
from tests.control_plane_integration import redeem_activation_code
from tests.vendor_harness import VendorService, remove_vendor_rows, vendor_headers

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ACTIVATION_URL = "/api/v1/entitlement/activation"
REFRESH_URL = "/api/v1/packs/refresh"
ENTITLEMENT_URL = "/api/v1/entitlement"
VENDOR_ADMIN_KEY = "pack-delivery-vendor-key"


@pytest.fixture
async def session() -> Iterator[AsyncSession]:  # type: ignore[misc]
    """A session inside an outer transaction, rolled back after the test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


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


def set_tier(vendor: VendorService, vendor_tenant_id: str, tier: str) -> None:
    with httpx.Client(timeout=10.0) as http:
        response = http.put(
            f"{vendor.base_url}/api/v1/tenants/{vendor_tenant_id}/entitlement",
            json={"tier": tier},
            headers=vendor_headers(vendor),
        )
    assert response.status_code == 200, response.text


def publish_form_mapping(vendor: VendorService) -> str:
    """Publish a ``form_mapping`` pack carrying the registry the image holds.

    Built from ``backend/forms/`` so the pack's content is the real mapping
    intelligence — which is what makes the last assertion in this test mean
    something: the deployment ends up extracting through the *vendor's* pack.
    """
    forms = {
        form.value: {
            "manifest": json.loads((forms_root() / form.value / "manifest.json").read_text()),
            "mapping": json.loads((forms_root() / form.value / "mapping.json").read_text()),
            "geometry": json.loads((forms_root() / form.value / "geometry.json").read_text()),
        }
        for form in (FormCode.NAR1, FormCode.ND2A)
    }
    with httpx.Client(timeout=10.0) as http:
        response = http.post(
            f"{vendor.base_url}/api/v1/packs",
            json={
                "pack_type": "form_mapping",
                "payload": {"forms": forms},
                "published_by": "vendor_ops",
            },
            headers=vendor_headers(vendor),
        )
    assert response.status_code == 201, response.text
    return str(response.json()["sha256"])


# --- The round trip -------------------------------------------------------


async def test_a_licensed_pack_reaches_the_deployment_end_to_end(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

            vendor_tenant_id = activate_deployment(client, vendor)
            digest = publish_form_mapping(vendor)

            # Free here: the refusal is local and the vendor is never asked —
            # a deployment does not ask for what its own licence does not carry.
            free = client.post(REFRESH_URL)
            assert free.status_code == 403, free.text
            assert free.json() == {"code": "packs_not_licensed", "reason": "tier_not_licensed"}

            # Licensed at the vendor, and the deployment refreshes its lease
            # to pick that up (ADR-0016) — after which it holds a licence to
            # refresh with, and its own state is active.
            set_tier(vendor, vendor_tenant_id, "standard")
            refreshed = client.post(f"{ENTITLEMENT_URL}/refresh")
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["license_state"] == "active"

            # The vendor's own gate, exercised for real: the deployment's
            # licence is live while the vendor's tier is not licensed.
            set_tier(vendor, vendor_tenant_id, "free")
            vendor_refused = client.post(REFRESH_URL)
            assert vendor_refused.status_code == 200, vendor_refused.text
            outcomes = {item["pack_type"]: item for item in vendor_refused.json()["outcomes"]}
            assert outcomes["form_mapping"]["action"] == "refused"
            assert outcomes["form_mapping"]["reason"] == (
                "control_plane_refused:entitlement_denied:tier_not_licensed"
            )
            assert await session.scalar(select(PackCache)) is None

            # Licensed again: the pack installs, verified against the digest
            # the vendor computed when it published.
            set_tier(vendor, vendor_tenant_id, "standard")
            installed = client.post(REFRESH_URL)
            assert installed.status_code == 200, installed.text
            outcomes = {item["pack_type"]: item for item in installed.json()["outcomes"]}
            assert outcomes["form_mapping"]["action"] == "installed"
            assert outcomes["form_mapping"]["version"] == 1
            assert outcomes["form_mapping"]["sha256"] == digest

            cached = await session.scalar(select(PackCache))
            assert cached is not None
            assert cached.sha256 == digest
            assert cached.pack_type == "form_mapping" and cached.version == 1

            entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.PACK_INSTALLED.value)
            )
            assert entry is not None
            assert entry.tenant_id == tenant.id
            assert entry.actor_user_id == admin.id
            assert entry.details == {
                "pack_type": "form_mapping",
                "version": 1,
                "sha256": digest,
                "signature": False,
            }

            # And the point of the whole channel: the cached pack is mapping
            # intelligence this deployment can extract through, because the
            # consumption seam builds the same registry from it.
            packed = load_form_registry(FormCode.NAR1, pack_payload=cached.payload)
            assert packed == image_registry(FormCode.NAR1)
            assert packed.items == image_registry(FormCode.NAR1).items
        finally:
            remove_vendor_rows(
                control_plane_database,
                since=run_started_at,
                vendor_level=("DELETE FROM pack_releases WHERE published_at >= :since",),
            )
