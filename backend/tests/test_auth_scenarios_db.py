"""DB-backed scenarios for staff login and session authentication (opt-in;
needs a migrated PostgreSQL — same setup as ``test_rbac_scenarios_db.py``).

Unlike the identity scenarios, these never override ``get_current_actor``:
every protected request authenticates through the real seam with a Bearer
token issued by ``POST /api/v1/auth/login`` — the Phase 1 plan's requirement
that exit-gate demos exercise the real RBAC path.

The deployment runs on a licensed tier here because these scenarios create
staff accounts: the Free-tier seat cap would refuse them, and that cap is
exercised in ``test_entitlement_scenarios_db.py`` instead. Every test runs
inside a transaction that is rolled back, so the review database stays
clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import AuthSession, Tenant, User
from app.adapters.persistence.session import get_session
from app.application import auth
from app.core.config import settings
from app.domain import mfa
from app.domain.auth import session_token_digest
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ADMIN_EMAIL = f"root-{uuid4()}@acme.example"
ADMIN_PASSWORD = "bootstrap-secret-1"
STAFF_EMAIL = f"staff-{uuid4()}@acme.example"
STAFF_PASSWORD = "staff-password-1"


@pytest.fixture(autouse=True)
def licensed_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """A licensed tier leaves the seat cap out of the login scenarios."""
    monkeypatch.setattr(settings, "license_tier", "standard")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession) -> TestClient:
    """The real app — including the real auth seam — with only the session overridden."""
    app: FastAPI = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def bootstrap(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tenants",
        json={
            "tenant_name": "Acme TCSP",
            "admin_email": ADMIN_EMAIL,
            "admin_display_name": "Root Admin",
            "admin_password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 201


def login(client: TestClient, email: str, password: str) -> str:
    """Log in and return the issued Bearer token."""
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_user_via_api(client: TestClient, token: str) -> int:
    """POST the staff-user payload through the real auth + RBAC stack."""
    return client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(token)).status_code


async def verified_admin_token(client: TestClient, session: AsyncSession) -> str:
    """The phase-7 login flow for an Administrator, end to end: password, then
    TOTP enrollment and the challenge — the only way to a usable admin
    session (``get_current_actor`` refuses the rest of the surface until
    the flag verifies)."""
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)
    now = datetime.now(UTC)
    enrolled = mfa.totp_at(secret, mfa.current_step(now.timestamp()))
    await auth.complete_mfa_enrollment(
        session, user=admin, secret=secret, code=enrolled, now=now
    )
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    row = await session.scalar(
        select(AuthSession).where(AuthSession.token_digest == session_token_digest(body))
    )
    assert row is not None
    challenge = mfa.totp_at(secret, mfa.current_step(now.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=admin, code=challenge, now=now, auth_session=row
    )
    return body


CREATE_USER_PAYLOAD = {
    "email": STAFF_EMAIL,
    "display_name": "Staff User",
    "role": "general",
    "password": STAFF_PASSWORD,
    "review_events": True,
}


# --- the happy path: login → session → real RBAC stack ---


async def test_login_issues_a_session_that_drives_the_real_stack(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)

    token = await verified_admin_token(client, session)
    response = client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(token))
    assert response.status_code == 201, response.text

    # The created General account can itself log in and is then authenticated
    # but not authorized on this Administrator-only route.
    staff_token = login(client, STAFF_EMAIL, STAFF_PASSWORD)
    denied = client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(staff_token))
    assert denied.status_code == 403


async def test_login_response_shape_and_secrecy(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)

    response = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_at"]
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["user"]["role"] == "administrator"
    # Neither the password nor its hash leaves the API in any field.
    assert "password" not in str(body).lower()


async def test_only_the_token_digest_is_stored(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)

    token = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    rows = (await session.scalars(select(AuthSession))).all()
    assert len(rows) == 1
    assert rows[0].token_digest == session_token_digest(token)
    assert rows[0].token_digest != token


# --- every credential failure is the same fail-closed 401 ---


async def test_wrong_password_is_401(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    response = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password-1"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_unknown_email_is_401_with_the_same_detail(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    response = client.post(
        "/api/v1/auth/login", json={"email": f"nobody-{uuid4()}@acme.example", "password": "x"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_inactive_user_cannot_log_in(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    token = await verified_admin_token(client, session)
    assert create_user_via_api(client, token) == 201

    staff = await session.scalar(select(User).where(User.email == STAFF_EMAIL))
    assert staff is not None
    staff.is_active = False
    await session.flush()

    response = client.post(
        "/api/v1/auth/login", json={"email": STAFF_EMAIL, "password": STAFF_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


async def test_user_without_credentials_cannot_log_in(session: AsyncSession) -> None:
    """An identity record with a NULL password_hash (e.g. pre-login-slice
    data) fails closed, never crashes."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"nocreds-{uuid4()}@example.com",
        display_name="No Creds",
        role="general",
    )
    session.add(user)
    await session.flush()

    client = make_client(session)
    response = client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "anything-12345"}
    )
    assert response.status_code == 401


# --- session resolution at the seam ---


async def test_unknown_or_tampered_token_is_401(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    token = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    for bad in (f"{token[:-1]}x" if token[-1] != "x" else f"{token[:-1]}y", "forged-token"):
        response = client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(bad))
        assert response.status_code == 401, bad


async def test_expired_session_is_401(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    token = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    expired = await session.scalar(
        select(AuthSession).where(AuthSession.token_digest == session_token_digest(token))
    )
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()

    response = client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(token))
    assert response.status_code == 401


async def test_deactivated_user_session_stops_authorizing(session: AsyncSession) -> None:
    """The session still resolves and the MFA flag verifies, but the RBAC
    context denies inactive accounts — fail closed one layer down."""
    client = make_client(session)
    await bootstrap(client)
    token = await verified_admin_token(client, session)
    assert create_user_via_api(client, token) == 201

    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    admin.is_active = False
    await session.flush()

    response = client.post("/api/v1/users", json=CREATE_USER_PAYLOAD, headers=bearer(token))
    assert response.status_code == 403
