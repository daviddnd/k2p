"""API-layer tests for the reply-parser cycle route (slice M5b; contract §5.1.13).

No database and no provider: the auth seam, RBAC context, session and storage
are overridden, and the cycle is monkeypatched, so what runs is the fail-closed
authentication, the single Administrator-only check, the report mapping, and
the refusal taxonomy — every per-message outcome the cycle can produce is a
report row, never an HTTP error for one message. The real cycle against
PostgreSQL — a parsed reply with its proposals, a gate denial, a gateway
refusal — lives in ``test_reply_parsing_scenarios_db.py``.

The mapping is the point: a parse report carries ids, counts and the cycle's
own bounded outcome strings — never a span of the client's words, never a body.
"""

from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import reply_parsing
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

PARSE_URL = "/api/v1/mail/parse-replies"

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),  # even holding every grantable permission
    company_ids=frozenset(),
)


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


class FakeSession:
    """The route's session seam: the cycle runs, the dependency commits."""

    async def commit(self) -> None:
        return None


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield FakeSession()  # type: ignore[misc]  # the cycle is monkeypatched here

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def report() -> reply_parsing.ParseCycleReport:
    """One cycle's worth of every outcome family — the taxonomy a report must
    carry as data: parsed, refused, denied, skipped, invalid, unavailable and
    a letter that failed after its parse landed."""
    company_id = uuid4()
    parse_id = uuid4()
    event_ids = (uuid4(), uuid4())
    return reply_parsing.ParseCycleReport(
        examined=7,
        parsed=1,
        outcomes=(
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="parsed",
                parse_id=parse_id,
                status="changes_reported",
                routing="auto_accept",
                event_ids=event_ids,
                withdrawn_event_ids=(uuid4(),),
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="denied:tier_capability_missing",
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="refused:ai_disabled",
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="skipped:document_unavailable",
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="skipped:no_reply_text",
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=uuid4(),
                outcome="invalid",
                routing="staff_review",
            ),
            reply_parsing.ParseReport(
                communication_id=uuid4(),
                company_id=company_id,
                outcome="letter_failed:no_confirmation_request",
                parse_id=uuid4(),
                status="changes_reported",
                routing="client_clarification",
            ),
        ),
    )


def test_the_route_is_administrator_only() -> None:
    assert build_client().post(PARSE_URL).status_code == 401
    assert (
        build_client(actor=ADMIN, context=GENERAL_CONTEXT).post(PARSE_URL).status_code == 403
    )


def test_the_route_reports_the_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts and the cycle's own vocabulary, one entry per reply — a refused,
    denied, skipped, invalid or letter-failed message is a row in this report,
    never an HTTP error the whole cycle answers."""
    expected = report()

    async def fake_cycle(*args: object, **kwargs: object) -> reply_parsing.ParseCycleReport:
        return expected

    monkeypatch.setattr(reply_parsing, "run_cycle", fake_cycle)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(PARSE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["parsed"]) == (7, 1)
    assert len(body["outcomes"]) == 7
    (parsed,) = [item for item in body["outcomes"] if item["outcome"] == "parsed"]
    assert parsed["parse_id"] == str(expected.outcomes[0].parse_id)
    assert parsed["status"] == "changes_reported"
    assert parsed["routing"] == "auto_accept"
    assert parsed["event_ids"] == [str(event_id) for event_id in expected.outcomes[0].event_ids]
    assert parsed["withdrawn_event_ids"] == [
        str(event_id) for event_id in expected.outcomes[0].withdrawn_event_ids
    ]
    outcomes = {item["outcome"]: item for item in body["outcomes"]}
    assert outcomes["denied:tier_capability_missing"]["company_id"] == str(
        expected.outcomes[1].company_id
    )
    assert outcomes["letter_failed:no_confirmation_request"]["status"] == "changes_reported"
    # The bounded wire shape: ids, codes, status and routing — nothing a client
    # wrote, and no field a span or a body could travel in.
    for item in body["outcomes"]:
        assert set(item) == {
            "communication_id",
            "company_id",
            "outcome",
            "parse_id",
            "status",
            "routing",
            "event_ids",
            "withdrawn_event_ids",
        }


def test_the_route_runs_one_cycle_for_the_actors_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mechanism, two entry points: the route is the manual trigger for the
    same ``run_cycle`` the loop runs, on the actor's tenant and dated today."""
    seen: dict[str, object] = {}
    store = FakeStore()

    async def fake_cycle(
        session: object, **kwargs: object
    ) -> reply_parsing.ParseCycleReport:
        seen.update(kwargs)
        seen["session"] = session
        return reply_parsing.ParseCycleReport(examined=0, parsed=0, outcomes=())

    monkeypatch.setattr(reply_parsing, "run_cycle", fake_cycle)

    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: ADMIN
    app.dependency_overrides[get_actor_context] = lambda: ADMIN_CONTEXT
    app.dependency_overrides[get_storage] = lambda: store

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield FakeSession()  # type: ignore[misc]

    app.dependency_overrides[get_session] = no_session

    response = TestClient(app).post(PARSE_URL)

    assert response.status_code == 200, response.text
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["actor_user_id"] == ADMIN.user_id
    assert seen["as_of"] == date.today()
    assert seen["store"] is store
    assert seen["scanner"] is not None
    assert seen["session"] is not None
