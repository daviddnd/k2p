"""API-layer RBAC dependency tests (scope §2.14; AGENTS.md single mechanism).

A throwaway probe app exercises ``require_permission`` end to end. The DB
load (``get_actor_context``) is overridden with fixed contexts; the auth seam
(``get_current_actor``) is exercised un-overridden to prove it fails closed.
Endpoint-driven DB-backed loading is covered in
``test_identity_scenarios_db.py``.
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_actor_context, require_permission
from app.domain.rbac import ActorContext, Permission, Role

COMPANY_A = uuid4()
COMPANY_B = uuid4()


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/companies/{company_id}/probe")
    async def probe(
        company_id: UUID,
        context: Annotated[ActorContext, Depends(require_permission(Permission.REVIEW_EVENTS))],
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tenant/probe")
    async def tenant_probe(
        context: Annotated[ActorContext, Depends(require_permission(Permission.REVIEW_EVENTS))],
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/raw/{company_id}/probe")
    async def raw_probe(
        company_id: str,
        context: Annotated[ActorContext, Depends(require_permission(Permission.REVIEW_EVENTS))],
    ) -> dict[str, bool]:
        return {"ok": True}

    return app


def client_with_context(context: ActorContext) -> TestClient:
    app = build_app()
    app.dependency_overrides[get_actor_context] = lambda: context
    return TestClient(app)


def test_unauthenticated_request_fails_closed() -> None:
    client = TestClient(build_app())
    response = client.get(f"/companies/{COMPANY_A}/probe")
    assert response.status_code == 401


def test_general_without_grant_is_forbidden() -> None:
    context = ActorContext(
        is_active=True, role=Role.GENERAL, permissions=frozenset(), company_ids=frozenset()
    )
    response = client_with_context(context).get(f"/companies/{COMPANY_A}/probe")
    assert response.status_code == 403


def test_general_with_permission_and_grant_is_allowed() -> None:
    context = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    response = client_with_context(context).get(f"/companies/{COMPANY_A}/probe")
    assert response.status_code == 200


def test_general_grant_does_not_leak_to_other_companies() -> None:
    context = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    response = client_with_context(context).get(f"/companies/{COMPANY_B}/probe")
    assert response.status_code == 403


def test_administrator_is_allowed_without_grants() -> None:
    context = ActorContext(
        is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
    )
    response = client_with_context(context).get(f"/companies/{COMPANY_A}/probe")
    assert response.status_code == 200


def test_tenant_scoped_route_is_administrator_only() -> None:
    general = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(Permission),
        company_ids=frozenset({COMPANY_A}),
    )
    assert client_with_context(general).get("/tenant/probe").status_code == 403
    admin = ActorContext(
        is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
    )
    assert client_with_context(admin).get("/tenant/probe").status_code == 200


def test_malformed_company_id_is_rejected() -> None:
    general = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    # Authorization runs before request validation: a non-UUID value resolves
    # to no company scope, so a General account is denied (403) even with
    # every permission and grant — the framework's 422 is never reached.
    assert client_with_context(general).get("/companies/not-a-uuid/probe").status_code == 403
    assert client_with_context(general).get("/raw/not-a-uuid/probe").status_code == 403
    # An Administrator passes the check and then fails UUID validation (422).
    admin = ActorContext(
        is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
    )
    assert client_with_context(admin).get("/companies/not-a-uuid/probe").status_code == 422
