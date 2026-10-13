"""DB-backed integrity-verification scenarios (opt-in; needs a migrated PostgreSQL).

The one read-only seam end to end against a real database: the tenant's full
audit chain verified, every registered document re-hashed against the records
registry — and the seam's two promises a reviewer must weigh (Review Focus 5):
a mismatch alerts but changes nothing, and the check itself writes nothing,
so the one ``integrity_check`` audit entry belongs to the caller (the route /
the worker), never to the seam.

The tampered cases are staged out of band, the audit-integrity precedent:
stored blobs are corrupted (or deleted) in the fake store behind the
registry's back, and a forged chain row is inserted directly — the
append-only triggers refuse UPDATE/DELETE (``test_audit_scenarios_db.py``),
so a direct insert with a wrong link is the only way a chain can lie.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import Company, Tenant, User
from app.application import audit, company_master, documents, integrity, scanning
from app.domain.audit import AuditAction
from app.domain.documents import MESSAGE_CONTENT_TYPE
from app.domain.integrity import MismatchKind

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ANNIVERSARY = date(2026, 11, 10)

EML = b"From: ai-filings@tcsp.example\r\nTo: d@x\r\nSubject: stored\r\n\r\nbody\r\n"


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes (the reminders-suite fake)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def deployment(session: AsyncSession) -> tuple[Tenant, User, Company]:
    """A tenant, its administrator and one company — seeded through the real
    write path, so the audit chain the check verifies is the genuine one."""
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
    company = await company_master.create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=admin.id,
        br_number="12345678",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=ANNIVERSARY,
        type="private",
    )
    return tenant, admin, company


async def register_document(
    session: AsyncSession,
    store: FakeStore,
    *,
    tenant_id,
    company_id,
    actor_user_id,
    marker: str = "stored",
    version: int = 1,
) -> Document:
    """One registered document through the registry's own ingestion order.

    ``marker`` distinguishes otherwise-identical letters: the storage key is
    content-addressed on collision, and identical bytes render an identical
    key even with the suffix (the WORM rule — never overwrite).
    """
    body = EML.replace(b"Subject: stored", f"Subject: {marker}".encode())
    return await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=company_id,
        actor_user_id=actor_user_id,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=body,
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=version,
    )


async def audit_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AuditLog)) or 0)


# --- The clean check ---------------------------------------------------------


async def test_a_clean_deployment_verifies_and_the_check_writes_nothing(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, admin, company = deployment
    first = await register_document(
        session,
        store,
        tenant_id=tenant.id,
        company_id=company.id,
        actor_user_id=admin.id,
        marker="stored-first",
    )
    second = await register_document(
        session,
        store,
        tenant_id=tenant.id,
        company_id=company.id,
        actor_user_id=admin.id,
        marker="stored-second",
    )
    entries_before = await audit_count(session)
    store_before = dict(store.objects)

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC)
    )

    assert result.chain_verified is True
    assert result.ok is True
    assert result.documents_checked == 2
    assert result.mismatches == ()
    # The read-only promise (Review Focus 5): no audit entry of its own, no
    # registry row touched, no blob rewritten.
    assert await audit_count(session) == entries_before
    assert store.objects == store_before
    for document in (first, second):
        assert (
            hashlib.sha256(store.objects[document.storage_key]).hexdigest() == document.sha256
        )


# --- Tampered cases, staged out of band ---------------------------------------


async def test_a_corrupted_blob_is_a_hash_mismatch_and_nothing_is_repaired(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, admin, company = deployment
    document = await register_document(
        session, store, tenant_id=tenant.id, company_id=company.id, actor_user_id=admin.id
    )
    original_sha256 = document.sha256
    entries_before = await audit_count(session)
    store.objects[document.storage_key] = b"corrupted out from under the registry"

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC)
    )

    # A blob problem never touches the chain leg's verdict.
    assert result.chain_verified is True
    assert result.ok is False
    (mismatch,) = result.mismatches
    assert mismatch.kind is MismatchKind.DOCUMENT_HASH_MISMATCH
    assert mismatch.document_id == document.id
    # Alert, not repair: the blob stays corrupted, the registry row keeps its
    # original digest, and the check still wrote nothing.
    assert store.objects[document.storage_key] == b"corrupted out from under the registry"
    assert document.sha256 == original_sha256
    assert await audit_count(session) == entries_before


async def test_an_unreadable_blob_alerts_and_does_not_abort_the_document_leg(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, admin, company = deployment
    await register_document(
        session, store, tenant_id=tenant.id, company_id=company.id, actor_user_id=admin.id
    )
    unreadable = await register_document(
        session, store, tenant_id=tenant.id, company_id=company.id, actor_user_id=admin.id
    )
    del store.objects[unreadable.storage_key]

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC)
    )

    # The unreadable blob still counted as checked; the leg finished.
    assert result.documents_checked == 2
    assert result.chain_verified is True
    (mismatch,) = result.mismatches
    assert mismatch.kind is MismatchKind.DOCUMENT_UNREADABLE
    assert mismatch.document_id == unreadable.id


async def test_a_forged_chain_row_is_an_audit_chain_break(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, admin, _ = deployment
    await audit.record_event(
        session, tenant_id=tenant.id, action=AuditAction.TENANT_CREATED, actor_user_id=admin.id
    )
    # The append-only triggers refuse UPDATE/DELETE, so the only way a chain
    # can lie is a direct insert that does not link to its predecessor.
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(id, tenant_id, occurred_at, action, details, prev_hash, entry_hash) "
            "VALUES (:id, :tenant_id, :occurred_at, 'forged_probe', '{}'::jsonb, NULL, :entry_hash)"
        ),
        {
            "id": uuid4(),
            "tenant_id": tenant.id,
            "occurred_at": datetime.now(UTC),
            "entry_hash": "f" * 64,
        },
    )
    await session.flush()

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC)
    )

    assert result.chain_verified is False
    assert result.ok is False
    assert result.documents_checked == 0
    assert any(m.kind is MismatchKind.AUDIT_CHAIN_BREAK for m in result.mismatches)
    # A chain break names no document.
    assert all(m.document_id is None for m in result.mismatches)


# --- Scoping and bounds --------------------------------------------------------


async def test_the_check_never_reads_another_tenants_chain_or_documents(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, _, _ = deployment
    other_tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    other_admin = User(
        tenant_id=other_tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Other Admin",
        role="administrator",
    )
    session.add(other_admin)
    await session.flush()
    other_company = await company_master.create_company(
        session,
        tenant_id=other_tenant.id,
        actor_user_id=other_admin.id,
        br_number="87654321",
        date_of_incorporation=date(2015, 6, 1),
        anniversary_date=ANNIVERSARY,
        type="private",
    )
    foreign = await register_document(
        session,
        store,
        tenant_id=other_tenant.id,
        company_id=other_company.id,
        actor_user_id=other_admin.id,
    )
    # The other tenant's blob is corrupt and its chain holds a forged row:
    # neither may surface in this tenant's check.
    store.objects[foreign.storage_key] = b"corrupted foreign blob"
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(id, tenant_id, occurred_at, action, details, prev_hash, entry_hash) "
            "VALUES (:id, :tenant_id, :occurred_at, 'forged_probe', '{}'::jsonb, NULL, :entry_hash)"
        ),
        {
            "id": uuid4(),
            "tenant_id": other_tenant.id,
            "occurred_at": datetime.now(UTC),
            "entry_hash": "e" * 64,
        },
    )
    await session.flush()

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC)
    )

    assert result.chain_verified is True
    assert result.ok is True
    assert result.documents_checked == 0
    assert result.mismatches == ()


async def test_limit_bounds_the_document_leg_for_an_operators_spot_check(
    session: AsyncSession, store: FakeStore, deployment: tuple[Tenant, User, Company]
) -> None:
    tenant, admin, company = deployment
    for index in range(3):
        await register_document(
            session,
            store,
            tenant_id=tenant.id,
            company_id=company.id,
            actor_user_id=admin.id,
            marker=f"stored-{index}",
        )

    result = await integrity.run_integrity_check(
        session, store, tenant_id=tenant.id, now=datetime.now(UTC), limit=2
    )

    assert result.documents_checked == 2
    assert result.ok is True
