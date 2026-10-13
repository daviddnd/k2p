"""DB-backed scenarios for the tenant-encryption seam (Phase 7 slice D1; opt-in).

``app.application.encryption`` is the one seam every consumer uses; these
scenarios pin its security properties against a real audit chain: first use
creates the key and records exactly one ``encryption_key_created`` entry on
the caller's session; later uses read the key and stay silent; concurrent
first use serializes on the tenant row lock — one key on disk, one chain
entry, no loser's envelope under a replaced key; a key file whose creation
rolled back self-heals (exactly one entry per key id); a tampered envelope
fails closed; and the key material never appears in the audit chain or an
exception message.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Tests run inside transactions that are rolled back, so the review
database stays clean; the concurrency test commits its tenant row alone
(never any audit rows) and deletes it afterwards — the only way two sessions
can observe each other's lock behavior.
"""

import asyncio
import base64
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant
from app.adapters.secrets.tenant_keys import TenantKeyStore, seal
from app.application import encryption
from app.core.config import settings
from app.domain.encryption import (
    KEY_MATERIAL_INVALID,
    EncryptionRefused,
    KeyPurpose,
    parse_envelope,
)

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

PLAINTEXT = "a TOTP seed, had this a real consumer"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
async def tenant(session: AsyncSession) -> Tenant:
    row = Tenant(name=f"tenant-{uuid4()}")
    session.add(row)
    await session.flush()
    return row


async def audit_rows(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    result = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return list(result)


async def test_first_use_creates_the_key_and_records_exactly_one_audit_entry(
    session: AsyncSession, tenant: Tenant
) -> None:
    envelope = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )

    parsed = parse_envelope(envelope)
    assert parsed.purpose == "totp_secret"
    # The key file exists, owner-only.
    key_path = settings.tenant_key_file
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key_material = key_path.read_bytes()
    assert parsed.key_id == hashlib.sha256(key_material).hexdigest()[:16]

    rows = await audit_rows(session, tenant.id)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "encryption_key_created"
    assert entry.actor_user_id is None  # the system created the key
    assert entry.details == {"key_id": parsed.key_id}


async def test_later_uses_read_the_key_and_stay_silent(
    session: AsyncSession, tenant: Tenant
) -> None:
    first = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )
    rows_after_first = await audit_rows(session, tenant.id)
    assert len(rows_after_first) == 1

    second = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )
    reopened = await encryption.decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=first
    )

    assert reopened == PLAINTEXT
    assert parse_envelope(second).key_id == parse_envelope(first).key_id
    assert len(await audit_rows(session, tenant.id)) == 1  # nothing new recorded


async def test_a_tampered_envelope_fails_closed(
    session: AsyncSession, tenant: Tenant
) -> None:
    envelope = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )
    document = json.loads(envelope)
    raw = bytearray(base64.b64decode(document["ciphertext_b64"]))
    raw[-1] ^= 0x01
    document["ciphertext_b64"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(EncryptionRefused) as excinfo:
        await encryption.decrypt_for_tenant(
            session, purpose=KeyPurpose.TOTP_SECRET, envelope=json.dumps(document)
        )
    assert excinfo.value.reason == "envelope_malformed"


async def test_a_malformed_key_file_fails_closed_without_a_new_audit_entry(
    session: AsyncSession, tenant: Tenant
) -> None:
    key_path = settings.tenant_key_file
    key_path.write_bytes(b"damaged-key-material")

    with pytest.raises(EncryptionRefused) as excinfo:
        await encryption.encrypt_for_tenant(
            session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
        )

    assert excinfo.value.reason == KEY_MATERIAL_INVALID
    assert key_path.read_bytes() == b"damaged-key-material"  # not regenerated
    assert await audit_rows(session, tenant.id) == []


async def test_key_material_never_appears_in_the_audit_chain(
    session: AsyncSession, tenant: Tenant
) -> None:
    envelope = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )
    await encryption.decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=envelope
    )
    key_material = settings.tenant_key_file.read_bytes()

    rows = await audit_rows(session, tenant.id)
    chain_text = json.dumps([dict(row.details) for row in rows], sort_keys=True)
    for material in (key_material.hex(), base64.b64encode(key_material).decode("ascii")):
        assert material not in chain_text
    # The envelope itself never rides the audit chain either.
    assert envelope not in chain_text


async def test_encryption_key_created_is_counted_once_per_chain(
    session: AsyncSession, tenant: Tenant
) -> None:
    await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext="one"
    )
    count = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action == "encryption_key_created",
        )
    )
    assert count == 1


async def test_a_key_file_without_a_chain_entry_is_healed_on_next_use(
    session: AsyncSession, tenant: Tenant
) -> None:
    # The creation's caller rolled back after the file was written: the key
    # is on disk, the chain is empty. The next use must record exactly one
    # entry for the key's id — and never a second one.
    staged, created = TenantKeyStore(settings.tenant_key_file).load_or_create()
    assert created is True
    assert await audit_rows(session, tenant.id) == []

    envelope = await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
    )

    rows = await audit_rows(session, tenant.id)
    assert len(rows) == 1
    assert rows[0].action == "encryption_key_created"
    assert rows[0].details == {"key_id": staged.key_id}
    assert parse_envelope(envelope).key_id == staged.key_id

    await encryption.encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext="again"
    )
    assert len(await audit_rows(session, tenant.id)) == 1


async def test_decrypt_heals_an_unrecorded_key_once(
    session: AsyncSession, tenant: Tenant
) -> None:
    staged, _ = TenantKeyStore(settings.tenant_key_file).load_or_create()
    envelope = seal(staged, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)
    assert await audit_rows(session, tenant.id) == []

    opened = await encryption.decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=envelope
    )
    assert opened == PLAINTEXT

    rows = await audit_rows(session, tenant.id)
    assert len(rows) == 1
    assert rows[0].action == "encryption_key_created"
    assert rows[0].details == {"key_id": staged.key_id}

    await encryption.decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=envelope
    )
    assert len(await audit_rows(session, tenant.id)) == 1


async def _commit_fresh_tenant() -> UUID:
    """One committed tenant row; the concurrency test deletes it afterwards."""
    async with (
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as session,
    ):
        tenant = Tenant(name=f"race-{uuid4()}")
        session.add(tenant)
        await session.flush()
        await connection.commit()
        return tenant.id


async def _encrypt_in_fresh_transaction() -> tuple[str, list[str]]:
    """One encrypt on a fresh session/transaction, rolled back after.

    Returns the envelope and the key ids its transaction saw recorded: the
    observable proof of how the lock serialized it against the other
    session's first use.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                envelope = await encryption.encrypt_for_tenant(
                    session, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
                )
                details = (
                    await session.scalars(
                        select(AuditLog.details).where(
                            AuditLog.action == "encryption_key_created"
                        )
                    )
                ).all()
                recorded = [row.get("key_id") for row in details]
        finally:
            await transaction.rollback()
    return envelope, recorded


async def test_concurrent_first_use_serializes_on_one_key_and_one_entry() -> None:
    # Two sessions race the absent key file. A takes the tenant row lock,
    # persists the key, and holds the lock with its transaction open; B must
    # wait — not race ahead to a second key that os.replace would let A
    # silently destroy. A then rolls back (the file persists, the entry
    # vanishes — the residual window), and B's wait ends: it re-reads the
    # file, finds A's key, heals the one chain entry, and seals under the
    # same key A used.
    tenant_id = await _commit_fresh_tenant()
    try:
        async with engine.connect() as connection_a:
            transaction_a = await connection_a.begin()
            async with AsyncSession(bind=connection_a, expire_on_commit=False) as session_a:
                first = await encryption.encrypt_for_tenant(
                    session_a, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT
                )
                first_key_id = parse_envelope(first).key_id

                waiter = asyncio.create_task(_encrypt_in_fresh_transaction())
                await asyncio.sleep(0.3)
                assert not waiter.done()  # serialized: B is waiting on the lock

                await transaction_a.rollback()  # A's entry vanishes; the file stays

            second, recorded = await asyncio.wait_for(waiter, timeout=30)

        key_material = settings.tenant_key_file.read_bytes()
        on_disk_id = hashlib.sha256(key_material).hexdigest()[:16]
        assert parse_envelope(second).key_id == first_key_id == on_disk_id
        # B healed exactly the one entry, for the key both envelopes share.
        assert recorded == [on_disk_id]
    finally:
        async with engine.connect() as connection:
            await connection.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await connection.commit()
