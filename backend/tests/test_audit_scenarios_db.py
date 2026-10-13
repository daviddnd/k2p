"""DB-backed audit-log scenarios (opt-in; needs a migrated PostgreSQL).

These verify migration ``0008_audit_log`` and the single write path
``app.application.audit.record_event`` against a real database: the
append-only triggers, per-tenant hash chaining, chain-head locking
semantics, composite-FK tenant pinning, and the domain chain verifier over
stored rows. Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database
migrated to head (see ``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant, User
from app.application import audit
from app.domain.audit import AuditAction, verify_chain

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


async def tenant_entries(session: AsyncSession, tenant: Tenant) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return list(rows.all())


# --- Hash chaining ---


async def test_first_entry_is_genesis_and_chain_links_forward(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)

    first = await audit.record_event(
        session, tenant_id=tenant.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin.id
    )
    second = await audit.record_event(
        session, tenant_id=tenant.id, action=AuditAction.USER_CREATED, actor_user_id=admin.id
    )

    assert first.prev_hash is None
    assert second.prev_hash == first.entry_hash
    assert first.seq < second.seq
    assert verify_chain(await tenant_entries(session, tenant)) == []


async def test_details_are_stored_exactly_as_hashed(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    entry = await audit.record_event(
        session,
        tenant_id=tenant.id,
        action=AuditAction.COMPANY_STATICS_CORRECTED,
        actor_user_id=admin.id,
        details={"before": {"cr_number": None}, "after": {"cr_number": "123"}},
    )
    assert entry.details == {"before": {"cr_number": None}, "after": {"cr_number": "123"}}
    assert verify_chain([entry]) == []


async def test_chains_are_independent_per_tenant(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)

    await audit.record_event(
        session, tenant_id=tenant_a.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin_a.id
    )
    await audit.record_event(
        session, tenant_id=tenant_a.id, action=AuditAction.USER_CREATED, actor_user_id=admin_a.id
    )
    await audit.record_event(
        session, tenant_id=tenant_b.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin_b.id
    )

    entries_a = await tenant_entries(session, tenant_a)
    entries_b = await tenant_entries(session, tenant_b)
    assert len(entries_a) == 2 and len(entries_b) == 1
    assert entries_b[0].prev_hash is None  # B's chain starts at its own genesis
    assert verify_chain(entries_a) == []
    assert verify_chain(entries_b) == []


# --- Append-only enforcement ---


async def test_update_is_rejected_by_trigger(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    entry = await audit.record_event(
        session, tenant_id=tenant.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin.id
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            text("UPDATE audit_log SET action = 'forged' WHERE id = :id"), {"id": entry.id}
        )


async def test_delete_is_rejected_by_trigger(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    entry = await audit.record_event(
        session, tenant_id=tenant.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin.id
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})


# --- Tenant pinning (F1 pattern) ---


async def test_cross_tenant_actor_is_uninsertable(session: AsyncSession) -> None:
    tenant_a, _ = await make_admin(session)
    _, admin_b = await make_admin(session)
    with pytest.raises(DBAPIError):
        await audit.record_event(
            session,
            tenant_id=tenant_a.id,
            action=AuditAction.USER_CREATED,
            actor_user_id=admin_b.id,  # belongs to tenant B
        )


async def test_llm_call_metadata_round_trips(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    entry = await audit.record_event(
        session,
        tenant_id=tenant.id,
        action=AuditAction.COMPANY_FACT_RECORDED,
        actor_user_id=admin.id,
        llm_call={"provider": "siliconflow", "model": "Qwen/Qwen2.5-72B-Instruct"},
    )
    assert entry.llm_call == {"provider": "siliconflow", "model": "Qwen/Qwen2.5-72B-Instruct"}
    assert verify_chain([entry]) == []
