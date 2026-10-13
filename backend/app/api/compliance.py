"""Compliance-calendar and dashboard endpoints (scope §2.3/§2.4;
contract §5.1 Dashboard and Calendar rows).

``GET /compliance/calendar?from=&to=`` is the in-app calendar read: the
NAR1 deadlines whose statutory due date falls in the requested range, each
with its 30/15/7-day alert band and green/amber/red risk at request time.
The range is required and the read is honest about its limits — ranges
wider than ``MAX_CALENDAR_RANGE_DAYS`` (366 days) are refused with 422,
never silently truncated, because licensed tiers carry unbounded
portfolios.

``GET /dashboard`` is the §2.3 widget aggregate this phase can source:
portfolio overview counts, upcoming deadlines, risk alerts, the pending
data-ingestion review, the pending change-event review (counts and
companies over the same set the cross-company queue lists), the pending
draft review (counts and companies over the drafts whose current version
awaits staff review), the pending client confirmations (the ``sent``
requests awaiting reply, with each chain's days elapsed, reminders sent,
next cadence instant and exhaustion, plus the §5.1 gate's reason where the
automation is blocked — §5.1.12's widget, an ungated read), per-company
status cards, and the "service not covered" flags the §5.1 gate already
emits. The response names the window
it was computed over (``as_of`` and the overdue lookback), so the numbers
are never silently bounded. Widgets whose entities or sources arrive later
(the activity feed, the draft approval and signature states) are
deliberately absent; see ``app.application.compliance``.

**Authorization.** Both reads span the whole portfolio and are therefore
open to every **active** staff account (``require_active``), not only
Administrators: the data is scoped in the application layer — an
Administrator sees every company, a General account exactly the companies
its access grants cover. A General account with no grants gets an empty
portfolio (200), never someone else's companies and never a 403 it could
not explain; an inactive account is refused (403).

Both routes are reads: no writes, no audit events (ADR-0009 records
changes to tenant data).
"""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_active
from app.application import compliance
from app.domain.compliance import ComplianceEventKind, RiskLevel
from app.domain.rbac import ActorContext
from app.domain.service_packages import AutomationType, GatingReason

router = APIRouter(prefix="/api/v1", tags=["compliance"])


class LateFeeExposureResponse(BaseModel):
    """The registration fee in force for a delivery today (slice L).

    ``fee_hkd`` is the ordinary on-time fee while the 42-day window stands
    and the applicable late band's fee once it has passed; ``late`` records
    which side of the window the read date is on.
    """

    fee_hkd: int
    late: bool


class CalendarEntryResponse(BaseModel):
    """One deadline as of the read's ``as_of`` date."""

    company_id: UUID
    br_number: str
    company_name: str | None
    kind: ComplianceEventKind
    due_date: date
    days_remaining: int
    risk: RiskLevel
    alert: ComplianceEventKind | None
    overdue: bool
    late_fee_exposure: LateFeeExposureResponse


class NotGeneratedResponse(BaseModel):
    """A company with no generated deadline, and why (rule not established)."""

    company_id: UUID
    company_type: str
    reason: str


class CalendarResponse(BaseModel):
    """The range read; ``from``/``to`` echo the request (the Python-visible
    field is ``from_`` because ``from`` is a keyword)."""

    from_: date = Field(serialization_alias="from")
    to: date
    as_of: date
    entries: list[CalendarEntryResponse]
    not_generated: list[NotGeneratedResponse]


class PortfolioOverviewResponse(BaseModel):
    total_companies: int
    due_within_30_days: int
    due_within_15_days: int
    due_within_7_days: int
    overdue: int
    late_fee_exposure_hkd: int


class PendingIngestionCompanyResponse(BaseModel):
    company_id: UUID
    company_name: str | None
    pending_records: int


class PendingIngestionResponse(BaseModel):
    pending_records: int
    companies: list[PendingIngestionCompanyResponse]


class PendingEventReviewCompanyResponse(BaseModel):
    """One company with change events awaiting staff review."""

    company_id: UUID
    company_name: str | None
    pending_events: int


class PendingEventReviewResponse(BaseModel):
    """The pending-event-review widget: the queue's set, in counts per company.

    The same set ``GET /change-events`` lists, so the widget's total is that
    queue's length; only companies with work appear.
    """

    pending_events: int
    companies: list[PendingEventReviewCompanyResponse]


class PendingDraftReviewCompanyResponse(BaseModel):
    """One company with drafts awaiting staff review."""

    company_id: UUID
    company_name: str | None
    pending_drafts: int


class PendingDraftReviewResponse(BaseModel):
    """The pending-draft-review widget: the drafts' set, in counts per company.

    One count per company over the drafts whose **current version** is
    ``generated`` or ``under_review`` — the same set the company's own draft
    list shows as work waiting, so the two cannot disagree; only companies
    with work appear.
    """

    pending_drafts: int
    companies: list[PendingDraftReviewCompanyResponse]


class PendingConfirmationRequestResponse(BaseModel):
    """One ``sent`` request awaiting reply, with its chain's read-time facts.

    ``next_reminder_at`` is the next undispatched cadence instant (null when
    the ladder is spent or the cadence is empty); ``automation_blocked`` is
    the §5.1 gate's own reason for the company's confirmation generation, or
    null when the gate would let the sweep run.
    """

    confirmation_request_id: UUID
    days_elapsed: int
    reminders_sent: int
    cadence_total: int
    next_reminder_at: datetime | None
    exhausted: bool
    automation_blocked: GatingReason | None


class PendingConfirmationsCompanyResponse(BaseModel):
    """One company's requests awaiting reply, oldest request first."""

    company_id: UUID
    company_name: str | None
    requests: list[PendingConfirmationRequestResponse]


class PendingConfirmationsResponse(BaseModel):
    """The pending-confirmations widget (scope §2.3; contract §5.1.12).

    Ungated per ADR-0012 — the customer's own data: a blocked automation is
    named, not implied, and present in the list either way.
    """

    companies: list[PendingConfirmationsCompanyResponse]


class CompanyCardResponse(BaseModel):
    """One card per visible company (scope §2.3).

    ``cr_number`` is nullable and rides along for the §2.3 search by name or
    CR number; the dashboard is where that search runs.
    """

    company_id: UUID
    br_number: str
    cr_number: str | None
    company_name: str | None
    status: str | None
    next_deadline: CalendarEntryResponse | None
    pending_ingestion_records: int


class ServiceGapResponse(BaseModel):
    """One automation type the §5.1 gate denies for one company."""

    company_id: UUID
    automation: AutomationType
    reason: GatingReason


class DashboardResponse(BaseModel):
    as_of: date
    window_from: date
    window_to: date
    portfolio: PortfolioOverviewResponse
    upcoming_deadlines: list[CalendarEntryResponse]
    risk_alerts: list[CalendarEntryResponse]
    pending_ingestion: PendingIngestionResponse
    pending_event_review: PendingEventReviewResponse
    pending_draft_review: PendingDraftReviewResponse
    pending_confirmations: PendingConfirmationsResponse
    company_cards: list[CompanyCardResponse]
    service_gaps: list[ServiceGapResponse]
    not_generated: list[NotGeneratedResponse]


def _entry_response(entry: compliance.CalendarEntry) -> CalendarEntryResponse:
    return CalendarEntryResponse(
        company_id=entry.company_id,
        br_number=entry.br_number,
        company_name=entry.company_name,
        kind=entry.kind,
        due_date=entry.due_date,
        days_remaining=entry.days_remaining,
        risk=entry.risk,
        alert=entry.alert,
        overdue=entry.overdue,
        late_fee_exposure=LateFeeExposureResponse(
            fee_hkd=entry.late_fee_exposure.fee_hkd,
            late=entry.late_fee_exposure.late,
        ),
    )


def _entries(entries: list[compliance.CalendarEntry]) -> list[CalendarEntryResponse]:
    return [_entry_response(entry) for entry in entries]


def _not_generated(items: list[compliance.NotGenerated]) -> list[NotGeneratedResponse]:
    return [
        NotGeneratedResponse(
            company_id=item.company_id,
            company_type=item.company_type,
            reason=item.reason,
        )
        for item in items
    ]


@router.get("/compliance/calendar")
async def get_calendar(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_active())],
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> CalendarResponse:
    """The NAR1 deadlines due in ``[from, to]``, scoped to the actor.

    422 for a range that is inverted or wider than the read allows
    (``MAX_CALENDAR_RANGE_DAYS``); companies whose due-date rule is not
    established appear in ``not_generated``, never as a guessed date.
    """
    try:
        view = await compliance.calendar(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            date_from=date_from,
            date_to=date_to,
            as_of=date.today(),
        )
    except compliance.CalendarRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return CalendarResponse(
        from_=view.date_from,
        to=view.date_to,
        as_of=view.as_of,
        entries=_entries(view.entries),
        not_generated=_not_generated(view.not_generated),
    )


@router.get("/dashboard")
async def get_dashboard(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_active())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DashboardResponse:
    """The portfolio widgets this phase can source, scoped to the actor."""
    view = await compliance.dashboard(
        session, tenant_id=actor.tenant_id, context=context, as_of=date.today()
    )
    return DashboardResponse(
        as_of=view.as_of,
        window_from=view.window_from,
        window_to=view.window_to,
        portfolio=PortfolioOverviewResponse(
            total_companies=view.portfolio.total_companies,
            due_within_30_days=view.portfolio.due_within_30_days,
            due_within_15_days=view.portfolio.due_within_15_days,
            due_within_7_days=view.portfolio.due_within_7_days,
            overdue=view.portfolio.overdue,
            late_fee_exposure_hkd=view.portfolio.late_fee_exposure_hkd,
        ),
        upcoming_deadlines=_entries(view.upcoming_deadlines),
        risk_alerts=_entries(view.risk_alerts),
        pending_ingestion=PendingIngestionResponse(
            pending_records=view.pending_ingestion.pending_records,
            companies=[
                PendingIngestionCompanyResponse(
                    company_id=item.company_id,
                    company_name=item.company_name,
                    pending_records=item.pending_records,
                )
                for item in view.pending_ingestion.companies
            ],
        ),
        pending_event_review=PendingEventReviewResponse(
            pending_events=view.pending_event_review.pending_events,
            companies=[
                PendingEventReviewCompanyResponse(
                    company_id=item.company_id,
                    company_name=item.company_name,
                    pending_events=item.pending_events,
                )
                for item in view.pending_event_review.companies
            ],
        ),
        pending_draft_review=PendingDraftReviewResponse(
            pending_drafts=view.pending_draft_review.pending_drafts,
            companies=[
                PendingDraftReviewCompanyResponse(
                    company_id=item.company_id,
                    company_name=item.company_name,
                    pending_drafts=item.pending_drafts,
                )
                for item in view.pending_draft_review.companies
            ],
        ),
        pending_confirmations=PendingConfirmationsResponse(
            companies=[
                PendingConfirmationsCompanyResponse(
                    company_id=item.company_id,
                    company_name=item.company_name,
                    requests=[
                        PendingConfirmationRequestResponse(
                            confirmation_request_id=req.confirmation_request_id,
                            days_elapsed=req.days_elapsed,
                            reminders_sent=req.reminders_sent,
                            cadence_total=req.cadence_total,
                            next_reminder_at=req.next_reminder_at,
                            exhausted=req.exhausted,
                            automation_blocked=req.automation_blocked,
                        )
                        for req in item.requests
                    ],
                )
                for item in view.pending_confirmations.companies
            ],
        ),
        company_cards=[
            CompanyCardResponse(
                company_id=card.company_id,
                br_number=card.br_number,
                cr_number=card.cr_number,
                company_name=card.company_name,
                status=card.status,
                next_deadline=(
                    None if card.next_deadline is None else _entry_response(card.next_deadline)
                ),
                pending_ingestion_records=card.pending_ingestion_records,
            )
            for card in view.company_cards
        ],
        service_gaps=[
            ServiceGapResponse(
                company_id=gap.company_id, automation=gap.automation, reason=gap.reason
            )
            for gap in view.service_gaps
        ],
        not_generated=_not_generated(view.not_generated),
    )
