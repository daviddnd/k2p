"""API-layer tests for the entitlement read and activation endpoints
(ADR-0012/0016; data-model §2.2 payload shape, §4.3 licence state).

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the single Administrator-only
authorization check (``require_admin``), and the derived view the endpoints
render for a configured tier and expiry are exercised. The configuration
source is patched through the real settings seam — the same path a
deployment provisions — so this covers settings → adapter → derived view →
response. The callback's *openness* is pinned here too: it is reached by a
browser arriving from the vendor site, so it is the one route that must not
demand an administrator. Real flows against PostgreSQL live in
``test_entitlement_scenarios_db.py`` and
``test_entitlement_lease_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.control_plane_client import ControlPlaneClient
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import audit, lease, packs
from app.core.config import settings
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import AutomationType
from app.main import create_app
from tests.fake_control_plane import FakeControlPlane

URL = "/api/v1/entitlement"
ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
NOW = datetime.now(UTC)
ALL_CAPABILITY_VALUES = sorted(member.value for member in AutomationType)

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),  # even holding every grantable permission
    company_ids=frozenset(),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


@pytest.fixture(autouse=True)
def pack_freeze_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the pack-cache freeze: this suite runs without a database.

    The entitlement read applies the §4.3 freeze to the pack cache
    (``app.application.packs``), which is one count query — and this suite
    deliberately has no session. The freeze itself is covered against
    PostgreSQL in ``test_pack_cache_scenarios_db.py``.
    """

    async def no_change(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(packs, "apply_license_state", no_change)


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the endpoint never reaches the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def configure(
    monkeypatch: pytest.MonkeyPatch, *, tier: str, expires_at: datetime | None = None
) -> None:
    """Provision the deployment configuration the adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


# --- Fail closed; Administrator-only ---


def test_unauthenticated_fails_closed() -> None:
    assert build_client().get(URL).status_code == 401


def test_general_account_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)
    assert client.get(URL).status_code == 403


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    assert client.get(URL).status_code == 403


# --- The derived view ---


def test_free_tier_derived_view(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, tier="free")
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "free"
    assert body["license_state"] == "active"
    assert body["capabilities"] == []
    assert body["seat_cap"] == 2
    assert body["company_cap"] == 100
    assert body["license_expires_at"] is None
    assert body["entitlement_version"] == 1


def test_standard_tier_with_future_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    expires_at = NOW + timedelta(days=30)
    configure(monkeypatch, tier="standard", expires_at=expires_at)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(URL).json()
    assert body["tier"] == "standard"
    assert body["license_state"] == "active"
    assert body["capabilities"] == ALL_CAPABILITY_VALUES
    # Per subscription/license: no fixed cap is configured in this phase.
    assert body["seat_cap"] is None
    assert body["company_cap"] is None
    assert datetime.fromisoformat(body["license_expires_at"]) == expires_at


def test_grace_keeps_capabilities_and_lapsed_lease_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch, tier="standard", expires_at=NOW - timedelta(days=3))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    in_grace = client.get(URL).json()
    assert in_grace["license_state"] == "grace"
    assert in_grace["capabilities"] == ALL_CAPABILITY_VALUES

    # Past the 7-day grace window the lease is dead: the tier stays
    # provisioned but the capability set collapses to Free (§4.3).
    configure(monkeypatch, tier="standard", expires_at=NOW - timedelta(days=8))
    degraded = client.get(URL).json()
    assert degraded["tier"] == "standard"
    assert degraded["license_state"] == "free"
    assert degraded["capabilities"] == []


def test_unknown_configured_tier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, tier="premium")
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(URL).json()
    assert body["tier"] == "free"
    assert body["capabilities"] == []
    assert body["seat_cap"] == 2
    assert body["company_cap"] == 100


# --- The CP1b activation surface: admin-only, except the callback ---

ACTIVATION_URL = "/api/v1/entitlement/activation"
REFRESH_URL = "/api/v1/entitlement/refresh"


def test_the_read_view_names_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Before any activation the source is deployment configuration, which is
    # what makes the precedence rule observable to an operator.
    configure(monkeypatch, tier="standard")
    body = build_client(actor=ADMIN, context=ADMIN_CONTEXT).get(URL).json()

    assert body["source"] == "configuration"


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_starting_reading_and_ending_an_activation_are_administrator_only(
    method: str,
) -> None:
    client = build_client()
    response = getattr(client, method)(ACTIVATION_URL)
    assert response.status_code == 401

    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)
    response = getattr(client, method)(ACTIVATION_URL)
    assert response.status_code == 403

    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    response = getattr(client, method)(ACTIVATION_URL)
    assert response.status_code == 403


def test_the_explicit_refresh_is_administrator_only() -> None:
    assert build_client().post(REFRESH_URL).status_code == 401
    assert (
        build_client(actor=ADMIN, context=GENERAL_CONTEXT).post(REFRESH_URL).status_code == 403
    )


def test_activation_refuses_when_no_control_plane_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-safe default: no vendor URL means the handshake refuses rather than
    # guessing an endpoint.
    monkeypatch.setattr(settings, "cp_base_url", "")
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(ACTIVATION_URL)

    assert response.status_code == 503


# --- The registration code the operator can present at activation start ---

CONTROL_PLANE_URL = "http://control-plane.test"


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


def _async_outcome(tier: str):
    """The refresh_lease stand-in for the hermetic silent test."""

    async def _refresh(*args, **kwargs):
        row = SimpleNamespace(tier=tier, entitlement_version=1)
        return SimpleNamespace(row=row, refreshed=True, reason=None)

    return _refresh()


async def _async_noop(*args, **kwargs) -> None:
    """The audit seam's stand-in — hermetic tests assert the call happened via behaviour."""


def test_activation_start_forwards_a_registration_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-10-11 amendment end to end at the API edge: with a valid code
    the vendor answers ``activated: true`` — this deployment stores the
    credential, caches the first lease, and has no URL to open. The lease
    refresh and audit seams stand in for the (hermetic) database here; the
    real round trip is the cross-deployable suite's silent test."""
    control_plane = FakeControlPlane()
    code = control_plane.issue_registration_code()
    use_control_plane(monkeypatch, control_plane)
    monkeypatch.setattr(lease, "refresh_lease", lambda *a, **k: _async_outcome(control_plane.tier))
    monkeypatch.setattr(audit, "record_event", _async_noop)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(ACTIVATION_URL, json={"registration_code": code})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["activated"] is True
    assert body["authorization_url"] is None
    assert control_plane.registration_codes[code] is not None  # the fake claimed it
    # No browser request exists to carry on with.
    assert control_plane.requests == {}


def test_activation_start_without_a_body_sends_no_registration_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(ACTIVATION_URL)

    assert response.status_code == 201, response.text
    pending = control_plane.requests[str(response.json()["request_id"])]
    assert pending.registration_code_present is False


def test_a_refused_registration_code_surfaces_the_vendors_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        ACTIVATION_URL, json={"registration_code": "a-code-the-vendor-never-issued"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "control_plane_refused:registration_code_invalid"
    assert control_plane.registrations == 0  # a refused claim creates no request


def test_the_callback_is_open_and_refuses_a_callback_with_no_pending_attempt() -> None:
    # Unauthenticated by necessity (the browser arrives from the vendor site),
    # so its authority is the pending attempt — which does not exist here.
    response = build_client().get(f"{ACTIVATION_URL}/callback?code=made-up&state=x")

    assert response.status_code == 400
    assert "no_pending_activation" in response.text
    assert response.headers["content-type"].startswith("text/html")
