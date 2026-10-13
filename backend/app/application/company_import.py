"""CSV portfolio-import orchestration (data-model §5.1.1; ADR-0013 Level 1).

Bulk onboarding of company master records from one CSV upload. This module
is a *caller* of the single write path, not a new one: every company is
created via ``app.application.company_master.create_company`` and every fact
is appended via ``record_fact`` with ``source_type="import_batch"``, so the
ADR-0007 supersession chain is maintained exactly as for manual edits.

Semantics (founder-approved design 2026-08-24):

- **Match-or-create** on the normalized BRN. A matched company is only ever
  appended to — import never overwrites anchor statics (corrections go
  through ``correct_statics``, the single statics write path) and never
  deletes or clears facts.
- **Per-row best-effort**: rows that fail validation are rejected with their
  reason; valid rows still commit.
- Re-importing the same file is convergent: creates become matches, and
  identical-``effective_date`` fact appends supersede via the §1
  ``created_at`` tie-break (guaranteed by ``clock_timestamp()`` even within
  this one transaction, migration 0004).
- The entitlement company cap is enforced by ``company_master.create_company``
  (the single write path): a row that would create a company beyond the cap
  is rejected like any other invalid row. Best-effort keeps the file's
  updates and in-cap creates committing, so re-importing a full portfolio
  is never blocked by the cap (entitlement design D6).

Import results are not persisted (D4): the response report is the record;
fact ``source_id`` stays null. Every per-row write is audited by the single
write paths (their events carry ``source_type="import_batch"``), and the
orchestrator emits one batch-level ``companies_imported`` summary event
(ADR-0009).
"""

import csv
import io
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence import company_master as fact_tables
from app.adapters.persistence.models import Company
from app.application import audit, company_master, entitlement
from app.domain.audit import AuditAction
from app.domain.company_import import (
    ImportHeaderError,
    ImportRowError,
    ParsedImportRow,
    parse_import_row,
    validate_header,
)

__all__ = [
    "ImportHeaderError",
    "ImportReport",
    "ImportRowOutcome",
    "import_companies_csv",
]


@dataclass(frozen=True)
class ImportRowOutcome:
    """The per-row result: what happened, to which company, or why not."""

    row: int  # 1-based data-row number (the header is row 0)
    br_number: str | None  # the row's BRN cell, raw — for locating the row
    outcome: Literal["created", "updated", "rejected"]
    company_id: UUID | None
    reason: str | None


@dataclass(frozen=True)
class ImportReport:
    """Per-row outcomes plus the summary counts the response carries."""

    outcomes: list[ImportRowOutcome]
    created: int
    updated: int
    rejected: int


async def import_companies_csv(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    created_by: UUID,
    csv_text: str,
    data_as_of: date,
) -> ImportReport:
    """Import one CSV portfolio file into the tenant (match-or-create).

    ``tenant_id``/``created_by`` come from the authenticated actor, never
    from the file. Raises ``ImportHeaderError`` before any write when the
    header fails the §5.1.1 column vocabulary. Services flush but never
    commit — the request edge commits, so a header failure leaves no writes.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    validate_header(reader.fieldnames or [])

    outcomes: list[ImportRowOutcome] = []
    for row_number, raw_row in enumerate(reader, start=1):
        raw_brn = (raw_row.get("br_number") or "").strip() or None
        if raw_row.get(None) is not None:
            # DictReader parks surplus cells under the None key: the row has
            # more values than the header has columns.
            outcomes.append(
                ImportRowOutcome(row_number, raw_brn, "rejected", None,
                                 "row has more cells than the header has columns")
            )
            continue
        try:
            parsed = parse_import_row(raw_row, data_as_of=data_as_of)
        except ImportRowError as exc:
            outcomes.append(ImportRowOutcome(row_number, raw_brn, "rejected", None, str(exc)))
            continue
        try:
            company, outcome = await _match_or_create(
                session, tenant_id=tenant_id, parsed=parsed, created_by=created_by
            )
        except entitlement.CompanyCapExceededError as exc:
            # A create beyond the entitlement cap is this row's failure, not
            # the file's: updates and in-cap creates still commit.
            outcomes.append(ImportRowOutcome(row_number, raw_brn, "rejected", None, str(exc)))
            continue
        await _record_facts(session, company=company, parsed=parsed, created_by=created_by)
        outcomes.append(ImportRowOutcome(row_number, raw_brn, outcome, company.id, None))

    report = ImportReport(
        outcomes=outcomes,
        created=sum(1 for outcome in outcomes if outcome.outcome == "created"),
        updated=sum(1 for outcome in outcomes if outcome.outcome == "updated"),
        rejected=sum(1 for outcome in outcomes if outcome.outcome == "rejected"),
    )
    # The batch-level entry: per-row writes are already audited by the
    # single write paths above (their events carry source_type
    # "import_batch"). The report itself is not persisted (D4).
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANIES_IMPORTED,
        actor_user_id=created_by,
        details={
            "created": report.created,
            "updated": report.updated,
            "rejected": report.rejected,
            "data_as_of": data_as_of,
        },
    )
    return report


async def _match_or_create(
    session: AsyncSession, *, tenant_id: UUID, parsed: ParsedImportRow, created_by: UUID
) -> tuple[Company, Literal["created", "updated"]]:
    """The normalized-BRN match: existing anchor, or a newly created one.

    Statics are written only at create; a matched company's statics are
    untouched (option (b) / D3 — corrections go through ``correct_statics``).
    """
    existing = await company_master.find_company_by_brn(
        session, tenant_id=tenant_id, br_number=parsed.br_number
    )
    if existing is not None:
        return existing, "updated"
    company = await company_master.create_company(
        session,
        tenant_id=tenant_id,
        actor_user_id=created_by,
        br_number=parsed.br_number,
        cr_number=parsed.cr_number,
        date_of_incorporation=parsed.date_of_incorporation,
        anniversary_date=parsed.anniversary_date,
        type=parsed.type.value,
    )
    return company, "created"


async def _record_facts(
    session: AsyncSession, *, company: Company, parsed: ParsedImportRow, created_by: UUID
) -> None:
    """Append one fact record per populated group via the single write path.

    The name fact is always present (§5.1.1 requires a name per row); every
    other group is appended only when its columns carried values. All writes
    share the row's ``effective_date`` and ``source_type="import_batch"``.
    """
    await company_master.record_fact(
        session,
        model=fact_tables.CompanyNameRecord,
        tenant_id=company.tenant_id,
        company_id=company.id,
        effective_date=parsed.effective_date,
        created_by=created_by,
        source_type="import_batch",
        name_en=parsed.name_en,
        name_zh=parsed.name_zh,
        business_name=parsed.business_name,
    )
    if parsed.registered_office is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.RegisteredOfficeRecord,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            **parsed.registered_office,
        )
    if parsed.status is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.CompanyStatusRecord,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            status=parsed.status,
        )
    if parsed.business_nature_code is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.BusinessNatureRecord,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            business_nature_code=parsed.business_nature_code,
            business_nature_description=parsed.business_nature_description,
        )
    if parsed.email is not None or parsed.contact_phone_hk is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.CompanyContactFact,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            email=parsed.email,
            contact_phone_hk=parsed.contact_phone_hk,
        )
    if parsed.mortgages_and_charges_total is not None or parsed.member_count is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.CompanyFilingFact,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            mortgages_and_charges_total=parsed.mortgages_and_charges_total,
            member_count=parsed.member_count,
        )
    if parsed.records_kept is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.RecordsKeptRecord,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            records_kept=parsed.records_kept,
        )
    if parsed.share_capital_classes is not None:
        await company_master.record_fact(
            session,
            model=fact_tables.ShareCapitalClassRecord,
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=parsed.effective_date,
            created_by=created_by,
            source_type="import_batch",
            classes=parsed.share_capital_classes,
        )
