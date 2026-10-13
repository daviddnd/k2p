"""API-layer tests for the CSV portfolio-import endpoint (data-model §5.1.1).

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the Administrator-only
authorization check, and request-level validation (missing ``data_as_of``,
non-UTF-8 body, header-vocabulary failures — all raised before the session
is touched) are exercised. Real import flows against PostgreSQL live in
``test_company_import_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

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

URL = "/api/v1/companies/import?data_as_of=2026-06-30"
VALID_CSV = (
    "br_number,name_en,date_of_incorporation,type\n"
    "12345678,EXAMPLE LIMITED,2010-03-01,private\n"
)


def build_client(
    actor: ActorRef | None = None, context: ActorContext | None = None
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # validation under test runs before the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def test_unauthenticated_is_401() -> None:
    client = build_client()
    response = client.post(URL, content=VALID_CSV.encode())
    assert response.status_code == 401


def test_general_account_is_403() -> None:
    client = build_client(ADMIN, GENERAL_CONTEXT)
    response = client.post(URL, content=VALID_CSV.encode())
    assert response.status_code == 403


def test_inactive_admin_is_403() -> None:
    client = build_client(ADMIN, INACTIVE_ADMIN_CONTEXT)
    response = client.post(URL, content=VALID_CSV.encode())
    assert response.status_code == 403


def test_missing_data_as_of_is_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post("/api/v1/companies/import", content=VALID_CSV.encode())
    assert response.status_code == 422


def test_non_utf8_body_is_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(URL, content=b"\xff\xfe\x00bad")
    assert response.status_code == 422
    assert "UTF-8" in response.json()["detail"]


def test_unknown_header_column_is_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    csv_text = "br_number,name_en,date_of_incorporation,type,emal\n"
    response = client.post(URL, content=csv_text.encode())
    assert response.status_code == 422
    assert "emal" in response.json()["detail"]


def test_missing_required_header_column_is_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(URL, content=b"br_number,name_en,type\n")
    assert response.status_code == 422
    assert "date_of_incorporation" in response.json()["detail"]


def test_oversized_content_length_is_413() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        URL,
        content=VALID_CSV.encode(),
        headers={"Content-Length": str(10_485_761)},
    )
    assert response.status_code == 413
    assert "10 MiB" in response.json()["detail"]


def test_oversized_body_is_413() -> None:
    # The stream cap catches bodies whose Content-Length is absent or
    # understated: 10 MiB + 1 byte of actual content is rejected.
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(URL, content=b"a" * (10_485_760 + 1))
    assert response.status_code == 413


def test_body_at_the_limit_is_accepted() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    header = b"br_number,name_en,date_of_incorporation,type,emal\n"
    padded = header + b" " * (10_485_760 - len(header))
    response = client.post(URL, content=padded)
    # Exactly at the cap the size gate passes; the request then fails the
    # header-vocabulary check (unknown column), which runs before the
    # session is touched.
    assert response.status_code == 422
    assert "emal" in response.json()["detail"]
