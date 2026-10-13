"""API-layer tests for the per-company audit compliance report (contract §5.1
Audit row; DoD 19; ruling R6).

These run without a database: the auth seam, RBAC context, session, and the
application read path are overridden, so what is exercised is fail-closed
authentication, the grant-gated authorization check
(``require_company_access``, no permission flag — reads are ungated per
ADR-0012), the unknown-company mapping, and the shape an operator reads.

The property this file guards: the response carries the tenant-chain
verification verdict (a company filter never re-verifies a subset), the
company identity block, and the company's entries as stored — the report is
a read; it writes nothing.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import audit_report
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
ACTOR_ID = uuid4()
URL = f"/api/v1/companies/{COMPANY_ID}/audit-report"

ADMIN = ActorRef(user_id=ACTOR_ID, tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)

GENERATED_AT = datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)


def report_payload() -> dict[str, Any]:
    """The report dict as the application read path composes it."""
    return {
        "report_version": 1,
        "generated_at": GENERATED_AT,
        "company": {
            "company_id": str(COMPANY_ID),
            "name": "Acme Holdings Limited",
            "br_number": "12345678",
            "cr_number": "CR987654",
        },
        "chain": {
            "verified": True,
            "checked_entries": 3,
            "problems": [],
            "scope": "verified over the tenant's full audit chain",
        },
        "entries": [
            {
                "seq": 1,
                "occurred_at": datetime(2026, 10, 2, 9, 1, 0, tzinfo=UTC),
                "actor": str(ACTOR_ID),
                "action": "company_created",
                "area": "data_changes",
                "entity_type": "company",
                "entity_id": str(COMPANY_ID),
                "details": {"br_number": "12345678"},
            }
        ],
    }


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is stubbed in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def test_the_read_fails_closed_without_a_session_or_a_grant() -> None:
    assert build_client().get(URL).status_code == 401
    assert (
        build_client(
            actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
            context=GENERAL_WITHOUT_GRANT,
        ).get(URL).status_code
        == 403
    )


def test_a_granted_account_reads_the_report_with_the_chain_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build(*args: object, **kwargs: object) -> dict[str, Any]:
        return report_payload()

    monkeypatch.setattr(audit_report, "build_company_report", fake_build)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )

    response = client.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"report_version", "generated_at", "company", "chain", "entries"}
    assert body["report_version"] == 1
    assert body["generated_at"] == "2026-10-02T09:00:00Z"
    assert body["company"] == {
        "company_id": str(COMPANY_ID),
        "name": "Acme Holdings Limited",
        "br_number": "12345678",
        "cr_number": "CR987654",
    }
    assert body["chain"]["verified"] is True
    assert body["chain"]["checked_entries"] == 3
    assert body["chain"]["scope"]
    assert len(body["entries"]) == 1
    assert set(body["entries"][0]) == {
        "seq",
        "occurred_at",
        "actor",
        "action",
        "area",
        "entity_type",
        "entity_id",
        "details",
    }
    assert body["entries"][0]["action"] == "company_created"
    assert body["entries"][0]["area"] == "data_changes"


def test_an_unknown_company_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*args: object, **kwargs: object) -> dict[str, Any]:
        raise CompanyNotFoundError

    monkeypatch.setattr(audit_report, "build_company_report", missing)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(URL)

    assert response.status_code == 404
    assert response.json()["detail"] == "company not found"


def test_the_route_passes_the_actors_tenant_the_path_company_and_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_build(*args: object, **kwargs: object) -> dict[str, Any]:
        seen.update(kwargs)
        return report_payload()

    monkeypatch.setattr(audit_report, "build_company_report", fake_build)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(URL)

    assert response.status_code == 200, response.text
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["company_id"] == COMPANY_ID
    generated_at = seen["generated_at"]
    assert isinstance(generated_at, datetime)
    assert abs((datetime.now(UTC) - generated_at).total_seconds()) < 30
