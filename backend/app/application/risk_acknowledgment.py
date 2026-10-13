"""The acknowledgment read helper and the acceptance write (rulings R1/R2).

One read helper — ``is_acknowledged_current`` — derives whether the tenant's
latest acceptance event in the audit chain carries the served statement
version (the domain version-compare rule). Ruling R2: the API read and the
M6 pre-flight fact both call this helper, so the interstitial and
``decide_call`` cannot disagree about the same fact. One write — ``accept``
— appends the acceptance through the single audit write path
(``app.application.audit.record_event``; ruling R1: an audit event, no new
table, no migration). A tenant already current writes nothing, so the
re-POST is idempotent; the chain holds one acceptance per statement version.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.application import audit
from app.domain import risk_acknowledgment
from app.domain.audit import AuditAction

#: The acceptance payload's closed key — the statement version the operator
#: confirmed. The event's action vocabulary is ``AuditAction`` (its one
#: writer is this module); the entry's actor and timestamp are the audit
#: row's own fields.
DETAILS_STATEMENT_VERSION_KEY = "statement_version"


@dataclass(frozen=True)
class AcknowledgmentState:
    """What one caller needs: the served statement and whether the tenant's
    latest acceptance carries its version."""

    statement_version: str
    statement_text: str
    acknowledged: bool


async def is_acknowledged_current(session: AsyncSession, *, tenant_id: UUID) -> bool:
    """The one derivation (ruling R2): the tenant's latest acceptance event
    in the audit chain, compared against the served statement version. An
    absent chain, an old version, or an unreadable payload all fail closed
    to ``False`` — an unacknowledged tenant is the default state."""
    details = await session.scalar(
        select(AuditLog.details)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED.value,
        )
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    if not isinstance(details, dict):
        return False
    version = details.get(DETAILS_STATEMENT_VERSION_KEY)
    return risk_acknowledgment.acceptance_counts(version if isinstance(version, str) else None)


async def get_acknowledgment_state(
    session: AsyncSession, *, tenant_id: UUID
) -> AcknowledgmentState:
    """The statement plus the acknowledged flag — the API read's answer."""
    return AcknowledgmentState(
        statement_version=risk_acknowledgment.STATEMENT_VERSION,
        statement_text=risk_acknowledgment.STATEMENT_TEXT,
        acknowledged=await is_acknowledged_current(session, tenant_id=tenant_id),
    )


async def accept(
    session: AsyncSession, *, tenant_id: UUID, actor_user_id: UUID
) -> AcknowledgmentState:
    """Record the operator's confirmation; returns the resulting state.

    Already current is a no-op — no second audit entry (the re-POST is
    idempotent). Otherwise one acceptance event rides the request's session
    through the single write path, actor from the session, the served
    statement version in the payload (ruling R1). Append-only by design: a
    version bump never rewrites history, it only stops old acceptances from
    counting.
    """
    if await is_acknowledged_current(session, tenant_id=tenant_id):
        return await get_acknowledgment_state(session, tenant_id=tenant_id)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED,
        actor_user_id=actor_user_id,
        details={DETAILS_STATEMENT_VERSION_KEY: risk_acknowledgment.STATEMENT_VERSION},
    )
    return await get_acknowledgment_state(session, tenant_id=tenant_id)
