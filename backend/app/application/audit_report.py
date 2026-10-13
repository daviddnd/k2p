"""The one read path for the per-company audit compliance report (scope §8;
DoD 19; contract §5.1 Audit row; ruling R6).

``build_company_report`` composes the report the route returns:

1. The company identity block (name, BR number, CR number) comes from the
   existing company-master read seams — the anchor row via
   ``app.application.company_master.get_company`` and the display name via
   ``app.application.compliance.display_names`` (ADR-0007's one as-of
   resolution; a second name read would be a second answer).
2. The tenant's **full** audit chain is read in ``seq`` order and verified
   with the single domain rule ``app.domain.audit.verify_chain`` — ruling
   R6: a company-filtered subset cannot re-verify its own chain because
   ``prev_hash`` links span companies, so the report embeds the tenant-wide
   verdict (Review Focus 4).
3. The company's entries are the chain-ordered rows whose ``company_id``
   matches — filter, never re-order.

The report is computed at read time, never stored (house style: computed
values are never columns); the path writes nothing and emits no audit entry
(reads are not audited, ADR-0009).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.application import company_master, compliance
from app.domain.audit import verify_chain
from app.domain.audit_report import ChainVerdict, build_report


async def build_company_report(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    generated_at: datetime,
) -> dict[str, Any]:
    """The company's compliance report dict; ``CompanyNotFoundError`` when the
    id is not one of the tenant's (unknown and cross-tenant are one answer).

    ``generated_at`` is the caller's clock read — the route owns the wall
    clock, this layer stays a pure composition over what it is given.
    """
    company = await company_master.get_company(
        session, tenant_id=tenant_id, company_id=company_id
    )
    name = (await compliance.display_names(session, [company.id], as_of=date.today())).get(
        company.id
    )
    chain = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.seq)
            )
        ).all()
    )
    verdict = ChainVerdict(checked_entries=len(chain), problems=tuple(verify_chain(chain)))
    company_block = {
        "company_id": str(company.id),
        "name": name,
        "br_number": company.br_number,
        "cr_number": company.cr_number,
    }
    entries = [entry for entry in chain if entry.company_id == company.id]
    return build_report(
        company_block, entries, verdict, generated_at=generated_at
    )
