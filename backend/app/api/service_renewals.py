"""Service-renewal endpoints and the §5.1.9 client-actions panel (scope §2.15;
data-model §2.9; contract §5.1.9).

The record of a period: whether the client is continuing the engagement for an
anniversary year, and whether the fee has arrived. The founder's revision of
2026-09-24 gives the record a smaller job than it first had — nothing is
refused for a `pending` row, and generating or preparing a document is not
gated at all. What the record holds is the release: a document that goes to a
client waits until the assigned package covers its work and the fee has
arrived (the gate lives in `app.application.service_renewals`; the routes here
are the record's).

Routes:

- ``GET /companies/{id}/service-renewals`` — the company's rows, newest period
  first.
- ``POST /companies/{id}/service-renewals`` — create-or-return the period's row
  (idempotent, **always 200**: the answer is "the row for this year", and a
  second call is the same request).
- ``POST /companies/{id}/service-renewals/{year}/decision`` — record the
  client's answer (``confirmed`` or ``declined``). **A decline requires a
  note**: a refusal that stops a period needs a reason, and that reason is
  what a reader finds in the row and in the audit chain. 409 ``already_closed``
  when the period is already decided — a decision stands, which is why the
  correction below exists.
- ``POST /companies/{id}/service-renewals/{year}/payment`` — record that the
  fee was received: a timestamp, the staff account, and an optional note.
  **Nothing is invoiced**: no amount, no currency, no line item, no receipt,
  no payment request, no provider call (scope §2.15 "tracking only — no
  invoicing or billing in v1.0"). 409 ``service_not_confirmed`` unless the row
  is ``confirmed`` — and 409 ``already_closed`` when it is already paid.
- ``POST /companies/{id}/service-renewals/{year}/correction`` — fix a decision
  that was recorded wrongly: `{decision: confirmed|declined, note}` with the
  note **required** (a correction to a regulated client record needs a reason).
  Allowed from ``confirmed``, ``declined`` and ``payment_received``, and only
  when the decision actually changes; a correction away from
  ``payment_received`` clears the payment, which is how a bounced cheque is put
  right (then the fee is recorded again when it really arrives). 409
  ``not_decided`` on a period nobody has answered, 409 ``decision_unchanged``
  when the correction would re-record the answer the row already holds.
- ``GET /client-actions?due_within=N`` — the panel: per company per anniversary
  year, every milestone from the tenant's configuration with its date, its days
  remaining, its state and — when it is ``blocked`` — the leg that refused. It
  is a portfolio-wide read, so it is open to every
  active account and scoped to what the actor may see — an Administrator sees
  the portfolio, a General account exactly its granted companies, and an
  inactive account nothing.

Everything but the panel is grant-gated per company (``require_company_access``
— recording a renewal is recording a fact about the company, the master-data
precedent, which is why payment recording needs no administrative role). A
decline ends nothing: see ``app.domain.service_renewals`` for why the service
assignment is deliberately untouched.
"""

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.service_renewals import ServiceRenewal
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_active, require_company_access
from app.api.tenant_config import invalid_stored_config
from app.application import service_renewals
from app.application.company_master import CompanyNotFoundError
from app.domain.milestones import MilestoneKind, MilestoneValidationError
from app.domain.rbac import ActorContext
from app.domain.service_renewals import (
    MilestoneBlockReason,
    MilestoneState,
    RenewalAction,
    RenewalStatus,
    RenewalTransitionError,
)

router = APIRouter(prefix="/api/v1", tags=["service-renewals"])

# A period is a calendar year a company's anniversary can fall in. Bounded so a
# typo is a 422 rather than a row nothing will ever read.
MIN_ANNIVERSARY_YEAR = 1900
MAX_ANNIVERSARY_YEAR = 2100

# The note's cap, matching the column. A decline's reason is a line or two of
# staff's own words, not a document.
MAX_NOTE_CHARS = 500


class Decision(StrEnum):
    """The two answers a client can give (the request body's vocabulary).

    Deliberately not ``RenewalStatus``: ``payment_received`` is what *staff*
    record about a period, never something a client answers with, and a body
    that could name it would be a second way into the same state.
    """

    CONFIRMED = "confirmed"
    DECLINED = "declined"


class CreateRenewalRequest(BaseModel):
    """Which period to create-or-return.

    ``anniversary_year`` is the year the company's anniversary (its made-up
    date) falls in — the period the milestone panel counts back from, and the
    same year the confirmation request's ``target_date`` names.
    """

    anniversary_year: int = Field(ge=MIN_ANNIVERSARY_YEAR, le=MAX_ANNIVERSARY_YEAR)


class DecisionRequest(BaseModel):
    """The client's answer for the period; a decline carries the reason."""

    decision: Decision
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


class PaymentRequest(BaseModel):
    """That the fee was received — and nothing about the money itself."""

    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


class CorrectionRequest(BaseModel):
    """A recorded decision put right, and the reason it is being put right.

    The two answers are the same two a client can give — a correction does not
    carry a third. ``note`` is optional *here* and required by the route, so
    that a missing note and a blank one answer the same ``422 note_required``
    rather than two different refusals.
    """

    decision: Decision
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


class RenewalResponse(BaseModel):
    """One period's renewal record.

    ``note`` is the row's current note — staff's line about the decision or the
    payment. Each version is in the audit chain, so a later payment note does
    not erase a decline's reason from the record.
    """

    id: UUID
    company_id: UUID
    anniversary_year: int
    status: str
    client_decided_at: datetime | None
    payment_received_at: datetime | None
    decided_by: UUID | None
    payment_by: UUID | None
    note: str | None
    created_at: datetime


class ClientActionMilestoneResponse(BaseModel):
    """One milestone of one period, as of the panel read's day.

    ``state`` is `pending | due | overdue | done | blocked`; ``days_remaining``
    is negative once the day has passed (the compliance calendar's own
    convention). ``blocked`` means the gate the panel can judge would refuse
    right now — the release gate's period-wide legs, the same predicate the
    download route consults; a document whose *form* the package does not cover
    is refused at the download, where the form is known.

    ``blocked_reason`` names **which** leg refused whenever ``state`` is
    ``blocked``, in the gate's own vocabulary (`no_assignment`,
    `assignment_ended`, `out_of_coverage`, `continuation_not_recorded`,
    `payment_not_received`) or `no_confirmation_request` for the reply
    milestone, whose block is a missing letter — a precondition rather than a
    gate. It is `null` whenever the milestone is not blocked. `form_not_covered`
    can never appear: a milestone names no form. The reason is what turns a
    bare `blocked` into an operator's next step, which is the standard every
    refusal in this API is held to.
    """

    kind: MilestoneKind
    date: date
    days_remaining: int
    state: MilestoneState
    blocked_reason: MilestoneBlockReason | None


class ClientActionPeriodResponse(BaseModel):
    """One company's one anniversary period, and every milestone configured.

    ``renewal_status`` is ``null`` when no row was recorded for the period yet
    — staff have not recorded the client's answer, which holds the release and
    nothing else.
    """

    company_id: UUID
    br_number: str
    company_name: str | None
    anniversary_year: int
    anniversary_date: date
    renewal_status: str | None
    milestones: list[ClientActionMilestoneResponse]


class ClientActionsResponse(BaseModel):
    """The panel, and the window it was computed over."""

    as_of: date
    due_within: int
    window_from: date
    window_to: date
    periods: list[ClientActionPeriodResponse]


def _renewal_response(row: ServiceRenewal) -> RenewalResponse:
    return RenewalResponse(
        id=row.id,
        company_id=row.company_id,
        anniversary_year=row.anniversary_year,
        status=row.status,
        client_decided_at=row.client_decided_at,
        payment_received_at=row.payment_received_at,
        decided_by=row.decided_by,
        payment_by=row.payment_by,
        note=row.note,
        created_at=row.created_at,
    )


# The decision a correction lands on, so this layer can name a correction's
# refusals precisely instead of inferring them from the table's shape.
_CORRECTION_TARGETS: Mapping[RenewalAction, RenewalStatus] = {
    RenewalAction.CORRECT_TO_CONFIRMED: RenewalStatus.CONFIRMED,
    RenewalAction.CORRECT_TO_DECLINED: RenewalStatus.DECLINED,
}


def _transition_refusal(exc: RenewalTransitionError) -> HTTPException:
    """The §5.1.9 names for a transition the guard refused.

    Four, because the caller's remedy differs.

    ``service_not_confirmed`` — a payment against a period the client has not
    confirmed: the record has to say the client is continuing before the money
    can mean anything, and the remedy is to record the answer.

    ``not_decided`` — a correction of a period nobody has answered: there is no
    recorded decision to put right, and the remedy is the decision route.

    ``decision_unchanged`` — a correction to the answer the row already holds:
    a correction exists to change a decision, so this one is refused rather
    than written as a chain entry recording nothing changing.

    ``already_closed`` — a decision on a period that is already decided, or a
    payment on one that is already paid: the record stands, and a second answer
    to the same question is not the decision route's to give (the correction
    route is where a wrong one is put right).
    """
    if (
        exc.action is RenewalAction.RECORD_PAYMENT
        and exc.status is not RenewalStatus.PAYMENT_RECEIVED
    ):
        return HTTPException(status_code=409, detail="service_not_confirmed")
    target = _CORRECTION_TARGETS.get(exc.action)
    if target is not None and exc.status is RenewalStatus.PENDING:
        return HTTPException(status_code=409, detail="not_decided")
    if target is exc.status:
        return HTTPException(status_code=409, detail="decision_unchanged")
    return HTTPException(status_code=409, detail="already_closed")


_AnniversaryYear = Annotated[int, Path(ge=MIN_ANNIVERSARY_YEAR, le=MAX_ANNIVERSARY_YEAR)]


@router.get("/companies/{company_id}/service-renewals")
async def list_service_renewals(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[RenewalResponse]:
    """The company's renewal records, newest period first.

    404 for an unknown or cross-tenant company. A company with no rows answers
    an empty list — a period nobody has recorded is not a period the client has
    confirmed, which is exactly what the gates make of it.
    """
    try:
        rows = await service_renewals.list_for_company(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_renewal_response(row) for row in rows]


@router.post("/companies/{company_id}/service-renewals")
async def create_service_renewal(
    company_id: UUID,
    payload: CreateRenewalRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RenewalResponse:
    """Create-or-return the period's renewal row (idempotent, always 200).

    The row starts ``pending``: staff have not recorded the client's answer
    yet. That is a fact about the record and nothing else — since the founder's
    revision of 2026-09-24 a ``pending`` row refuses nothing. The call is how
    staff note that the continuation ask went out — nothing in the product
    makes the ask itself (the sending slice is M2), so the ask is made by hand
    and the answer is what the record holds. 404 for an unknown or cross-tenant
    company.
    """
    try:
        row = await service_renewals.get_or_create(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            anniversary_year=payload.anniversary_year,
            actor_user_id=actor.user_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return _renewal_response(row)


@router.post("/companies/{company_id}/service-renewals/{anniversary_year}/decision")
async def decide_service_renewal(
    company_id: UUID,
    anniversary_year: _AnniversaryYear,
    payload: DecisionRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RenewalResponse:
    """Record the client's answer for the period.

    The answer is a record, not a gate: it changes nothing about what staff may
    draft or prepare. ``confirmed`` is the only status a payment can follow;
    ``declined`` requires a ``note`` — 422 ``note_required`` without one,
    because a refusal that stops a period needs a reason. Declining **does not
    touch the service assignment**: the period simply has no renewed coverage,
    which the §5.1 gate already reads as ``out_of_coverage`` once it ends.

    404 for an unknown company or a period with no row (create it first); 409
    ``already_closed`` when the period is already decided — a decision stands,
    and a wrong one is put right through the correction route below.
    """
    if payload.decision is Decision.DECLINED and not (payload.note or "").strip():
        raise HTTPException(status_code=422, detail="note_required")
    try:
        row = await service_renewals.decide(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            anniversary_year=anniversary_year,
            decision=RenewalStatus(payload.decision.value),
            actor_user_id=actor.user_id,
            note=payload.note,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except service_renewals.RenewalNotFoundError:
        raise HTTPException(status_code=404, detail="renewal not found") from None
    except RenewalTransitionError as exc:
        raise _transition_refusal(exc) from None
    return _renewal_response(row)


@router.post("/companies/{company_id}/service-renewals/{anniversary_year}/correction")
async def correct_service_renewal(
    company_id: UUID,
    anniversary_year: _AnniversaryYear,
    payload: CorrectionRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RenewalResponse:
    """Put a recorded decision right, with the reason it is being put right.

    The record distinguishes the two acts deliberately: the decision route
    records a client's answer, once, and the correction route overwrites one —
    because a mis-clicked ``declined`` would otherwise stop a client's whole
    year, and a wrongly-recorded payment would leave the release gate wrong for
    ever. A correction is admitted from ``confirmed``, ``declined`` and
    ``payment_received``, and only when it changes the decision; correcting
    away from ``payment_received`` clears the payment stamp (a bounced cheque),
    and the fee is then recorded again when it really arrives.

    ``note`` is **required** (422 ``note_required``): a correction to a
    regulated client record is a change that needs a reason, and the reason is
    what a reader finds in the row and in the audit chain, beside the status it
    moved from and to. 404 for an unknown company or a period with no row; 409
    ``not_decided`` when nobody has answered the period yet and 409
    ``decision_unchanged`` when the correction names the answer already on the
    row.
    """
    if not (payload.note or "").strip():
        raise HTTPException(status_code=422, detail="note_required")
    try:
        row = await service_renewals.correct(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            anniversary_year=anniversary_year,
            decision=RenewalStatus(payload.decision.value),
            actor_user_id=actor.user_id,
            note=payload.note,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except service_renewals.RenewalNotFoundError:
        raise HTTPException(status_code=404, detail="renewal not found") from None
    except RenewalTransitionError as exc:
        raise _transition_refusal(exc) from None
    return _renewal_response(row)


@router.post("/companies/{company_id}/service-renewals/{anniversary_year}/payment")
async def record_service_renewal_payment(
    company_id: UUID,
    anniversary_year: _AnniversaryYear,
    payload: PaymentRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RenewalResponse:
    """Record that the period's fee was received.

    A timestamp, the staff account, and an optional note — **no amount, no
    currency, no invoice, no receipt** (ruling 2026-09-24; scope §2.15). What
    it unlocks is the release of the period's documents to the client, and
    nothing else.

    404 for an unknown company or a period with no row; 409
    ``service_not_confirmed`` when the period is not ``confirmed`` (the
    client's answer comes first); 409 ``already_closed`` when the fee is
    already recorded.
    """
    try:
        row = await service_renewals.record_payment(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            anniversary_year=anniversary_year,
            actor_user_id=actor.user_id,
            note=payload.note,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except service_renewals.RenewalNotFoundError:
        raise HTTPException(status_code=404, detail="renewal not found") from None
    except RenewalTransitionError as exc:
        raise _transition_refusal(exc) from None
    return _renewal_response(row)


@router.get("/client-actions")
async def get_client_actions(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_active())],
    session: Annotated[AsyncSession, Depends(get_session)],
    due_within: Annotated[
        int,
        Query(
            ge=1,
            le=service_renewals.MAX_DUE_WITHIN_DAYS,
            description="How many days ahead the panel looks, per company and period.",
        ),
    ] = service_renewals.DEFAULT_DUE_WITHIN_DAYS,
) -> ClientActionsResponse:
    """The milestone panel: what is due, per company, per anniversary year.

    The §5.1.9 read behind "one board" — every milestone the tenant's
    configuration defines, with its date, its days remaining and its state.
    A period is listed while at least one of its milestone dates falls in the
    window (`window_from`/`window_to` in the response; the lookback past it is
    the dashboard's own 30 days).

    **Authorization**: open to every active account, with the data scoped in
    the application layer — an Administrator sees every company, a General
    account exactly its granted companies, an inactive account nothing (403).
    A read: no writes, no audit entries.

    409 ``tenant_config_invalid: <violations>`` when the stored milestone block
    the panel is derived from does not validate: the panel cannot be computed
    from a block nobody can read, and the refusal names what is wrong (contract
    §5.1.3) rather than answering an untyped error.
    """
    try:
        view = await service_renewals.pending_actions(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            as_of=date.today(),
            due_within=due_within,
        )
    except MilestoneValidationError as exc:
        raise invalid_stored_config(exc) from None
    return ClientActionsResponse(
        as_of=view.as_of,
        due_within=view.due_within,
        window_from=view.window_from,
        window_to=view.window_to,
        periods=[
            ClientActionPeriodResponse(
                company_id=period.company_id,
                br_number=period.br_number,
                company_name=period.company_name,
                anniversary_year=period.anniversary_year,
                anniversary_date=period.anniversary_date,
                renewal_status=period.renewal_status,
                milestones=[
                    ClientActionMilestoneResponse(
                        kind=milestone.kind,
                        date=milestone.date,
                        days_remaining=milestone.days_remaining,
                        state=milestone.state,
                        blocked_reason=milestone.blocked_reason,
                    )
                    for milestone in period.milestones
                ],
            )
            for period in view.periods
        ],
    )
