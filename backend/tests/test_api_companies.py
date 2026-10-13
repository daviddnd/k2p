"""API-layer tests for the company-master endpoints.

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the single authorization
check (create/list Administrator-only via ``require_admin``; company-scoped
routes grant-gated via ``require_company_access``), and request validation
are exercised. Real create/patch/as-of flows against PostgreSQL live in
``test_company_master_api_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import company_master, entitlement
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
COMPANY_ID = uuid4()

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)
GENERAL_CONTEXT_OTHER_COMPANY = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    # A grant for some other company must not open this one.
    company_ids=frozenset({uuid4()}),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
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
        yield None  # type: ignore[misc]  # endpoints under test never reach the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


CREATE_PAYLOAD = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}
PATCH_PAYLOAD = {
    "effective_date": "2026-01-01",
    "name": {"name_en": "EXAMPLE LIMITED"},
}
STATICS_PAYLOAD = {"br_number": "87654321", "cr_number": "7654321"}
ROUTES = [
    ("POST", "/api/v1/companies", CREATE_PAYLOAD),
    ("GET", "/api/v1/companies", None),
    ("GET", f"/api/v1/companies/{COMPANY_ID}", None),
    ("PATCH", f"/api/v1/companies/{COMPANY_ID}", PATCH_PAYLOAD),
    ("PATCH", f"/api/v1/companies/{COMPANY_ID}/statics", STATICS_PAYLOAD),
    ("GET", f"/api/v1/companies/{COMPANY_ID}/master-data?as_of=2026-01-01", None),
]


def _request(client: TestClient, method: str, url: str, payload: dict | None) -> int:
    if method == "GET":
        return client.get(url).status_code
    if method == "POST":
        return client.post(url, json=payload).status_code
    return client.patch(url, json=payload).status_code


# --- Fail closed; create/list Administrator-only; company-scoped grant-gated ---

COMPANY_SCOPED_ROUTES = [route for route in ROUTES if route[1] != "/api/v1/companies"]


def test_unauthenticated_fails_closed_on_all_routes() -> None:
    client = build_client()
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 401, (method, url)


def test_general_without_company_grant_is_forbidden_on_all_routes() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_CONTEXT
    )
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


def test_general_grant_for_another_company_does_not_open_this_one() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
        context=GENERAL_CONTEXT_OTHER_COMPANY,
    )
    for method, url, payload in COMPANY_SCOPED_ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


def test_general_with_grant_stays_forbidden_on_create_and_list() -> None:
    # Portfolio composition is Administrator-only even for granted staff.
    granted = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(Permission),
        company_ids=frozenset({COMPANY_ID}),
    )
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=granted
    )
    assert client.post("/api/v1/companies", json=CREATE_PAYLOAD).status_code == 403
    assert client.get("/api/v1/companies").status_code == 403


def test_general_with_grant_stays_forbidden_on_statics_correction() -> None:
    # Statics correction is Administrator-only even for granted staff: unlike
    # fact appends, it is not ordinary master-data maintenance (option (b)
    # decision 2026-08-24).
    granted = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(Permission),
        company_ids=frozenset({COMPANY_ID}),
    )
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=granted
    )
    response = client.patch(f"/api/v1/companies/{COMPANY_ID}/statics", json=STATICS_PAYLOAD)
    assert response.status_code == 403


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


# --- POST /api/v1/companies: request validation ---


def test_create_company_rejects_invalid_payload() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    bad_type = client.post("/api/v1/companies", json={**CREATE_PAYLOAD, "type": "llc"})
    empty_brn = client.post("/api/v1/companies", json={**CREATE_PAYLOAD, "br_number": ""})
    bad_date = client.post(
        "/api/v1/companies", json={**CREATE_PAYLOAD, "anniversary_date": "01/03/2026"}
    )
    missing = client.post("/api/v1/companies", json={"br_number": "12345678"})
    assert bad_type.status_code == 422
    assert empty_brn.status_code == 422
    assert bad_date.status_code == 422
    assert missing.status_code == 422


def test_create_company_cap_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def over_cap(session: object, **kwargs: object) -> object:
        raise entitlement.CompanyCapExceededError(50)

    monkeypatch.setattr(company_master, "create_company", over_cap)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post("/api/v1/companies", json=CREATE_PAYLOAD)
    assert response.status_code == 409
    assert "company_cap_exceeded" in response.json()["detail"]


# --- PATCH /api/v1/companies/{id}: request validation ---


def test_patch_requires_at_least_one_fact_group() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.patch(
        f"/api/v1/companies/{COMPANY_ID}", json={"effective_date": "2026-01-01"}
    )
    assert response.status_code == 422


def test_patch_name_group_requires_a_registered_name() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={"effective_date": "2026-01-01", "name": {"business_name": "ONLY TRADING NAME"}},
    )
    assert response.status_code == 422


def test_patch_contact_group_requires_a_channel() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={"effective_date": "2026-01-01", "contact": {}},
    )
    assert response.status_code == 422


def test_patch_rejects_invalid_fact_values() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    bad_status = client.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={"effective_date": "2026-01-01", "status": {"status": "closed"}},
    )
    bad_currency = client.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={
            "effective_date": "2026-01-01",
            "share_capital_classes": {
                "classes": [
                    {
                        "class_name": "Ordinary",
                        "currency": "HKDD",
                        "total_shares": 100,
                        "total_amount": "100.00",
                        "total_paid_up": "100.00",
                    }
                ]
            },
        },
    )
    empty_classes = client.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={"effective_date": "2026-01-01", "share_capital_classes": {"classes": []}},
    )
    assert bad_status.status_code == 422
    assert bad_currency.status_code == 422
    assert empty_classes.status_code == 422


# --- PATCH /api/v1/companies/{id}/statics: request validation ---


def test_statics_correction_requires_at_least_one_field() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.patch(f"/api/v1/companies/{COMPANY_ID}/statics", json={})
    assert response.status_code == 422


def test_statics_correction_rejects_invalid_values() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    bad_type = client.patch(
        f"/api/v1/companies/{COMPANY_ID}/statics", json={"type": "llc"}
    )
    empty_brn = client.patch(
        f"/api/v1/companies/{COMPANY_ID}/statics", json={"br_number": ""}
    )
    bad_date = client.patch(
        f"/api/v1/companies/{COMPANY_ID}/statics",
        json={"date_of_incorporation": "01/03/2010"},
    )
    assert bad_type.status_code == 422
    assert empty_brn.status_code == 422
    assert bad_date.status_code == 422


# --- GET master-data: as_of validation ---


def test_master_data_rejects_malformed_as_of() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(f"/api/v1/companies/{COMPANY_ID}/master-data?as_of=01-01-2026")
    assert response.status_code == 422


def test_company_id_must_be_a_uuid() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get("/api/v1/companies/not-a-uuid").status_code == 422
    assert client.get("/api/v1/companies/not-a-uuid/master-data").status_code == 422
