"""DB-backed scenarios for the identity endpoints (opt-in; needs a migrated
PostgreSQL — same setup as ``test_rbac_scenarios_db.py``).

These exercise the real router stack (``create_app``) with only the auth seam
and session overridden, so every request flows through the real
``get_actor_context`` → ``load_actor_context`` → ``is_allowed`` path against
the database — the endpoint-driven loader coverage the Phase 1 plan calls for.

The deployment runs on a licensed tier here: these scenarios are about
identity and authorization, and the seat cap the Free tier imposes on
``POST /users`` is exercised in ``test_entitlement_scenarios_db.py``
instead. Every test runs inside a transaction that is rolled back, so the
review database stays clean.
"""

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application.identity import EmailAlreadyInUseError, create_user
from app.core.config import settings
from app.domain.rbac import Role
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")


@pytest.fixture(autouse=True)
def licensed_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """A licensed tier leaves the seat cap out of these scenarios' way."""
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


async def seed_tenant_with_admin(session: AsyncSession) -> tuple[Tenant, User]:
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


BOOTSTRAP_PAYLOAD = {
    "tenant_name": "Acme TCSP",
    "admin_email": "root@acme.example",
    "admin_display_name": "Root Admin",
    "admin_password": "bootstrap-secret-1",
}


# --- POST /api/v1/tenants (bootstrap) ---


async def test_bootstrap_creates_tenant_and_first_administrator(session: AsyncSession) -> None:
    response = make_client(session).post("/api/v1/tenants", json=BOOTSTRAP_PAYLOAD)
    assert response.status_code == 201
    body = response.json()

    tenant = await session.get(Tenant, UUID(body["tenant_id"]))
    assert tenant is not None and tenant.name == "Acme TCSP"

    admin = await session.get(User, UUID(body["administrator"]["id"]))
    assert admin is not None
    assert admin.tenant_id == tenant.id
    assert admin.email == "root@acme.example"
    assert admin.role == "administrator"
    assert admin.is_active is True
    # An Administrator needs no grants; the flags stay denied (fail-safe default).
    flags = (admin.review_events, admin.approve_events, admin.review_drafts, admin.approve_drafts)
    assert flags == (False, False, False, False)


async def test_bootstrap_is_closed_once_a_tenant_exists(session: AsyncSession) -> None:
    await seed_tenant_with_admin(session)
    response = make_client(session).post("/api/v1/tenants", json=BOOTSTRAP_PAYLOAD)
    assert response.status_code == 409


async def test_bootstrap_armed_with_the_correct_token_is_unchanged(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Armed and presented correctly, the guard lets the bootstrap through
    and what it creates is exactly what it creates unguarded."""
    monkeypatch.setattr(settings, "setup_token", "db-scenario-setup-token")
    response = make_client(session).post(
        "/api/v1/tenants",
        json=BOOTSTRAP_PAYLOAD,
        headers={"X-Setup-Token": "db-scenario-setup-token"},
    )
    assert response.status_code == 201
    body = response.json()

    tenant = await session.get(Tenant, UUID(body["tenant_id"]))
    assert tenant is not None and tenant.name == "Acme TCSP"

    admin = await session.get(User, UUID(body["administrator"]["id"]))
    assert admin is not None
    assert admin.tenant_id == tenant.id
    assert admin.email == "root@acme.example"
    assert admin.role == "administrator"
    assert admin.is_active is True


async def test_bootstrap_disarmed_is_unchanged(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disarmed, the guard is inert: no header, same bootstrap as today."""
    monkeypatch.setattr(settings, "setup_token", "")
    response = make_client(session).post("/api/v1/tenants", json=BOOTSTRAP_PAYLOAD)
    assert response.status_code == 201


# --- POST /api/v1/users through the real RBAC stack ---


async def test_admin_creates_users_through_real_stack(session: AsyncSession) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = client.post(
        "/api/v1/users",
        json={
            "email": "staff@example.com",
            "display_name": "Staff User",
            "role": "general",
            "password": "staff-password-1",
            "review_events": True,
            "approve_drafts": True,
        },
    )
    assert response.status_code == 201
    body = response.json()
    # The new user lands in the actor's own tenant — the request body carries
    # no tenant_id, so cross-tenant creation is impossible by construction.
    assert body["tenant_id"] == str(tenant.id)
    assert body["review_events"] is True
    assert body["approve_events"] is False
    assert body["approve_drafts"] is True
    assert body["review_drafts"] is False
    assert body["is_active"] is True

    user = await session.get(User, UUID(body["id"]))
    assert user is not None and user.tenant_id == tenant.id
    assert (user.review_events, user.approve_events, user.review_drafts, user.approve_drafts) == (
        True,
        False,
        False,
        True,
    )


async def test_created_user_defaults_to_denied_permissions(session: AsyncSession) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post(
        "/api/v1/users",
        json={
            "email": "second-admin@example.com",
            "display_name": "Second Admin",
            "role": "administrator",
            "password": "admin-password-2",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["review_events"], body["approve_events"]) == (False, False)
    assert (body["review_drafts"], body["approve_drafts"]) == (False, False)


async def test_created_general_user_is_denied_user_management(session: AsyncSession) -> None:
    """The loader maps the newly created user: a General account holding every
    grantable permission is still denied on this company-less route."""
    tenant, admin = await seed_tenant_with_admin(session)
    admin_client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    created = admin_client.post(
        "/api/v1/users",
        json={
            "email": "staff@example.com",
            "display_name": "Staff User",
            "role": "general",
            "password": "staff-password-1",
            "review_events": True,
            "approve_events": True,
            "review_drafts": True,
            "approve_drafts": True,
        },
    )
    assert created.status_code == 201
    staff_id = UUID(created.json()["id"])

    staff_client = make_client(session, ActorRef(user_id=staff_id, tenant_id=tenant.id))
    response = staff_client.post(
        "/api/v1/users",
        json={
            "email": "third@example.com",
            "display_name": "Third",
            "role": "general",
            "password": "third-password-1",
        },
    )
    assert response.status_code == 403


async def test_duplicate_email_in_same_tenant_conflicts(session: AsyncSession) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    payload = {
        "email": "staff@example.com",
        "display_name": "Staff User",
        "role": "general",
        "password": "staff-password-1",
    }
    assert client.post("/api/v1/users", json=payload).status_code == 201
    assert client.post("/api/v1/users", json=payload).status_code == 409


async def test_same_email_is_allowed_in_another_tenant(session: AsyncSession) -> None:
    """The duplicate check is scoped per tenant (matching the unique
    constraint); only a same-tenant duplicate is refused."""
    tenant_a, _ = await seed_tenant_with_admin(session)
    tenant_b, _ = await seed_tenant_with_admin(session)
    await create_user(
        session,
        tenant_id=tenant_a.id,
        actor_user_id=None,
        email="staff@example.com",
        display_name="Staff A",
        role=Role.GENERAL,
        password="staff-password-1",
    )
    await create_user(
        session,
        tenant_id=tenant_b.id,
        actor_user_id=None,
        email="staff@example.com",
        display_name="Staff B",
        role=Role.GENERAL,
        password="staff-password-1",
    )
    with pytest.raises(EmailAlreadyInUseError):
        await create_user(
            session,
            tenant_id=tenant_a.id,
            actor_user_id=None,
            email="staff@example.com",
            display_name="Staff A2",
            role=Role.GENERAL,
            password="staff-password-1",
        )


async def test_unknown_or_cross_tenant_actor_is_forbidden(session: AsyncSession) -> None:
    tenant, _ = await seed_tenant_with_admin(session)
    other_tenant, other_admin = await seed_tenant_with_admin(session)
    payload = {
        "email": "staff@example.com",
        "display_name": "Staff User",
        "role": "general",
        "password": "staff-password-1",
    }
    # Unknown user id.
    unknown = make_client(session, ActorRef(user_id=uuid4(), tenant_id=tenant.id))
    assert unknown.post("/api/v1/users", json=payload).status_code == 403
    # A real administrator whose id is paired with another tenant's id
    # (forged session token shape) must never authorize.
    forged = make_client(session, ActorRef(user_id=other_admin.id, tenant_id=tenant.id))
    assert forged.post("/api/v1/users", json=payload).status_code == 403
    assert other_tenant.id != tenant.id


async def test_inactive_administrator_is_denied_through_real_stack(session: AsyncSession) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    admin.is_active = False
    await session.flush()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post(
        "/api/v1/users",
        json={
            "email": "staff@example.com",
            "display_name": "Staff User",
            "role": "general",
            "password": "staff-password-1",
        },
    )
    assert response.status_code == 403
