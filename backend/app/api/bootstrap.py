"""The first-run read: does this deployment have its tenant yet?

The desktop launcher polls this before deciding whether to open the first-run
wizard — it replaces the launcher's earlier empty-``POST /tenants`` probe with
an honest read. Guarded by the same setup-token seam as the bootstrap write:
armed, the header must match; disarmed, the read is open on loopback exactly
like the unguarded dev stack it fronts. Answers ``tenant_exists`` only — it
never discloses more of the tenant than a following bootstrap attempt would.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import Tenant
from app.adapters.persistence.session import get_session
from app.api.deps import require_setup_token

router = APIRouter(prefix="/api/v1", tags=["bootstrap"])


class BootstrapStateResponse(BaseModel):
    tenant_exists: bool


@router.get("/bootstrap-state")
async def bootstrap_state(
    _: Annotated[None, Depends(require_setup_token)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BootstrapStateResponse:
    tenant_id = await session.scalar(select(Tenant.id).limit(1))
    return BootstrapStateResponse(tenant_exists=tenant_id is not None)
