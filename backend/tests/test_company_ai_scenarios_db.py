"""DB-backed scenarios for the company AI surface (M6.3; opt-in; needs a
migrated PostgreSQL).

The contracted ``ai_disabled`` endpoint against a real database: the flag's
round trip with its audit entries on the same chain, and the 404 for an
unknown or cross-tenant company — and, for the register's retirement itself,
migration ``0037``'s upgrade/downgrade round trip (downgrade restores 0028's
shape exactly; the retirement is a downgrade away until the HELD sign-off).

The suite's original first half — the Q1.8 register walk (the migration's
round trip, the CHECK pairing and the tenant-pinning FK as the schema's
backstops, the opt-in write route, the re-notice overwrite, the gate-unset
refusal, the register fields on the response) — retired with the register
itself (ADR-0019 §2, the consent-model transcription, migration `0037`):
the columns, the constraints, the route and the audit action no longer
exist, and the gateway's pre-flight no longer consults them.

The session override is the other DB suites' shape: a plain ``yield``, so
the deployment's request-edge commit is replaced by the savepoint the
fixture opens — every test rolls back and the review database stays clean.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head.
"""

import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.domain.audit import AuditAction
from app.domain.rbac import ActorContext, Role
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app over the test session, RBAC context overridden to admin."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_actor_context] = lambda: ADMIN_CONTEXT

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed(session: AsyncSession) -> tuple[Tenant, User, Company]:
    """A tenant, its Administrator, and one company."""
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
    company = Company(
        tenant_id=tenant.id,
        cr_number="1234567",
        br_number="12345678",
        date_of_incorporation="2010-03-01",
        anniversary_date="2026-03-01",
        type="private",
    )
    session.add(company)
    await session.flush()
    return tenant, admin, company


async def audit_entries(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.company_id == company_id)
        .order_by(AuditLog.seq)
    )
    return list(rows.all())


# --- POST /companies/{id}/ai-disabled ---


async def test_ai_disabled_round_trip_with_audit(session: AsyncSession) -> None:
    tenant, admin, company = await seed(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    on = client.post(
        f"/api/v1/companies/{company.id}/ai-disabled", json={"disabled": True}
    )
    assert on.status_code == 200
    assert on.json()["ai_disabled"] is True
    await session.flush()
    assert company.ai_disabled is True

    entries = await audit_entries(session, tenant_id=tenant.id, company_id=company.id)
    assert [entry.action for entry in entries] == [AuditAction.COMPANY_AI_DISABLED_UPDATED.value]
    assert entries[0].details == {"before": False, "after": True}
    assert entries[0].actor_user_id == admin.id

    off = client.post(
        f"/api/v1/companies/{company.id}/ai-disabled", json={"disabled": False}
    )
    assert off.status_code == 200
    entries = await audit_entries(session, tenant_id=tenant.id, company_id=company.id)
    assert [entry.action for entry in entries] == [
        AuditAction.COMPANY_AI_DISABLED_UPDATED.value,
        AuditAction.COMPANY_AI_DISABLED_UPDATED.value,
    ]
    assert entries[1].details == {"before": True, "after": False}


async def test_ai_disabled_unknown_company_is_404(session: AsyncSession) -> None:
    tenant, admin, _ = await seed(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post(
        f"/api/v1/companies/{uuid4()}/ai-disabled", json={"disabled": True}
    )
    assert response.status_code == 404


# --- Migration 0037's own round trip ---


def _load_revision() -> object:
    """The 0037 revision module, importable without the versions dir being a package."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0037_retire_ai_opt_in_register.py"
    )
    spec = importlib.util.spec_from_file_location("revision_0037_retire_ai_opt_in_register", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION = _load_revision()


async def test_migration_0037_round_trip_restores_the_register(session: AsyncSession) -> None:
    """At head the register is gone; downgrade restores it exactly as 0028
    built it — columns, both CHECKs, the tenant-pinning FK — and upgrade
    drops it again. The retirement is a downgrade away until the HELD
    sign-off, so the script's own promises are what this test drives, on the
    same connection the suite rolls back."""

    def _shape(sync_connection: object) -> tuple[set[str], set[str], set[str]]:
        inspector = inspect(sync_connection)
        columns = {column["name"] for column in inspector.get_columns("companies")}
        checks = {check["name"] for check in inspector.get_check_constraints("companies")}
        fks = {fk["name"] for fk in inspector.get_foreign_keys("companies")}
        return columns, checks, fks

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION, direction)()

        return _drive

    connection = await session.connection()
    columns, checks, fks = await connection.run_sync(_shape)
    assert "ai_notice_delivered_at" not in columns
    assert "ai_notice_basis" not in columns
    assert "ai_notice_recorded_by" not in columns

    await connection.run_sync(_run("downgrade"))
    columns, checks, fks = await connection.run_sync(_shape)
    assert {"ai_notice_delivered_at", "ai_notice_basis", "ai_notice_recorded_by"} <= columns
    assert "companies_ai_notice_basis_check" in checks
    assert "companies_ai_notice_pair_check" in checks
    assert "fk_companies_ai_notice_recorded_by" in fks

    await connection.run_sync(_run("upgrade"))
    columns, checks, fks = await connection.run_sync(_shape)
    assert "ai_notice_delivered_at" not in columns
    assert "ai_notice_basis" not in columns
    assert "ai_notice_recorded_by" not in columns
    assert "companies_ai_notice_basis_check" not in checks
    assert "companies_ai_notice_pair_check" not in checks
    assert "fk_companies_ai_notice_recorded_by" not in fks
