"""DB-backed scenarios for the staff-MFA lifecycle and the verified-session
enforcement gate (opt-in; needs a migrated PostgreSQL — same setup as
``test_auth_scenarios_db.py``).

E3 owns no endpoints (E4 owns ``/auth/mfa/*``), so the lifecycle is driven
through ``app.application.auth`` on the test session, while the enforcement
gate is exercised over HTTP with Bearer tokens issued by the real login
route — every protected request still authenticates through the real seam.

The deployment runs on a licensed tier here because these scenarios create
staff accounts, and every test runs inside a transaction that is rolled
back, so the review database stays clean. Time is pinned (``NOW``/``LATER``)
so the TOTP step arithmetic is deterministic.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import AuthSession, User
from app.adapters.persistence.session import get_session
from app.application import auth
from app.core.config import settings
from app.domain import mfa
from app.domain.audit import AuditAction
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
OPERATOR_EMAIL = f"operator-{uuid4()}@acme.example"
OPERATOR_PASSWORD = "operator-secret-1"

#: Pinned instants — one TOTP step apart, so codes are deterministic.
NOW = datetime.fromtimestamp(1_800_000_000, tz=UTC)
LATER = NOW + timedelta(seconds=30)

MFA_ACTIONS = (AuditAction.MFA_ENROLLED.value, AuditAction.MFA_CLEARED.value)


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


def login(client: TestClient, email: str, password: str) -> dict:
    """Log in and return the parsed response body."""
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def enrolled_admin(session: AsyncSession) -> tuple[User, str]:
    """The bootstrapped Administrator, enrolled via the real lifecycle."""
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)
    code = mfa.totp_at(secret, mfa.current_step(NOW.timestamp()))
    await auth.complete_mfa_enrollment(session, user=admin, secret=secret, code=code, now=NOW)
    return admin, secret


async def current_session_row(session: AsyncSession, token: str) -> AuthSession:
    row = await session.scalar(
        select(AuthSession).where(AuthSession.token_digest == session_token_digest(token))
    )
    assert row is not None
    return row


async def verified_admin_token(
    client: TestClient, session: AsyncSession, admin: User, secret: str
) -> str:
    """Log the enrolled admin in and pass the challenge — a usable session."""
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    row = await current_session_row(session, body["access_token"])
    code = mfa.totp_at(secret, mfa.current_step(LATER.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=admin, code=code, now=LATER, auth_session=row
    )
    return body["access_token"]


async def mfa_audit_count(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.in_(MFA_ACTIONS))
    ) or 0


# --- the enrollment lifecycle: begin → refuse → complete ---


async def test_begin_yields_secret_and_uri_without_writing_anything(
    session: AsyncSession,
) -> None:
    await bootstrap(make_client(session))
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None

    secret, uri = await auth.begin_mfa_enrollment(session, user=admin)

    assert uri.startswith("otpauth://totp/K2P%3A")
    assert f"secret={secret}" in uri
    assert "algorithm=SHA1&digits=6&period=30" in uri
    await session.refresh(admin)
    assert admin.totp_secret is None
    assert admin.totp_enrolled_at is None


async def test_wrong_enrollment_code_is_refused_and_nothing_is_written(
    session: AsyncSession,
) -> None:
    await bootstrap(make_client(session))
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)
    before = await mfa_audit_count(session)

    # Outside the ±1 window: a code that is well-formed but not current.
    stale = mfa.totp_at(secret, mfa.current_step(NOW.timestamp()) + 5)
    with pytest.raises(ValueError, match=mfa.CODE_INVALID):
        await auth.complete_mfa_enrollment(session, user=admin, secret=secret, code=stale, now=NOW)
    malformed = "12 45"
    with pytest.raises(ValueError, match=mfa.CODE_INVALID):
        await auth.complete_mfa_enrollment(
            session, user=admin, secret=secret, code=malformed, now=NOW
        )

    await session.refresh(admin)
    assert admin.totp_secret is None
    assert admin.totp_enrolled_at is None
    assert await mfa_audit_count(session) == before


async def test_correct_code_seals_the_secret_and_audits(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    secret, _uri = await auth.begin_mfa_enrollment(session, user=admin)

    code = mfa.totp_at(secret, mfa.current_step(NOW.timestamp()))
    await auth.complete_mfa_enrollment(session, user=admin, secret=secret, code=code, now=NOW)

    await session.refresh(admin)
    assert admin.totp_secret is not None
    assert admin.totp_secret != secret  # the D1 envelope, never the plaintext
    assert secret not in admin.totp_secret
    assert admin.totp_enrolled_at == NOW
    assert admin.totp_last_step is None

    entry = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.MFA_ENROLLED.value)
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    assert entry is not None
    assert entry.actor_user_id == admin.id
    assert entry.entity_type == "user"
    assert entry.entity_id == admin.id
    assert secret not in str(entry.details)


# --- the login flow and the enforcement gate ---


async def test_unenrolled_admin_is_forced_through_enrollment(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)

    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "pending"

    # An existing admin route refuses with the named 403 + the state.
    response = client.get("/api/v1/entitlement", headers=bearer(body["access_token"]))
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "mfa_verification_required",
        "enrollment_state": "pending",
    }


async def test_enrolled_admin_login_requires_mfa_and_the_gate_covers_admin_routes(
    session: AsyncSession,
) -> None:
    client = make_client(session)
    await bootstrap(client)
    await enrolled_admin(session)

    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "enrolled"

    response = client.get("/api/v1/entitlement", headers=bearer(body["access_token"]))
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "mfa_verification_required",
        "enrollment_state": "enrolled",
    }

    # The allowlist covers E4's surface and the login route itself, and
    # deliberately NOT the clear route (recovery needs a verified operator).
    from app.api import deps

    allowed = [
        "/api/v1/auth/login",
        "/api/v1/auth/mfa/enrollment",
        "/api/v1/auth/mfa/enrollment/complete",
        "/api/v1/auth/mfa/verify",
        "/api/v1/auth/mfa/status",
    ]
    for path in allowed:
        assert any(path.startswith(prefix) for prefix in deps.MFA_ALLOWED_PATH_PREFIXES), path
    for path in ("/api/v1/entitlement", "/api/v1/auth/mfa/clear", "/api/v1/admin/integrity"):
        assert not any(
            path.startswith(prefix) for prefix in deps.MFA_ALLOWED_PATH_PREFIXES
        ), path


async def test_general_staff_login_is_untouched_and_routes_work(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin, secret = await enrolled_admin(session)
    admin_token = await verified_admin_token(client, session, admin, secret)
    response = client.post(
        "/api/v1/users",
        json={
            "email": STAFF_EMAIL,
            "display_name": "Staff User",
            "role": "general",
            "password": STAFF_PASSWORD,
            "review_events": True,
        },
        headers=bearer(admin_token),
    )
    assert response.status_code == 201, response.text

    body = login(client, STAFF_EMAIL, STAFF_PASSWORD)
    assert body["mfa_required"] is False
    assert body["enrollment_state"] == "not_required"

    # The MFA gate never touches a verified General session: an active-staff
    # route works. (Slice ①: an *unverified* enrolled General session is
    # gated exactly like an Administrator's — the next test walks that.)
    response = client.get("/api/v1/dashboard", headers=bearer(body["access_token"]))
    assert response.status_code == 200, response.text


async def test_enrolled_general_staff_owes_the_challenge_end_to_end(
    session: AsyncSession,
) -> None:
    """Slice ① (2026-10-11), over HTTP: a General staff account binds an
    authenticator from its own verified session (self-service enrollment),
    and from the next sign-in the session owes the challenge and the gate
    holds until it passes."""
    client = make_client(session)
    await bootstrap(client)
    admin, secret = await enrolled_admin(session)
    admin_token = await verified_admin_token(client, session, admin, secret)
    response = client.post(
        "/api/v1/users",
        json={
            "email": STAFF_EMAIL,
            "display_name": "Staff User",
            "role": "general",
            "password": STAFF_PASSWORD,
            "review_events": True,
        },
        headers=bearer(admin_token),
    )
    assert response.status_code == 201, response.text

    # The staff sign-in is ordinary; the enrollment surface answers.
    body = login(client, STAFF_EMAIL, STAFF_PASSWORD)
    assert body["mfa_required"] is False
    staff_token = body["access_token"]
    response = client.post("/api/v1/auth/mfa/enrollment", headers=bearer(staff_token))
    assert response.status_code == 200, response.text
    begun = response.json()
    code = mfa.totp_at(begun["secret"], mfa.current_step(datetime.now(UTC).timestamp()))
    response = client.post(
        "/api/v1/auth/mfa/enrollment/complete",
        headers=bearer(staff_token),
        json={"code": code, "secret": begun["secret"]},
    )
    assert response.status_code == 200, response.text
    # The live session stays verified; the status read already says enrolled.
    response = client.get("/api/v1/auth/mfa/status", headers=bearer(staff_token))
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": True}

    # The next sign-in owes the challenge, and the gate holds until it passes.
    body = login(client, STAFF_EMAIL, STAFF_PASSWORD)
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "enrolled"
    fresh_token = body["access_token"]
    response = client.get("/api/v1/dashboard", headers=bearer(fresh_token))
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "mfa_verification_required",
        "enrollment_state": "enrolled",
    }

    staff = await session.scalar(select(User).where(User.email == STAFF_EMAIL))
    assert staff is not None
    plaintext = await _plaintext_secret(session, staff)
    challenge = mfa.totp_at(plaintext, mfa.current_step(datetime.now(UTC).timestamp()))
    response = client.post(
        "/api/v1/auth/mfa/verify", headers=bearer(fresh_token), json={"code": challenge}
    )
    assert response.status_code == 200, response.text
    response = client.get("/api/v1/dashboard", headers=bearer(fresh_token))
    assert response.status_code == 200, response.text


# --- the login challenge: verify, replay containment, watermark ---


async def test_wrong_challenge_is_refused_and_nothing_is_written(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin, _secret = await enrolled_admin(session)
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    row = await current_session_row(session, body["access_token"])
    before = await mfa_audit_count(session)

    wrong = mfa.totp_at(
        (await _plaintext_secret(session, admin)), mfa.current_step(NOW.timestamp()) + 4
    )
    with pytest.raises(ValueError, match=mfa.CODE_INVALID):
        await auth.verify_mfa_challenge(
            session, user=admin, code=wrong, now=NOW, auth_session=row
        )

    await session.refresh(row)
    assert row.mfa_verified is False
    await session.refresh(admin)
    assert admin.totp_last_step is None
    assert await mfa_audit_count(session) == before
    # Still gated.
    response = client.get("/api/v1/entitlement", headers=bearer(body["access_token"]))
    assert response.status_code == 403


async def _plaintext_secret(session: AsyncSession, user: User) -> str:
    """Test-only: open the sealed envelope so a code can be computed."""
    from app.application.encryption import decrypt_for_tenant
    from app.domain.encryption import KeyPurpose

    assert user.totp_secret is not None
    return await decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=user.totp_secret
    )


async def test_challenge_replay_is_refused_and_the_watermark_persists_across_sessions(
    session: AsyncSession,
) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin, secret = await enrolled_admin(session)

    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    row = await current_session_row(session, body["access_token"])
    code = mfa.totp_at(secret, mfa.current_step(NOW.timestamp()))
    await auth.verify_mfa_challenge(session, user=admin, code=code, now=NOW, auth_session=row)

    await session.refresh(row)
    assert row.mfa_verified is True
    await session.refresh(admin)
    assert admin.totp_last_step == mfa.current_step(NOW.timestamp())

    # The session is usable once verified.
    response = client.get("/api/v1/entitlement", headers=bearer(body["access_token"]))
    assert response.status_code == 200, response.text

    # The same code on a NEW session is a replay: refused, watermark unmoved.
    second = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    second_row = await current_session_row(session, second["access_token"])
    with pytest.raises(ValueError, match=mfa.CODE_INVALID):
        await auth.verify_mfa_challenge(
            session, user=admin, code=code, now=NOW, auth_session=second_row
        )
    await session.refresh(second_row)
    assert second_row.mfa_verified is False
    await session.refresh(admin)
    assert admin.totp_last_step == mfa.current_step(NOW.timestamp())

    # The next step's code verifies the new session and advances the watermark.
    next_code = mfa.totp_at(secret, mfa.current_step(LATER.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=admin, code=next_code, now=LATER, auth_session=second_row
    )
    await session.refresh(second_row)
    assert second_row.mfa_verified is True
    await session.refresh(admin)
    assert admin.totp_last_step == mfa.current_step(LATER.timestamp())
    response = client.get("/api/v1/entitlement", headers=bearer(second["access_token"]))
    assert response.status_code == 200, response.text


async def test_challenge_refuses_when_not_enrolled(session: AsyncSession) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    row = await current_session_row(session, body["access_token"])

    with pytest.raises(ValueError, match=mfa.NOT_ENROLLED):
        await auth.verify_mfa_challenge(
            session, user=admin, code="123456", now=NOW, auth_session=row
        )
    await session.refresh(row)
    assert row.mfa_verified is False


# --- R5 recovery: clear_mfa ---


async def _create_admin(client: TestClient, token: str, email: str, password: str) -> None:
    response = client.post(
        "/api/v1/users",
        json={
            "email": email,
            "display_name": "Operator Admin",
            "role": "administrator",
            "password": password,
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text


async def test_clear_mfa_clears_audits_and_refuses_the_unenrolled(
    session: AsyncSession,
) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin, secret = await enrolled_admin(session)

    # The operator is a second Administrator acting after their own challenge.
    first_token = await verified_admin_token(client, session, admin, secret)
    await _create_admin(client, first_token, OPERATOR_EMAIL, OPERATOR_PASSWORD)
    operator = await session.scalar(select(User).where(User.email == OPERATOR_EMAIL))
    assert operator is not None

    # Clearing a target with no enrollment refuses, named, nothing written.
    with pytest.raises(ValueError, match=mfa.NOT_ENROLLED):
        await auth.clear_mfa(session, target_user=operator, actor=operator)

    operator_secret, _uri = await auth.begin_mfa_enrollment(session, user=operator)
    operator_code = mfa.totp_at(operator_secret, mfa.current_step(NOW.timestamp()))
    await auth.complete_mfa_enrollment(
        session, user=operator, secret=operator_secret, code=operator_code, now=NOW
    )
    operator_login = login(client, OPERATOR_EMAIL, OPERATOR_PASSWORD)
    operator_row = await current_session_row(session, operator_login["access_token"])
    operator_challenge = mfa.totp_at(operator_secret, mfa.current_step(LATER.timestamp()))
    await auth.verify_mfa_challenge(
        session, user=operator, code=operator_challenge, now=LATER, auth_session=operator_row
    )

    # The real recovery: the operator clears the admin's enrollment.
    await auth.clear_mfa(session, target_user=admin, actor=operator)

    await session.refresh(admin)
    assert admin.totp_secret is None
    assert admin.totp_enrolled_at is None
    assert admin.totp_last_step is None

    entry = await session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.MFA_CLEARED.value)
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    assert entry is not None
    assert entry.actor_user_id == operator.id
    assert entry.details["target_user_id"] == str(admin.id)
    assert secret not in str(entry.details)

    # The cleared admin now logs in as pending and is forced to re-enroll.
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert body["mfa_required"] is True
    assert body["enrollment_state"] == "pending"


async def test_clear_mfa_asserts_the_caller_is_an_administrator(
    session: AsyncSession,
) -> None:
    client = make_client(session)
    await bootstrap(client)
    admin, secret = await enrolled_admin(session)
    admin_token = await verified_admin_token(client, session, admin, secret)
    response = client.post(
        "/api/v1/users",
        json={
            "email": STAFF_EMAIL,
            "display_name": "Staff User",
            "role": "general",
            "password": STAFF_PASSWORD,
            "review_events": True,
        },
        headers=bearer(admin_token),
    )
    assert response.status_code == 201, response.text
    staff = await session.scalar(select(User).where(User.email == STAFF_EMAIL))
    assert staff is not None

    with pytest.raises(ValueError, match="forbidden"):
        await auth.clear_mfa(session, target_user=admin, actor=staff)

    # Nothing was cleared, nothing audited.
    await session.refresh(admin)
    assert admin.totp_secret is not None
    assert await mfa_audit_count(session) == 1  # the enrollment alone
