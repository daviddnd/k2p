"""Prior-filing ingestion orchestration (scope §2.1; data-model §2.7, §2.9;
contract §5.1 Ingestion row).

- ``extract_prior_filing`` is the **gated automation trigger**: it consults
  the single automation-gating seam (``service_packages.check_automation``)
  with ``AutomationType.DOCUMENT_EXTRACTION`` and fails closed with the
  gate's reason, then reads the stored document through the document-store
  port, runs the extractor adapter, and persists one run — the batch, its
  proposed records, and the §2.7 assessment.
- ``get_batch`` is the review-queue read: the batch, its records, and the
  newest assessment for the document it read (assessments accumulate per
  subject, §2.7), and ``list_batches`` is the company view's list — every
  run for the company with its decision counts, no records loaded.
- ``confirm_record`` / ``reject_record`` are the staff decisions.
  Confirmation writes master data **only** through the existing single
  write paths: ``company_master.record_fact`` for effective-dated facts —
  the fact group's row is assembled from the batch's own records, so a
  group written together stays together — and
  ``company_master.correct_statics`` for the anchor statics, which are
  Administrator-only everywhere else in the product, so this service
  refuses them unless the caller states the actor may write statics (the
  API layer's administrative decision, made with the single RBAC rule).
  Rejection writes nothing.

Confirming and rejecting are deliberately **not** gated automation: they
are ordinary staff work on proposals that already exist, and must keep
working on a lapsed licence (ADR-0012 — the customer is never locked out
of their own data). Extraction proposals are `pending` on arrival and only
a confirmation makes them authoritative (scope §2.1).

Services flush but never commit — the API session dependency commits at the
request edge, so a failed request leaves no partial writes.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.pdf.extractor import EXTRACTOR_MODEL, ExtractionResult, extract
from app.adapters.persistence import company_master as fact_tables
from app.adapters.persistence.company_master import _EffectiveDatedFact
from app.adapters.persistence.prior_filing import (
    ExtractionAssessment,
    ImportBatch,
    ImportBatchRecord,
)
from app.application import audit, company_master, documents, service_packages
from app.domain.audit import AuditAction
from app.domain.documents import DocumentKind
from app.domain.prior_filing import (
    AssessmentComponent,
    AssessmentSubject,
    FormCode,
    RecordStatus,
    TargetSpec,
    TargetWrite,
    ValueKind,
    overall_score,
    routing_for,
    target_spec,
)
from app.domain.service_packages import AutomationType, GatingReason

# This slice reads NAR1 into company master data. ND2A's registry targets
# are change events (a later slice's entity), so asking for it fails closed
# rather than producing a batch of proposals nothing can apply.
SUPPORTED_EXTRACTION_FORMS = frozenset({FormCode.NAR1})

# Fact group → the table the confirmation writes. ``group`` names in
# app.domain.prior_filing target specs are these keys, keeping the domain
# free of ORM knowledge.
_FACT_MODELS: dict[str, type[_EffectiveDatedFact]] = {
    "company_name": fact_tables.CompanyNameRecord,
    "contact": fact_tables.CompanyContactFact,
    "filing": fact_tables.CompanyFilingFact,
    "registered_office": fact_tables.RegisteredOfficeRecord,
    "business_nature": fact_tables.BusinessNatureRecord,
}

# What a complete fact group needs, mirroring the tables' own NOT NULL
# columns and CHECK constraints: each inner tuple is an "at least one of"
# requirement, a singleton being a plain requirement. A filing that does
# not state a whole group cannot be confirmed into it — the reviewer enters
# the rest of the group by hand instead (the CSV import makes the same
# all-or-nothing call for addresses).
_FACT_GROUP_REQUIRED: dict[str, tuple[tuple[str, ...], ...]] = {
    "company_name": (("name_en", "name_zh"),),
    "contact": (("email", "contact_phone_hk"),),
    "registered_office": (("flat",), ("building",), ("street",), ("district",)),
    "business_nature": (("business_nature_code",), ("business_nature_description",)),
}


class AutomationDeniedError(Exception):
    """The §5.1 gate refused the extraction; ``reason`` names the failing leg."""

    def __init__(self, reason: GatingReason) -> None:
        self.reason = reason
        super().__init__(f"document extraction is not licensed for this company: {reason.value}")


class NotAPriorFilingError(Exception):
    """The document is registered under a kind that is not a prior filing."""


class UnsupportedFormError(Exception):
    """The named form's registry targets are outside this slice's vocabulary."""


class BatchNotFoundError(Exception):
    """No batch with this id exists on the company (cross-tenant → unknown)."""


class RecordNotFoundError(Exception):
    """No record with this id exists on the batch."""


class RecordAlreadyDecidedError(Exception):
    """The record is already confirmed or rejected; decisions are final."""


class EffectiveDateRequiredError(Exception):
    """Neither the filing nor the reviewer supplied an effective date."""


class IncompleteFactGroupError(Exception):
    """The batch does not state a whole fact group; nothing is fabricated."""

    def __init__(self, group: str, missing: tuple[str, ...]) -> None:
        self.group = group
        self.missing = missing
        super().__init__(
            f"the filing does not state {group}: {', '.join(missing)} still required"
        )


class StaticTargetConfirmationError(Exception):
    """A static target was offered for confirmation without administrative rights."""


@dataclass(frozen=True)
class ExtractionRun:
    """One batch with its records and the assessment that produced them."""

    batch: ImportBatch
    records: list[ImportBatchRecord]
    assessment: ExtractionAssessment | None


@dataclass(frozen=True)
class BatchSummary:
    """One extraction run as a company's batch list shows it.

    The company view needs to name the run and say how much of it still
    waits on staff *without* loading every record of every batch; the counts
    are the pending-ingestion widget's own vocabulary, grouped per batch
    instead of per company.
    """

    batch: ImportBatch
    records_total: int
    records_pending: int
    records_confirmed: int
    records_rejected: int


async def extract_prior_filing(
    session: AsyncSession,
    store: documents.DocumentStore,
    *,
    tenant_id: UUID,
    company_id: UUID,
    document_id: UUID,
    form: FormCode,
    actor_user_id: UUID | None,
) -> ExtractionRun:
    """Read one stored prior filing into a review batch (gated automation).

    Fails closed before reading anything when the §5.1 gate denies the
    action (``AutomationDeniedError``) and before writing anything when the
    document is unknown (``documents.DocumentNotFoundError``,
    ``company_master.CompanyNotFoundError``), is not a prior filing
    (``NotAPriorFilingError``), or names a form outside this slice
    (``UnsupportedFormError``). A ``needs_ocr`` document still produces a
    batch: the run happened, and its assessment records why nothing was
    read.
    """
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        automation_type=AutomationType.DOCUMENT_EXTRACTION,
        as_of=date.today(),
    )
    if not decision.allowed:
        assert decision.reason is not None  # denied decisions always carry one
        raise AutomationDeniedError(decision.reason)
    document = await documents.get_document(
        session, tenant_id=tenant_id, company_id=company_id, document_id=document_id
    )
    # ADR-0009: a document that is not scanned clean is never processed, and
    # extraction is the processing path. The refusal is the documents
    # registry's own (``DocumentNotAvailableError``), raised before any read.
    documents.ensure_usable(document)
    if document.kind != DocumentKind.PRIOR_FILING.value:
        raise NotAPriorFilingError(
            f"document kind {document.kind!r} is not a prior filing"
        )
    if form not in SUPPORTED_EXTRACTION_FORMS:
        raise UnsupportedFormError(
            f"extraction of form {form.value!r} is not part of this slice"
        )

    result = extract(await store.get_bytes(document.storage_key), form=form)

    batch = ImportBatch(
        tenant_id=tenant_id,
        company_id=document.company_id,
        document_id=document.id,
        form=form.value,
        shape=result.shape.value,
        created_by=actor_user_id,
    )
    session.add(batch)
    await session.flush()
    records = [
        ImportBatchRecord(
            batch_id=batch.id,
            tenant_id=tenant_id,
            target=field.target,
            value=field.value,
            confidence=_confidence(field.confidence),
            status=RecordStatus.PENDING.value,
            # Only effective-dated facts carry the filing's own date; the
            # statics are corrected in place and are not time-bound.
            effective_date=(
                result.made_up_date
                if target_spec(field.target).write is TargetWrite.FACT
                else None
            ),
        )
        for field in result.fields
    ]
    session.add_all(records)
    field_scores = {field.target: field.confidence for field in result.fields}
    score = overall_score(field_scores)
    routing = routing_for(field_scores)
    assessment = ExtractionAssessment(
        tenant_id=tenant_id,
        subject_type=AssessmentSubject.DOCUMENT.value,
        subject_id=document.id,
        component=AssessmentComponent.DOCUMENT_EXTRACTOR.value,
        # Named by the reader that did the reading: the geometry reader for
        # the digital shapes, the OCR pipeline plus its engine for a scan.
        model=result.model,
        field_scores=field_scores,
        signal_log=_signal_log(result),
        # Nothing assessed (a shape with no readable field) has no score and
        # therefore no routing — never a misleading zero.
        overall_score=None if score is None else _confidence(score),
        routing=None if routing is None else routing.value,
    )
    session.add(assessment)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.PRIOR_FILING_EXTRACTED,
        actor_user_id=actor_user_id,
        company_id=batch.company_id,
        entity_type="import_batch",
        entity_id=batch.id,
        details={
            "document_id": document.id,
            "form": batch.form,
            "shape": batch.shape,
            "records": len(records),
            "routing": assessment.routing,
        },
    )
    return ExtractionRun(batch=batch, records=records, assessment=assessment)


async def get_batch(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, batch_id: UUID
) -> ExtractionRun:
    """One batch with its records; cross-tenant ids are unknown (404).

    The assessment returned is the newest for the batch's document — §2.7
    lets one subject accumulate assessments (a re-read adds another).
    """
    batch = await _get_batch(
        session, tenant_id=tenant_id, company_id=company_id, batch_id=batch_id
    )
    return ExtractionRun(
        batch=batch,
        records=await _records_of(session, batch=batch),
        assessment=await _latest_assessment(session, tenant_id=tenant_id, batch=batch),
    )


async def list_batches(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[BatchSummary]:
    """One company's extraction runs, newest first, with their decision counts.

    A run always leaves a batch row, including one that proposed nothing (a
    scan the reader refused), so the list carries those too and each summary
    says how many records it holds. Counts come from one grouped query: the
    list never loads the records themselves.
    """
    await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = (
        await session.execute(
            select(ImportBatch, ImportBatchRecord.status, func.count(ImportBatchRecord.id))
            .outerjoin(ImportBatchRecord, ImportBatchRecord.batch_id == ImportBatch.id)
            .where(
                ImportBatch.tenant_id == tenant_id,
                ImportBatch.company_id == company_id,
            )
            .group_by(ImportBatch.id, ImportBatchRecord.status)
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        )
    ).all()
    batches: dict[UUID, ImportBatch] = {}
    counts: dict[UUID, dict[str, int]] = {}
    for batch, status, count in rows:
        batches.setdefault(batch.id, batch)
        by_status = counts.setdefault(batch.id, {})
        if status is not None:
            by_status[status] = count
    summaries: list[BatchSummary] = []
    for batch_id, batch in batches.items():
        by_status = counts[batch_id]
        summaries.append(
            BatchSummary(
                batch=batch,
                records_total=sum(by_status.values()),
                records_pending=by_status.get(RecordStatus.PENDING.value, 0),
                records_confirmed=by_status.get(RecordStatus.CONFIRMED.value, 0),
                records_rejected=by_status.get(RecordStatus.REJECTED.value, 0),
            )
        )
    return summaries


async def confirm_record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    batch_id: UUID,
    record_id: UUID,
    actor_user_id: UUID | None,
    can_write_statics: bool,
    effective_date: date | None = None,
) -> ImportBatchRecord:
    """Confirm one proposed record: write it, then mark it decided.

    ``can_write_statics`` is the API layer's administrative decision
    (``app.domain.rbac.is_admin`` via the shared dependency): the two
    static targets are corrected only through the Administrator-only
    statics path, so a caller without that right gets
    ``StaticTargetConfirmationError`` rather than a weakened rule.

    Effective-dated targets need a date: the filing's own made-up date
    when the extractor read one, otherwise the reviewer's
    (``EffectiveDateRequiredError``); a supplied date always wins. The
    fact group's row is assembled from this batch's records for that group
    (rejected siblings excluded), and a group the filing does not state in
    full is refused (``IncompleteFactGroupError``) instead of padded with
    nulls.
    """
    batch = await _get_batch(
        session, tenant_id=tenant_id, company_id=company_id, batch_id=batch_id
    )
    record = await _get_record(session, batch=batch, record_id=record_id)
    if record.status != RecordStatus.PENDING.value:
        raise RecordAlreadyDecidedError(f"record is already {record.status}")
    spec = target_spec(record.target)
    stated_effective_date = record.effective_date
    supplied = effective_date if effective_date is not None else stated_effective_date

    match spec.write:
        case TargetWrite.STATIC:
            if not can_write_statics:
                raise StaticTargetConfirmationError(
                    f"{record.target} is an anchor static: correcting it is an "
                    "administrator action"
                )
            await company_master.correct_statics(
                session,
                tenant_id=tenant_id,
                company_id=batch.company_id,
                actor_user_id=actor_user_id,
                changes={_column(spec): record.value},
            )
        case TargetWrite.FACT:
            if supplied is None:
                raise EffectiveDateRequiredError(
                    f"{record.target} has no effective date: the filing does not "
                    "state one, so the reviewer must supply it"
                )
            payload = await _fact_payload(session, batch=batch, spec=spec, value=record.value)
            await company_master.record_fact(
                session,
                model=_FACT_MODELS[_group(spec)],
                tenant_id=tenant_id,
                company_id=batch.company_id,
                effective_date=supplied,
                created_by=actor_user_id,
                source_type="import_batch",
                source_id=batch.id,
                **payload,
            )
            # The record carries the decided date, so the review queue shows
            # what was written; the audit entry below keeps both dates.
            record.effective_date = supplied
        case _:  # TargetWrite.CONTEXT is read-only run metadata, never a record
            raise ValueError(f"target {record.target!r} is not a confirmable target")

    record.status = RecordStatus.CONFIRMED.value
    record.decided_by = actor_user_id
    record.decided_at = datetime.now(UTC)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.IMPORT_RECORD_CONFIRMED,
        actor_user_id=actor_user_id,
        company_id=batch.company_id,
        entity_type="import_batch_record",
        entity_id=record.id,
        details={
            "batch_id": batch.id,
            "document_id": batch.document_id,
            "target": record.target,
            "value": record.value,
            "confidence": record.confidence,
            # What the filing stated, and the date actually written.
            "stated_effective_date": stated_effective_date,
            "effective_date": supplied if spec.write is TargetWrite.FACT else None,
        },
    )
    return record


async def reject_record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    batch_id: UUID,
    record_id: UUID,
    actor_user_id: UUID | None,
) -> ImportBatchRecord:
    """Reject one proposed record: nothing is written to master data.

    Rejection is the same decision work as confirmation and is not gated
    (ADR-0012) — a rejected proposal stays in the batch as the record of
    what the filing said and what staff decided about it.
    """
    batch = await _get_batch(
        session, tenant_id=tenant_id, company_id=company_id, batch_id=batch_id
    )
    record = await _get_record(session, batch=batch, record_id=record_id)
    if record.status != RecordStatus.PENDING.value:
        raise RecordAlreadyDecidedError(f"record is already {record.status}")
    record.status = RecordStatus.REJECTED.value
    record.decided_by = actor_user_id
    record.decided_at = datetime.now(UTC)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.IMPORT_RECORD_REJECTED,
        actor_user_id=actor_user_id,
        company_id=batch.company_id,
        entity_type="import_batch_record",
        entity_id=record.id,
        details={
            "batch_id": batch.id,
            "document_id": batch.document_id,
            "target": record.target,
            "value": record.value,
        },
    )
    return record


async def _get_batch(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, batch_id: UUID
) -> ImportBatch:
    """The batch, or a not-found — cross-tenant ids are indistinguishable."""
    await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    batch = await session.scalar(
        select(ImportBatch).where(
            ImportBatch.id == batch_id,
            ImportBatch.company_id == company_id,
            ImportBatch.tenant_id == tenant_id,
        )
    )
    if batch is None:
        raise BatchNotFoundError
    return batch


async def latest_batch_values(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, form: FormCode
) -> dict[str, str]:
    """The latest run's read values for one form, keyed by target — the §3.6 basis.

    The draft's prior-filing cross-check compares the new draft against "the
    company's most recent prior filing of the same form type". That is the
    company's newest batch **for that form** (newest ``created_at``), and its
    records whose status is not ``rejected``: a rejection is staff's decision
    that the filing did not state that value, while a record still pending is
    the filing's own read — which is exactly what the cross-check is about. The
    reviewer's corrections are not a second source here: a confirmed record
    still carries the filing's value (``confirm_record`` writes that value;
    only the effective date can be supplied by hand).

    No batch → an empty map, which the caller reads as "nothing to compare" —
    every ``changed`` flag false and no ``unexpected_change`` flag, because
    there is no prior filing to have changed from. A read: no writes, no audit
    entries.
    """
    batch: ImportBatch | None = await session.scalar(
        select(ImportBatch)
        .where(
            ImportBatch.tenant_id == tenant_id,
            ImportBatch.company_id == company_id,
            ImportBatch.form == form.value,
        )
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(1)
    )
    if batch is None:
        return {}
    rows = await session.scalars(
        select(ImportBatchRecord)
        .where(
            ImportBatchRecord.batch_id == batch.id,
            ImportBatchRecord.status != RecordStatus.REJECTED.value,
        )
        .order_by(ImportBatchRecord.target)
    )
    return {record.target: record.value for record in rows.all()}


async def batch_document_ids(
    session: AsyncSession, *, tenant_id: UUID, batch_ids: Collection[UUID]
) -> dict[UUID, UUID]:
    """The stored document behind each of these runs, keyed by batch id.

    The draft's provenance reads this: a fact an import batch wrote is
    ``backfill_document_id`` — the filing the value was read from is the
    document the batch read, which is what a reviewer following the value back
    wants to open. One query for every batch a generation met, rather than one
    per fact row; a batch that no longer resolves is simply absent from the map
    (the caller falls back to the fact row itself).
    """
    if not batch_ids:
        return {}
    rows = await session.execute(
        select(ImportBatch.id, ImportBatch.document_id).where(
            ImportBatch.tenant_id == tenant_id, ImportBatch.id.in_(batch_ids)
        )
    )
    return {batch_id: document_id for batch_id, document_id in rows.all()}


async def _get_record(
    session: AsyncSession, *, batch: ImportBatch, record_id: UUID
) -> ImportBatchRecord:
    record = await session.scalar(
        select(ImportBatchRecord).where(
            ImportBatchRecord.id == record_id,
            ImportBatchRecord.batch_id == batch.id,
            ImportBatchRecord.tenant_id == batch.tenant_id,
        )
    )
    if record is None:
        raise RecordNotFoundError
    return record


async def _records_of(session: AsyncSession, *, batch: ImportBatch) -> list[ImportBatchRecord]:
    """The batch's records in target order (stable for the review queue)."""
    rows = await session.scalars(
        select(ImportBatchRecord)
        .where(ImportBatchRecord.batch_id == batch.id)
        .order_by(ImportBatchRecord.target)
    )
    return list(rows.all())


async def _latest_assessment(
    session: AsyncSession, *, tenant_id: UUID, batch: ImportBatch
) -> ExtractionAssessment | None:
    assessment: ExtractionAssessment | None = await session.scalar(
        select(ExtractionAssessment)
        .where(
            ExtractionAssessment.tenant_id == tenant_id,
            ExtractionAssessment.subject_id == batch.document_id,
            ExtractionAssessment.component == AssessmentComponent.DOCUMENT_EXTRACTOR.value,
        )
        .order_by(ExtractionAssessment.created_at.desc(), ExtractionAssessment.id)
        .limit(1)
    )
    return assessment


async def _fact_payload(
    session: AsyncSession, *, batch: ImportBatch, spec: TargetSpec, value: str
) -> dict[str, object]:
    """One confirmed fact row, assembled from the batch's own records.

    A fact group is written whole: the row carries the confirmed value plus
    every other record of the same group in this batch that was not
    rejected, and nothing the filing did not state.
    """
    group = _group(spec)
    payload: dict[str, object] = {}
    for sibling in await _records_of(session, batch=batch):
        if sibling.status == RecordStatus.REJECTED.value:
            continue
        sibling_spec = target_spec(sibling.target)
        if sibling_spec.write is not TargetWrite.FACT or _group(sibling_spec) != group:
            continue
        payload[_column(sibling_spec)] = _typed_value(sibling_spec, sibling.value)
    payload[_column(spec)] = _typed_value(spec, value)
    missing = [
        " or ".join(alternative)
        for alternative in _FACT_GROUP_REQUIRED.get(group, ())
        if not any(column in payload for column in alternative)
    ]
    if missing:
        raise IncompleteFactGroupError(group, tuple(missing))
    return payload


def _group(spec: TargetSpec) -> str:
    assert spec.group is not None  # every FACT target names its group
    return spec.group


def _column(spec: TargetSpec) -> str:
    assert spec.column is not None  # every writable target names its column
    return spec.column


def _typed_value(spec: TargetSpec, value: str) -> object:
    """The stored string as the column's Python type (the domain rule made
    the string canonical, so this cannot fail)."""
    match spec.value_kind:
        case ValueKind.INTEGER:
            return int(value)
        case ValueKind.DECIMAL:
            return Decimal(value)
        case _:
            return value


def _confidence(score: float) -> Decimal:
    """A confidence score within the column's ``Numeric(4, 3)`` scale."""
    return Decimal(str(score)).quantize(Decimal("0.001"))


def _signal_log(result: ExtractionResult) -> dict[str, object]:
    """The §2.7 signal log: the shape, per-field signals, and why a target
    produced no proposal."""
    return {
        "shape": result.shape.value,
        "fields": {
            field.target: [field.origin.value, *(signal.value for signal in field.signals)]
            for field in result.fields
        }
        | {
            target: [signal.value for signal in signals]
            for target, signals in result.skipped.items()
        },
        "document": [signal.value for signal in result.document_signals],
    }


__all__ = [
    "EXTRACTOR_MODEL",
    "SUPPORTED_EXTRACTION_FORMS",
    "AutomationDeniedError",
    "BatchNotFoundError",
    "BatchSummary",
    "EffectiveDateRequiredError",
    "ExtractionRun",
    "IncompleteFactGroupError",
    "NotAPriorFilingError",
    "RecordAlreadyDecidedError",
    "RecordNotFoundError",
    "StaticTargetConfirmationError",
    "UnsupportedFormError",
    "batch_document_ids",
    "confirm_record",
    "extract_prior_filing",
    "get_batch",
    "latest_batch_values",
    "list_batches",
    "reject_record",
]
