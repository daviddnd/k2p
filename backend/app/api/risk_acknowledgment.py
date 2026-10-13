"""The pre-use risk-acknowledgment routes (the 2026-10-04 founder rulings).

``GET /api/v1/risk-acknowledgment`` serves the full statement text with the
tenant's acknowledgment state — open to every active staff account, because
every role meets the first-run interstitial (ruling R5: the UI renders the
served text verbatim, never a paraphrase). ``POST`` records an
Administrator's confirmation through the application seam; the write is
Administrator-only (scope §2.15 assigns tenant-level configuration to the
Administrator), and the seam's idempotency rule makes a repeat POST a no-op.
A statement version bump re-opens the acknowledgment by construction — the
GET starts answering ``acknowledged: false`` until an Administrator confirms
the revised text.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_active, require_admin
from app.application import risk_acknowledgment
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/risk-acknowledgment", tags=["risk-acknowledgment"])


class RiskAcknowledgmentResponse(BaseModel):
    statement_version: str
    statement_text: str
    acknowledged: bool


@router.get("")
async def get_risk_acknowledgment(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_active())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RiskAcknowledgmentResponse:
    """The served statement and the tenant's acknowledgment state."""
    state = await risk_acknowledgment.get_acknowledgment_state(
        session, tenant_id=actor.tenant_id
    )
    return RiskAcknowledgmentResponse(
        statement_version=state.statement_version,
        statement_text=state.statement_text,
        acknowledged=state.acknowledged,
    )


@router.post("")
async def post_risk_acknowledgment(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RiskAcknowledgmentResponse:
    """Record the Administrator's confirmation; the resulting state answers."""
    state = await risk_acknowledgment.accept(
        session, tenant_id=actor.tenant_id, actor_user_id=actor.user_id
    )
    return RiskAcknowledgmentResponse(
        statement_version=state.statement_version,
        statement_text=state.statement_text,
        acknowledged=state.acknowledged,
    )
