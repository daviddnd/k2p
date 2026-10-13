"""DB-backed service-package scenarios (opt-in; needs a migrated PostgreSQL).

These verify migration ``0007_service_packages`` and the package /
assignment / gating services and endpoints against a real database: CRUD
round trips through the real stack, per-tenant name uniqueness, the F1
composite-FK tenant pinning (cross-tenant rows uninsertable), the
one-active-assignment partial unique index, coverage-order backstop, the
renewal flow (end → re-assign → re-activation conflict), delete protection
for referenced packages, ``covered_forms`` — the release gate's work
dimension, and the NOT NULL migration ``0021`` added — and
``check_automation`` — the single §5.1 gating seam — for every denial
reason. The gate sources its tier capability leg
from the entitlement seam, so these scenarios run with a licensed tier
configured (the Free-tier denial is asserted explicitly, and the full
tier/lease matrix lives in ``test_entitlement_scenarios_db.py``). Skipped
unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to head
(see ``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.service_packages import ServiceAssignment, ServicePackage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import company_master, service_packages
from app.core.config import settings
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

PACKAGES_URL = "/api/v1/service-packages"
AS_OF = date(2026, 6, 30)
COVERAGE = {"coverage_start": "2026-01-01", "coverage_end": "2026-12-31"}


@pytest.fixture(autouse=True)
def licensed_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gating scenarios run on a licensed tier; the Free denial is explicit."""
    monkeypatch.setattr(settings, "license_tier", "standard")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def make_admin(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


def admin_client(session: AsyncSession, tenant: Tenant, admin: User) -> TestClient:
    return make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))


async def make_company(session: AsyncSession, tenant: Tenant):
    return await company_master.create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2020, 1, 2),
        anniversary_date=date(2026, 1, 2),
        type="private",
    )


def create_package(client: TestClient, name: str = "Annual retainer", **overrides: object) -> dict:
    payload: dict = {
        "name": name,
        "included_services": ["ai_reply_parsing"],
        "covered_forms": ["NAR1", "ND2A"],
        "fee": "12000.00",
    }
    payload.update(overrides)
    response = client.post(PACKAGES_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def assignments_url(company_id: object) -> str:
    return f"/api/v1/companies/{company_id}/service-assignments"


# --- Package CRUD through the real stack ---


async def test_package_crud_round_trip(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    created = create_package(client)
    assert created["tenant_id"] == str(tenant.id)
    assert created["fee"] == "12000.00"
    assert created["included_services"] == ["ai_reply_parsing"]

    listing = client.get(PACKAGES_URL)
    assert [row["id"] for row in listing.json()] == [created["id"]]

    updated = client.patch(
        f"{PACKAGES_URL}/{created['id']}",
        json={"included_services": [], "fee": None},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["included_services"] == []
    assert updated.json()["fee"] is None

    assert client.delete(f"{PACKAGES_URL}/{created['id']}").status_code == 204
    assert client.get(f"{PACKAGES_URL}/{created['id']}").status_code == 404


async def test_package_name_unique_per_tenant_but_reusable_across_tenants(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    other_tenant, other_admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    other_client = admin_client(session, other_tenant, other_admin)

    create_package(client)
    duplicate = client.post(
        PACKAGES_URL, json={"name": "Annual retainer", "covered_forms": ["NAR1"]}
    )
    assert duplicate.status_code == 409
    # The same name in another tenant is fine.
    create_package(other_client)
    # Rename into an existing name in the same tenant is also a 409.
    second = create_package(client, name="One-off")
    renamed = client.patch(f"{PACKAGES_URL}/{second['id']}", json={"name": "Annual retainer"})
    assert renamed.status_code == 409


async def test_unknown_service_values_rejected_through_the_stack(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.post(
        PACKAGES_URL,
        json={
            "name": "P",
            "included_services": ["invoicing", "teleportation"],
            "covered_forms": ["NAR1"],
        },
    )
    assert response.status_code == 422
    assert "invoicing" in response.json()["detail"]
    assert "teleportation" in response.json()["detail"]


async def test_cross_tenant_package_ids_are_invisible(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    other_tenant, other_admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    other_client = admin_client(session, other_tenant, other_admin)

    foreign = create_package(other_client, name="Foreign")
    assert client.get(f"{PACKAGES_URL}/{foreign['id']}").status_code == 404
    assert client.patch(f"{PACKAGES_URL}/{foreign['id']}", json={"name": "X"}).status_code == 404
    assert client.delete(f"{PACKAGES_URL}/{foreign['id']}").status_code == 404
    assert client.get(PACKAGES_URL).json() == []


async def test_the_covered_work_survives_the_round_trip_and_can_be_narrowed(
    session: AsyncSession,
) -> None:
    """``covered_forms`` is the release gate's input (§5.1.9), so it is part of
    the resource: read back exactly as written, narrowed through the same route
    the migration's backfill comment points at, and refused when it names no
    work or a code the registry does not ship."""
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    created = create_package(client, covered_forms=["NAR1"])
    assert created["covered_forms"] == ["NAR1"]

    narrowed = client.patch(
        f"{PACKAGES_URL}/{created['id']}", json={"covered_forms": ["NAR1", "ND2A"]}
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["covered_forms"] == ["NAR1", "ND2A"]

    emptied = client.patch(f"{PACKAGES_URL}/{created['id']}", json={"covered_forms": []})
    assert emptied.status_code == 422, emptied.text
    assert emptied.json()["detail"] == "covered_forms must name at least one form"
    assert client.get(f"{PACKAGES_URL}/{created['id']}").json()["covered_forms"] == [
        "NAR1",
        "ND2A",
    ]  # the refused update changed nothing

    unknown = client.patch(f"{PACKAGES_URL}/{created['id']}", json={"covered_forms": ["NN3"]})
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["detail"] == "unknown forms: NN3"


async def test_a_package_row_cannot_be_inserted_without_its_covered_work(
    session: AsyncSession,
) -> None:
    """The column is NOT NULL with no server default (migration ``0021``): the
    declared work is a property of the row rather than of the code that happens
    to write it, so a hand-inserted package can neither claim coverage it never
    declared nor claim none at all."""
    tenant, _ = await make_admin(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ServicePackage(
                    tenant_id=tenant.id, name=f"pkg-{uuid4()}", included_services=[]
                )
            )
            await session.flush()


# --- Assignments: lifecycle and invariants ---


async def test_assignment_lifecycle_and_renewal_flow(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = await make_company(session, tenant)
    package = create_package(client)

    created = client.post(
        assignments_url(company.id), json={"package_id": package["id"], **COVERAGE}
    )
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["status"] == "active"

    # A second active assignment is rejected.
    conflict = client.post(
        assignments_url(company.id), json={"package_id": package["id"], **COVERAGE}
    )
    assert conflict.status_code == 409

    # End the current assignment, then renew with a new coverage period.
    ended = client.patch(
        f"{assignments_url(company.id)}/{first['id']}", json={"status": "ended"}
    )
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"

    renewed = client.post(
        assignments_url(company.id),
        json={
            "package_id": package["id"],
            "coverage_start": "2027-01-01",
            "coverage_end": "2027-12-31",
        },
    )
    assert renewed.status_code == 201, renewed.text

    # Re-activating the ended assignment now conflicts with the active one.
    reactivate = client.patch(
        f"{assignments_url(company.id)}/{first['id']}", json={"status": "active"}
    )
    assert reactivate.status_code == 409

    # The listing keeps the full history.
    listing = client.get(assignments_url(company.id))
    assert len(listing.json()) == 2


async def test_assignment_references_unknown_or_cross_tenant_ids(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    other_tenant, other_admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    other_client = admin_client(session, other_tenant, other_admin)
    company = await make_company(session, tenant)
    foreign_company = await make_company(session, other_tenant)
    package = create_package(client)
    foreign_package = create_package(other_client, name="Foreign")

    url = assignments_url(company.id)
    # Unknown / cross-tenant package → 404 either way (existence never leaks).
    assert (
        client.post(url, json={"package_id": str(uuid4()), **COVERAGE}).status_code == 404
    )
    assert (
        client.post(url, json={"package_id": foreign_package["id"], **COVERAGE}).status_code
        == 404
    )
    # Cross-tenant company → 404.
    assert (
        client.post(
            assignments_url(foreign_company.id),
            json={"package_id": package["id"], **COVERAGE},
        ).status_code
        == 404
    )


async def test_composite_fks_reject_cross_tenant_assignment_rows(session: AsyncSession) -> None:
    """The F1 backstop: a row pinning company/package to another tenant is
    uninsertable at the database level, in both mismatch directions."""
    tenant, admin = await make_admin(session)
    other_tenant, _ = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = await make_company(session, tenant)
    package = create_package(client)
    foreign_company = await make_company(session, other_tenant)

    # Our company, but the row claims the other tenant.
    row = ServiceAssignment(
        company_id=company.id,
        package_id=UUID(package["id"]),
        tenant_id=other_tenant.id,
        coverage_start=date(2026, 1, 1),
        coverage_end=date(2026, 12, 31),
        status="active",
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    # Our tenant, but the company belongs to the other tenant.
    row = ServiceAssignment(
        company_id=foreign_company.id,
        package_id=UUID(package["id"]),
        tenant_id=tenant.id,
        coverage_start=date(2026, 1, 1),
        coverage_end=date(2026, 12, 31),
        status="active",
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_one_active_assignment_partial_unique_index_backstop(
    session: AsyncSession,
) -> None:
    """A direct insert of a second active assignment is rejected by the
    partial unique index even when the application pre-check is bypassed."""
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = await make_company(session, tenant)
    package = create_package(client)
    package_id = UUID(package["id"])

    client.post(assignments_url(company.id), json={"package_id": package["id"], **COVERAGE})

    session.add(
        ServiceAssignment(
            company_id=company.id,
            package_id=package_id,
            tenant_id=tenant.id,
            coverage_start=date(2026, 1, 1),
            coverage_end=date(2026, 12, 31),
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_coverage_order_check_backstop(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = await make_company(session, tenant)
    package = create_package(client)

    session.add(
        ServiceAssignment(
            company_id=company.id,
            package_id=UUID(package["id"]),
            tenant_id=tenant.id,
            coverage_start=date(2026, 12, 31),
            coverage_end=date(2026, 1, 1),
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_referenced_package_cannot_be_deleted(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = await make_company(session, tenant)
    package = create_package(client)

    client.post(assignments_url(company.id), json={"package_id": package["id"], **COVERAGE})
    assert client.delete(f"{PACKAGES_URL}/{package['id']}").status_code == 409


# --- check_automation: the single §5.1 gating seam ---


async def assign(
    session: AsyncSession,
    tenant: Tenant,
    company_id,
    *,
    services: list[str],
    coverage_start: date = date(2026, 1, 1),
    coverage_end: date = date(2026, 12, 31),
    status: str = "active",
) -> None:
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        name=f"pkg-{uuid4()}",
        included_services=services,
        covered_forms=["NAR1", "ND2A"],
    )
    session.add(
        ServiceAssignment(
            company_id=company_id,
            package_id=package.id,
            tenant_id=tenant.id,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            status=status,
        )
    )
    await session.flush()


async def gate(session: AsyncSession, tenant: Tenant, company_id, **overrides: object):
    kwargs: dict = {
        "automation_type": AutomationType.AI_REPLY_PARSING,
        "as_of": AS_OF,
    }
    kwargs.update(overrides)
    return await service_packages.check_automation(
        session, tenant_id=tenant.id, company_id=company_id, **kwargs
    )


async def test_gate_allows_when_all_three_legs_pass(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)
    await assign(session, tenant, company.id, services=["ai_reply_parsing"])

    decision = await gate(session, tenant, company.id)
    assert decision.allowed is True
    assert decision.reason is None


async def test_gate_no_assignment(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)

    decision = await gate(session, tenant, company.id)
    assert decision.allowed is False
    assert decision.reason is GatingReason.NO_ASSIGNMENT


async def test_gate_free_tier_fails_closed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)
    await assign(session, tenant, company.id, services=["ai_reply_parsing"])

    # Unlicensed deployment (the default): the tier capability leg fails
    # closed even though the company's commercial coverage is in place.
    monkeypatch.setattr(settings, "license_tier", "free")
    decision = await gate(session, tenant, company.id)
    assert decision.allowed is False
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING


async def test_gate_ended_assignment(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)
    await assign(session, tenant, company.id, services=["ai_reply_parsing"], status="ended")

    decision = await gate(session, tenant, company.id)
    assert decision.allowed is False
    assert decision.reason is GatingReason.ASSIGNMENT_ENDED


async def test_gate_out_of_coverage(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)
    await assign(
        session,
        tenant,
        company.id,
        services=["ai_reply_parsing"],
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 12, 31),
    )

    decision = await gate(session, tenant, company.id)
    assert decision.allowed is False
    assert decision.reason is GatingReason.OUT_OF_COVERAGE


async def test_gate_service_not_included_is_per_service(session: AsyncSession) -> None:
    """Per-service gating (ADR-0013 amendment): a package covering prefill
    but not reply parsing leaves reply parsing manual while prefill runs."""
    tenant, _ = await make_admin(session)
    company = await make_company(session, tenant)
    await assign(session, tenant, company.id, services=["automated_prefill"])

    denied = await gate(session, tenant, company.id)
    assert denied.allowed is False
    assert denied.reason is GatingReason.SERVICE_NOT_INCLUDED

    allowed = await gate(
        session, tenant, company.id, automation_type=AutomationType.AUTOMATED_PREFILL
    )
    assert allowed.allowed is True
