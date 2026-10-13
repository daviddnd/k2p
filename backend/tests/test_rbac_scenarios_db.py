"""DB-backed RBAC review scenarios (opt-in; needs a migrated PostgreSQL).

These scenarios verify the identity schema (migration ``0001_identity_rbac``)
and the context loader against a real database — the parts the in-memory
domain/API tests cannot reach. They are skipped unless
``AICS_TEST_DATABASE_URL`` points at a database migrated to head, e.g.::

    docker run -d --name aics-rbac-review -p 55432:5432 \
        -e POSTGRES_USER=aics -e POSTGRES_PASSWORD=aics-dev-only \
        -e POSTGRES_DB=aics postgres:16.15-alpine
    AICS_DATABASE_URL=postgresql+psycopg://aics:aics-dev-only@localhost:55432/aics \
        .venv/bin/alembic upgrade head
    AICS_TEST_DATABASE_URL=postgresql+psycopg://aics:aics-dev-only@localhost:55432/aics \
        .venv/bin/pytest tests/test_rbac_scenarios_db.py

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_permission
from app.application.rbac import load_actor_context
from app.domain.rbac import ActorContext, Permission, Role

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


async def make_user(
    session: AsyncSession,
    tenant: Tenant,
    *,
    role: str = "general",
    is_active: bool = True,
    review_events: bool = False,
    approve_events: bool = False,
    review_drafts: bool = False,
    approve_drafts: bool = False,
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Review User",
        role=role,
        is_active=is_active,
        review_events=review_events,
        approve_events=approve_events,
        review_drafts=review_drafts,
        approve_drafts=approve_drafts,
    )
    session.add(user)
    await session.flush()
    return user


async def make_company(session: AsyncSession, tenant: Tenant) -> Company:
    """A company anchor with the statics migration 0003 requires."""
    company = Company(
        tenant_id=tenant.id,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=date(2026, 3, 1),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


# --- Loader scenarios (app/application/rbac.py) ---


async def test_loader_returns_none_for_unknown_user(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    assert await load_actor_context(session, user_id=uuid4(), tenant_id=tenant.id) is None


async def test_loader_rejects_cross_tenant_user_id(session: AsyncSession) -> None:
    tenant_a = Tenant(name=f"tenant-a-{uuid4()}")
    tenant_b = Tenant(name=f"tenant-b-{uuid4()}")
    session.add_all([tenant_a, tenant_b])
    await session.flush()
    user = await make_user(session, tenant_a)
    assert await load_actor_context(session, user_id=user.id, tenant_id=tenant_b.id) is None


async def test_loader_maps_permission_flags_and_grants(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = await make_user(session, tenant, review_events=True, approve_drafts=False)
    other_user = await make_user(session, tenant, review_events=True)
    company_a = await make_company(session, tenant)
    company_b = await make_company(session, tenant)
    session.add_all(
        [
            CompanyAccessGrant(user_id=user.id, company_id=company_a.id, tenant_id=tenant.id),
            CompanyAccessGrant(user_id=user.id, company_id=company_b.id, tenant_id=tenant.id),
            # Another user's grant must not leak into this user's context.
            CompanyAccessGrant(user_id=other_user.id, company_id=company_a.id, tenant_id=tenant.id),
        ]
    )
    await session.flush()

    context = await load_actor_context(session, user_id=user.id, tenant_id=tenant.id)
    assert context is not None
    assert context.is_active is True
    assert context.role is Role.GENERAL
    assert context.permissions == frozenset({Permission.REVIEW_EVENTS})
    assert context.company_ids == frozenset({company_a.id, company_b.id})


async def test_loader_loads_inactive_user_for_denial(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = await make_user(session, tenant, is_active=False, review_events=True)
    context = await load_actor_context(session, user_id=user.id, tenant_id=tenant.id)
    # The loader reports the facts; is_allowed enforces the denial.
    assert context is not None and context.is_active is False


# --- End-to-end through the real dependency stack (auth seam overridden) ---


def build_probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/companies/{company_id}/probe")
    async def probe(
        company_id: UUID,
        context: Annotated[ActorContext, Depends(require_permission(Permission.REVIEW_EVENTS))],
    ) -> dict[str, bool]:
        return {"ok": True}

    return app


async def seed_company_with_staff(
    session: AsyncSession,
) -> tuple[Tenant, Company, User, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    company = await make_company(session, tenant)
    admin = await make_user(session, tenant, role="administrator")
    staff = await make_user(session, tenant, review_events=True)
    session.add(CompanyAccessGrant(user_id=staff.id, company_id=company.id, tenant_id=tenant.id))
    await session.flush()
    return tenant, company, admin, staff


def probe_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    app = build_probe_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def test_admin_and_granted_staff_pass_real_stack(session: AsyncSession) -> None:
    tenant, company, admin, staff = await seed_company_with_staff(session)
    admin_client = probe_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    assert admin_client.get(f"/companies/{company.id}/probe").status_code == 200
    staff_client = probe_client(session, ActorRef(user_id=staff.id, tenant_id=tenant.id))
    assert staff_client.get(f"/companies/{company.id}/probe").status_code == 200


async def test_staff_denied_other_tenants_company(session: AsyncSession) -> None:
    tenant, _, _, staff = await seed_company_with_staff(session)
    other_tenant = Tenant(name=f"tenant-other-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    other_company = await make_company(session, other_tenant)
    client = probe_client(session, ActorRef(user_id=staff.id, tenant_id=tenant.id))
    assert client.get(f"/companies/{other_company.id}/probe").status_code == 403


async def test_cross_tenant_actor_ref_is_forbidden(session: AsyncSession) -> None:
    tenant, company, _, _ = await seed_company_with_staff(session)
    other_tenant = Tenant(name=f"tenant-other-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    # A session token naming a user from another tenant must never authorize.
    admin_other = await make_user(session, other_tenant, role="administrator")
    client = probe_client(session, ActorRef(user_id=admin_other.id, tenant_id=tenant.id))
    assert client.get(f"/companies/{company.id}/probe").status_code == 403


async def test_unknown_user_is_forbidden(session: AsyncSession) -> None:
    tenant, company, _, _ = await seed_company_with_staff(session)
    client = probe_client(session, ActorRef(user_id=uuid4(), tenant_id=tenant.id))
    assert client.get(f"/companies/{company.id}/probe").status_code == 403


# --- Schema constraint scenarios (migration 0001_identity_rbac) ---


async def test_cross_tenant_grant_row_is_uninsertable(session: AsyncSession) -> None:
    """F1 fix: one ``tenant_id`` on the grant row must match both the user's
    and the company's tenant (composite FKs). Whichever tenant a bad row
    claims, one of the two composite FKs rejects it — so the read-path filter
    in ``load_actor_context`` is defense in depth, not the only barrier."""
    tenant, _, _, staff = await seed_company_with_staff(session)
    other_tenant = Tenant(name=f"tenant-other-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    other_company = await make_company(session, other_tenant)
    # Claiming the user's tenant: the (company_id, tenant_id) FK fails.
    session.add(
        CompanyAccessGrant(user_id=staff.id, company_id=other_company.id, tenant_id=tenant.id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    # Claiming the company's tenant: the (user_id, tenant_id) FK fails.
    session.add(
        CompanyAccessGrant(user_id=staff.id, company_id=other_company.id, tenant_id=other_tenant.id)
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_role_check_constraint_rejects_unknown_role(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    session.add(
        User(
            tenant_id=tenant.id,
            email=f"{uuid4()}@example.com",
            display_name="Bad Role",
            role="superuser",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_email_is_unique_per_tenant(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    email = f"{uuid4()}@example.com"
    await make_user(session, tenant)
    session.add(User(tenant_id=tenant.id, email=email, display_name="First", role="general"))
    await session.flush()
    session.add(User(tenant_id=tenant.id, email=email, display_name="Duplicate", role="general"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_grant_requires_real_user_and_company(session: AsyncSession) -> None:
    session.add(CompanyAccessGrant(user_id=uuid4(), company_id=uuid4(), tenant_id=uuid4()))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_new_user_defaults_to_denied_and_active(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    email = f"{uuid4()}@example.com"
    await session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, display_name, role) "
            "VALUES (:id, :tenant_id, :email, 'Defaults Check', 'general')"
        ),
        {"id": uuid4(), "tenant_id": tenant.id, "email": email},
    )
    row = (
        await session.execute(
            text(
                "SELECT review_events, approve_events, review_drafts, approve_drafts, is_active "
                "FROM users WHERE email = :email"
            ),
            {"email": email},
        )
    ).one()
    assert tuple(row) == (False, False, False, False, True)
