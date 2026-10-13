"""Draft endpoints (contract §4.2, §5.1; data-model §2.8; scope §2.9).

The §5.1 row, exactly:

- ``POST /companies/{id}/drafts`` — generate one draft version for a form and a
  target date. **The gated automation** (DoD §9 item 27): the route answers the
  §5.1 gate's own reason as ``403 automation_denied: <reason>`` and a denial
  writes nothing at all. Grant-gated per company with no permission flag — the
  trigger is ordinary staff work once the package allows it.
- ``GET /companies/{id}/drafts`` — the company's drafts: each lineage's current
  version, newest first. Grant-gated, no permission flag.
- ``GET /drafts/{id}`` — one draft: the current version with its full document
  (``resolved_state``, ``field_provenance``, ``qc_flags``, ``filing_metadata``,
  ``form_version``) plus the lineage's earlier versions as metadata.
- ``GET /drafts/{id}/transitions`` — the actions the actor may perform **now**,
  computed from the §4.2 table and the actor's legs (``review_drafts``,
  ``approve_drafts``, and the no-flag grant ``upload_signed`` carries). Unwired
  actions and system rows never appear: the frontend renders buttons from this
  list and holds no rule of its own.
- ``POST /drafts/{id}/transitions`` — perform one wired action. The body carries
  an optional rationale and, for ``edit``, the filing-metadata keys and the
  private-company statement the request actually states. ``send_for_signature``
  is the release: it fills the pinned CR template from the version's own
  snapshot (F4's filler), stores the PDF as a WORM ``generated_draft`` document
  through the records registry's write path, moves the draft to
  ``sent_for_signature``, and answers with the ``signature_package`` block (the
  document's id and the fill's escalations).
- ``POST /drafts/{id}/signature`` — the client signature upload (scope §2.10):
  the raw signed PDF, declared type + magic-bytes validated, stored as a WORM
  ``signed_draft`` document through the same registry write path, and the
  ``upload_signed`` transition performed in the same request — the draft moves
  to ``signature_received`` beside its stored signature copy.
- ``POST /drafts/{id}/exports`` — the export (§2.12, P6-4): one approved draft
  becomes a submission package. The body carries the mode (``e_registry_prep``
  | ``paper`` | ``api_ready``); the response is the FilingPackage — the stored
  WORM ``export_package`` document, the derived envelope and the exporting
  actor. The transitions POST performs the same action with ``mode`` in its
  body (ruling R4: one executor, two doors). No mode submits anything.

**Reads are ungated by §5.1** (ADR-0012 — the customer's own data): the two
draft permission flags gate the transitions, never the reads. The id-scoped
routes learn their company from the row, so the per-company grant is checked in
the application layer through the same single decision the company-scoped
dependency uses (``require_company_access`` resolves it from the path; there is
no company in these paths).

The refusals the actions raise are contract §5.1.3's vocabulary:
``automation_denied: <reason>`` (generation, and P6-3's ``regenerate`` —
ruling R3 gates it exactly like generation),
``nar1_rule_not_established``,
``transition_not_allowed``, ``rationale_required`` (both of ``reject``'s
legs), ``statement_confirmation_required`` / ``statement_not_made``
(``approve``'s statement leg), ``export_mode_required`` / ``export_mode_invalid``
(the export doors' body shapes), ``statement_not_confirmed`` (the export's
statement gate), ``draft_conflict`` (a lost race on the version insert —
re-read and retry),
``filing_metadata_invalid: <field>``, and the filler's own two —
``draft_font_unavailable`` (503) and ``font_glyph_unavailable: <chars>``
(409). A cross-tenant or unknown id is a 404, one answer for both, like every
other tenant-scoped read.
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.pdf.filler import DraftFontUnavailableError, GlyphUnavailableError
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import (
    ActorRef,
    get_actor_context,
    get_current_actor,
    require_company_access,
)
from app.api.documents import _read_capped_body, get_storage
from app.application import drafts, scanning
from app.application.company_master import CompanyNotFoundError
from app.application.drafts import (
    AutomationDeniedError,
    DraftActionNotWiredError,
    DraftConflictError,
    DraftExportRefusedError,
    DraftForbiddenError,
    DraftInvalidError,
    DraftNotFoundError,
    DraftRationaleRequiredError,
    DraftTransitionError,
    DraftTransitions,
    DraftView,
    FilingPackageView,
    SignaturePackage,
)
from app.domain.compliance import DueDateRuleNotEstablishedError
from app.domain.documents import (
    ContentTypeMismatchError,
    UnsupportedContentTypeError,
    UploadTooLargeError,
)
from app.domain.drafts import DraftAction, DraftStatus, SignatureStatus
from app.domain.export_package import ExportMode, ExportModeInvalidError, validate_mode
from app.domain.prior_filing import FormCode
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["drafts"])

# The API's own cap on a rationale: a review or approval note is a line or two,
# not a report (the column is ``Text`` and carries no length of its own).
MAX_RATIONALE_CHARS = 500


class DraftGenerateRequest(BaseModel):
    """One generation: the form and the as-of date (§2.8).

    ``form_type`` is the contract's ``nar1 | nd2a``; ``target_date`` is the
    draft's as-of date — the NAR1's made-up date anchor and the ND2A's filing
    date — from which every effective date is resolved.
    """

    model_config = ConfigDict(extra="forbid")

    form_type: FormCode
    target_date: date


class DraftTransitionRequest(BaseModel):
    """One action on one draft: ``{action, filing_metadata?, ...}``.

    The action vocabulary is §4.2's whole — unwired members included, so the API
    can refuse them with their named 409 rather than as an unknown value. The
    edit fields may only accompany ``edit``, and they are optional so an absent
    key keeps the version's value (the merge is one level into ``return``,
    ``presentor``, ``signature`` and ``consent``). ``confirm_statement`` may
    only accompany ``approve`` — the item-16 statement confirmation (ruling
    R2): required (``true``) when the draft makes the statement, refused on a
    draft that makes none.

    Unknown body fields are refused (``extra="forbid"``): a misspelled
    ``filing_metadata`` must not read as an edit that changed nothing.

    ``mode`` may only accompany ``export`` (ruling R4): through this door the
    export carries its mode in the body — without one the body refuses 422
    ``export_mode_required``, and on any other action a stated mode refuses
    422 ``{action}: carries mode`` (the ``confirm_statement`` body's-shape
    pattern). The canonical export door is ``POST /drafts/{id}/exports
    {mode}``; both call the one ``export_draft``.
    """

    model_config = ConfigDict(extra="forbid")

    action: DraftAction
    filing_metadata: dict[str, Any] | None = None
    private_company_statement: bool | None = None
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_CHARS)
    confirm_statement: bool | None = None
    mode: str | None = None


class FormVersionResponse(BaseModel):
    """ADR-0010's pin: the registry revision the draft was built against."""

    specification_no: str
    template_sha256: str


class DraftHistoryEntryResponse(BaseModel):
    """One earlier version of the draft, as metadata only.

    The lineage in one line per version: which number, where it stood and when
    it was written. The snapshot of a past version is a regeneration's business,
    not this read's — the current version *is* the draft.
    """

    version_no: int
    status: DraftStatus
    created_at: datetime
    created_by: UUID | None


class SignaturePackageEscalationResponse(BaseModel):
    """One escalation the fill raised: the kind and the sentence a human reads."""

    kind: str
    detail: str


class SignaturePackageResponse(BaseModel):
    """The signature-ready package one release produced (scope §2.10).

    The stored WORM document and the escalations — the boxes the fill could not
    complete from the draft (the presentor's reference, a licensee's details,
    the §1 protected class), each named so staff know what to complete by hand.
    An escalation never refuses the release: a draft awaiting a detail is
    ordinary work in progress, and the list is exactly what the person
    reviewing the package has to check.
    """

    document_id: UUID
    escalations: list[SignaturePackageEscalationResponse]


class DraftResponse(BaseModel):
    """One draft version, in full — §2.8's fields plus the two the phase added.

    ``applied_event_ids`` is the §4.1 linkage (the events this version applied),
    ``form_version`` the ADR-0010 pin. ``history`` carries the versions before
    the current one, newest first; it is empty on the history entries
    themselves, which are metadata, not drafts. ``signature_package`` is the
    release's own block: the document ``send_for_signature`` just stored and the
    escalations it raised, and ``null`` on every other read and every other
    action.
    """

    id: UUID
    company_id: UUID
    form_type: FormCode
    version_no: int
    target_date: date
    status: DraftStatus
    resolved_state: dict[str, Any]
    field_provenance: dict[str, Any]
    qc_flags: list[dict[str, Any]]
    filing_metadata: dict[str, Any]
    applied_event_ids: list[UUID]
    form_version: FormVersionResponse
    private_company_statement: bool | None
    statement_confirmed_by: UUID | None
    statement_confirmed_at: datetime | None
    signature_status: SignatureStatus
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    approved_by: UUID | None
    approved_at: datetime | None
    rationale: str | None
    comments: str | None
    invalidated_at: datetime | None
    invalidation_reason: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    history: list[DraftHistoryEntryResponse]
    signature_package: SignaturePackageResponse | None = None


class AllowedActionResponse(BaseModel):
    """One action the actor may perform now, as the frontend renders it."""

    action: DraftAction
    to_status: DraftStatus
    rationale_required: bool


class DraftTransitionsResponse(BaseModel):
    """The §4.2 read for one actor: the draft's state, and their allowed actions.

    Only wired, leg-admitted rows appear: an unwired action would be a button
    that always 409s, and a system row (``invalidate``) is nobody's to press.
    """

    draft_id: UUID
    company_id: UUID
    version_no: int
    status: DraftStatus
    actions: list[AllowedActionResponse]


class DraftExportRequest(BaseModel):
    """One export: the mode alone (§5.1, §2.12).

    ``mode`` is validated against the vocabulary before the service is
    reached — an unknown value is refused 422 ``export_mode_invalid`` (the
    mode decides what the filing package is, so it is never coerced or
    defaulted).
    """

    model_config = ConfigDict(extra="forbid")

    mode: str


class FilingPackageResponse(BaseModel):
    """One export as stored (§2.8's FilingPackage): the artifact and the record.

    ``document_id`` is the WORM ``export_package`` document the primary
    artifact was stored as — the flattened PDF for ``e_registry_prep`` and
    ``paper``, the deterministic JSON for ``api_ready`` — and ``envelope``
    carries the derived counts (sheet counts, page-number pairs) plus the
    companion documents' ids (the e-Registry helper, the paper checklist;
    ``None`` where the mode carries none).
    """

    id: UUID
    draft_id: UUID
    draft_version_no: int
    mode: ExportMode
    document_id: UUID
    envelope: dict[str, Any]
    exported_by: UUID
    exported_at: datetime


def _signature_package_response(
    package: SignaturePackage | None,
) -> SignaturePackageResponse | None:
    """The release's block, or ``None`` for a response that is not a release."""
    if package is None:
        return None
    return SignaturePackageResponse(
        document_id=package.document.id,
        escalations=[
            SignaturePackageEscalationResponse(kind=item.kind, detail=item.detail)
            for item in package.escalations
        ],
    )


def _draft_response(
    view: DraftView, package: SignaturePackage | None = None
) -> DraftResponse:
    row: Draft = view.current
    return DraftResponse(
        id=row.id,
        company_id=row.company_id,
        form_type=FormCode(row.form_type),
        version_no=row.version_no,
        target_date=row.target_date,
        status=DraftStatus(row.status),
        resolved_state=row.resolved_state,
        field_provenance=row.field_provenance,
        qc_flags=list(row.qc_flags or []),
        filing_metadata=row.filing_metadata,
        applied_event_ids=[UUID(value) for value in (row.applied_event_ids or [])],
        form_version=FormVersionResponse(
            specification_no=str(row.form_version["specification_no"]),
            template_sha256=str(row.form_version["template_sha256"]),
        ),
        private_company_statement=row.private_company_statement,
        statement_confirmed_by=row.statement_confirmed_by,
        statement_confirmed_at=row.statement_confirmed_at,
        signature_status=SignatureStatus(row.signature_status),
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        rationale=row.rationale,
        comments=row.comments,
        invalidated_at=row.invalidated_at,
        invalidation_reason=row.invalidation_reason,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        history=[
            DraftHistoryEntryResponse(
                version_no=entry.version_no,
                status=DraftStatus(entry.status),
                created_at=entry.created_at,
                created_by=entry.created_by,
            )
            for entry in view.history
        ],
        signature_package=_signature_package_response(package),
    )


def _transition_refusal(exc: Exception) -> HTTPException:
    """The §5.1.3 name for a transition the executor refused.

    One mapping for the routes, so the two ends of a transition — the read that
    offered it and the POST that performed it — cannot describe one refusal in
    two vocabularies.
    """
    match exc:
        case DraftActionNotWiredError(reason=reason):
            return HTTPException(status_code=409, detail=reason)
        case DraftTransitionError():
            return HTTPException(status_code=409, detail="transition_not_allowed")
        case DraftRationaleRequiredError():
            return HTTPException(status_code=422, detail="rationale_required")
        case DraftInvalidError(reasons=reasons):
            return HTTPException(status_code=422, detail="; ".join(reasons))
        case DraftExportRefusedError(reason=reason):
            # The export's statement gate (§2.11's additional leg), raised by
            # ``export_draft`` when the executor's ``export`` arm runs: a 409
            # about the draft's state, not a 422 about the request.
            return HTTPException(status_code=409, detail=reason)
        case DraftForbiddenError():
            return HTTPException(status_code=403, detail="forbidden")
        case DraftNotFoundError():
            return HTTPException(status_code=404, detail="draft not found")
        case AutomationDeniedError(reason=reason):
            # P6-3 / ruling R3: ``regenerate`` rides the generation seam, so the
            # §5.1 gate's denial answers exactly like generation's — 403 with
            # the gate's own reason, and nothing written.
            return HTTPException(status_code=403, detail=f"automation_denied: {reason.value}")
        case DraftConflictError():
            # A lost race on the version insert — the caller re-reads and retries.
            return HTTPException(status_code=409, detail="draft_conflict")
        case _:
            raise exc


# The transition body's edit fields: present only when the request stated one
# (``model_fields_set``), so an absent field means "keep" and an explicit null
# means "clear". The route passes the stated set through; the application
# merges whatever arrived.
_EDIT_BODY_FIELDS = frozenset({"filing_metadata", "private_company_statement"})


@router.post("/companies/{company_id}/drafts", status_code=201)
async def generate_draft(
    company_id: UUID,
    payload: DraftGenerateRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DraftResponse:
    """Generate one draft version (the gated automation).

    The §5.1 gate is consulted with ``automated_prefill`` and fails closed with
    its own reason — 403 ``automation_denied: <reason>`` — and a denial writes
    **nothing**: no version row, no event move, no audit entry. 404 for an
    unknown or cross-tenant company, 422 ``nar1_rule_not_established`` when an
    NAR1 target's company type has no established anniversary rule, and 409
    ``draft_conflict`` when another generation won the version race (re-read and
    retry).
    """
    try:
        view = await drafts.generate_draft(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            form_type=payload.form_type,
            target_date=payload.target_date,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except AutomationDeniedError as exc:
        raise HTTPException(
            status_code=403, detail=f"automation_denied: {exc.reason.value}"
        ) from None
    except DueDateRuleNotEstablishedError:
        raise HTTPException(status_code=422, detail="nar1_rule_not_established") from None
    except DraftConflictError:
        raise HTTPException(status_code=409, detail="draft_conflict") from None
    return _draft_response(view)


@router.get("/companies/{company_id}/drafts")
async def list_drafts(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DraftResponse]:
    """The company's drafts, each lineage's current version, newest first.

    404 for an unknown or cross-tenant company. Ungated by §5.1 (the customer's
    own data, ADR-0012) — no permission flag, no automation gate. A read: no
    writes, no audit entries.
    """
    try:
        views = await drafts.list_drafts(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_draft_response(view) for view in views]


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DraftResponse:
    """One draft: the current version, and the versions before it.

    The actor's per-company access grant is checked in the application layer
    (the path carries no company). 404 for an unknown or cross-tenant draft, 403
    without the company's grant; no permission flag gates this read. A read: no
    writes, no audit entries.
    """
    try:
        view = await drafts.get_draft(
            session, tenant_id=actor.tenant_id, context=context, draft_id=draft_id
        )
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="draft not found") from None
    except DraftForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    return _draft_response(view)


@router.get("/drafts/{draft_id}/transitions")
async def get_draft_transitions(
    draft_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DraftTransitionsResponse:
    """The actions the actor may perform on this draft right now.

    The §4.2 table filtered by the actor's legs, and nothing else: unwired
    actions and system rows never appear. 404 for an unknown or cross-tenant
    draft, 403 without the company's grant.
    """
    try:
        view: DraftTransitions = await drafts.transitions_for(
            session, tenant_id=actor.tenant_id, context=context, draft_id=draft_id
        )
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="draft not found") from None
    except DraftForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    return DraftTransitionsResponse(
        draft_id=view.row.id,
        company_id=view.row.company_id,
        version_no=view.row.version_no,
        status=DraftStatus(view.row.status),
        actions=[
            AllowedActionResponse(
                action=item.action,
                to_status=item.to_status,
                rationale_required=item.rationale_required,
            )
            for item in view.actions
        ],
    )


@router.post("/drafts/{draft_id}/transitions")
async def post_draft_transition(
    draft_id: UUID,
    payload: DraftTransitionRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> DraftResponse:
    """Perform one action on one draft — the only path that moves a status.

    The executor owns the legality (the §4.2 table) and the actor's leg
    (``review_drafts``/``approve_drafts``, the no-flag grant ``upload_signed``
    carries, Administrator any); this route carries the request and maps the
    refusals: 409 ``transition_not_allowed`` for one the status does not
    admit, 422 ``rationale_required`` for a rejection without its reason, 422
    ``statement_confirmation_required`` / ``statement_not_made`` for the
    statement leg on ``approve``, 422 ``export_mode_required`` /
    ``export_mode_invalid`` and ``{action}: carries mode`` for the export
    body's shapes (P6-4, ruling R4), 422 with the domain's reasons verbatim
    (``filing_metadata_invalid: <field>``, ``edit: nothing to change``,
    ``{action}: carries edit fields``, ``{action}: carries
    confirm_statement``, ``regenerate: carries rationale``,
    ``export: carries rationale`` — both rows are rationale-free, and a
    stated note refuses rather than vanishes),
    403 without the action's leg or the company's grant, and 404 for an
    unknown or cross-tenant draft.

    **``export`` (P6-4) is performed with ``mode`` in the body** — one
    executor, two doors (ruling R4): the same ``export_draft`` the dedicated
    ``POST /drafts/{id}/exports`` route calls, with the moved draft as the
    response; the FilingPackage itself is the dedicated route's answer. Its
    own refusals map exactly like the export route's — 409
    ``statement_not_confirmed`` travels as the executor's
    ``DraftExportRefusedError``, with its own case in ``_transition_refusal``.

    **``regenerate`` (P6-3) appends the next version of a rejected lineage**:
    it rides the generation seam, so the §5.1 gate's denial answers 403
    ``automation_denied: <reason>`` (and writes nothing) and a lost race on
    the version insert answers 409 ``draft_conflict`` — the same vocabulary
    as the generation route.

    **``send_for_signature`` answers with the package it produced**: the stored
    document's id and the fill's escalations, in the ``signature_package``
    block. Its own hard refusals write nothing and are named: 503
    ``draft_font_unavailable`` when the deployment has no readable font to
    embed, and 409 ``font_glyph_unavailable: <chars>`` when a value carries a
    character that font cannot render (Simplified Chinese — the CR requires
    Traditional).

    **Two failures are deliberately uncaught and answer 500** (the controller's
    ruling, 2026-09-28): a ``FillPlanError`` — the pinned registry's items and
    the planner disagree, e.g. an item the planner has no rule for — and the
    writer's own ``ValueError`` for a ``STRIKE`` planned onto a field with no
    non-blank ``/Opt``. Both mean the deployment's registry and the code that
    reads it have parted ways: a deployment fault a caller cannot fix and must
    not be handed as a refusal code, and neither can mislead it about what to
    do next (§5.1.3 deliberately has no row for them). The "nothing is written"
    property holds either way — both are raised inside the executor's
    transaction before the store write and the status move, so the draft, the
    document store and the audit chain are untouched.
    """
    try:
        export_mode = None if payload.mode is None else validate_mode(payload.mode)
    except ExportModeInvalidError:
        raise HTTPException(status_code=422, detail="export_mode_invalid") from None
    try:
        result = await drafts.transition(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            context=context,
            draft_id=draft_id,
            action=payload.action,
            actor_user_id=actor.user_id,
            stated=_EDIT_BODY_FIELDS & payload.model_fields_set,
            filing_metadata=payload.filing_metadata,
            private_company_statement=payload.private_company_statement,
            rationale=payload.rationale,
            confirm_statement=payload.confirm_statement,
            mode=export_mode,
        )
    except (
        DraftNotFoundError,
        DraftForbiddenError,
        DraftActionNotWiredError,
        DraftTransitionError,
        DraftRationaleRequiredError,
        DraftInvalidError,
        # P6-4: the export's statement gate, mapped in ``_transition_refusal``.
        DraftExportRefusedError,
        # P6-3: ``regenerate``'s §5.1 gate, mapped in ``_transition_refusal``.
        AutomationDeniedError,
        # A lost race on the regenerate's version insert.
        DraftConflictError,
    ) as exc:
        raise _transition_refusal(exc) from None
    except DraftFontUnavailableError:
        raise HTTPException(status_code=503, detail="draft_font_unavailable") from None
    except GlyphUnavailableError as exc:
        raise HTTPException(
            status_code=409, detail=f"font_glyph_unavailable: {exc.characters}"
        ) from None
    return _draft_response(result.view, result.signature_package)


@router.post("/drafts/{draft_id}/exports", status_code=201)
async def export_draft(
    draft_id: UUID,
    payload: DraftExportRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> FilingPackageResponse:
    """Export one approved draft as a submission package (§2.12, P6-4).

    The canonical export door (ruling R4): the body carries the mode —
    ``e_registry_prep`` (the flattened PDF plus an e-Registry step-by-step
    helper), ``paper`` (the flattened PDF plus a covering checklist with the
    fee block), or ``api_ready`` (the deterministic JSON artifact) — and the
    response is the FilingPackage: the stored WORM ``export_package``
    document, the derived envelope and the exporting actor. The transitions
    POST performs the same action with ``mode`` in its body; one executor,
    two doors. No mode submits anything — the e-Registry prep is a helper
    document, the paper mode covers a human lodgement, and the api-ready
    JSON waits for a future channel.

    The refusals, each before anything is written: 404 for an unknown or
    cross-tenant draft, 403 without the company grant or the
    ``approve_drafts`` leg (§4.2's actor column), 409 ``transition_not_allowed``
    from any status but ``approved`` (the export hard gate — and the
    second-export idempotence refusal), 409 ``statement_not_confirmed`` when
    an NAR1 item-16 declaration stands unconfirmed (§2.11's statement leg,
    the domain rule ``ensure_exportable`` shared with ``approve``), 422
    ``export_mode_invalid`` for a mode outside the vocabulary (validated
    before the service is reached), and the filler's own two — 503
    ``draft_font_unavailable`` and 409 ``font_glyph_unavailable: <chars>``.
    A ``FillPlanError`` or the writer's ``ValueError`` stays deliberately
    uncaught (the deployment-fault doctrine the transitions route states):
    a registry the code contradicts is raised, not smoothed, and nothing is
    written either way.
    """
    try:
        mode = validate_mode(payload.mode)
    except ExportModeInvalidError:
        raise HTTPException(status_code=422, detail="export_mode_invalid") from None
    try:
        package: FilingPackageView = await drafts.export_draft(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            context=context,
            draft_id=draft_id,
            mode=mode,
            actor_user_id=actor.user_id,
        )
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="draft not found") from None
    except DraftForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    except DraftTransitionError:
        raise HTTPException(status_code=409, detail="transition_not_allowed") from None
    except DraftExportRefusedError as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from None
    except DraftFontUnavailableError:
        raise HTTPException(status_code=503, detail="draft_font_unavailable") from None
    except GlyphUnavailableError as exc:
        raise HTTPException(
            status_code=409, detail=f"font_glyph_unavailable: {exc.characters}"
        ) from None
    return FilingPackageResponse(
        id=package.id,
        draft_id=package.draft_id,
        draft_version_no=package.draft_version_no,
        mode=package.mode,
        document_id=package.document_id,
        envelope=package.envelope,
        exported_by=package.exported_by,
        exported_at=package.exported_at,
    )


@router.post("/drafts/{draft_id}/signature")
async def upload_signed_draft(
    request: Request,
    draft_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> DraftResponse:
    """Store the client-signed copy and move the draft (scope §2.10, P6-1).

    The body is the raw signed PDF; the ``Content-Type`` header is the
    declared type and must match the body's magic bytes (scope §8) — the
    records-registry upload route's own shape (25 MiB cap, 413 past it). The
    application seam performs the ``upload_signed`` transition in the same
    request: the WORM ``signed_draft`` document and the draft's move to
    ``signature_received`` commit or roll back together.

    The refusals: 404 for an unknown or cross-tenant draft, 403 without the
    company's grant (the row's only leg, checked in the application layer —
    the path carries no company), 409 ``transition_not_allowed`` for a draft
    not awaiting the signature (including the double-upload case — the second
    upload lands on ``signature_received``, which admits nothing), 413 over
    the cap, and 422 with the documents seam's own validation words
    (unsupported declared type; declared type the bytes belie). A refusal
    writes nothing: no document row, no status move, no audit entry.
    """
    data = await _read_capped_body(request)
    content_type = (request.headers.get("content-type") or "").partition(";")[0].strip()
    try:
        view = await drafts.record_signed_upload(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            context=context,
            draft_id=draft_id,
            actor_user_id=actor.user_id,
            data=data,
            content_type=content_type,
        )
    except DraftNotFoundError:
        raise HTTPException(status_code=404, detail="draft not found") from None
    except DraftForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    except DraftTransitionError:
        raise HTTPException(status_code=409, detail="transition_not_allowed") from None
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except (UnsupportedContentTypeError, ContentTypeMismatchError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _draft_response(view)
