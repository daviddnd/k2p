"""API-layer tests for the TOTP-MFA surface (phase 7, scope §8).

Hermetic, like ``test_api_auth.py``: no database. The identity rows are plain
ORM objects staged behind a fake session, the presentation seam
(``auth.resolve_session``) is patched to return one of them, and the
application functions are patched where a call would write — so every test
observes exactly the route's mapping: status codes, the named refusal
vocabulary, the caller/secret arguments handed down, and the gate's allowlist
reality. The real end-to-end lifecycle (enrollment → challenge → verified,
replay containment, the DB-backed gate) lives in ``test_mfa_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import AuthSession, User
from app.adapters.persistence.session import get_session
from app.api import deps as api_deps
from app.application import auth as auth_app
from app.domain.rbac import ActorContext, Role
from app.main import create_app

AUTH_HEADER = {"Authorization": "Bearer presented-token"}
ENROLLMENT = "/api/v1/auth/mfa/enrollment"
COMPLETE = "/api/v1/auth/mfa/enrollment/complete"
VERIFY = "/api/v1/auth/mfa/verify"
STATUS = "/api/v1/auth/mfa/status"
CLEAR = "/api/v1/auth/mfa/clear"


class FakeSession:
    """The identity rows a test stages, addressable by primary key."""

    def __init__(self, rows: dict[UUID, User]) -> None:
        self.rows = rows

    async def get(self, entity: type[object], ident: UUID) -> object | None:
        if entity is not User:
            return None
        return self.rows.get(ident)


def make_user(
    *,
    role: str = "administrator",
    totp_secret: str | None = None,
    user_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> User:
    return User(
        id=user_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        email="admin@tcsp.example",
        display_name="A. Secretary",
        role=role,
        review_events=True,
        approve_events=True,
        review_drafts=True,
        approve_drafts=True,
        is_active=True,
        totp_secret=totp_secret,
    )


def make_auth_session(*, user: User, mfa_verified: bool) -> AuthSession:
    return AuthSession(
        user_id=user.id,
        tenant_id=user.tenant_id,
        token_digest="digest",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        mfa_verified=mfa_verified,
    )


def build_client(rows: dict[UUID, User]) -> TestClient:
    app: FastAPI = create_app()
    fake = FakeSession(rows)

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield fake  # type: ignore[misc]  # the stand-in implements the used seam

    app.dependency_overrides[get_session] = fake_session
    return TestClient(app, raise_server_exceptions=False)


def stage_presentation(
    monkeypatch: pytest.MonkeyPatch, *, auth_session: AuthSession | None
) -> None:
    async def fake_resolve(
        session: AsyncSession, *, token: str
    ) -> AuthSession | None:
        return auth_session

    monkeypatch.setattr(auth_app, "resolve_session", fake_resolve)


def stage_admin_context(monkeypatch: pytest.MonkeyPatch, *, administrator: bool) -> None:
    async def fake_load(
        session: AsyncSession, *, user_id: UUID, tenant_id: UUID
    ) -> ActorContext:
        return ActorContext(
            is_active=True,
            role=Role.ADMINISTRATOR if administrator else Role.GENERAL,
            permissions=frozenset(),
            company_ids=frozenset(),
        )

    monkeypatch.setattr(api_deps, "load_actor_context", fake_load)


@pytest.fixture
def admin_setup(monkeypatch: pytest.MonkeyPatch):
    """A staged authenticated presentation plus the RBAC context.

    ``role`` names the staged account's role (the user row and the context
    agree); slice ① opened the enrollment/verify/status surface to General
    staff, so most tests still stage the Administrator while the staff tests
    pass ``role="general"``.
    """

    def setup(
        *,
        role: str = "administrator",
        totp_secret: str | None = None,
        mfa_verified: bool = True,
    ):
        user = make_user(role=role, totp_secret=totp_secret)
        row = make_auth_session(user=user, mfa_verified=mfa_verified)
        client = build_client({user.id: user})
        stage_presentation(monkeypatch, auth_session=row)
        stage_admin_context(monkeypatch, administrator=role == "administrator")
        return client, user, row

    return setup


# --- POST /api/v1/auth/mfa/enrollment: begin -------------------------------


def test_enrollment_begin_returns_the_secret_once(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, user, _row = admin_setup()
    captured: dict[str, object] = {}

    async def fake_begin(session: AsyncSession, *, user: User) -> tuple[str, str]:
        captured["user"] = user
        return ("JBSWY3DPEHPK3PXP", "otpauth://totp/K2P:admin%40tcsp.example?secret=JBSWY3DPEHPK3PXP")

    monkeypatch.setattr(auth_app, "begin_mfa_enrollment", fake_begin)
    response = client.post(ENROLLMENT, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {
        "secret": "JBSWY3DPEHPK3PXP",
        "uri": "otpauth://totp/K2P:admin%40tcsp.example?secret=JBSWY3DPEHPK3PXP",
        "enrollment_state": "pending",
    }
    assert captured["user"] is user


def test_enrollment_begin_requires_authentication(admin_setup) -> None:
    client, _user, _row = admin_setup()
    response = client.post(ENROLLMENT)
    assert response.status_code == 401


def test_enrollment_begin_is_open_to_general_staff(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    """Slice ①: binding an authenticator is staff self-service, not admin-only."""
    client, user, _row = admin_setup(role="general")
    captured: dict[str, object] = {}

    async def fake_begin(session: AsyncSession, *, user: User) -> tuple[str, str]:
        captured["user"] = user
        return ("JBSWY3DPEHPK3PXP", "otpauth://totp/K2P:x?secret=JBSWY3DPEHPK3PXP")

    monkeypatch.setattr(auth_app, "begin_mfa_enrollment", fake_begin)
    response = client.post(ENROLLMENT, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {
        "secret": "JBSWY3DPEHPK3PXP",
        "uri": "otpauth://totp/K2P:x?secret=JBSWY3DPEHPK3PXP",
        "enrollment_state": "pending",
    }
    assert captured["user"] is user


# --- POST /api/v1/auth/mfa/enrollment/complete -----------------------------


def test_enrollment_complete_hands_back_the_caller_held_secret(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, user, _row = admin_setup()
    captured: dict[str, object] = {}

    async def fake_complete(
        session: AsyncSession,
        *,
        user: User,
        secret: str,
        code: str,
        now: datetime,
    ) -> None:
        captured.update(user=user, secret=secret, code=code, now=now)

    monkeypatch.setattr(auth_app, "complete_mfa_enrollment", fake_complete)
    response = client.post(
        COMPLETE, headers=AUTH_HEADER, json={"code": "123456", "secret": "JBSWY3DPEHPK3PXP"}
    )
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled"}
    assert captured["user"] is user
    assert captured["secret"] == "JBSWY3DPEHPK3PXP"
    assert captured["code"] == "123456"
    assert captured["now"].tzinfo is not None  # type: ignore[union-attr]


@pytest.mark.parametrize("code", ["000000", "12", "abcdef"])
def test_enrollment_complete_one_refusal_for_wrong_and_malformed(
    monkeypatch: pytest.MonkeyPatch, admin_setup, code: str
) -> None:
    client, _user, _row = admin_setup()

    async def fake_complete(
        session: AsyncSession, *, user: User, secret: str, code: str, now: datetime
    ) -> None:
        raise ValueError("totp_invalid")

    monkeypatch.setattr(auth_app, "complete_mfa_enrollment", fake_complete)
    response = client.post(
        COMPLETE, headers=AUTH_HEADER, json={"code": code, "secret": "JBSWY3DPEHPK3PXP"}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "totp_invalid"}


def test_enrollment_complete_malformed_secret_is_the_same_refusal(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup()

    async def fake_complete(
        session: AsyncSession, *, user: User, secret: str, code: str, now: datetime
    ) -> None:
        raise ValueError("totp_secret_malformed")

    monkeypatch.setattr(auth_app, "complete_mfa_enrollment", fake_complete)
    response = client.post(COMPLETE, headers=AUTH_HEADER, json={"code": "123456", "secret": "!!"})
    assert response.status_code == 422
    assert response.json() == {"detail": "totp_invalid"}


def test_enrollment_complete_already_enrolled(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup()

    async def fake_complete(
        session: AsyncSession, *, user: User, secret: str, code: str, now: datetime
    ) -> None:
        raise ValueError("mfa_already_enrolled")

    monkeypatch.setattr(auth_app, "complete_mfa_enrollment", fake_complete)
    response = client.post(
        COMPLETE, headers=AUTH_HEADER, json={"code": "123456", "secret": "JBSWY3DPEHPK3PXP"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "mfa_already_enrolled"}


def test_enrollment_complete_requires_a_code(admin_setup) -> None:
    client, _user, _row = admin_setup()
    response = client.post(COMPLETE, headers=AUTH_HEADER, json={"secret": "JBSWY3DPEHPK3PXP"})
    assert response.status_code == 422


# --- POST /api/v1/auth/mfa/verify: the login challenge ---------------------


def test_verify_marks_the_presented_session_verified(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, user, row = admin_setup(totp_secret="sealed", mfa_verified=False)
    captured: dict[str, object] = {}

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        captured.update(user=user, code=code, auth_session=auth_session)
        auth_session.mfa_verified = True

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"})
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": True}
    assert captured["user"] is user
    assert captured["auth_session"] is row
    assert row.mfa_verified is True


def test_verify_refused_code_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, row = admin_setup(totp_secret="sealed", mfa_verified=False)

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        raise ValueError("totp_invalid")

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"})
    assert response.status_code == 422
    assert response.json() == {"detail": "totp_invalid"}
    assert row.mfa_verified is False


def test_verify_already_verified_session_is_not_required(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=True)
    called = False

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"})
    assert response.status_code == 409
    assert response.json() == {"detail": "mfa_not_required"}
    assert called is False


def test_verify_unenrolled_account(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup(totp_secret=None, mfa_verified=False)

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        raise ValueError("mfa_not_enrolled")

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"})
    assert response.status_code == 409
    assert response.json() == {"detail": "mfa_not_enrolled"}


# --- GET /api/v1/auth/mfa/status --------------------------------------------


def test_status_enrolled_admin_awaiting_challenge(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=False)
    response = client.get(STATUS, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": False}


def test_status_pending_admin_in_forced_enrollment(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret=None, mfa_verified=False)
    response = client.get(STATUS, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "pending", "mfa_verified": False}


def test_status_verified_administrator(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=True)
    response = client.get(STATUS, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": True}


# --- POST /api/v1/auth/mfa/clear: the R5 recovery ---------------------------


def test_clear_recovery_clears_the_target(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    _client, actor, _row = admin_setup()
    target = make_user(tenant_id=actor.tenant_id)
    client_app = build_client({actor.id: actor, target.id: target})
    captured: dict[str, object] = {}

    async def fake_clear(session: AsyncSession, *, target_user: User, actor: User) -> None:
        captured.update(target_user=target_user, actor=actor)

    monkeypatch.setattr(auth_app, "clear_mfa", fake_clear)
    response = client_app.post(CLEAR, headers=AUTH_HEADER, json={"user_id": str(target.id)})
    assert response.status_code == 200
    assert response.json() == {"cleared": True}
    assert captured["target_user"] is target
    assert captured["actor"] is actor


def test_clear_unknown_or_cross_tenant_target_is_404(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _actor, _row = admin_setup()
    elsewhere = make_user()  # a different tenant, not staged for this actor
    called = False

    async def fake_clear(session: AsyncSession, *, target_user: User, actor: User) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(auth_app, "clear_mfa", fake_clear)
    response = client.post(CLEAR, headers=AUTH_HEADER, json={"user_id": str(elsewhere.id)})
    assert response.status_code == 404
    assert called is False


def test_clear_target_not_enrolled(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    _client, actor, _row = admin_setup()
    target = make_user(tenant_id=actor.tenant_id)
    client_app = build_client({actor.id: actor, target.id: target})

    async def fake_clear(session: AsyncSession, *, target_user: User, actor: User) -> None:
        raise ValueError("mfa_not_enrolled")

    monkeypatch.setattr(auth_app, "clear_mfa", fake_clear)
    response = client_app.post(CLEAR, headers=AUTH_HEADER, json={"user_id": str(target.id)})
    assert response.status_code == 409
    assert response.json() == {"detail": "mfa_not_enrolled"}


def test_clear_general_staff_refused(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup()
    stage_admin_context(monkeypatch, administrator=False)
    response = client.post(CLEAR, headers=AUTH_HEADER, json={"user_id": str(uuid4())})
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


# --- The gate's allowlist reality -------------------------------------------


def test_unverified_admin_reaches_the_allowlisted_surface(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=False)

    async def fake_begin(session: AsyncSession, *, user: User) -> tuple[str, str]:
        return ("JBSWY3DPEHPK3PXP", "otpauth://totp/K2P:x?secret=JBSWY3DPEHPK3PXP")

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        auth_session.mfa_verified = True

    monkeypatch.setattr(auth_app, "begin_mfa_enrollment", fake_begin)
    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)

    assert client.post(ENROLLMENT, headers=AUTH_HEADER).status_code == 200
    assert client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"}).status_code == 200
    assert client.get(STATUS, headers=AUTH_HEADER).status_code == 200


def test_unverified_admin_refused_off_the_allowlist(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=False)
    response = client.get("/api/v1/entitlement", headers=AUTH_HEADER)
    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": "mfa_verification_required", "enrollment_state": "enrolled"}
    }


def test_unverified_admin_refused_on_clear(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=False)
    response = client.post(CLEAR, headers=AUTH_HEADER, json={"user_id": str(uuid4())})
    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": "mfa_verification_required", "enrollment_state": "enrolled"}
    }


def test_unenrolled_admin_gate_names_pending(admin_setup) -> None:
    client, _user, _row = admin_setup(totp_secret=None, mfa_verified=False)
    response = client.get("/api/v1/entitlement", headers=AUTH_HEADER)
    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": "mfa_verification_required", "enrollment_state": "pending"}
    }


# --- Slice ① (2026-10-11): the MFA surface is open to General staff ---------


def test_enrollment_complete_is_open_to_general_staff(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, user, _row = admin_setup(role="general")
    captured: dict[str, object] = {}

    async def fake_complete(
        session: AsyncSession, *, user: User, secret: str, code: str, now: datetime
    ) -> None:
        captured.update(user=user, secret=secret, code=code)

    monkeypatch.setattr(auth_app, "complete_mfa_enrollment", fake_complete)
    response = client.post(
        COMPLETE, headers=AUTH_HEADER, json={"code": "123456", "secret": "JBSWY3DPEHPK3PXP"}
    )
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled"}
    assert captured["user"] is user


def test_verify_challenge_is_open_to_enrolled_general_staff(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    """The login challenge an enrolled General staff session owes (slice ①):
    the same route, the same shape, the presented session marked verified."""
    client, user, row = admin_setup(role="general", totp_secret="sealed", mfa_verified=False)
    captured: dict[str, object] = {}

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        captured.update(user=user, code=code, auth_session=auth_session)
        auth_session.mfa_verified = True

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"})
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": True}
    assert captured["user"] is user
    assert captured["auth_session"] is row
    assert row.mfa_verified is True


def test_status_enrolled_general_staff(admin_setup) -> None:
    client, _user, _row = admin_setup(role="general", totp_secret="sealed", mfa_verified=True)
    response = client.get(STATUS, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "enrolled", "mfa_verified": True}


def test_status_unenrolled_general_staff_is_not_required(admin_setup) -> None:
    client, _user, _row = admin_setup(role="general")
    response = client.get(STATUS, headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"enrollment_state": "not_required", "mfa_verified": True}


def test_unverified_enrolled_general_staff_refused_off_the_allowlist(admin_setup) -> None:
    """Slice ①'s security half: an unverified General session is restricted
    exactly like an Administrator's — nothing off the allowlist."""
    client, _user, _row = admin_setup(role="general", totp_secret="sealed", mfa_verified=False)
    response = client.get("/api/v1/entitlement", headers=AUTH_HEADER)
    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": "mfa_verification_required", "enrollment_state": "enrolled"}
    }


def test_unverified_enrolled_general_staff_reaches_the_allowlisted_surface(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    client, _user, _row = admin_setup(role="general", totp_secret="sealed", mfa_verified=False)

    async def fake_begin(session: AsyncSession, *, user: User) -> tuple[str, str]:
        return ("JBSWY3DPEHPK3PXP", "otpauth://totp/K2P:x?secret=JBSWY3DPEHPK3PXP")

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        auth_session.mfa_verified = True

    monkeypatch.setattr(auth_app, "begin_mfa_enrollment", fake_begin)
    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)

    assert client.post(ENROLLMENT, headers=AUTH_HEADER).status_code == 200
    assert client.post(VERIFY, headers=AUTH_HEADER, json={"code": "123456"}).status_code == 200
    assert client.get(STATUS, headers=AUTH_HEADER).status_code == 200


# --- The challenge is throttled like the login (review fix pass, 2026-10-12) -


def test_verify_challenge_is_throttled_after_repeated_refusals(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    """The TOTP challenge is online-brute-forceable without a throttle (each
    attempt costs one HMAC, not one scrypt): beyond the seam's threshold the
    challenge is refused before any verification work, same uniform 429."""
    client, _user, _row = admin_setup(totp_secret="sealed", mfa_verified=False)
    calls = {"count": 0}

    async def fake_verify(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        calls["count"] += 1
        raise ValueError("totp_invalid")

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", fake_verify)
    for _ in range(5):
        response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"})
        assert response.status_code == 422
        assert response.json() == {"detail": "totp_invalid"}

    refused = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"})
    assert refused.status_code == 429
    assert refused.json() == {"detail": "too_many_attempts"}
    assert refused.headers["Retry-After"] == "2"
    # The refusal answers before the verification work: five checks, not six.
    assert calls["count"] == 5


def test_verify_success_clears_the_challenge_throttle(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    """A successful challenge clears the account ledger, so a still-unverified
    sibling session of the same account gets a fresh count — observed across
    two sessions because a verified one is never re-challenged (409)."""
    client, user, row = admin_setup(totp_secret="sealed", mfa_verified=False)
    second = make_auth_session(user=user, mfa_verified=False)
    by_token = {"presented-token": row, "second-token": second}

    async def fake_resolve(
        session: AsyncSession, *, token: str
    ) -> AuthSession | None:
        return by_token.get(token)

    monkeypatch.setattr(auth_app, "resolve_session", fake_resolve)

    async def failing(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        raise ValueError("totp_invalid")

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", failing)
    for _ in range(4):
        assert client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"}).status_code == 422

    async def succeeding(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        auth_session.mfa_verified = True

    monkeypatch.setattr(auth_app, "verify_mfa_challenge", succeeding)
    second_header = {"Authorization": "Bearer second-token"}
    ok = client.post(VERIFY, headers=second_header, json={"code": "123456"})
    assert ok.status_code == 200
    assert second.mfa_verified is True

    # The success washed the account's slate: the first session's next wrong
    # code is the ordinary 422, not a 429 stacked on the old four.
    monkeypatch.setattr(auth_app, "verify_mfa_challenge", failing)
    response = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"})
    assert response.status_code == 422
    assert response.json() == {"detail": "totp_invalid"}


def test_the_gate_allowlist_honours_path_boundaries() -> None:
    """A prefix entry admits itself and its sub-paths only — a future sibling
    route must never be silently allowlisted by string coincidence."""
    from app.api import deps

    assert deps._path_allowed("/api/v1/auth/login")
    assert not deps._path_allowed("/api/v1/auth/loginx")
    assert deps._path_allowed("/api/v1/auth/mfa/enrollment")
    assert deps._path_allowed("/api/v1/auth/mfa/enrollment/complete")
    assert deps._path_allowed("/api/v1/auth/mfa/verify")
    assert deps._path_allowed("/api/v1/auth/mfa/status")
    assert not deps._path_allowed("/api/v1/auth/mfa/clear")
    assert not deps._path_allowed("/api/v1/entitlement")


def test_verify_runs_under_an_account_only_lockout(
    monkeypatch: pytest.MonkeyPatch, admin_setup
) -> None:
    """The founder ruling applies to the challenge too: five wrong codes on
    one session heat the account AND that session; a fresh session of the
    same account still reaches the verification work — a correct code
    passes and clears."""
    client, user, row = admin_setup(totp_secret="sealed", mfa_verified=False)
    second = make_auth_session(user=user, mfa_verified=False)
    second.token_digest = "digest-second"  # the throttle keys on the digest
    by_token = {"presented-token": row, "second-token": second}
    calls = {"count": 0}

    async def fake_resolve(session: AsyncSession, *, token: str) -> AuthSession | None:
        return by_token.get(token)

    async def failing(
        session: AsyncSession,
        *,
        user: User,
        code: str,
        now: datetime,
        auth_session: AuthSession,
    ) -> None:
        calls["count"] += 1
        raise ValueError("totp_invalid")

    monkeypatch.setattr(auth_app, "resolve_session", fake_resolve)
    monkeypatch.setattr(auth_app, "verify_mfa_challenge", failing)
    for _ in range(5):
        assert client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"}).status_code == 422
    # Both the session and the account keys are hot: refused before work.
    refused = client.post(VERIFY, headers=AUTH_HEADER, json={"code": "999999"})
    assert refused.status_code == 429
    assert calls["count"] == 5

    # A fresh session of the locked account: the verification runs (422, counted).
    second_header = {"Authorization": "Bearer second-token"}
    reached = client.post(VERIFY, headers=second_header, json={"code": "999999"})
    assert reached.status_code == 422
    assert calls["count"] == 6
