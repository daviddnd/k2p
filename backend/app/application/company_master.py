"""Company-master orchestration: the only write path for company facts.

Per ADR-0007 ("every create/update path must go through services that
maintain the supersession chain"), all writes to the ``Company`` anchor and
the eight effective-dated fact tables in
``app.adapters.persistence.company_master`` go through this module:

- ``create_company`` inserts the anchor row, normalizing the BRN first so
  the ``(tenant_id, br_number)`` unique constraint catches duplicates
  regardless of input formatting.
- ``record_fact`` appends one effective-dated fact row and maintains the
  supersession chain: validity windows are half-open intervals
  ``[valid_from, valid_to)``; the previously-current record is closed, never
  edited beyond its ``valid_to``, and rows are never deleted.
- ``correct_statics`` is the one sanctioned overwrite path (option (b)
  decision 2026-08-24): anchor statics are corrected in place, not
  effective-dated, for BRN changes and data-entry errors on the other
  statics. Administrator-only at the API layer.
- ``set_ai_disabled`` is the company-row write path for the per-company AI
  objection flag (slice M6): an overwrite path with the audit chain as the
  history.
- ``get_company`` / ``list_companies`` are the anchor-row read paths.
- ``get_company_master`` is the as-of read path, resolving every fact group
  through the single domain rule ``resolve_as_of``.

Every write path emits its audit event on the same session via
``app.application.audit.record_event`` (ADR-0009), so the change and its
audit entry commit or roll back together at the request edge.

Services flush but never commit — the API session dependency
(``app.adapters.persistence.session.get_session``) commits at the request
edge on a clean return, so a failed request leaves no partial writes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.company_master import (
    BusinessNatureRecord,
    CompanyContactFact,
    CompanyFilingFact,
    CompanyNameRecord,
    CompanyStatusRecord,
    RecordsKeptRecord,
    RegisteredOfficeRecord,
    ShareCapitalClassRecord,
    _EffectiveDatedFact,
)
from app.adapters.persistence.models import Company
from app.application import audit, calendar_sync, entitlement
from app.domain.audit import AuditAction
from app.domain.company_master import normalize_brn
from app.domain.effective_dating import resolve_as_of


class BrnAlreadyInUseError(Exception):
    """The normalized BRN is already taken by another company in the tenant."""


class CompanyNotFoundError(Exception):
    """No company with this id exists in the tenant.

    Cross-tenant ids are indistinguishable from unknown ids: existence is
    never leaked across tenant boundaries.
    """


@dataclass(frozen=True)
class CompanyMasterSnapshot:
    """The company statics plus each fact group resolved as of one date."""

    company: Company
    name: CompanyNameRecord | None
    registered_office: RegisteredOfficeRecord | None
    status: CompanyStatusRecord | None
    business_nature: BusinessNatureRecord | None
    contact: CompanyContactFact | None
    filing: CompanyFilingFact | None
    records_kept: RecordsKeptRecord | None
    share_capital_classes: ShareCapitalClassRecord | None


async def get_company(session: AsyncSession, *, tenant_id: UUID, company_id: UUID) -> Company:
    """The company, or ``CompanyNotFoundError`` when it is not in the tenant."""
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.tenant_id == tenant_id)
    )
    if company is None:
        raise CompanyNotFoundError
    return company


async def list_companies(session: AsyncSession, *, tenant_id: UUID) -> list[Company]:
    """Every company in the tenant, ordered by BRN for a stable listing.

    Unpaginated in this slice: the Free tier caps portfolios at 100 companies
    (ADR-0012); revisit when a larger deployment needs it.
    """
    rows = await session.scalars(
        select(Company).where(Company.tenant_id == tenant_id).order_by(Company.br_number)
    )
    return list(rows.all())


async def find_company_by_brn(
    session: AsyncSession, *, tenant_id: UUID, br_number: str
) -> Company | None:
    """The tenant's company with this BRN, or ``None`` — the import match seam.

    The BRN is normalized before lookup so formatting variants
    (``"1234 5678"`` vs ``"12345678"``) match the same anchor row. Used by
    the CSV import's match-or-create path; cross-tenant BRNs are invisible.
    """
    normalized_brn = normalize_brn(br_number)
    company = await session.scalar(
        select(Company).where(
            Company.tenant_id == tenant_id, Company.br_number == normalized_brn
        )
    )
    return company


async def create_company(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    br_number: str,
    date_of_incorporation: date,
    anniversary_date: date,
    type: str,
    cr_number: str | None = None,
) -> Company:
    """Insert the ``Company`` anchor row (data-model §2.3 statics).

    ``tenant_id`` is taken as an explicit parameter, never from client
    input. The BRN is normalized before write; the duplicate check mirrors
    the ``(tenant_id, br_number)`` unique constraint, which remains the
    database backstop. The entitlement company cap is enforced here — the
    single write path every creation route (including CSV import) uses — so
    a tenant at its cap raises ``CompanyCapExceededError``; existing rows
    stay fully usable (ADR-0012).
    """
    normalized_brn = normalize_brn(br_number)
    taken = await session.scalar(
        select(Company.id).where(
            Company.tenant_id == tenant_id, Company.br_number == normalized_brn
        )
    )
    if taken is not None:
        raise BrnAlreadyInUseError
    await entitlement.ensure_company_available(session, tenant_id=tenant_id)
    company = Company(
        tenant_id=tenant_id,
        cr_number=cr_number,
        br_number=normalized_brn,
        date_of_incorporation=date_of_incorporation,
        anniversary_date=anniversary_date,
        type=type,
    )
    session.add(company)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANY_CREATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="company",
        entity_id=company.id,
        details={"br_number": company.br_number, "type": company.type},
    )
    return company


async def correct_statics(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> Company:
    """Overwrite ``Company`` anchor statics in place (option (b) decision
    2026-08-24).

    This is the one sanctioned exception to append-only writes: statics are
    not effective-dated, so corrections (BRN changes, data-entry errors on
    ``cr_number`` / ``date_of_incorporation`` / ``anniversary_date`` /
    ``type``) overwrite the anchor row. Supported ``changes`` keys are exactly
    those five fields; a key present with value ``None`` clears a nullable
    field. A corrected BRN is normalized before write; when the normalized
    value is already held by another company in the tenant,
    ``BrnAlreadyInUseError`` is raised (the unique constraint remains the
    backstop). Cross-tenant ids are indistinguishable from unknown ones via
    the shared ``get_company`` seam.
    """
    company = await get_company(session, tenant_id=tenant_id, company_id=company_id)
    before = {field: getattr(company, field) for field in changes}
    if "br_number" in changes:
        normalized_brn = normalize_brn(changes["br_number"])
        taken = await session.scalar(
            select(Company.id).where(
                Company.tenant_id == tenant_id,
                Company.br_number == normalized_brn,
                Company.id != company.id,
            )
        )
        if taken is not None:
            raise BrnAlreadyInUseError
        company.br_number = normalized_brn
    for field in ("cr_number", "date_of_incorporation", "anniversary_date", "type"):
        if field in changes:
            setattr(company, field, changes[field])
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANY_STATICS_CORRECTED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="company",
        entity_id=company.id,
        # The stored values, not the raw request: a corrected BRN is
        # recorded normalized, as written.
        details={
            "before": before,
            "after": {field: getattr(company, field) for field in changes},
        },
    )
    if {"anniversary_date", "type"} & changes.keys():
        # Ruling R4: these are the corrections that move a due date (the
        # anniversary recomputes every NAR1 obligation; the type decides
        # whether a rule exists at all), so the push sweep is asked to
        # reconcile — a no-op when no loop runs, never an error here.
        calendar_sync.request_push_sweep(tenant_id)
    return company


async def set_ai_disabled(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    disabled: bool,
) -> Company:
    """Set the per-company AI objection flag (scope §7; slice M6).

    The deliberate human opt-out that blocks every LLM call for the company
    — the gateway's second leg, independent of the opt-in register, so
    either mechanism alone stops a call and both default safe. Overwrite
    semantics: every call records the assertion and its actor, and the audit
    chain carries the before/after even when they agree. Administrator-only
    at the API layer.
    """
    company = await get_company(session, tenant_id=tenant_id, company_id=company_id)
    before = company.ai_disabled
    company.ai_disabled = disabled
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANY_AI_DISABLED_UPDATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="company",
        entity_id=company.id,
        details={"before": before, "after": disabled},
    )
    return company


async def record_fact[FactT: _EffectiveDatedFact](
    session: AsyncSession,
    *,
    model: type[FactT],
    tenant_id: UUID,
    company_id: UUID,
    effective_date: date,
    created_by: UUID | None,
    source_type: str = "manual",
    source_id: UUID | None = None,
    **payload: Any,
) -> FactT:
    """Append one effective-dated fact row, maintaining the supersession chain.

    The new row's window is ``[effective_date, next_effective_date)`` where
    the upper bound is the earliest existing record with a later
    ``effective_date`` (a backdated insert), or open-ended when there is
    none. The record ``resolve_as_of`` would pick over existing rows as of
    ``effective_date`` is closed with ``valid_to = effective_date`` — a
    same-day correction therefore leaves the superseded record an empty
    window, recording it as fully superseded. No column other than a
    superseded record's ``valid_to`` is ever updated; rows are never
    deleted (ADR-0007).
    """
    company = await get_company(session, tenant_id=tenant_id, company_id=company_id)
    existing = (
        await session.scalars(select(model).where(model.company_id == company.id))
    ).all()
    superseded = resolve_as_of(existing, effective_date)
    next_effective = min(
        (row.effective_date for row in existing if row.effective_date > effective_date),
        default=None,
    )
    record = model(
        tenant_id=company.tenant_id,
        company_id=company.id,
        effective_date=effective_date,
        valid_from=effective_date,
        valid_to=next_effective,
        source_type=source_type,
        source_id=source_id,
        created_by=created_by,
        **payload,
    )
    session.add(record)
    if superseded is not None:
        superseded.valid_to = effective_date
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=company.tenant_id,
        action=AuditAction.COMPANY_FACT_RECORDED,
        actor_user_id=created_by,
        company_id=company.id,
        entity_type=model.__tablename__,
        entity_id=record.id,
        details={
            "effective_date": effective_date,
            "source_type": source_type,
            "payload": payload,
        },
    )
    return record


async def _resolve_one[FactT: _EffectiveDatedFact](
    session: AsyncSession, model: type[FactT], company_id: UUID, as_of: date
) -> FactT | None:
    """The record of one fact group in effect as of ``as_of``, or ``None``."""
    rows = (await session.scalars(select(model).where(model.company_id == company_id))).all()
    return resolve_as_of(rows, as_of)


async def get_company_master(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, as_of: date
) -> CompanyMasterSnapshot:
    """The company statics plus every fact group resolved as of ``as_of``.

    Groups with nothing in effect (never written, or only future-dated)
    resolve to ``None``. This is the read path for
    ``GET /companies/{id}/master-data?as_of=``.
    """
    company = await get_company(session, tenant_id=tenant_id, company_id=company_id)
    return CompanyMasterSnapshot(
        company=company,
        name=await _resolve_one(session, CompanyNameRecord, company.id, as_of),
        registered_office=await _resolve_one(session, RegisteredOfficeRecord, company.id, as_of),
        status=await _resolve_one(session, CompanyStatusRecord, company.id, as_of),
        business_nature=await _resolve_one(session, BusinessNatureRecord, company.id, as_of),
        contact=await _resolve_one(session, CompanyContactFact, company.id, as_of),
        filing=await _resolve_one(session, CompanyFilingFact, company.id, as_of),
        records_kept=await _resolve_one(session, RecordsKeptRecord, company.id, as_of),
        share_capital_classes=await _resolve_one(
            session, ShareCapitalClassRecord, company.id, as_of
        ),
    )
