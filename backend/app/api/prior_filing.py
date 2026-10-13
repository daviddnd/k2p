"""Prior-filing ingestion and review-queue endpoints (scope §2.1; contract
§2.7/§2.9, §5.1 Ingestion row).

``POST /companies/{id}/import-batches`` triggers one extraction run over a
stored prior filing. It is **gated automation** (scope §2.15, founder
decision 2026-09-21): the trigger consults the single automation-gating seam
with ``AutomationType.DOCUMENT_EXTRACTION`` and fails closed, surfacing the
gate's reason so the dashboard can say which leg denied it. Uploading the
document stays ungated in every tier — the gate is on turning it into data.

``GET .../import-batches`` is the company view's list: every run for the
company with its decision counts, newest first, so the review screen can
show what exists without loading every record.

``GET .../import-batches/{id}`` is the review queue's read: the batch, its
records (pending, confirmed, rejected), and the assessment behind them.

``POST .../records/{id}/confirm|reject`` are the staff decisions and are
**not** gated: they are ordinary work on proposals that already exist, and
must keep working on a lapsed licence (ADR-0012 — the customer is never
locked out of their own data). Confirmation of one of the two anchor
statics requires an Administrator, because statics are corrected only
through the Administrator-only statics path (option (b), 2026-08-24); the
check uses the single RBAC decision, not a per-route rule.

All four routes are grant-gated via ``require_company_access`` — reviewing
extracted prior-filing data is staff work on the company, the same rule as
fact appends. Every mutation emits its audit event in the same transaction
(ADR-0009).
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.prior_filing import (
    ExtractionAssessment,
    ImportBatch,
    ImportBatchRecord,
)
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_company_access
from app.api.documents import get_storage
from app.application import documents, prior_filing
from app.application.company_master import BrnAlreadyInUseError, CompanyNotFoundError
from app.application.documents import DocumentNotFoundError
from app.domain.prior_filing import FormCode, UnknownTargetError
from app.domain.rbac import ActorContext, is_admin

router = APIRouter(prefix="/api/v1/companies", tags=["prior-filing"])


class TriggerExtractionRequest(BaseModel):
    """Which stored document to read, and as which form.

    The form is named by the caller because the ``documents`` row does not
    carry a form code: the upload's ``form=`` query parameter only shapes
    the storage key, and inferring the form from a flattened or scanned
    page is the registration problem the OCR slice (E2) takes on.
    """

    document_id: UUID
    form: FormCode


class ImportBatchRecordResponse(BaseModel):
    """One proposed field and the staff decision on it."""

    id: UUID
    target: str
    value: str
    confidence: float
    status: str
    effective_date: date | None
    decided_by: UUID | None
    decided_at: datetime | None


class ExtractionAssessmentResponse(BaseModel):
    """The §2.7 diagnostics behind the batch's routing.

    ``signal_log`` carries the input shape, the per-field signals, and the
    targets that produced no proposal; ``field_scores`` maps target →
    confidence and ``overall_score`` is their minimum.
    """

    id: UUID
    subject_type: str
    subject_id: UUID
    component: str
    model: str
    field_scores: dict[str, float]
    signal_log: dict[str, Any]
    overall_score: float | None
    routing: str | None
    created_at: datetime


class ImportBatchResponse(BaseModel):
    """One extraction run with its records and assessment."""

    id: UUID
    company_id: UUID
    document_id: UUID
    form: str
    shape: str
    created_by: UUID | None
    created_at: datetime
    records: list[ImportBatchRecordResponse]
    assessment: ExtractionAssessmentResponse | None


class ImportBatchSummaryResponse(BaseModel):
    """One run in the company's batch list — counts, never the records.

    The review screen shows the per-record detail for the batch the reviewer
    picked; the list only has to say which runs exist and how much of each
    still waits on staff.
    """

    id: UUID
    company_id: UUID
    document_id: UUID
    form: str
    shape: str
    created_by: UUID | None
    created_at: datetime
    records_total: int
    records_pending: int
    records_confirmed: int
    records_rejected: int


def _record_response(record: ImportBatchRecord) -> ImportBatchRecordResponse:
    return ImportBatchRecordResponse(
        id=record.id,
        target=record.target,
        value=record.value,
        confidence=float(record.confidence),
        status=record.status,
        effective_date=record.effective_date,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
    )


def _assessment_response(assessment: ExtractionAssessment) -> ExtractionAssessmentResponse:
    return ExtractionAssessmentResponse(
        id=assessment.id,
        subject_type=assessment.subject_type,
        subject_id=assessment.subject_id,
        component=assessment.component,
        model=assessment.model,
        field_scores={target: float(score) for target, score in assessment.field_scores.items()},
        signal_log=assessment.signal_log,
        overall_score=(
            None if assessment.overall_score is None else float(assessment.overall_score)
        ),
        routing=assessment.routing,
        created_at=assessment.created_at,
    )


def _batch_response(batch: ImportBatch, run: prior_filing.ExtractionRun) -> ImportBatchResponse:
    return ImportBatchResponse(
        id=batch.id,
        company_id=batch.company_id,
        document_id=batch.document_id,
        form=batch.form,
        shape=batch.shape,
        created_by=batch.created_by,
        created_at=batch.created_at,
        records=[_record_response(record) for record in run.records],
        assessment=None if run.assessment is None else _assessment_response(run.assessment),
    )


def _summary_response(summary: prior_filing.BatchSummary) -> ImportBatchSummaryResponse:
    batch = summary.batch
    return ImportBatchSummaryResponse(
        id=batch.id,
        company_id=batch.company_id,
        document_id=batch.document_id,
        form=batch.form,
        shape=batch.shape,
        created_by=batch.created_by,
        created_at=batch.created_at,
        records_total=summary.records_total,
        records_pending=summary.records_pending,
        records_confirmed=summary.records_confirmed,
        records_rejected=summary.records_rejected,
    )


@router.post("/{company_id}/import-batches", status_code=201)
async def trigger_extraction(
    company_id: UUID,
    payload: TriggerExtractionRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ImportBatchResponse:
    """Extract one stored prior filing into a review batch (gated).

    403 with the gate's reason when extraction is not licensed for the
    company (tier capability, assignment, coverage, or the package's
    services); 404 for unknown companies and documents; 422 when the
    document is not a prior filing or names a form this slice does not
    read.
    """
    try:
        run = await prior_filing.extract_prior_filing(
            session,
            store,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            document_id=payload.document_id,
            form=payload.form,
            actor_user_id=actor.user_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except DocumentNotFoundError:
        raise HTTPException(status_code=404, detail="document not found") from None
    except documents.DocumentNotAvailableError as exc:
        raise HTTPException(
            status_code=409, detail=f"document_not_available: {exc.scan_status}"
        ) from None
    except prior_filing.AutomationDeniedError as exc:
        raise HTTPException(
            status_code=403, detail=f"automation_denied: {exc.reason.value}"
        ) from None
    except prior_filing.NotAPriorFilingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except prior_filing.UnsupportedFormError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _batch_response(run.batch, run)


@router.get("/{company_id}/import-batches")
async def list_batches(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ImportBatchSummaryResponse]:
    """The company's extraction runs, newest first, with their counts.

    Ungated like the rest of the review queue (ADR-0012): listing what the
    deployment already read is staff work on their own data, so it keeps
    working on a lapsed licence. 404 for an unknown or cross-tenant company.
    """
    try:
        summaries = await prior_filing.list_batches(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_summary_response(summary) for summary in summaries]


@router.get("/{company_id}/import-batches/{batch_id}")
async def get_batch(
    company_id: UUID,
    batch_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportBatchResponse:
    """One batch with its records and assessment (404 for unknown ids)."""
    try:
        run = await prior_filing.get_batch(
            session, tenant_id=actor.tenant_id, company_id=company_id, batch_id=batch_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except prior_filing.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="batch not found") from None
    return _batch_response(run.batch, run)


class ConfirmRecordRequest(BaseModel):
    """The reviewer's effective date, when the filing does not state one."""

    effective_date: date | None = None


@router.post("/{company_id}/import-batches/{batch_id}/records/{record_id}/confirm")
async def confirm_record(
    company_id: UUID,
    batch_id: UUID,
    record_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: ConfirmRecordRequest | None = None,
) -> ImportBatchRecordResponse:
    """Confirm one proposed record and write it to master data.

    Ungated (ADR-0012: staff decide their own data). 403 when the record is
    an anchor static and the actor is not an Administrator; 409 when the
    record is already decided; 422 when no effective date is available or
    the batch does not state its fact group in full.
    """
    try:
        record = await prior_filing.confirm_record(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            batch_id=batch_id,
            record_id=record_id,
            actor_user_id=actor.user_id,
            can_write_statics=is_admin(context),
            effective_date=None if payload is None else payload.effective_date,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except prior_filing.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="batch not found") from None
    except prior_filing.RecordNotFoundError:
        raise HTTPException(status_code=404, detail="record not found") from None
    except prior_filing.StaticTargetConfirmationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except prior_filing.RecordAlreadyDecidedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except BrnAlreadyInUseError:
        raise HTTPException(status_code=409, detail="br_number already in use") from None
    except (prior_filing.EffectiveDateRequiredError, prior_filing.IncompleteFactGroupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except UnknownTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _record_response(record)


@router.post("/{company_id}/import-batches/{batch_id}/records/{record_id}/reject")
async def reject_record(
    company_id: UUID,
    batch_id: UUID,
    record_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ImportBatchRecordResponse:
    """Reject one proposed record; nothing reaches master data.

    Ungated, like confirmation: a rejection is a decision about data the
    deployment already holds (ADR-0012).
    """
    try:
        record = await prior_filing.reject_record(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            batch_id=batch_id,
            record_id=record_id,
            actor_user_id=actor.user_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except prior_filing.BatchNotFoundError:
        raise HTTPException(status_code=404, detail="batch not found") from None
    except prior_filing.RecordNotFoundError:
        raise HTTPException(status_code=404, detail="record not found") from None
    except prior_filing.RecordAlreadyDecidedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _record_response(record)
