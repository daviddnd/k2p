"""Company-master endpoints (data-model §5.1 Companies; ADR-0007).

Authorization (founder decision 2026-08-24): creating and listing companies
is Administrator-only (``require_admin``) — portfolio composition is an
administrative function. The CSV portfolio import (``POST /companies/import``,
data-model §5.1.1) is Administrator-only on the same rule and ungated in
every tier. Company-scoped routes (read statics, read master data, append
facts) are grant-gated (``require_company_access``): Administrators pass for
any tenant company, General staff need a per-company access grant; no §2.9
permission flag is required because those four flags gate only the
review/approve workflow.

Mutations route through ``app.application.company_master`` — the only write
path for company facts (ADR-0007). ``PATCH`` appends new effective-dated
fact records; there are no raw row-edit endpoints for facts. Anchor statics
have exactly one correction path: ``PATCH /companies/{id}/statics``
(option (b) decision 2026-08-24) — Administrator-only, overwrite (not
effective-dated), for BRN changes and data-entry errors on the other
statics. The per-company AI surface (slice M6) has one contracted write,
Administrator-only: ``POST /companies/{id}/ai-disabled`` (the deliberate
objection flag, scope §7 — the TCSP's objection-handling valve per ADR-0019
§2). It rides the ``CompanyResponse`` shape.

Creation is cap-gated (entitlement design D6): ``POST /companies`` returns
409 ``company_cap_exceeded`` once the tenant holds as many companies as its
tier allows, and the CSV import rejects only the individual rows that would
exceed the cap. Reading and updating existing companies is never gated.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence import company_master as fact_tables
from app.adapters.persistence.models import Company
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin, require_company_access
from app.application import company_import, company_master, entitlement
from app.application.company_master import CompanyMasterSnapshot
from app.domain.company_master import CompanyStatus, CompanyType
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/companies", tags=["companies"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"
# Import body cap (data-model §5.1.1): ~5000 fully-populated rows (~2000
# pathological ones) — an order of magnitude above the pilot target
# portfolio range (50-500 companies, scope §11), still memory-trivial.
_MAX_IMPORT_BYTES = 10_485_760  # 10 MiB


async def _read_capped_body(request: Request) -> bytes:
    """The request body, bounded: 413 as soon as the cap is exceeded.

    A present ``Content-Length`` over the cap is rejected before any bytes
    are read; the stream itself is then capped, so an absent or understated
    header cannot cause an unbounded read.
    """
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _MAX_IMPORT_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail="CSV body exceeds the 10 MiB limit; split the file and import in batches",
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_IMPORT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="CSV body exceeds the 10 MiB limit; split the file and import in batches",
            )
    return bytes(body)


# --- Request payloads ---


class CreateCompanyRequest(BaseModel):
    """``Company`` anchor statics (data-model §2.3).

    Facts are not accepted here: they are appended with explicit effective
    dates via ``PATCH`` so every time-bound value enters through the
    supersession-owning write path.
    """

    br_number: str = Field(min_length=1, max_length=20)
    cr_number: str | None = Field(default=None, max_length=20)
    date_of_incorporation: date
    anniversary_date: date
    type: CompanyType


class AddressPayload(BaseModel):
    """The §2.5 Address value object; ``country`` is omitted for HK addresses."""

    flat: str = Field(min_length=1, max_length=200)
    building: str = Field(min_length=1, max_length=200)
    street: str = Field(min_length=1, max_length=200)
    district: str = Field(min_length=1, max_length=100)
    country: str | None = Field(default=None, max_length=100)


class NameFactPayload(BaseModel):
    # Mirrors the DB check: at least one registered name is required.
    name_en: str | None = Field(default=None, min_length=1, max_length=300)
    name_zh: str | None = Field(default=None, min_length=1, max_length=300)
    business_name: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def _some_name_required(self) -> "NameFactPayload":
        if self.name_en is None and self.name_zh is None:
            raise ValueError("name_en or name_zh is required")
        return self


class StatusFactPayload(BaseModel):
    status: CompanyStatus


class BusinessNatureFactPayload(BaseModel):
    business_nature_code: str = Field(min_length=1, max_length=20)
    business_nature_description: str = Field(min_length=1, max_length=300)


class ContactFactPayload(BaseModel):
    # Mirrors the DB check: at least one contact channel is required.
    email: str | None = Field(default=None, max_length=320, pattern=_EMAIL_PATTERN)
    # HK subscriber number only; +852 is pre-printed on the form (§2.3).
    contact_phone_hk: str | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def _some_channel_required(self) -> "ContactFactPayload":
        if self.email is None and self.contact_phone_hk is None:
            raise ValueError("email or contact_phone_hk is required")
        return self


class FilingFactPayload(BaseModel):
    mortgages_and_charges_total: Decimal | None = Field(default=None, ge=0)
    # Only for companies without share capital (§2.3).
    member_count: int | None = Field(default=None, ge=0)


class RecordsKeptItem(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    address: AddressPayload


class RecordsKeptFactPayload(BaseModel):
    records_kept: list[RecordsKeptItem] = Field(min_length=1)


class ShareCapitalClassItem(BaseModel):
    class_name: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    total_shares: int = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    total_paid_up: Decimal = Field(ge=0)


class ShareCapitalClassesFactPayload(BaseModel):
    classes: list[ShareCapitalClassItem] = Field(min_length=1)


class CorrectStaticsRequest(BaseModel):
    """Overwrite correction for ``Company`` anchor statics (option (b)
    decision 2026-08-24).

    Omitted fields are unchanged; a field present with value ``null`` clears
    it (only ``cr_number`` is nullable). At least one field is required.
    """

    br_number: str | None = Field(default=None, min_length=1, max_length=20)
    cr_number: str | None = Field(default=None, max_length=20)
    date_of_incorporation: date | None = None
    anniversary_date: date | None = None
    type: CompanyType | None = None

    @model_validator(mode="after")
    def _some_field_required(self) -> "CorrectStaticsRequest":
        if not self.model_fields_set:
            raise ValueError("at least one statics field is required")
        return self


class SetAiDisabledRequest(BaseModel):
    """The per-company AI objection flag (scope §7): one boolean, asserted.

    ``strict`` so a string like ``"yes"`` is a 422, not a truthy coercion —
    the flag's value is the whole request, and a client that misspells it
    should hear so, not flip a switch it never named.
    """

    disabled: bool = Field(strict=True)


class PatchCompanyRequest(BaseModel):
    """One effective-dated update: every group present is appended as a new
    fact record sharing ``effective_date``. At least one group is required.
    """

    effective_date: date
    name: NameFactPayload | None = None
    registered_office: AddressPayload | None = None
    status: StatusFactPayload | None = None
    business_nature: BusinessNatureFactPayload | None = None
    contact: ContactFactPayload | None = None
    filing: FilingFactPayload | None = None
    records_kept: RecordsKeptFactPayload | None = None
    share_capital_classes: ShareCapitalClassesFactPayload | None = None

    @model_validator(mode="after")
    def _some_group_required(self) -> "PatchCompanyRequest":
        groups = (
            self.name,
            self.registered_office,
            self.status,
            self.business_nature,
            self.contact,
            self.filing,
            self.records_kept,
            self.share_capital_classes,
        )
        if all(group is None for group in groups):
            raise ValueError("at least one fact group is required")
        return self


# --- Responses ---


class ImportRowOutcomeResponse(BaseModel):
    """One CSV row's result (data-model §5.1.1)."""

    row: int
    br_number: str | None
    outcome: str
    company_id: UUID | None
    reason: str | None


class ImportReportResponse(BaseModel):
    """The per-row outcome report for one portfolio import."""

    created: int
    updated: int
    rejected: int
    rows: list[ImportRowOutcomeResponse]


class CompanyResponse(BaseModel):
    """The ``Company`` anchor statics plus the per-company AI surface."""

    id: UUID
    tenant_id: UUID
    cr_number: str | None
    br_number: str
    date_of_incorporation: date
    anniversary_date: date
    type: CompanyType
    ai_disabled: bool


class CompanyMasterDataResponse(BaseModel):
    """Statics plus each fact group resolved as of one date (ADR-0007).

    A group is ``None`` when nothing is in effect as of ``as_of`` (never
    written, or only future-dated). Each resolved group carries the
    ``effective_date`` of the record it resolved to.
    """

    company: CompanyResponse
    as_of: date
    name: dict[str, Any] | None
    registered_office: dict[str, Any] | None
    status: dict[str, Any] | None
    business_nature: dict[str, Any] | None
    contact: dict[str, Any] | None
    filing: dict[str, Any] | None
    records_kept: dict[str, Any] | None
    share_capital_classes: dict[str, Any] | None


def _company_response(company: Company) -> CompanyResponse:
    return CompanyResponse(
        id=company.id,
        tenant_id=company.tenant_id,
        cr_number=company.cr_number,
        br_number=company.br_number,
        date_of_incorporation=company.date_of_incorporation,
        anniversary_date=company.anniversary_date,
        type=CompanyType(company.type),
        ai_disabled=company.ai_disabled,
    )


def _fact_response(record: Any, *fields: str) -> dict[str, Any] | None:
    """One resolved fact group: ``effective_date`` plus its payload fields."""
    if record is None:
        return None
    return {"effective_date": record.effective_date} | {
        field: getattr(record, field) for field in fields
    }


def _master_data_response(
    snapshot: CompanyMasterSnapshot, as_of: date
) -> CompanyMasterDataResponse:
    return CompanyMasterDataResponse(
        company=_company_response(snapshot.company),
        as_of=as_of,
        name=_fact_response(snapshot.name, "name_en", "name_zh", "business_name"),
        registered_office=_fact_response(
            snapshot.registered_office, "flat", "building", "street", "district", "country"
        ),
        status=_fact_response(snapshot.status, "status"),
        business_nature=_fact_response(
            snapshot.business_nature, "business_nature_code", "business_nature_description"
        ),
        contact=_fact_response(snapshot.contact, "email", "contact_phone_hk"),
        filing=_fact_response(
            snapshot.filing, "mortgages_and_charges_total", "member_count"
        ),
        records_kept=_fact_response(snapshot.records_kept, "records_kept"),
        share_capital_classes=_fact_response(snapshot.share_capital_classes, "classes"),
    )


# --- Routes ---


@router.post("", status_code=201)
async def create_company(
    payload: CreateCompanyRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Create the ``Company`` anchor row in the actor's tenant.

    The body carries no ``tenant_id`` — the company lands in the actor's own
    tenant, so cross-tenant creation is impossible by construction. 409
    ``company_cap_exceeded`` once the tenant holds as many companies as its
    entitlement allows.
    """
    try:
        company = await company_master.create_company(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            br_number=payload.br_number,
            cr_number=payload.cr_number,
            date_of_incorporation=payload.date_of_incorporation,
            anniversary_date=payload.anniversary_date,
            type=payload.type.value,
        )
    except company_master.BrnAlreadyInUseError:
        raise HTTPException(status_code=409, detail="br_number already in use") from None
    except entitlement.CompanyCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _company_response(company)


@router.get("")
async def list_companies(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CompanyResponse]:
    """Every company in the actor's tenant (anchor statics only)."""
    companies = await company_master.list_companies(session, tenant_id=actor.tenant_id)
    return [_company_response(company) for company in companies]


@router.post("/import")
async def import_companies(
    request: Request,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    data_as_of: Annotated[date, Query()],
) -> ImportReportResponse:
    """Bulk portfolio onboarding from one CSV file (data-model §5.1.1).

    The body is the raw CSV text (UTF-8; a BOM is tolerated); ``data_as_of``
    is the date the exported data reflects and the default effective date
    for every fact the file writes. Administrator-only — portfolio
    composition is an administrative function, same as create/list.
    Match-or-create on the normalized BRN; per-row best-effort with an
    outcome report. Ungated in every tier. Rows creating a company beyond
    the entitlement company cap are rejected individually (the file's
    updates and in-cap creates still commit). Bodies over
    ``_MAX_IMPORT_BYTES`` are rejected with 413 — split the file and import
    in batches (match-or-create makes batched imports convergent).
    """
    csv_bytes = await _read_capped_body(request)
    try:
        csv_text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="body must be UTF-8 CSV text") from None
    try:
        report = await company_import.import_companies_csv(
            session,
            tenant_id=actor.tenant_id,
            created_by=actor.user_id,
            csv_text=csv_text,
            data_as_of=data_as_of,
        )
    except company_import.ImportHeaderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ImportReportResponse(
        created=report.created,
        updated=report.updated,
        rejected=report.rejected,
        rows=[ImportRowOutcomeResponse(**vars(outcome)) for outcome in report.outcomes],
    )


@router.get("/{company_id}")
async def get_company(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """One company's anchor statics; 404 for unknown or cross-tenant ids."""
    try:
        company = await company_master.get_company(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except company_master.CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return _company_response(company)


@router.patch("/{company_id}/statics")
async def correct_company_statics(
    company_id: UUID,
    payload: CorrectStaticsRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Correct anchor statics by overwrite (option (b) decision 2026-08-24).

    Administrator-only: unlike fact appends, statics correction is not
    ordinary master-data maintenance, so granted General staff are denied.
    Supersession semantics do not apply — statics are not effective-dated.
    The correction is audit-logged with before/after values (ADR-0009).
    """
    changes: dict[str, Any] = payload.model_dump(include=payload.model_fields_set)
    if "type" in changes and changes["type"] is not None:
        changes["type"] = changes["type"].value
    try:
        company = await company_master.correct_statics(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            changes=changes,
        )
    except company_master.CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except company_master.BrnAlreadyInUseError:
        raise HTTPException(status_code=409, detail="br_number already in use") from None
    return _company_response(company)


@router.post("/{company_id}/ai-disabled")
async def set_company_ai_disabled(
    company_id: UUID,
    payload: SetAiDisabledRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Set the per-company AI objection flag (scope §7; slice M6).

    Administrator-only, the anchor-statics precedent: flipping the switch
    that blocks every LLM call for a company is not ordinary staff
    master-data maintenance. Every assertion is audit-logged with the
    before/after and the actor (ADR-0009) — the chain, not the column, is
    the history of who objected and when. 404 for an unknown or cross-tenant
    company.
    """
    try:
        company = await company_master.set_ai_disabled(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            disabled=payload.disabled,
        )
    except company_master.CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return _company_response(company)


@router.get("/{company_id}/master-data")
async def get_master_data(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[date | None, Query()] = None,
) -> CompanyMasterDataResponse:
    """Statics plus every fact group resolved as of ``as_of`` (default: today)."""
    as_of = as_of if as_of is not None else date.today()
    try:
        snapshot = await company_master.get_company_master(
            session, tenant_id=actor.tenant_id, company_id=company_id, as_of=as_of
        )
    except company_master.CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return _master_data_response(snapshot, as_of)


@router.patch("/{company_id}")
async def patch_company(
    company_id: UUID,
    payload: PatchCompanyRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyMasterDataResponse:
    """Append effective-dated fact records; returns the snapshot as of the
    patch's ``effective_date`` so the response shows exactly what the write
    resolved to.

    Each call fans out to ``record_fact`` per group present — the service
    owns the supersession chain; no row is ever edited in place. Facts are
    recorded with ``source_type="manual"`` and the actor as creator.
    """
    try:
        if payload.name is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.CompanyNameRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                name_en=payload.name.name_en,
                name_zh=payload.name.name_zh,
                business_name=payload.name.business_name,
            )
        if payload.registered_office is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.RegisteredOfficeRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                **payload.registered_office.model_dump(),
            )
        if payload.status is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.CompanyStatusRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                status=payload.status.status.value,
            )
        if payload.business_nature is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.BusinessNatureRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                **payload.business_nature.model_dump(),
            )
        if payload.contact is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.CompanyContactFact,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                email=payload.contact.email,
                contact_phone_hk=payload.contact.contact_phone_hk,
            )
        if payload.filing is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.CompanyFilingFact,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                mortgages_and_charges_total=payload.filing.mortgages_and_charges_total,
                member_count=payload.filing.member_count,
            )
        if payload.records_kept is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.RecordsKeptRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                records_kept=[
                    item.model_dump() for item in payload.records_kept.records_kept
                ],
            )
        if payload.share_capital_classes is not None:
            await company_master.record_fact(
                session,
                model=fact_tables.ShareCapitalClassRecord,
                tenant_id=actor.tenant_id,
                company_id=company_id,
                effective_date=payload.effective_date,
                created_by=actor.user_id,
                classes=[
                    item.model_dump(mode="json")
                    for item in payload.share_capital_classes.classes
                ],
            )
        snapshot = await company_master.get_company_master(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            as_of=payload.effective_date,
        )
    except company_master.CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return _master_data_response(snapshot, payload.effective_date)
