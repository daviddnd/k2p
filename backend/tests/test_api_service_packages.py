"""API-layer tests for the service-package and service-assignment endpoints
(ADR-0013 item 4; data-model §5.1 Service packages row).

These run without a database: the auth seam, RBAC context, and session are
overridden, and the application services are monkeypatched, so only
fail-closed authentication, the single Administrator-only authorization
check (``require_admin`` on every route — packages and commercial coverage
are administrative configuration, consistent with the contacts and
tenant-config decisions), request validation, and the typed-error → HTTP
mappings are exercised. Real flows against PostgreSQL live in
``test_service_packages_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.service_packages import ServiceAssignment, ServicePackage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import service_packages
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import (
    AssignmentStatus,
    CoveredFormsError,
    UnknownServiceError,
)
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
PACKAGE_ID = uuid4()
ASSIGNMENT_ID = uuid4()
COMPANY_ID = uuid4()

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),  # even a grant for the path company
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


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


CREATE_PACKAGE_PAYLOAD = {
    "name": "Annual retainer",
    "included_services": ["confirmation_generation", "ai_reply_parsing"],
    "covered_forms": ["NAR1", "ND2A"],
    "fee": "12000.00",
}
CREATE_ASSIGNMENT_PAYLOAD = {
    "package_id": str(PACKAGE_ID),
    "coverage_start": "2026-01-01",
    "coverage_end": "2026-12-31",
}

ROUTES = [
    ("POST", "/api/v1/service-packages", CREATE_PACKAGE_PAYLOAD),
    ("GET", "/api/v1/service-packages", None),
    ("GET", f"/api/v1/service-packages/{PACKAGE_ID}", None),
    ("PATCH", f"/api/v1/service-packages/{PACKAGE_ID}", {"name": "Renamed"}),
    ("DELETE", f"/api/v1/service-packages/{PACKAGE_ID}", None),
    (
        "POST",
        f"/api/v1/companies/{COMPANY_ID}/service-assignments",
        CREATE_ASSIGNMENT_PAYLOAD,
    ),
    ("GET", f"/api/v1/companies/{COMPANY_ID}/service-assignments", None),
    (
        "PATCH",
        f"/api/v1/companies/{COMPANY_ID}/service-assignments/{ASSIGNMENT_ID}",
        {"status": "ended"},
    ),
]


def _request(client: TestClient, method: str, url: str, payload: dict | None) -> int:
    if method == "GET":
        return client.get(url).status_code
    if method == "POST":
        return client.post(url, json=payload).status_code
    if method == "DELETE":
        return client.delete(url).status_code
    return client.patch(url, json=payload).status_code


# --- Fail closed; every route Administrator-only ---


def test_unauthenticated_fails_closed_on_all_routes() -> None:
    client = build_client()
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 401, (method, url)


def test_general_is_forbidden_on_all_routes_even_with_company_grant() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_CONTEXT
    )
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


# --- Request validation (422 before any service call) ---


def test_create_package_validates_name_services_and_fee() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = "/api/v1/service-packages"
    assert client.post(url, json={"name": ""}).status_code == 422
    assert (
        client.post(
            url,
            json={
                "name": "P",
                "included_services": ["invoicing"],
                "covered_forms": ["NAR1"],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(url, json={"name": "P", "covered_forms": ["NAR1"], "fee": "not-a-number"})
    ).status_code == 422
    assert (
        client.post(url, json={"name": "P", "covered_forms": ["NAR1"], "fee": "-5"}).status_code
        == 422
    )


def test_create_package_requires_the_covered_work() -> None:
    """``covered_forms`` has no default: a package declares the work it covers
    (§5.1.9's release gate reads the list), so omitting it is a 422 from the
    body rather than a package that silently covers everything — or nothing."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = "/api/v1/service-packages"

    assert client.post(url, json={"name": "P"}).status_code == 422
    assert client.post(url, json={"name": "P", "covered_forms": []}).status_code == 422
    assert (
        client.post(url, json={"name": "P", "covered_forms": ["NNC2"]}).status_code == 422
    )


def test_create_package_lists_the_unknown_form_the_registry_does_not_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The covered-work refusal is the domain rule's own message, mapped where
    the caller reads it (the service-package routes' 422 shape)."""

    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        raise CoveredFormsError("unknown forms: NNC2")

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        "/api/v1/service-packages", json={"name": "P", "covered_forms": ["NNC2"]}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown forms: NNC2"


def test_the_covered_work_is_readable_and_updatable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list is part of the resource a reader sees and a caller can narrow —
    which is the migration's documented remedy for the backfilled guess."""

    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["covered_forms"] == ["NAR1"]
        return make_package(covered_forms=["NAR1"])

    async def fake_update(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["changes"] == {"covered_forms": ["NAR1"]}
        return make_package(covered_forms=["NAR1"])

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    monkeypatch.setattr(service_packages, "update_package", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    created = client.post(
        "/api/v1/service-packages", json={"name": "P", "covered_forms": ["NAR1"]}
    )
    assert created.status_code == 201, created.text
    assert created.json()["covered_forms"] == ["NAR1"]

    patched = client.patch(
        f"/api/v1/service-packages/{PACKAGE_ID}", json={"covered_forms": ["NAR1"]}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["covered_forms"] == ["NAR1"]


def test_an_update_that_names_no_covered_form_leaves_the_list_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial update's shape: a body that does not mention the list does
    not pass one, so the field's own default can never empty a package's
    covered work."""
    seen: dict[str, object] = {}

    async def fake_update(session: object, **kwargs: object) -> ServicePackage:
        seen.update(kwargs)
        return make_package()

    monkeypatch.setattr(service_packages, "update_package", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(f"/api/v1/service-packages/{PACKAGE_ID}", json={"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert seen["changes"] == {"name": "Renamed"}


def test_create_package_allows_empty_services_for_a_manual_only_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["included_services"] == []
        return make_package(included_services=[], fee=None)

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        "/api/v1/service-packages",
        json={"name": "Manual only", "covered_forms": ["NAR1"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["included_services"] == []


def test_update_package_requires_at_least_one_field() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.patch(f"/api/v1/service-packages/{PACKAGE_ID}", json={}).status_code == 422


def test_create_assignment_validates_ids_and_coverage_order() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/companies/{COMPANY_ID}/service-assignments"
    assert (
        client.post(
            url,
            json={
                "package_id": "not-a-uuid",
                "coverage_start": "2026-01-01",
                "coverage_end": "2026-12-31",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={
                "package_id": str(PACKAGE_ID),
                "coverage_start": "2026-12-31",
                "coverage_end": "2026-01-01",
            },
        ).status_code
        == 422
    )


def test_update_assignment_requires_at_least_one_field() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/companies/{COMPANY_ID}/service-assignments/{ASSIGNMENT_ID}"
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"status": "lost"}).status_code == 422


# --- Happy paths and error mappings (services monkeypatched) ---


def make_package(**overrides: object) -> ServicePackage:
    values: dict[str, object] = {
        "id": PACKAGE_ID,
        "tenant_id": ADMIN.tenant_id,
        "name": "Annual retainer",
        "included_services": ["confirmation_generation", "ai_reply_parsing"],
        "covered_forms": ["NAR1", "ND2A"],
        "fee": Decimal("12000.00"),
    }
    values.update(overrides)
    return ServicePackage(**values)  # type: ignore[arg-type]


def make_assignment(**overrides: object) -> ServiceAssignment:
    values: dict[str, object] = {
        "id": ASSIGNMENT_ID,
        "company_id": COMPANY_ID,
        "package_id": PACKAGE_ID,
        "tenant_id": ADMIN.tenant_id,
        "coverage_start": date(2026, 1, 1),
        "coverage_end": date(2026, 12, 31),
        "status": AssignmentStatus.ACTIVE.value,
    }
    values.update(overrides)
    return ServiceAssignment(**values)  # type: ignore[arg-type]


def test_create_package_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["name"] == "Annual retainer"
        assert kwargs["fee"] == "12000.00"
        return make_package()

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post("/api/v1/service-packages", json=CREATE_PACKAGE_PAYLOAD)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(PACKAGE_ID)
    assert body["fee"] == "12000.00"
    assert body["included_services"] == ["confirmation_generation", "ai_reply_parsing"]
    assert body["covered_forms"] == ["NAR1", "ND2A"]


def test_create_package_unknown_service_maps_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        raise UnknownServiceError(["invoicing"])

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    payload = {"name": "P", "included_services": ["invoicing"], "covered_forms": ["NAR1"]}
    assert client.post("/api/v1/service-packages", json=payload).status_code == 422


def test_create_package_duplicate_name_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServicePackage:
        raise service_packages.PackageNameInUseError

    monkeypatch.setattr(service_packages, "create_package", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post("/api/v1/service-packages", json=CREATE_PACKAGE_PAYLOAD)
    assert response.status_code == 409


def test_list_and_get_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(session: object, **kwargs: object) -> list[ServicePackage]:
        return [make_package()]

    async def fake_get(session: object, **kwargs: object) -> ServicePackage:
        return make_package()

    monkeypatch.setattr(service_packages, "list_packages", fake_list)
    monkeypatch.setattr(service_packages, "get_package", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    listing = client.get("/api/v1/service-packages")
    assert listing.status_code == 200
    assert listing.json()[0]["name"] == "Annual retainer"

    detail = client.get(f"/api/v1/service-packages/{PACKAGE_ID}")
    assert detail.status_code == 200
    assert detail.json()["id"] == str(PACKAGE_ID)


def test_get_package_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(session: object, **kwargs: object) -> ServicePackage:
        raise service_packages.PackageNotFoundError

    monkeypatch.setattr(service_packages, "get_package", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(f"/api/v1/service-packages/{PACKAGE_ID}").status_code == 404


def test_update_package_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_update(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["changes"] == {"included_services": []}
        return make_package(included_services=[])

    monkeypatch.setattr(service_packages, "update_package", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(
        f"/api/v1/service-packages/{PACKAGE_ID}", json={"included_services": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["included_services"] == []


def test_update_package_clears_fee_with_explicit_null(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_update(session: object, **kwargs: object) -> ServicePackage:
        assert kwargs["changes"] == {"fee": None}
        return make_package(fee=None)

    monkeypatch.setattr(service_packages, "update_package", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(f"/api/v1/service-packages/{PACKAGE_ID}", json={"fee": None})
    assert response.status_code == 200, response.text
    assert response.json()["fee"] is None


def test_delete_package_returns_204_and_in_use_maps_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_delete(session: object, **kwargs: object) -> None:
        return None

    async def in_use_delete(session: object, **kwargs: object) -> None:
        raise service_packages.PackageInUseError

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(service_packages, "delete_package", fake_delete)
    assert client.delete(f"/api/v1/service-packages/{PACKAGE_ID}").status_code == 204
    monkeypatch.setattr(service_packages, "delete_package", in_use_delete)
    assert client.delete(f"/api/v1/service-packages/{PACKAGE_ID}").status_code == 409


def test_create_assignment_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServiceAssignment:
        assert kwargs["coverage_start"] == date(2026, 1, 1)
        assert kwargs["coverage_end"] == date(2026, 12, 31)
        return make_assignment()

    monkeypatch.setattr(service_packages, "create_assignment", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        f"/api/v1/companies/{COMPANY_ID}/service-assignments", json=CREATE_ASSIGNMENT_PAYLOAD
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(ASSIGNMENT_ID)
    assert body["status"] == "active"


def test_create_assignment_unknown_company_or_package_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown_company(session: object, **kwargs: object) -> ServiceAssignment:
        raise CompanyNotFoundError

    async def unknown_package(session: object, **kwargs: object) -> ServiceAssignment:
        raise service_packages.PackageNotFoundError

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/companies/{COMPANY_ID}/service-assignments"
    monkeypatch.setattr(service_packages, "create_assignment", unknown_company)
    assert client.post(url, json=CREATE_ASSIGNMENT_PAYLOAD).status_code == 404
    monkeypatch.setattr(service_packages, "create_assignment", unknown_package)
    assert client.post(url, json=CREATE_ASSIGNMENT_PAYLOAD).status_code == 404


def test_create_assignment_second_active_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> ServiceAssignment:
        raise service_packages.ActiveAssignmentExistsError

    monkeypatch.setattr(service_packages, "create_assignment", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"/api/v1/companies/{COMPANY_ID}/service-assignments", json=CREATE_ASSIGNMENT_PAYLOAD
    )
    assert response.status_code == 409


def test_list_company_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(session: object, **kwargs: object) -> list[ServiceAssignment]:
        return [make_assignment()]

    monkeypatch.setattr(service_packages, "list_company_assignments", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"/api/v1/companies/{COMPANY_ID}/service-assignments")
    assert response.status_code == 200
    assert response.json()[0]["package_id"] == str(PACKAGE_ID)


def test_update_assignment_end_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_update(session: object, **kwargs: object) -> ServiceAssignment:
        assert kwargs["changes"] == {"status": AssignmentStatus.ENDED}
        return make_assignment(status=AssignmentStatus.ENDED.value)

    monkeypatch.setattr(service_packages, "update_assignment", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(
        f"/api/v1/companies/{COMPANY_ID}/service-assignments/{ASSIGNMENT_ID}",
        json={"status": "ended"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ended"


def test_update_assignment_not_found_and_invalid_coverage_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_found(session: object, **kwargs: object) -> ServiceAssignment:
        raise service_packages.AssignmentNotFoundError

    async def bad_coverage(session: object, **kwargs: object) -> ServiceAssignment:
        raise service_packages.InvalidCoverageError

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/companies/{COMPANY_ID}/service-assignments/{ASSIGNMENT_ID}"
    monkeypatch.setattr(service_packages, "update_assignment", not_found)
    assert client.patch(url, json={"status": "ended"}).status_code == 404
    monkeypatch.setattr(service_packages, "update_assignment", bad_coverage)
    assert client.patch(url, json={"coverage_end": "2025-12-31"}).status_code == 422
