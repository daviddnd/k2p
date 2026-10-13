"""Change-event endpoints (scope §2.7-§2.8; data-model §2.6; contract §4.1,
§5.1).

The §5.1 row, exactly:

- ``POST /companies/{id}/change-events`` — enter one by hand (scope §2.7's
  fallback input). Grant-gated per company with no permission flag: recording
  a change a client reported is ordinary staff work, the same rule as a
  master-data fact append, and the §5.1 automation gate does not apply to the
  manual path in any tier (ADR-0012).
- ``GET /companies/{id}/change-events`` — the company's events, current
  versions, newest first, each with the versions before it and its computed
  15-day window. Grant-gated, no permission flag (a read of the company's own
  record).
- ``GET /change-events`` — the cross-company **review queue**: every event
  whose current version awaits staff review (``pending`` or ``under_review``)
  across the companies the actor may see, soonest deadline first, each item
  carrying its company and its computed window. Open to every active account,
  scoped in the application layer exactly as the dashboard is; no action list
  (the transitions read is the sole allowed-actions authority).
- ``GET /change-events/{id}/transitions`` — the actions the actor may perform
  **now**, computed from the §4.1 table and the actor's two event legs
  (``review_events``/``approve_events``). The backend is the sole authority:
  the frontend renders buttons from this list and holds no rule of its own.
- ``POST /change-events/{id}/transitions`` — perform one action. The body
  carries an optional ``rationale`` (required by ``reject`` and by the
  staff-recorded ``client_withdraw``) and, for ``edit``, the fields the new
  version changes.
- ``GET /change-events/{id}/assessments`` — the §2.7 diagnostics linked to the
  event, newest first: truthfully empty for parser-proposed events — the
  parse's diagnostic row hangs off the reply's Communication (§2.7), not the
  event.

The id-scoped routes learn their company from the event row, so the
per-company grant is checked in the application layer through the same domain
decision the company-scoped dependency uses (``app.api.deps`` resolves it from
the path; there is no company in these paths). The refusals the actions raise
are the §5.1.3 vocabulary: ``change_event_invalid: <violations>``,
``reserved_event_type``, ``protected_class_unavailable: <field>``,
``rationale_required``, ``transition_not_allowed``,
``reconfirmation_not_wired`` (M5b wired the reconfirmation exchange, so the
code names ``override`` alone) / ``supersede_not_wired`` — ``draft_not_wired``
retired with P6-4, which wired ``submit`` as the system row the draft's export
performs — and ``change_event_conflict``. A cross-tenant or unknown event id
is a 404, one answer for both, like every other tenant-scoped read.
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import (
    ActorRef,
    get_actor_context,
    get_current_actor,
    require_active,
    require_company_access,
)
from app.api.documents import get_storage
from app.application import change_events, reconfirmation, scanning
from app.application.change_events import (
    ChangeEventActionNotWiredError,
    ChangeEventConflictError,
    ChangeEventForbiddenError,
    ChangeEventInvalidError,
    ChangeEventNotFoundError,
    ChangeEventProtectedClassError,
    ChangeEventRationaleRequiredError,
    ChangeEventReservedTypeError,
    ChangeEventTransitionError,
)
from app.application.company_master import CompanyNotFoundError
from app.domain.change_events import (
    ChangeEventAction,
    ChangeEventRelation,
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
)
from app.domain.compliance import ComplianceEventKind, RiskLevel
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["change-events"])

# The rationale's cap, matching the column: a rejection's reason, or the
# channel a client's withdrawal came through, is a line or two — not a report.
MAX_RATIONALE_CHARS = 500
# The verbatim source text the effective date was parsed from (the reply-parse
# schema); for a manual entry it is whatever staff copied, so it stays short.
MAX_SOURCE_TEXT_CHARS = 500


class CreateChangeEventRequest(BaseModel):
    """One hand-entered change event (scope §2.7).

    ``payload`` is validated by the domain grammar per ``type``, not here: the
    shape is the registry's, and a second statement of it in this layer would
    be the copy that drifts. ``related_event_id``/``relation`` are the §2.6
    linkage — a manual correction or withdrawal of an earlier event — and the
    reference must be an event of the same company.
    """

    model_config = ConfigDict(extra="forbid")

    type: ChangeEventType
    effective_date: date
    effective_date_text: str | None = Field(default=None, max_length=MAX_SOURCE_TEXT_CHARS)
    informed_date: date | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    related_event_id: UUID | None = None
    relation: ChangeEventRelation | None = None


class TransitionRequest(BaseModel):
    """One action on one event: ``{action, rationale?, ...edit fields}``.

    The action vocabulary is §4.1's whole — unwired members included, so the
    API can refuse them with their named 409 rather than as an unknown value.
    The edit fields are the ones a revision may change (``EDIT_FIELDS``); they
    may only accompany ``edit``, and they are optional so an absent field
    keeps the current version's value. ``effective_date_text`` and
    ``informed_date`` accept an explicit ``null`` to clear them.

    Unknown body fields are refused (``extra="forbid"``): a misspelled
    ``payload`` must not read as an edit that changed nothing.
    """

    model_config = ConfigDict(extra="forbid")

    action: ChangeEventAction
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_CHARS)
    type: ChangeEventType | None = None
    effective_date: date | None = None
    effective_date_text: str | None = Field(default=None, max_length=MAX_SOURCE_TEXT_CHARS)
    informed_date: date | None = None
    payload: dict[str, Any] | None = None


class ChangeWindowResponse(BaseModel):
    """One event's 15-day statutory window, counted as of the read's day.

    ``deadline`` is ``effective_date + 15`` (Cap. 622 ss.645/652 and CR PAM 5
    per scope §2.4), ``days_remaining`` is negative once past it, and
    ``is_late`` is False on the deadline day itself ("within 15 days after the
    change" includes the 15th day). ``risk``/``alert`` are the same 30/15/7
    bands the compliance calendar paints (``risk_band``): an approaching
    window is amber inside 15 days and red inside 7, a past one is red and
    ``is_late``.
    """

    deadline: date
    days_remaining: int
    is_late: bool
    risk: RiskLevel
    alert: ComplianceEventKind | None


class ChangeEventVersionResponse(BaseModel):
    """One version row of a change event — §2.6's fields, nothing dropped.

    ``payload`` is returned in full to a caller with the company's access: this
    is the TCSP's own data, and a payload the API withheld would be one the
    staff who must verify it cannot see. It carries no §1 protected-class field
    — the grammar refuses `hkid_full`, `hkid_check_digit`, `passport_full` and
    `usual_residential_address` (`protected_class_unavailable`), so a stored
    version never holds that class and this read never returns one. The
    public-page partial identifiers are ordinary payload fields and do travel
    here, to the company's own staff.
    """

    id: UUID
    version_no: int
    company_id: UUID
    type: ChangeEventType
    status: ChangeEventStatus
    effective_date: date
    effective_date_text: str | None
    informed_date: date | None
    payload: dict[str, Any]
    source_type: ChangeEventSourceType
    source_id: UUID | None
    related_event_id: UUID | None
    relation: ChangeEventRelation | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    approved_by: UUID | None
    approved_at: datetime | None
    rationale: str | None
    created_by: UUID | None
    created_at: datetime


class ChangeEventResponse(ChangeEventVersionResponse):
    """An event: its current version, flat, plus the versions before it.

    The current version's fields are the response's own, so a caller that
    wants today's state reads one object; ``history`` holds the earlier
    versions, newest first. The alternative the brief allowed — a version
    count plus a versions endpoint — was rejected: the §5.1 row names no such
    route, and a count answers "how many" and never "what". ``change_window``
    is the current version's 15-day countdown, counted as of the read's day —
    the history's rows carry none, because a countdown belongs to the version
    the event stands at.
    """

    history: list[ChangeEventVersionResponse]
    change_window: ChangeWindowResponse


class AllowedActionResponse(BaseModel):
    """One action the actor may perform now, as the frontend renders it."""

    action: ChangeEventAction
    to_status: ChangeEventStatus
    rationale_required: bool


class ChangeEventTransitionsResponse(BaseModel):
    """The §4.1 read for one actor: the event's state, and their allowed actions.

    Only wired, leg-admitted rows appear: an unwired action would be a button
    that always 409s, and a system row (``apply``/``submit``/``client_reply``)
    is nobody's to press.
    """

    event_id: UUID
    company_id: UUID
    version_no: int
    status: ChangeEventStatus
    actions: list[AllowedActionResponse]


class ChangeEventQueueItemResponse(ChangeEventVersionResponse):
    """One awaiting-review event as the cross-company queue lists it.

    The current version flat, the company it belongs to (the queue spans
    companies, so the row must name one), and the computed window. No
    ``history`` — the company read carries the lineage — and no action list:
    ``GET /change-events/{id}/transitions`` is the sole allowed-actions
    authority.
    """

    br_number: str
    company_name: str | None
    change_window: ChangeWindowResponse


class ExtractionAssessmentResponse(BaseModel):
    """The §2.7 diagnostics behind a parse of this event (the parser's, M5b)."""

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


# The transition body's edit fields: present only when the request stated one
# (``model_fields_set``), so an absent field means "keep" and an explicit null
# means "clear" where clearing is legal.
_EDIT_BODY_FIELDS = frozenset(
    {"type", "effective_date", "effective_date_text", "informed_date", "payload"}
)


def _version_response(row: ChangeEvent) -> ChangeEventVersionResponse:
    return ChangeEventVersionResponse(
        id=row.id,
        version_no=row.version_no,
        company_id=row.company_id,
        type=ChangeEventType(row.type),
        status=ChangeEventStatus(row.status),
        effective_date=row.effective_date,
        effective_date_text=row.effective_date_text,
        informed_date=row.informed_date,
        payload=row.payload,
        source_type=ChangeEventSourceType(row.source_type),
        source_id=row.source_id,
        related_event_id=row.related_event_id,
        relation=None if row.relation is None else ChangeEventRelation(row.relation),
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        rationale=row.rationale,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _window_response(window: change_events.ChangeWindowView) -> ChangeWindowResponse:
    return ChangeWindowResponse(
        deadline=window.deadline,
        days_remaining=window.days_remaining,
        is_late=window.is_late,
        risk=window.risk,
        alert=window.alert,
    )


def _event_response(view: change_events.ChangeEventView) -> ChangeEventResponse:
    current = _version_response(view.current)
    return ChangeEventResponse(
        **current.model_dump(),
        history=[_version_response(row) for row in view.history],
        change_window=_window_response(view.window),
    )


def _queue_item_response(
    item: change_events.ChangeEventQueueItem,
) -> ChangeEventQueueItemResponse:
    current = _version_response(item.current)
    return ChangeEventQueueItemResponse(
        **current.model_dump(),
        br_number=item.br_number,
        company_name=item.company_name,
        change_window=_window_response(item.window),
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


def _invalid(exc: ChangeEventInvalidError) -> HTTPException:
    """The typed refusal for a body that leaves the rules (contract §5.1.3).

    The violations travel in the detail — field paths and rules, never client
    data — the way the stored tenant-configuration block's do, so a caller
    sees every problem in one answer instead of one per request.
    """
    return HTTPException(
        status_code=422, detail=f"change_event_invalid: {'; '.join(exc.errors)}"
    )


def _transition_refusal(exc: Exception) -> HTTPException:
    """The §5.1.3 name for a transition the executor refused.

    One mapping for the routes, so the two ends of a transition — the read
    that offered it and the POST that performed it — cannot describe one
    refusal in two vocabularies.
    """
    match exc:
        case ChangeEventActionNotWiredError(reason=reason):
            return HTTPException(status_code=409, detail=reason)
        case ChangeEventTransitionError():
            return HTTPException(status_code=409, detail="transition_not_allowed")
        case ChangeEventRationaleRequiredError():
            return HTTPException(status_code=422, detail="rationale_required")
        case reconfirmation.ReconfirmationError(reason=reason):
            # The send_reconfirmation letter could not compose: the
            # confirmation trigger's own mapping — a missing record
            # (no request thread on the event's provenance, no name in
            # force, an unreadable send-mode block, nothing to restate) is a
            # 422; an unconfigured mailbox is a 503. The event never moved.
            status = 503 if reason == "mail_not_configured" else 422
            return HTTPException(status_code=status, detail=reason)
        case ChangeEventProtectedClassError(detail=detail):
            return HTTPException(status_code=422, detail=detail)
        case ChangeEventInvalidError():
            return _invalid(exc)
        case ChangeEventReservedTypeError():
            return HTTPException(status_code=422, detail="reserved_event_type")
        case ChangeEventConflictError():
            return HTTPException(status_code=409, detail="change_event_conflict")
        case ChangeEventForbiddenError():
            return HTTPException(status_code=403, detail="forbidden")
        case ChangeEventNotFoundError():
            return HTTPException(status_code=404, detail="change event not found")
        case _:
            raise exc


def _edit_changes(body: TransitionRequest) -> dict[str, Any]:
    """The edit fields this request actually stated (absent keeps, null clears)."""
    stated = _EDIT_BODY_FIELDS & body.model_fields_set
    return {field: getattr(body, field) for field in stated}


@router.post("/companies/{company_id}/change-events", status_code=201)
async def create_change_event(
    company_id: UUID,
    payload: CreateChangeEventRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChangeEventResponse:
    """Enter one change event by hand (scope §2.7's fallback input).

    The event enters ``pending`` at version 1 with ``source_type=manual``,
    ungated by the §5.1 automation gate (the manual path is ungated in every
    tier) and grant-gated per company. 404 for an unknown or cross-tenant
    company; 422 ``reserved_event_type`` for ``company_name_change`` (§2.6
    reserves it for Wave 2/NNC2), 422 ``protected_class_unavailable: <field>``
    for a §1 protected-class field (refused until field-level encryption
    exists — §8 scope, Phase 7) and 422 ``change_event_invalid: <violations>``
    for anything else the payload grammar, the date rule (``informed_date >=
    effective_date``) or the linkage rules refuse.
    """
    try:
        view = await change_events.create(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            event_type=payload.type,
            effective_date=payload.effective_date,
            payload=payload.payload,
            effective_date_text=payload.effective_date_text,
            informed_date=payload.informed_date,
            related_event_id=payload.related_event_id,
            relation=payload.relation,
            actor_user_id=actor.user_id,
            as_of=date.today(),
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except (
        ChangeEventReservedTypeError,
        ChangeEventProtectedClassError,
        ChangeEventInvalidError,
    ) as exc:
        raise _transition_refusal(exc) from None
    return _event_response(view)


@router.get("/companies/{company_id}/change-events")
async def list_change_events(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ChangeEventResponse]:
    """The company's change events, current versions, newest first.

    Every event is listed, soft-deleted ones included and visibly marked: this
    is the only read they appear in, and hiding them would make a deleted
    event unfindable rather than tidy. 404 for an unknown or cross-tenant
    company. A read: no writes, no audit entries.
    """
    try:
        views = await change_events.list_events(
            session, tenant_id=actor.tenant_id, company_id=company_id, as_of=date.today()
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_event_response(view) for view in views]


@router.get("/change-events")
async def list_change_event_queue(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_active())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ChangeEventQueueItemResponse]:
    """The cross-company review queue (scope §2.8's review queue; DoD §9 item 10).

    Every event whose current version awaits staff review — ``pending`` or
    ``under_review`` — across the companies the actor may see, the soonest
    statutory deadline first, each item carrying its company and its computed
    15-day window. Open to every **active** account, like the dashboard, with
    the data scoped in the application layer: an Administrator sees the
    tenant, a General account exactly the companies its access grants cover
    (and one with no grants gets an empty queue, never someone else's events).
    The items carry no action list: ``GET /change-events/{id}/transitions``
    stays the sole allowed-actions authority. A read: no writes, no audit
    entries.
    """
    items = await change_events.queue(
        session, tenant_id=actor.tenant_id, context=context, as_of=date.today()
    )
    return [_queue_item_response(item) for item in items]


@router.get("/change-events/{event_id}/transitions")
async def get_change_event_transitions(
    event_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChangeEventTransitionsResponse:
    """The actions the actor may perform on this event right now.

    The §4.1 table filtered by the actor's legs, and nothing else: unwired
    actions and system rows never appear. The actor's per-company access grant
    is checked in the application layer (the path carries no company). 404 for
    an unknown or cross-tenant event, 403 without the company's grant.
    """
    try:
        view = await change_events.transitions_for(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            event_id=event_id,
        )
    except ChangeEventNotFoundError:
        raise HTTPException(status_code=404, detail="change event not found") from None
    except ChangeEventForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    return ChangeEventTransitionsResponse(
        event_id=view.row.id,
        company_id=view.row.company_id,
        version_no=view.row.version_no,
        status=ChangeEventStatus(view.row.status),
        actions=[
            AllowedActionResponse(
                action=item.action,
                to_status=item.to_status,
                rationale_required=item.rationale_required,
            )
            for item in view.actions
        ],
    )


@router.post("/change-events/{event_id}/transitions")
async def post_change_event_transition(
    event_id: UUID,
    payload: TransitionRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ChangeEventResponse:
    """Perform one action on one event — the only path that moves a status.

    The executor owns the legality (the §4.1 table) and the permission leg
    (``review_events``/``approve_events``, Administrator any); this route
    carries the request and maps the refusals: 409
    ``reconfirmation_not_wired`` / ``supersede_not_wired`` for an action whose
    side effects do not exist yet — never a silent acceptance (P6-4 retired
    ``draft_not_wired`` — ``submit`` is the system row the draft's export
    performs, and a staff POST of it answers the system-row 409 below) — 409
    ``transition_not_allowed`` for one the event's status
    does not admit or the system rows (``apply``/``client_reply``/``submit``)
    no staff account may press, 422 ``rationale_required`` for a rejection or a
    staff-recorded withdrawal without a reason, 422
    ``change_event_invalid: <violations>`` for an edit the grammar refuses,
    the reconfirmer's bounded reasons for a ``send_reconfirmation`` whose
    letter cannot compose (422 for a missing record — no request thread on
    the event's provenance, no name in force, an unreadable send-mode block —
    and 503 ``mail_not_configured``; the event stays ``pending``, never
    half-performed), 409 ``change_event_conflict`` when another revision won
    the race (re-read and retry), 403 without the action's leg or the
    company's grant, and 404 for an unknown or cross-tenant event.
    """
    try:
        view = await change_events.transition(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            event_id=event_id,
            action=payload.action,
            actor_user_id=actor.user_id,
            rationale=payload.rationale,
            changes=_edit_changes(payload),
            store=store,
            scanner=scanning.scanner(),
            as_of=date.today(),
        )
    except (
        ChangeEventNotFoundError,
        ChangeEventForbiddenError,
        ChangeEventActionNotWiredError,
        ChangeEventTransitionError,
        ChangeEventRationaleRequiredError,
        ChangeEventProtectedClassError,
        ChangeEventInvalidError,
        ChangeEventConflictError,
        reconfirmation.ReconfirmationError,
    ) as exc:
        raise _transition_refusal(exc) from None
    return _event_response(view)


@router.get("/change-events/{event_id}/assessments")
async def list_change_event_assessments(
    event_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ExtractionAssessmentResponse]:
    """The §2.7 assessments linked to this event, newest first.

    Truthfully empty for parser-proposed events: the parser exists, but its
    diagnostic row hangs off the reply's ``Communication``
    (``subject_type=communication``, §2.7), not the event — the parse behind
    a proposed event surfaces through the communications parse read, and a
    hand-entered event has no parse behind it at all. 404 for an unknown or
    cross-tenant event, 403 without the company's grant.
    """
    try:
        assessments = await change_events.list_assessments(
            session, tenant_id=actor.tenant_id, context=context, event_id=event_id
        )
    except ChangeEventNotFoundError:
        raise HTTPException(status_code=404, detail="change event not found") from None
    except ChangeEventForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    return [_assessment_response(assessment) for assessment in assessments]
