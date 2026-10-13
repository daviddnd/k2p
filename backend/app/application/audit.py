"""Audit orchestration (ADR-0009): the single write path for audit entries.

``record_event`` is the only code path that appends to ``audit_log``:

1. It takes a row lock on the tenant row, serializing chain appends per
   tenant — including the genesis append, where there is no chain head to
   lock (ADR-0009 notes hash chaining serializes inserts per chain;
   acceptable at MVP volume).
2. It reads the tenant's current chain head and computes the new entry's
   hash from its full content plus that link, via the single domain rule
   (``app.domain.audit.compute_entry_hash``).
3. ``details``/``llm_call`` are canonicalized to ``jsonable`` form before
   hashing *and* storage, so the stored content is exactly the hashed
   content — recomputation during integrity verification always matches.

Callers write their mutation and the audit entry on the same session, so
both commit or roll back together at the request edge (ADR-0009: no code
path mutates workflow state without an audit entry). Services flush but
never commit.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant
from app.domain.audit import AuditAction, compute_entry_hash, jsonable


async def record_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    action: AuditAction,
    actor_user_id: UUID | None = None,
    company_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    details: Mapping[str, Any] | None = None,
    llm_call: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Append one audit entry to the tenant's chain; returns the entry.

    ``actor_user_id`` is ``None`` for system actors. ``company_id`` scopes
    the event to a company (the per-company compliance report, scope §8,
    filters on it); both are pinned to ``tenant_id`` by composite foreign
    keys. ``details`` carries the before/after summary (ADR-0009).
    """
    # Serialize chain appends per tenant, genesis included: the tenant row
    # lock is taken before the chain head is read, so two concurrent
    # appends can never compute the same link.
    await session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    prev_hash = await session.scalar(
        select(AuditLog.entry_hash)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    occurred_at = datetime.now(UTC)
    stored_details = jsonable(dict(details)) if details is not None else {}
    stored_llm_call = jsonable(dict(llm_call)) if llm_call is not None else None
    entry = AuditLog(
        tenant_id=tenant_id,
        occurred_at=occurred_at,
        actor_user_id=actor_user_id,
        company_id=company_id,
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        details=stored_details,
        llm_call=stored_llm_call,
        prev_hash=prev_hash,
        entry_hash=compute_entry_hash(
            tenant_id=tenant_id,
            occurred_at=occurred_at,
            actor_user_id=actor_user_id,
            company_id=company_id,
            action=action.value,
            entity_type=entity_type,
            entity_id=entity_id,
            details=stored_details,
            llm_call=stored_llm_call,
            prev_hash=prev_hash,
        ),
    )
    session.add(entry)
    await session.flush()
    return entry
