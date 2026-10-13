"""API-layer tests for the login throttle (slice ②, 2026-10-11).

Hermetic, like ``test_api_auth.py``: no database — ``app.application.auth.login``
is patched to fail or succeed, so every test observes exactly the route's
throttle wiring: the 429 + ``Retry-After`` shape, the named uniform refusal,
the per-source and per-account keys, the X-Forwarded-For source parse, and
the success path. The backoff schedule itself is the domain suite's job
(``test_login_throttle_domain.py``).

The throttle registry is process-wide, so each test clears it on both sides;
emails are unique per test on top, so no heat leaks into other suites.
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

LOGIN = "/api/v1/auth/login"
BURST_EMAIL = "throttle-burst@tcsp.example"
OTHER_EMAIL = "throttle-other@tcsp.example"
PAYLOAD = {"email": BURST_EMAIL, "password": "anything"}


def build_client() -> TestClient:
    app: FastAPI = create_app()

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the patched login never reads a session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app, raise_server_exceptions=False)


def stage_login_outcome(monkeypatch: pytest.MonkeyPatch, *, succeed: bool) -> dict[str, int]:
    """Patch ``auth.login`` to fail or succeed; returns a call counter."""
    calls = {"count": 0}

    async def fake_login(
        session: AsyncSession, *, email: str, password: str
    ) -> tuple[str, User, AuthSession] | None:
        calls["count"] += 1
        if not succeed:
            return None
        user = User(
            id=uuid4(),
            tenant_id=uuid4(),
            email=email,
            display_name="Test User",
            role="general",
            review_events=False,
            approve_events=False,
            review_drafts=False,
            approve_drafts=False,
            is_active=True,
        )
        auth_session = AuthSession(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_digest="digest",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mfa_verified=True,
        )
        return "token", user, auth_session

    monkeypatch.setattr(auth_app, "login", fake_login)
    return calls


def test_burst_refuses_with_the_named_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client()
    calls = stage_login_outcome(monkeypatch, succeed=False)

    for _ in range(5):
        assert client.post(LOGIN, json=PAYLOAD).status_code == 401
    refused = client.post(LOGIN, json=PAYLOAD)

    assert refused.status_code == 429
    assert refused.json() == {"detail": "too_many_attempts"}
    assert refused.headers["Retry-After"] == "2"
    # The refusal answers before any credential work: five checks, not six.
    assert calls["count"] == 5


def test_refused_attempts_do_not_extend_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client()
    calls = stage_login_outcome(monkeypatch, succeed=False)

    for _ in range(5):
        client.post(LOGIN, json=PAYLOAD)
    first = client.post(LOGIN, json=PAYLOAD)
    second = client.post(LOGIN, json=PAYLOAD)

    assert first.status_code == 429
    assert second.status_code == 429
    # Pumping the locked endpoint records nothing: the wait does not grow.
    assert second.headers["Retry-After"] == first.headers["Retry-After"]
    assert calls["count"] == 5


def test_success_path_is_unaffected_and_clears_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client()
    stage_login_outcome(monkeypatch, succeed=False)
    for _ in range(4):
        assert client.post(LOGIN, json=PAYLOAD).status_code == 401

    stage_login_outcome(monkeypatch, succeed=True)
    ok = client.post(LOGIN, json=PAYLOAD)
    assert ok.status_code == 200
    assert ok.json()["mfa_required"] is False

    # The success washed the slate: one fresh failure is the ordinary 401,
    # not a 429 — the count restarted instead of stacking on the old four.
    stage_login_outcome(monkeypatch, succeed=False)
    assert client.post(LOGIN, json=PAYLOAD).status_code == 401


def test_the_refusal_fires_only_when_both_keys_are_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-10-12 founder ruling: a single hot key never locks anyone
    out — the attempt reaches the credential check, and a correct password
    signs in and clears both ledgers. The pre-work 429 fires only when the
    source AND the account keys are both inside their backoff."""
    client = build_client()
    calls = stage_login_outcome(monkeypatch, succeed=False)
    other = {"email": OTHER_EMAIL, "password": "anything"}

    for _ in range(5):
        client.post(LOGIN, json=PAYLOAD)
    # Both keys hot → refused before work, one uniform body.
    assert client.post(LOGIN, json=PAYLOAD).status_code == 429
    assert calls["count"] == 5

    fresh_source = {"X-Forwarded-For": "203.0.113.9"}
    # Hot account, fresh source: the check runs (401, counted) — no 429.
    hot_account = client.post(LOGIN, json=PAYLOAD, headers=fresh_source)
    assert hot_account.status_code == 401
    assert calls["count"] == 6
    # Hot source (five failures spread over five accounts), fresh account:
    # the office-egress shape — the check runs here too.
    for index in range(5):
        client.post(LOGIN, json={"email": f"user-{index}@tcsp.example", "password": "x"})
    hot_source = client.post(LOGIN, json=other)
    assert hot_source.status_code == 401
    assert calls["count"] == 12
    # Both fresh: ordinary 401 as always.
    assert client.post(LOGIN, json=other, headers=fresh_source).status_code == 401
    assert calls["count"] == 13


def test_a_correct_password_escapes_an_account_lockout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the ruling: while an attacker keeps an account's ledger
    hot, the victim's correct password from a fresh source still signs in
    and washes the slate — no indefinite lockout, no operator step."""
    client = build_client()
    stage_login_outcome(monkeypatch, succeed=False)
    # The attack: five failures, both keys hot, kept hot by the 6th's refusal.
    for _ in range(5):
        assert client.post(LOGIN, json=PAYLOAD).status_code == 401
    assert client.post(LOGIN, json=PAYLOAD).status_code == 429

    # The victim, same account, a different source, the RIGHT password.
    victim_source = {"X-Forwarded-For": "198.51.100.77"}
    stage_login_outcome(monkeypatch, succeed=True)
    ok = client.post(LOGIN, json=PAYLOAD, headers=victim_source)
    assert ok.status_code == 200
    assert ok.json()["mfa_required"] is False

    # The slate washed: a fresh failure from the victim's source is a new
    # first count, not a 429 stacked on the attack.
    stage_login_outcome(monkeypatch, succeed=False)
    again = client.post(LOGIN, json=PAYLOAD, headers=victim_source)
    assert again.status_code == 401


def test_the_source_key_is_the_forwarded_for_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """nginx appends the observed peer, so the right-most entry is the only
    trustworthy one; a client-supplied left part must not dilute the key."""
    client = build_client()
    stage_login_outcome(monkeypatch, succeed=False)
    behind_nginx = {"X-Forwarded-For": "9.9.9.9, 203.0.113.7"}

    for _ in range(5):
        client.post(LOGIN, json=PAYLOAD, headers=behind_nginx)
    assert client.post(LOGIN, json=PAYLOAD, headers=behind_nginx).status_code == 429

    # Same account, same right-most peer, spoofed left part → same key, 429.
    spoofed_left = {"X-Forwarded-For": "198.51.100.3, 203.0.113.7"}
    assert client.post(LOGIN, json=PAYLOAD, headers=spoofed_left).status_code == 429

    # The keyed source was the forwarded tail, not the testclient peer: a
    # fresh account without the header (bare peer source) is allowed through.
    fresh = {"email": OTHER_EMAIL, "password": "anything"}
    assert client.post(LOGIN, json=fresh).status_code == 401
