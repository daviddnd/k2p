"""API-layer tests for the risk-acknowledgment endpoints (hermetic; no database).

The route surface follows the house API-test shape: fail-closed
authentication, the read open to every active staff account (every role sees
the interstitial), the confirmation write Administrator-only, and the
response shape. The acceptance itself runs for real: ``record_event`` is
replaced by an in-memory chain (the gateway-suite precedent) and the session
by a dummy answering the read helper's single query from that chain — so the
audit payload, the idempotency rule, and the version-bump re-open are the
real application behaviour, not a stub's. The real PostgreSQL walk lives in
``test_risk_acknowledgment_scenarios_db.py`` (Task 3).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import risk_acknowledgment
from app.domain import risk_acknowledgment as statement
from app.domain.audit import AuditAction
from app.domain.rbac import ActorContext, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True, role=Role.GENERAL, permissions=frozenset(), company_ids=frozenset()
)

URL = "/api/v1/risk-acknowledgment"


class FakeChain:
    """The replaced audit seam: the entries ``record_event`` would have appended."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []


class FakeSession:
    """A dummy session: the read helper's single scalar query is answered
    from the fake chain (the newest acceptance's details); anything else is a
    programming error the test surfaces, not a silent default."""

    def __init__(self, chain: FakeChain) -> None:
        self._chain = chain

    async def scalar(self, query: object) -> object:
        for entry in reversed(self._chain.entries):
            if entry["action"] == AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED:
                return dict(entry["details"])
        return None


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    """The acceptance write seam replaced by an in-memory chain."""

    fake_chain = FakeChain()

    async def fake_record_event(session: object, **kwargs: Any) -> None:
        fake_chain.entries.append(kwargs)

    monkeypatch.setattr(risk_acknowledgment.audit, "record_event", fake_record_event)
    return fake_chain


def build_client(
    chain: FakeChain,
    actor: ActorRef | None = None,
    context: ActorContext | None = None,
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    session = FakeSession(chain)

    async def fake_session() -> AsyncIterator[AsyncSession]:
        yield session  # type: ignore[misc]  # the chain seam replaces the SQL boundary

    app.dependency_overrides[get_session] = fake_session
    return TestClient(app)


# --- Fail closed; the write is Administrator-only, the read is any active staff ---


def test_unauthenticated_fails_closed_on_both_routes(chain: FakeChain) -> None:
    client = build_client(chain)
    assert client.get(URL).status_code == 401
    assert client.post(URL).status_code == 401


def test_general_may_read_but_cannot_confirm(chain: FakeChain) -> None:
    client = build_client(
        chain,
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
        context=GENERAL_CONTEXT,
    )
    assert client.get(URL).status_code == 200
    assert client.post(URL).status_code == 403
    assert chain.entries == []


# --- The confirmation and its audit footprint ---


def test_get_serves_the_statement_unacknowledged_initially(chain: FakeChain) -> None:
    client = build_client(chain, actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["statement_version"] == "v3"
    assert body["statement_text"] == statement.STATEMENT_TEXT
    assert body["acknowledged"] is False


def test_administrator_confirm_lands_one_acceptance(chain: FakeChain) -> None:
    client = build_client(chain, actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(URL)
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    assert len(chain.entries) == 1
    (entry,) = chain.entries
    assert entry["action"] == AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED
    assert entry["actor_user_id"] == ADMIN.user_id
    assert entry["tenant_id"] == ADMIN.tenant_id
    assert entry["details"] == {"statement_version": "v3"}
    # The read the UI relies on agrees with the write (ruling R2: one read
    # helper behind both).
    assert client.get(URL).json()["acknowledged"] is True


def test_repost_is_idempotent(chain: FakeChain) -> None:
    client = build_client(chain, actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(URL).status_code == 200
    assert client.post(URL).status_code == 200
    assert len(chain.entries) == 1


def test_a_statement_version_bump_reopens_the_acknowledgment(
    chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = build_client(chain, actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(URL).status_code == 200

    monkeypatch.setattr(statement, "STATEMENT_VERSION", "v2-test")
    response = client.get(URL)
    assert response.json()["statement_version"] == "v2-test"
    assert response.json()["acknowledged"] is False

    # Confirming the revised statement lands a second acceptance at the new
    # version; the old v3 entry stays in the chain (append-only) and simply
    # stops counting.
    assert client.post(URL).status_code == 200
    assert [entry["details"]["statement_version"] for entry in chain.entries] == [
        "v3",
        "v2-test",
    ]
    assert client.get(URL).json()["acknowledged"] is True
