"""API-layer tests for the login endpoint and the real authentication seam.

These run without a database: payload validation is rejected before any
session use, and ``get_current_actor`` fails closed on missing or malformed
Authorization headers before it would touch the session. Real login flows
against PostgreSQL live in ``test_auth_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import AuthSession, User
from app.adapters.persistence.session import get_session
from app.application import auth as auth_app
from app.main import create_app


def build_client() -> TestClient:
    app: FastAPI = create_app()

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # paths under test never reach the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app, raise_server_exceptions=False)


def patch_login(
    monkeypatch, *, role: str, mfa_verified: bool, totp_secret: str | None
) -> None:
    """Stage one successful login without a database.

    The user/session rows are plain ORM objects; the route reads exactly the
    facts the MFA response fields derive from — the session's ``mfa_verified``
    flag and the user's enrollment column.
    """

    async def fake_login(
        session: AsyncSession, *, email: str, password: str
    ) -> tuple[str, User, AuthSession]:
        user = User(
            id=uuid4(),
            tenant_id=uuid4(),
            email=email,
            display_name="Test User",
            role=role,
            review_events=False,
            approve_events=False,
            review_drafts=False,
            approve_drafts=False,
            is_active=True,
            totp_secret=totp_secret,
        )
        auth_session = AuthSession(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_digest="digest",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mfa_verified=mfa_verified,
        )
        return "token", user, auth_session

    monkeypatch.setattr(auth_app, "login", fake_login)


LOGIN_PAYLOAD = {"email": "staff@tcsp.example", "password": "anything"}


# --- POST /api/v1/auth/login: request validation ---


def test_login_rejects_invalid_payload() -> None:
    client = build_client()
    bad_email = client.post("/api/v1/auth/login", json={"email": "nope", "password": "x"})
    missing_pw = client.post("/api/v1/auth/login", json={"email": "a@b.co"})
    empty_pw = client.post("/api/v1/auth/login", json={"email": "a@b.co", "password": ""})
    assert bad_email.status_code == 422
    assert missing_pw.status_code == 422
    assert empty_pw.status_code == 422


# --- get_current_actor (the real seam): fail closed on missing/malformed credentials ---


def test_protected_route_without_authorization_header_is_401() -> None:
    response = build_client().post("/api/v1/users", json={})
    assert response.status_code == 401


def test_protected_route_with_malformed_authorization_is_401() -> None:
    client = build_client()
    for header in ("Token abc", "Bearer", "Bearer ", "bearer", "Basic dXNlcjpwYXNz"):
        response = client.post(
            "/api/v1/users", json={}, headers={"Authorization": header}
        )
        assert response.status_code == 401, header


# --- POST /api/v1/auth/login: the MFA fields on the response ---


def test_login_response_mfa_fields_not_required_for_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_login(monkeypatch, role="general", mfa_verified=True, totp_secret=None)
    response = build_client().post("/api/v1/auth/login", json=LOGIN_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is False
    assert body["enrollment_state"] == "not_required"


def test_login_response_mfa_fields_enrolled_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_login(monkeypatch, role="administrator", mfa_verified=False, totp_secret="sealed")
    response = build_client().post("/api/v1/auth/login", json=LOGIN_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "enrolled"


def test_login_response_mfa_fields_pending_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_login(monkeypatch, role="administrator", mfa_verified=False, totp_secret=None)
    response = build_client().post("/api/v1/auth/login", json=LOGIN_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "pending"


def test_login_response_mfa_fields_enrolled_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice ①: an enrolled General staff account owes the challenge exactly
    like an enrolled Administrator — the response names ``enrolled``."""
    patch_login(monkeypatch, role="general", mfa_verified=False, totp_secret="sealed")
    response = build_client().post("/api/v1/auth/login", json=LOGIN_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "enrolled"
