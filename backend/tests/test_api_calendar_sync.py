"""API-layer tests for the calendar-feed routes (scope §2.4; ADR-0013 item 3;
contract §5.1).

Without a database: the auth seam, the RBAC context, and the session are
overridden and the application services are monkeypatched, so what is
exercised here is fail-closed authentication, the Administrator-only
management routes, the one-time plaintext (the create response carries it;
the list response has no field that could), the feed's refusal statuses and
its content type. The gating, isolation, revocation, and audit behaviour
are database scenarios in ``test_calendar_feed_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import calendar_sync
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import GatingReason
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
CREDENTIALS_URL = "/api/v1/calendar/feed-credentials"
FEED_URL = "/api/v1/calendar/feed.ics"
TOKEN = "test-token-not-a-real-secret"

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

MANAGEMENT_ROUTES = [
    ("POST", CREDENTIALS_URL),
    ("GET", CREDENTIALS_URL),
    ("POST", f"{CREDENTIALS_URL}/{uuid4()}/revoke"),
]


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def credential_view(**overrides: object) -> calendar_sync.FeedCredentialView:
    values: dict[str, object] = {
        "id": uuid4(),
        "created_by": ADMIN.user_id,
        "created_at": datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
        "revoked_at": None,
    }
    values.update(overrides)
    return calendar_sync.FeedCredentialView(**values)  # type: ignore[arg-type]


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> calendar_sync.FeedCredentialView:
    """A create call that succeeds, returning a known credential."""
    view = credential_view()
    issued = calendar_sync.IssuedFeedCredential(credential=view, token=TOKEN)

    async def fake_create(
        session: AsyncSession, *, tenant_id: object, actor_user_id: object
    ) -> calendar_sync.IssuedFeedCredential:
        return issued

    monkeypatch.setattr(calendar_sync, "create_credential", fake_create)
    return view


@pytest.fixture
def empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed that builds successfully and projects nothing."""

    async def fake_build(
        session: AsyncSession, *, token: str, as_of: date
    ) -> calendar_sync.CalendarFeed:
        return calendar_sync.CalendarFeed(
            as_of=as_of,
            window_from=as_of,
            window_to=as_of,
            events=(),
            refusals=(),
        )

    monkeypatch.setattr(calendar_sync, "build_feed", fake_build)


# --- Fail closed ---


def test_management_routes_need_a_session() -> None:
    client = build_client()
    for method, url in MANAGEMENT_ROUTES:
        assert client.request(method, url).status_code == 401, url


@pytest.mark.parametrize("context", [GENERAL_CONTEXT, INACTIVE_ADMIN_CONTEXT])
def test_management_routes_are_administrator_only(context: ActorContext) -> None:
    client = build_client(actor=ADMIN, context=context)
    for method, url in MANAGEMENT_ROUTES:
        assert client.request(method, url).status_code == 403, url


def test_the_feed_itself_needs_no_session_assumptions() -> None:
    """Unauthenticated by design: no Authorization header, and no 401 for
    *that* reason — only for a token it cannot resolve."""
    client = build_client()
    response = client.get(FEED_URL)
    assert response.status_code == 401
    assert "token" in response.json()["detail"]


# --- The credential's one-time plaintext ---


def test_create_returns_the_subscription_url_once(
    created: calendar_sync.FeedCredentialView,
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(CREDENTIALS_URL)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(created.id)
    assert body["subscription_url"] == f"http://testserver{FEED_URL}?token={TOKEN}"
    assert body["revoked_at"] is None
    assert body["created_by"] == str(ADMIN.user_id)


def test_create_refuses_a_second_active_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: AsyncSession, **kwargs: object) -> object:
        raise calendar_sync.ActiveFeedCredentialExistsError

    monkeypatch.setattr(calendar_sync, "create_credential", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(CREDENTIALS_URL)
    assert response.status_code == 409
    assert "revoke" in response.json()["detail"]


def test_the_listing_carries_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    view = credential_view()

    async def fake_list(session: AsyncSession, *, tenant_id: object) -> list[object]:
        return [view]

    monkeypatch.setattr(calendar_sync, "list_credentials", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(CREDENTIALS_URL)
    assert response.status_code == 200, response.text
    credentials = response.json()
    assert [item["id"] for item in credentials] == [str(view.id)]
    assert set(credentials[0]) == {"id", "created_by", "created_at", "revoked_at"}
    assert TOKEN not in response.text


def test_revoking_an_unknown_credential_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_revoke(session: AsyncSession, **kwargs: object) -> object:
        raise calendar_sync.FeedCredentialNotFoundError

    monkeypatch.setattr(calendar_sync, "revoke_credential", fake_revoke)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(f"{CREDENTIALS_URL}/{uuid4()}/revoke")
    assert response.status_code == 404


def test_revoke_returns_the_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    view = credential_view(revoked_at=datetime(2026, 9, 21, 11, 0, tzinfo=UTC))

    async def fake_revoke(session: AsyncSession, **kwargs: object) -> object:
        return view

    monkeypatch.setattr(calendar_sync, "revoke_credential", fake_revoke)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(f"{CREDENTIALS_URL}/{view.id}/revoke")
    assert response.status_code == 200, response.text
    assert response.json()["revoked_at"] == "2026-09-21T11:00:00Z"


# --- The feed response ---


def test_an_unknown_or_revoked_token_is_a_401(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(session: AsyncSession, **kwargs: object) -> object:
        raise calendar_sync.InvalidFeedTokenError

    monkeypatch.setattr(calendar_sync, "build_feed", fake_build)
    client = build_client()
    response = client.get(f"{FEED_URL}?token={TOKEN}")
    assert response.status_code == 401
    assert response.json()["detail"] == "unknown or revoked feed token"


def test_an_empty_token_is_a_401() -> None:
    client = build_client()
    assert client.get(FEED_URL).status_code == 401
    assert client.get(f"{FEED_URL}?token=").status_code == 401


def test_a_denied_feed_carries_the_gate_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(session: AsyncSession, **kwargs: object) -> object:
        raise calendar_sync.AutomationDeniedError(GatingReason.TIER_CAPABILITY_MISSING)

    monkeypatch.setattr(calendar_sync, "build_feed", fake_build)
    client = build_client()
    response = client.get(f"{FEED_URL}?token={TOKEN}")
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: tier_capability_missing"


def test_the_feed_is_served_as_calendar_text_and_never_cached(
    empty_feed: None,
) -> None:
    client = build_client()
    response = client.get(f"{FEED_URL}?token={TOKEN}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith("BEGIN:VCALENDAR\r\n")
    assert response.text.endswith("END:VCALENDAR\r\n")


def test_the_feed_body_is_rendered_through_the_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(
        session: AsyncSession, *, token: str, as_of: date
    ) -> calendar_sync.CalendarFeed:
        return calendar_sync.CalendarFeed(
            as_of=as_of,
            window_from=as_of,
            window_to=as_of,
            events=(
                calendar_sync.IcsEvent(
                    date=date(2026, 10, 1),
                    uid="nar1_due-company-2026@aics.compliance.local",
                    summary="NAR1 annual return due: Acme Limited",
                    description="…",
                    alarms=(),
                ),
            ),
            refusals=(
                calendar_sync.FeedRefusal(
                    company_id=uuid4(), company_type="public", reason="nar1_rule_not_established"
                ),
            ),
        )

    monkeypatch.setattr(calendar_sync, "build_feed", fake_build)
    client = build_client()
    body = client.get(f"{FEED_URL}?token={TOKEN}").text
    assert "SUMMARY:NAR1 annual return due: Acme Limited" in body
    assert "DTSTART;VALUE=DATE:20261001" in body
    # Folding and CRLF survive the HTTP layer untouched — the refusal line is
    # long enough to fold, so read it unfolded.
    assert body.count("\n") == body.count("\r\n")
    unfolded = body.replace("\r\n ", "")
    assert "X-AICS-NOT-GENERATED:" in unfolded
    assert "nar1_rule_not_established" in unfolded
