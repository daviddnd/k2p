"""The tenant-encryption seam — the one path every consumer uses (slice D1).

Two functions, both riding the caller's session (ADR-0009: the audit entry
and the caller's mutation commit or roll back together at the request edge):

- ``encrypt_for_tenant`` seals ``plaintext`` for ``purpose`` under the
  tenant's key and returns the canonical envelope JSON;
- ``decrypt_for_tenant`` opens one envelope sealed for ``purpose``.

Key custody lives in the adapter (``app.adapters.secrets.tenant_keys``). The
audit chain records each key's creation — actor ``NULL`` (the system created
it), details the key's id alone, never the material, never an envelope —
and the seam guarantees the invariant **one ``encryption_key_created`` entry
per key id, never zero**:

- *First use is serialized.* ``encrypt_for_tenant`` takes the tenant row
  lock (``with_for_update``, the ``application/audit.py`` precedent) **before**
  checking the key file. Two concurrent first uses therefore cannot both see
  an absent file: the second waits on the lock, re-reads, and gets the key
  the first persisted — never a loser envelope sealed under a key that was
  replaced before it was written.
- *Creation is recorded on the session; the file is not transactional.*
  The key file is written when the caller's transaction may still roll back,
  leaving a key on disk whose creation nobody recorded. The seam self-heals:
  whenever a key is in use and no ``encryption_key_created`` entry for its
  id is in the chain, the entry is recorded then, idempotently. The residual
  window is stated honestly: between the file write and the caller's commit,
  a crash or rollback leaves the key unrecorded until the next use heals it.
  The key was still unrecorded during that window — the heal closes the
  record, it does not retroactively cover the gap.

The tenant the entry belongs to is resolved the way the other system-level
seams resolve it (``commands.py``): a deployment is single-tenant
(ADR-0002), so "the tenant" is the one row the local ``tenants`` table
holds. None, or several, means the key creation cannot be attributed to a
compliance record, and the seam refuses before touching the key file — an
unattributed key creation is not permitted.

Everything fails closed through the domain's closed refusal vocabulary
(``EncryptionRefused.reason``); nothing is coerced, regenerated, or
defaulted. Purposes are the closed ``KeyPurpose`` enum; the §1 protected
class has no member and no path here (ruling R2).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant
from app.adapters.secrets.tenant_keys import TenantKey, TenantKeyStore, open_envelope, seal
from app.application import audit
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.encryption import KEY_UNKNOWN, EncryptionRefused, KeyPurpose


class EncryptionSeamRefused(Exception):
    """The seam could not attribute the key creation to a tenant.

    ``reason`` is ``no_local_tenant`` or ``local_tenant_ambiguous`` — the
    same vocabulary ``commands.py`` uses for the identical question.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def encrypt_for_tenant(
    session: AsyncSession, *, purpose: KeyPurpose, plaintext: str
) -> str:
    """Seal ``plaintext`` under the tenant key, recording the key's creation.

    The tenant row lock is taken before the key file is checked, so a
    concurrent first use waits, re-reads, and seals under the persisted key.
    After the key is in hand, its creation is ensured recorded (see the
    module docstring: one entry per key id, self-healing after a rollback).
    """
    store = TenantKeyStore(settings.tenant_key_file)
    tenant_id = await _locked_tenant_id(session)
    key, _created = store.load_or_create()
    await _ensure_creation_recorded(session, tenant_id, key)
    return seal(key, purpose=purpose, plaintext=plaintext)


async def decrypt_for_tenant(
    session: AsyncSession, *, purpose: KeyPurpose, envelope: str
) -> str:
    """Open one envelope sealed for ``purpose``; refuses on the closed vocabulary.

    A deployment that has never created its key has sealed nothing: the
    absent key refuses as ``key_unknown`` rather than minting a key as a
    side effect of a read. The key's creation is ensured recorded, same
    self-heal as the encrypt path.
    """
    key = TenantKeyStore(settings.tenant_key_file).load()
    if key is None:
        raise EncryptionRefused(KEY_UNKNOWN)
    await _heal_creation_record(session, key)
    return open_envelope(key, purpose=purpose.value, envelope=envelope)


async def _ensure_creation_recorded(
    session: AsyncSession, tenant_id: UUID, key: TenantKey
) -> None:
    """Record the key's creation unless the chain already carries its id.

    Called with the tenant row locked, so the check-and-record cannot race
    a concurrent first use: the waiter sees the winner's committed entry
    once its own lock grants.
    """
    if await _recorded_for_tenant(session, tenant_id, key.key_id):
        return
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ENCRYPTION_KEY_CREATED,
        entity_type="tenant_encryption_key",
        details={"key_id": key.key_id},
    )


async def _heal_creation_record(session: AsyncSession, key: TenantKey) -> None:
    """The self-heal: a key on disk with no chain entry gets one, idempotently.

    Fast path first — an entry visible anywhere means nothing to do and no
    lock taken. Otherwise take the tenant row lock and re-check: a
    concurrent first use may have recorded the entry while this session was
    on its way to the lock, and the invariant is one entry per key id.
    """
    if await _recorded_anywhere(session, key.key_id):
        return
    tenant_id = await _locked_tenant_id(session)
    if await _recorded_for_tenant(session, tenant_id, key.key_id):
        return
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ENCRYPTION_KEY_CREATED,
        entity_type="tenant_encryption_key",
        details={"key_id": key.key_id},
    )


async def _recorded_for_tenant(
    session: AsyncSession, tenant_id: UUID, key_id: str
) -> bool:
    """Whether ``tenant_id``'s chain already records this key id's creation."""
    rows = await session.scalars(
        select(AuditLog.details)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.ENCRYPTION_KEY_CREATED.value,
        )
        .order_by(AuditLog.seq)
    )
    return any(details.get("key_id") == key_id for details in rows)


async def _recorded_anywhere(session: AsyncSession, key_id: str) -> bool:
    """Whether any chain records this key id (single-tenant fast path)."""
    rows = await session.scalars(
        select(AuditLog.details).where(
            AuditLog.action == AuditAction.ENCRYPTION_KEY_CREATED.value
        )
    )
    return any(details.get("key_id") == key_id for details in rows)


async def _locked_tenant_id(session: AsyncSession) -> UUID:
    """The tenant whose audit chain records the key creation (ADR-0002), locked.

    ``with_for_update`` serializes first use of the key file across
    sessions: the winner persists the key and records it, the waiter
    re-reads the file once the lock grants and sees a key, not an absence.
    Exactly one row is the single-tenant deployment; none or several means
    the seam cannot say whose compliance record the creation belongs to, and
    it refuses before touching the key file.
    """
    tenant_ids = list(
        (
            await session.scalars(
                select(Tenant.id).order_by(Tenant.created_at).limit(2).with_for_update()
            )
        ).all()
    )
    if not tenant_ids:
        raise EncryptionSeamRefused("no_local_tenant")
    if len(tenant_ids) > 1:
        raise EncryptionSeamRefused("local_tenant_ambiguous")
    return tenant_ids[0]
