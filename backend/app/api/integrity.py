"""The integrity-verification route (ADR-0009/0011; scope §8; Review Focus 5).

``POST /api/v1/admin/integrity-check`` runs the one read-only seam and records
exactly one ``integrity_check`` audit entry — actor = the administrator,
details = the counts and the first ≤10 mismatch ids (the single domain rule
``audit_details``). The check itself never writes; this route owns the audit
entry, exactly as the loop owns its own copy with a system actor.

The honesty rules: a storage outage mid-check degrades to
``document_unreadable`` findings and still answers 200 with the real result —
a mismatch alerts, it changes nothing. Only a check that cannot run at all
(the database is down) is the named 503 ``integrity_check_unavailable``, and
then nothing is written. Administrator-only (``require_admin``), like the
retrospective scan: the check sweeps every company in the tenant.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.api.documents import get_storage
from app.application import audit, integrity
from app.domain.audit import AuditAction
from app.domain.integrity import MismatchKind, audit_details
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/admin", tags=["integrity"])


class IntegrityCheckRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1)


class MismatchItem(BaseModel):
    kind: MismatchKind
    document_id: UUID | None
    detail: str


class IntegrityCheckResponse(BaseModel):
    checked_at: datetime
    chain_verified: bool
    documents_checked: int
    mismatch_count: int
    mismatches: list[MismatchItem]


@router.post("/integrity-check")
async def run_integrity_check(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
    body: IntegrityCheckRequest | None = None,
) -> IntegrityCheckResponse:
    """Run the integrity check and record its one audit entry (Administrator-only).

    ``{limit}`` bounds the document leg for an operator's spot check; without
    it every registered document is re-hashed. A storage outage mid-check is
    part of the honest result, not an error — only a database the check
    cannot read at all is the named 503, and nothing is written then.
    """
    now = datetime.now(UTC)
    try:
        result = await integrity.run_integrity_check(
            session,
            store,
            tenant_id=actor.tenant_id,
            now=now,
            limit=None if body is None else body.limit,
        )
        await audit.record_event(
            session,
            tenant_id=actor.tenant_id,
            action=AuditAction.INTEGRITY_CHECK,
            actor_user_id=actor.user_id,
            details=audit_details(result),
        )
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="integrity_check_unavailable") from None
    return IntegrityCheckResponse(
        checked_at=now,
        chain_verified=result.chain_verified,
        documents_checked=result.documents_checked,
        mismatch_count=result.mismatch_count,
        mismatches=[
            MismatchItem(
                kind=mismatch.kind, document_id=mismatch.document_id, detail=mismatch.detail
            )
            for mismatch in result.mismatches
        ],
    )
