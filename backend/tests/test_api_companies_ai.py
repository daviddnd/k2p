"""API-layer tests for the company AI surface (M6.3): the contracted
``POST /companies/{id}/ai-disabled`` endpoint.

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the single authorization check
(**Administrator-only** — the per-company objection is an administrative act,
the anchor-statics precedent), request validation, the 404 mapping, and the
response shape are exercised. The real writes against PostgreSQL — the flag
and its audit entries — live in ``test_company_ai_scenarios_db.py``.

The file's original second half — the Q1.8 opt-in register write
``POST /companies/{id}/ai-opt-in`` and the register fields on the response —
retired with the register itself (ADR-0019 §2, the consent-model
transcription): the route, the write path and the columns no longer exist.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import Company
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import company_master
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
COMPANY_ID = uuid4()

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
# Granted for exactly the company under test: even a full-grant General
# account must stay forbidden — the route is Administrator-only.
GRANTED_GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

AI_DISABLED_ROUTE = f"/api/v1/companies/{COMPANY_ID}/ai-disabled"


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # endpoints under test never reach the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def make_company(**overrides: object) -> Company:
    """An un-persisted ``Company`` carrying every response field."""
    values: dict[str, object] = {
        "id": COMPANY_ID,
        "tenant_id": ADMIN.tenant_id,
        "cr_number": "1234567",
        "br_number": "12345678",
        "date_of_incorporation": "2010-03-01",
        "anniversary_date": "2026-03-01",
        "type": "private",
        "ai_disabled": False,
    }
    values.update(overrides)
    return Company(**values)  # type: ignore[arg-type]


# --- Fail closed; the route is Administrator-only ---


def test_unauthenticated_fails_closed() -> None:
    client = build_client()
    assert client.post(AI_DISABLED_ROUTE, json={"disabled": True}).status_code == 401


def test_general_account_is_forbidden_even_with_the_company_grant() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
        context=GRANTED_GENERAL_CONTEXT,
    )
    assert client.post(AI_DISABLED_ROUTE, json={"disabled": True}).status_code == 403


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    assert client.post(AI_DISABLED_ROUTE, json={"disabled": True}).status_code == 403


# --- POST /ai-disabled: validation, mapping, response shape ---


def test_ai_disabled_requires_a_boolean() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(AI_DISABLED_ROUTE, json={}).status_code == 422
    assert client.post(AI_DISABLED_ROUTE, json={"disabled": "yes"}).status_code == 422
    assert client.post(AI_DISABLED_ROUTE, json={"disabled": None}).status_code == 422


def test_ai_disabled_unknown_company_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_found(session: object, **kwargs: object) -> object:
        raise company_master.CompanyNotFoundError

    monkeypatch.setattr(company_master, "set_ai_disabled", not_found)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(AI_DISABLED_ROUTE, json={"disabled": True})
    assert response.status_code == 404


def test_ai_disabled_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_set(session: object, **kwargs: object) -> Company:
        return make_company(ai_disabled=True)

    monkeypatch.setattr(company_master, "set_ai_disabled", fake_set)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(AI_DISABLED_ROUTE, json={"disabled": True})
    assert response.status_code == 200
    body = response.json()
    assert body["ai_disabled"] is True


def test_company_id_must_be_a_uuid() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert (
        client.post("/api/v1/companies/not-a-uuid/ai-disabled", json={"disabled": True}).status_code
        == 422
    )
