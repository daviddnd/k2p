"""The per-company audit compliance report route (contract §5.1 Audit row;
DoD 19; scope §8).

``GET /companies/{id}/audit-report`` is the per-company compliance export —
uncapped in every tier (ADR-0012: the customer's own data is never behind
the licence gate). Grant-gated via ``require_company_access`` with no
permission flag, exactly like the other company-scoped reads: reading a
client's audit trail is ordinary staff work on the company's own data.
Administrators pass for any tenant company; a General account needs its
access grant. Unknown and cross-tenant ids are one 404, the tenant-scoped
read the application layer owns.

The response is the report the application read path composes: the fixed
``report_version``, the generation instant, the company identity block, the
tenant-chain verification verdict (a company filter never re-verifies a
subset — ruling R6's ``scope`` note travels with it), and the company's
entries in chain order as stored. The route is a pure read: it writes
nothing and emits no audit event.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_company_access
from app.application import audit_report
from app.application.company_master import CompanyNotFoundError
from app.domain.audit_report import ReportArea
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/companies", tags=["audit-report"])


class CompanyIdentityResponse(BaseModel):
    company_id: UUID
    name: str | None
    br_number: str
    cr_number: str | None


class ChainVerdictResponse(BaseModel):
    verified: bool
    checked_entries: int
    problems: list[str]
    scope: str


class ReportEntryResponse(BaseModel):
    seq: int
    occurred_at: datetime
    actor: UUID | None
    action: str
    area: ReportArea
    entity_type: str | None
    entity_id: UUID | None
    details: dict[str, Any]


class AuditReportResponse(BaseModel):
    report_version: int
    generated_at: datetime
    company: CompanyIdentityResponse
    chain: ChainVerdictResponse
    entries: list[ReportEntryResponse]


@router.get("/{company_id}/audit-report")
async def get_company_audit_report(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuditReportResponse:
    """The company's audit compliance report, computed at read time.

    The ``chain`` block carries the tenant-wide verification verdict, not a
    re-verification of the company's subset (``prev_hash`` links span
    companies; see the block's ``scope`` note).
    """
    try:
        report = await audit_report.build_company_report(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            generated_at=datetime.now(UTC),
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return AuditReportResponse.model_validate(report)
