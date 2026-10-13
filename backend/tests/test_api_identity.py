"""API-layer tests for the identity endpoints (tenant bootstrap + user creation).

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the single authorization
check, and request validation are exercised. Real creation flows against
PostgreSQL live in ``test_identity_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import entitlement, identity
from app.core.config import settings
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)


def build_client(
    actor: ActorRef | None = None, context: ActorContext | None = None
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # endpoints under test never reach the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


USER_PAYLOAD = {
    "email": "staff@tcsp.example",
    "display_name": "Staff User",
    "role": "general",
    "password": "initial-password-1",
}


# --- POST /api/v1/users: fail closed, then Administrators only ---


def test_create_user_unauthenticated_fails_closed() -> None:
    response = build_client().post("/api/v1/users", json=USER_PAYLOAD)
    assert response.status_code == 401


def test_create_user_general_account_is_forbidden() -> None:
    # Even holding every grantable permission, a General account cannot call
    # tenant-scoped (company-less) routes.
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_CONTEXT
    )
    response = client.post("/api/v1/users", json=USER_PAYLOAD)
    assert response.status_code == 403


def test_create_user_inactive_administrator_is_forbidden() -> None:
    context = ActorContext(
        is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
    )
    response = build_client(actor=ADMIN, context=context).post("/api/v1/users", json=USER_PAYLOAD)
    assert response.status_code == 403


def test_create_user_rejects_invalid_payload() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    bad_role = client.post("/api/v1/users", json={**USER_PAYLOAD, "role": "superuser"})
    bad_email = client.post("/api/v1/users", json={**USER_PAYLOAD, "email": "not-an-email"})
    missing = client.post("/api/v1/users", json={"email": "a@b.co"})
    assert bad_role.status_code == 422
    assert bad_email.status_code == 422
    assert missing.status_code == 422


def test_create_user_seat_cap_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def over_cap(session: object, **kwargs: object) -> object:
        raise entitlement.SeatCapExceededError(1)

    monkeypatch.setattr(identity, "create_user", over_cap)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post("/api/v1/users", json=USER_PAYLOAD)
    assert response.status_code == 409
    assert "seat_cap_exceeded" in response.json()["detail"]


# --- POST /api/v1/tenants: bootstrap validation (creation flow is DB-backed) ---


def test_bootstrap_rejects_invalid_payload() -> None:
    client = build_client()
    valid = {
        "tenant_name": "Acme TCSP",
        "admin_email": "root@acme.example",
        "admin_display_name": "Root Admin",
        "admin_password": "bootstrap-secret-1",
    }
    assert client.post("/api/v1/tenants", json={**valid, "admin_email": "nope"}).status_code == 422
    assert client.post("/api/v1/tenants", json={**valid, "tenant_name": ""}).status_code == 422
    assert client.post("/api/v1/tenants", json={"tenant_name": "Acme TCSP"}).status_code == 422
    assert (
        client.post("/api/v1/tenants", json={**valid, "admin_password": "short"}).status_code == 422
    )


# --- POST /api/v1/tenants: the opt-in bootstrap guard (AICS_SETUP_TOKEN) ---

BOOTSTRAP_PAYLOAD = {
    "tenant_name": "Acme TCSP",
    "admin_email": "root@acme.example",
    "admin_display_name": "Root Admin",
    "admin_password": "bootstrap-secret-1",
}
SETUP_TOKEN = "api-test-setup-token"


def test_bootstrap_armed_without_the_header_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "setup_token", SETUP_TOKEN)
    response = build_client().post("/api/v1/tenants", json=BOOTSTRAP_PAYLOAD)
    assert response.status_code == 401
    assert response.json()["detail"] == "setup_token_required"


def test_bootstrap_armed_with_a_wrong_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "setup_token", SETUP_TOKEN)
    response = build_client().post(
        "/api/v1/tenants", json=BOOTSTRAP_PAYLOAD, headers={"X-Setup-Token": "wrong"}
    )
    assert response.status_code == 401
    # The body never distinguishes "missing" from "wrong" — a probe learns nothing.
    assert response.json()["detail"] == "setup_token_required"


def test_bootstrap_armed_with_the_correct_token_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "setup_token", SETUP_TOKEN)

    async def already_exists(session: object, **kwargs: object) -> object:
        # The guard runs before any session use; reaching the application
        # function at all proves it passed.
        raise identity.TenantAlreadyExistsError

    monkeypatch.setattr(identity, "create_tenant_with_administrator", already_exists)
    response = build_client().post(
        "/api/v1/tenants", json=BOOTSTRAP_PAYLOAD, headers={"X-Setup-Token": SETUP_TOKEN}
    )
    assert response.status_code == 409
