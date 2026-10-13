"""Compliance-calendar and dashboard read model (scope §2.3/§2.4; contract §5.1).

The two portfolio-wide reads, composed from — never re-deriving — the
domain rules in ``app.domain.compliance``:

- ``calendar`` returns one entry per (company, anniversary year) whose
  NAR1 due date falls in the requested range, each with the alert band and
  risk colour ``risk_band`` computes for it as seen from ``as_of``, and the
  registration fee in force for a delivery on ``as_of`` against the entry's
  own anniversary year (slice L's ``late_fee_exposure``, priced from the
  dated schedule in ``backend/rules/``). The company's status is resolved
  **as of the entry's own due date** (the single ADR-0007 rule), so a status
  change regenerates the set: a company dormant on a due date drops out, one
  that has returned to active brings its deadlines back, and a change
  effective *after* a deadline leaves that deadline standing. Companies whose
  type has no established rule are reported in ``not_generated`` — the domain
  refused a date rather than guessing one.
- ``dashboard`` is the §2.3 aggregate this phase can source: portfolio
  overview counts — including ``late_fee_exposure_hkd``, the late fees the
  overdue deadlines would pay if filed today (slice L) — upcoming deadlines,
  risk alerts, the pending data-ingestion review (the extraction slice's
  ``pending`` records), the
  pending change-event review (the events awaiting staff review, counted
  per company — ``app.application.change_events.queue`` is the read that
  lists them), the pending draft review (drafts whose **current version**
  awaits staff review, counted per company — the company's own list,
  ``app.application.drafts.list_drafts``, is the read that shows them), the
  pending client confirmations (the ``sent`` requests awaiting reply, with
  the reminder chain's read-time facts and the §5.1 gate's reason where the
  automation is blocked — §5.1.12's widget, ungated per ADR-0012), the
  per-company status cards, and the service-package "not covered" flags
  (the §5.1 gate's own ``GatingReason`` values). Widgets whose entities or
  sources arrive in later phases — draft approval and signature states
  (Phase 6), the activity feed — are deliberately absent rather than faked.

**Scoping happens here, once.** Both reads span whichever companies the
actor may see: an Administrator sees every company in the tenant; a General
account sees exactly the companies its access grants cover — the same
per-company rule ``can_access_company`` applies to company-scoped routes,
and least privilege at portfolio scale. A General account with no grants
gets an empty portfolio (never someone else's data); an inactive account
gets nothing, even when a caller skips the API layer's gate.

**What this read model cannot know.** Nothing here records that a return
was *filed* — the draft/export history that would clear a deadline lands in
Phase 5 — so a past statutory due date is reported as overdue, and the
dashboard counts overdue only within ``OVERDUE_LOOKBACK_DAYS``: a deadline
that passed longer ago has left the operational window and the product
cannot tell a filed one from a missed one at any distance. Overdue
semantics tighten when filing state exists; until then the bound is
explicit in the response (``window_from``/``window_to``) rather than
silent.

Reads only: no writes, and deliberately no audit events (ADR-0009's chain
records changes to tenant data; these are derived views).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.company_master import (
    CompanyNameRecord,
    CompanyStatusRecord,
    _EffectiveDatedFact,
)
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.models import Company
from app.adapters.persistence.prior_filing import ImportBatch, ImportBatchRecord
from app.adapters.rules.loader import load_fee_schedule
from app.application import service_packages, tenant_config
from app.domain.change_events import AWAITING_REVIEW_STATUSES as EVENT_AWAITING_REVIEW_STATUSES
from app.domain.communications import CommunicationKind, Direction
from app.domain.compliance import (
    ComplianceEventKind,
    DueDateRuleNotEstablishedError,
    FeeSchedule,
    LateFeeExposure,
    RiskLevel,
    anniversary_in_year,
    dormant_excluded,
    late_fee_exposure,
    nar1_due_date,
    risk_band,
)
from app.domain.confirmation import RequestStatus
from app.domain.drafts import AWAITING_REVIEW_STATUSES as DRAFT_AWAITING_REVIEW_STATUSES
from app.domain.effective_dating import resolve_as_of
from app.domain.milestones import MilestoneValidationError
from app.domain.prior_filing import FormCode, RecordStatus
from app.domain.rbac import ActorContext, is_admin
from app.domain.reminders import ReminderState, next_due_instant, reminder_state
from app.domain.service_packages import (
    AutomationGateDecision,
    AutomationType,
    GatingReason,
)

# The refusal the domain raises, as a response-facing reason code.
NAR1_RULE_NOT_ESTABLISHED = "nar1_rule_not_established"

# The dashboard's fixed horizon: it counts and lists deadlines due within
# this many days, and treats a deadline that passed within the same span as
# overdue (``OVERDUE_LOOKBACK_DAYS``) for the no-filing-state reason in the
# module docstring. Both are echoed in the response — nothing is truncated
# silently.
DASHBOARD_HORIZON_DAYS = 30
OVERDUE_LOOKBACK_DAYS = 30

# The per-company status card shows the next NAR1 deadline even when it is
# months away, so cards read a full annual cycle ahead.
CARD_HORIZON_DAYS = 366

# The calendar is range-driven; a range wider than this is refused (422 at
# the API), never truncated.
MAX_CALENDAR_RANGE_DAYS = 366


class CalendarRangeError(ValueError):
    """The requested calendar range is empty or wider than the read allows."""


@dataclass(frozen=True)
class CalendarEntry:
    """One statutory deadline for one company, banded as of ``as_of``.

    ``anniversary_year`` names the obligation's period — the anniversary
    year whose return this deadline belongs to. It is not in the API
    response (a caller reads the date); it is what the calendar-sync
    projection keys its UID on, because the period identifies the
    obligation while the due date it produces may move. ``late_fee_exposure``
    (slice L) is the registration fee in force for a delivery on ``as_of``
    against this same anniversary year — composed here, over exactly the
    generated entries, never a second set.
    """

    company_id: UUID
    br_number: str
    company_name: str | None
    kind: ComplianceEventKind
    anniversary_year: int
    due_date: date
    days_remaining: int
    risk: RiskLevel
    alert: ComplianceEventKind | None
    overdue: bool
    late_fee_exposure: LateFeeExposure


@dataclass(frozen=True)
class NotGenerated:
    """A company for which the calendar generated nothing, and why.

    ``reason`` is ``NAR1_RULE_NOT_ESTABLISHED``: the domain refused to guess
    a due date for the company's type. The dormant exclusion is the
    documented rule, not an anomaly, and is not listed here.
    """

    company_id: UUID
    company_type: str
    reason: str


@dataclass(frozen=True)
class Calendar:
    date_from: date
    date_to: date
    as_of: date
    entries: list[CalendarEntry]
    not_generated: list[NotGenerated]


@dataclass(frozen=True)
class PortfolioOverview:
    total_companies: int
    due_within_30_days: int
    due_within_15_days: int
    due_within_7_days: int
    overdue: int
    # The sum of the late-band fees the overdue entries would pay if filed
    # today (slice L) — the dashboard's "late-fee exposure" number.
    late_fee_exposure_hkd: int


@dataclass(frozen=True)
class PendingIngestionCompany:
    company_id: UUID
    company_name: str | None
    pending_records: int


@dataclass(frozen=True)
class PendingIngestion:
    pending_records: int
    companies: list[PendingIngestionCompany]


@dataclass(frozen=True)
class PendingEventReviewCompany:
    """One company with change events awaiting staff review."""

    company_id: UUID
    company_name: str | None
    pending_events: int


@dataclass(frozen=True)
class PendingEventReview:
    """The pending-event-review widget, in counts.

    ``pending_events`` is the portfolio total; ``companies`` lists only the
    companies with work, ordered by count then BRN. The set counted is the
    cross-company queue's own (``AWAITING_REVIEW_STATUSES``), so the widget's
    number is the queue's length.
    """

    pending_events: int
    companies: list[PendingEventReviewCompany]


@dataclass(frozen=True)
class PendingDraftReviewCompany:
    """One company with drafts awaiting staff review."""

    company_id: UUID
    company_name: str | None
    pending_drafts: int


@dataclass(frozen=True)
class PendingDraftReview:
    """The pending-draft-review widget, in counts.

    ``pending_drafts`` is the portfolio total; ``companies`` lists only the
    companies with work, ordered by count then BRN. A draft is counted once,
    on its **current version** (the highest ``version_no``) — the same rule
    the company's own draft list selects by — so a lineage whose earlier row
    still says ``generated`` is not counted off its own history. The set
    counted is ``app.domain.drafts.AWAITING_REVIEW_STATUSES``.
    """

    pending_drafts: int
    companies: list[PendingDraftReviewCompany]


@dataclass(frozen=True)
class CompanyCard:
    """One card per visible company: next deadline and what waits on staff.

    ``cr_number`` rides along because scope §2.3's search is by *name or CR
    number*, and the card is the only portfolio-wide read carrying both —
    the alternative is a second read per keystroke. It is nullable: most
    companies have none recorded (NAR1 spec 2/2025 has no CR-number box).
    """

    company_id: UUID
    br_number: str
    cr_number: str | None
    company_name: str | None
    status: str | None
    next_deadline: CalendarEntry | None
    pending_ingestion_records: int


@dataclass(frozen=True)
class ServiceGap:
    """One automation type the §5.1 gate denies for one company."""

    company_id: UUID
    automation: AutomationType
    reason: GatingReason


@dataclass(frozen=True)
class PendingConfirmationRequest:
    """One ``sent`` request awaiting reply, with its chain's read-time facts.

    ``days_elapsed`` counts from the moment the request's letter left (the
    outbound ``Communication``); ``reminders_sent`` is the count of accepted
    reminder rows; ``next_reminder_at`` is the next undispatched cadence
    instant (``None`` when the ladder is spent or the cadence is empty);
    ``exhausted`` is the domain chain state — ``True`` from the final cadence
    point, whether or not the final reminder has delivered, because the chain
    is time-bound, not send-bound. ``automation_blocked`` is the §5.1 gate's
    own reason for (company, confirmation_generation), or ``None`` when the
    gate would let the sweep run.
    """

    confirmation_request_id: UUID
    days_elapsed: int
    reminders_sent: int
    cadence_total: int
    next_reminder_at: datetime | None
    exhausted: bool
    automation_blocked: GatingReason | None


@dataclass(frozen=True)
class PendingConfirmationsCompany:
    """One company's requests awaiting reply, oldest request first."""

    company_id: UUID
    company_name: str | None
    requests: list[PendingConfirmationRequest]


@dataclass(frozen=True)
class PendingConfirmations:
    """The pending-confirmations widget (scope §2.3; contract §5.1.12).

    Per company, the ``sent`` requests a reply has not joined — answered,
    superseded and undelivered requests are excluded. An ungated read
    (ADR-0012): a blocked automation is named, not implied, and the data is
    present either way.
    """

    companies: list[PendingConfirmationsCompany]


@dataclass(frozen=True)
class Dashboard:
    as_of: date
    window_from: date
    window_to: date
    portfolio: PortfolioOverview
    upcoming_deadlines: list[CalendarEntry]
    risk_alerts: list[CalendarEntry]
    pending_ingestion: PendingIngestion
    pending_event_review: PendingEventReview
    pending_draft_review: PendingDraftReview
    pending_confirmations: PendingConfirmations
    company_cards: list[CompanyCard]
    service_gaps: list[ServiceGap]
    not_generated: list[NotGenerated]


def company_scope(context: ActorContext) -> frozenset[UUID] | None:
    """The companies a portfolio-wide read may cover (``None`` = all).

    The single scoping rule for every portfolio-wide read — the calendar, the
    dashboard, and the §5.1.9 client-actions panel
    (``app.application.service_renewals``), which is why this is public: an
    Administrator sees every company in the tenant; a General account sees
    exactly the companies its access grants cover, and nothing else — the
    per-company least privilege ``can_access_company`` enforces route by route.
    An inactive account is scoped to nothing even if a caller skipped the API
    layer's gate.
    """
    if not context.is_active:
        return frozenset()
    if is_admin(context):
        return None
    return context.company_ids


async def visible_companies(
    session: AsyncSession, *, tenant_id: UUID, scope: frozenset[UUID] | None
) -> list[Company]:
    """The in-scope companies, ordered by BRN for a stable read."""
    query = select(Company).where(Company.tenant_id == tenant_id).order_by(Company.br_number)
    if scope is not None:
        query = query.where(Company.id.in_(scope))
    return list((await session.scalars(query)).all())


async def _facts_by_company[FactT: _EffectiveDatedFact](
    session: AsyncSession, model: type[FactT], company_ids: Sequence[UUID]
) -> dict[UUID, list[FactT]]:
    """One company's fact-group rows, grouped — the as-of rule reads the set."""
    grouped: dict[UUID, list[FactT]] = {}
    rows = await session.scalars(select(model).where(model.company_id.in_(company_ids)))
    for row in rows:
        grouped.setdefault(row.company_id, []).append(row)
    return grouped


async def display_names(
    session: AsyncSession, company_ids: Sequence[UUID], *, as_of: date
) -> dict[UUID, str | None]:
    """Each company's display name as of ``as_of``: English registered name,
    else the Chinese one, else the business name.

    Public for the same reason ``company_scope`` is: the §5.1.9 panel read
    names the companies it lists, and a second as-of name resolution would be a
    second answer (ADR-0007's one rule).
    """
    records = await _facts_by_company(session, CompanyNameRecord, company_ids)
    names: dict[UUID, str | None] = {}
    for company_id, rows in records.items():
        name = resolve_as_of(rows, as_of)
        names[company_id] = (
            None if name is None else (name.name_en or name.name_zh or name.business_name)
        )
    return names


def _generate_entries(
    companies: Sequence[Company],
    statuses: Mapping[UUID, Sequence[CompanyStatusRecord]],
    names: Mapping[UUID, str | None],
    *,
    date_from: date,
    date_to: date,
    as_of: date,
    schedule: FeeSchedule,
) -> tuple[list[CalendarEntry], list[NotGenerated]]:
    """The NAR1 deadlines inside the range, banded as of ``as_of``.

    For every company type the domain has a rule for, one deadline per
    anniversary year whose 42nd day lands in the range; the company's
    status is resolved as of that deadline, and a dormant status suppresses
    it. Companies whose type has no established rule are reported instead
    of skipped silently. Each entry also carries the registration fee in
    force for a delivery on ``as_of`` against its own anniversary year
    (slice L) — priced from the dated schedule, so a deadline the domain
    refused never has a fee either.
    """
    entries: list[CalendarEntry] = []
    not_generated: list[NotGenerated] = []
    for company in companies:
        company_statuses = statuses.get(company.id, ())
        # due(Y)'s earliest possible day is Feb 12 of Y and its latest Feb
        # 11 of Y+1, so [date_from.year - 1, date_to.year] covers every
        # anniversary that can land in the range.
        for year in range(date_from.year - 1, date_to.year + 1):
            try:
                due = nar1_due_date(company.anniversary_date, year, company_type=company.type)
            except DueDateRuleNotEstablishedError:
                not_generated.append(
                    NotGenerated(
                        company_id=company.id,
                        company_type=company.type,
                        reason=NAR1_RULE_NOT_ESTABLISHED,
                    )
                )
                break
            if not date_from <= due <= date_to:
                continue
            status = resolve_as_of(company_statuses, due)
            if dormant_excluded(status.status if status is not None else None):
                continue
            band = risk_band((due - as_of).days)
            entries.append(
                CalendarEntry(
                    company_id=company.id,
                    br_number=company.br_number,
                    company_name=names.get(company.id),
                    kind=ComplianceEventKind.NAR1_DUE,
                    anniversary_year=year,
                    due_date=due,
                    days_remaining=band.days_remaining,
                    risk=band.level,
                    alert=band.alert,
                    overdue=band.overdue,
                    late_fee_exposure=late_fee_exposure(
                        anniversary_in_year(company.anniversary_date, year),
                        as_of,
                        schedule,
                    ),
                )
            )
    entries.sort(key=lambda entry: (entry.due_date, entry.br_number))
    return entries, not_generated


async def calendar(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    date_from: date,
    date_to: date,
    as_of: date,
) -> Calendar:
    """The NAR1 deadlines in ``[date_from, date_to]``, scoped to the actor.

    Raises ``CalendarRangeError`` for an inverted range or one wider than
    ``MAX_CALENDAR_RANGE_DAYS`` — the endpoint refuses rather than
    truncating a caller's window.
    """
    if date_to < date_from:
        raise CalendarRangeError("to must not be before from")
    if (date_to - date_from).days + 1 > MAX_CALENDAR_RANGE_DAYS:
        raise CalendarRangeError(f"calendar range is limited to {MAX_CALENDAR_RANGE_DAYS} days")
    companies = await visible_companies(session, tenant_id=tenant_id, scope=company_scope(context))
    company_ids = [company.id for company in companies]
    entries, not_generated = _generate_entries(
        companies,
        await _facts_by_company(session, CompanyStatusRecord, company_ids),
        await display_names(session, company_ids, as_of=as_of),
        date_from=date_from,
        date_to=date_to,
        as_of=as_of,
        schedule=load_fee_schedule(FormCode.NAR1),
    )
    return Calendar(
        date_from=date_from,
        date_to=date_to,
        as_of=as_of,
        entries=entries,
        not_generated=not_generated,
    )


async def dashboard(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, as_of: date
) -> Dashboard:
    """The §2.3 widget aggregate for the companies the actor may see.

    ``upcoming_deadlines`` is everything due within
    ``DASHBOARD_HORIZON_DAYS``; ``risk_alerts`` is the amber-and-red subset
    (due within 15 days, or overdue within the lookback). Counts come from
    the same generated set the lists do, so a card, a count and a list can
    never disagree. ``not_generated`` carries the companies whose due-date
    rule is not established — the same refusal the calendar reports, so a
    card with no next deadline is never silently unexplained.
    """
    companies = await visible_companies(session, tenant_id=tenant_id, scope=company_scope(context))
    company_ids = [company.id for company in companies]
    names = await display_names(session, company_ids, as_of=as_of)
    statuses = await _facts_by_company(session, CompanyStatusRecord, company_ids)
    window_from = as_of - timedelta(days=OVERDUE_LOOKBACK_DAYS)
    window_to = as_of + timedelta(days=CARD_HORIZON_DAYS)
    entries, not_generated = _generate_entries(
        companies,
        statuses,
        names,
        date_from=window_from,
        date_to=window_to,
        as_of=as_of,
        schedule=load_fee_schedule(FormCode.NAR1),
    )
    # Entries are sorted by due date, so the first per company is the one to
    # act on: a within-lookback overdue deadline, else the next one due.
    next_by_company: dict[UUID, CalendarEntry] = {}
    for entry in entries:
        next_by_company.setdefault(entry.company_id, entry)
    status_by_company: dict[UUID, str | None] = {}
    for company_id, rows in statuses.items():
        status = resolve_as_of(rows, as_of)
        status_by_company[company_id] = status.status if status is not None else None
    pending = await _pending_ingestion(
        session, tenant_id=tenant_id, companies=companies, names=names
    )
    pending_by_company = {item.company_id: item.pending_records for item in pending.companies}
    events = await _pending_event_review(
        session, tenant_id=tenant_id, companies=companies, names=names
    )
    draft_review = await _pending_draft_review(
        session, tenant_id=tenant_id, companies=companies, names=names
    )
    decisions = await service_packages.gating_decisions_for_companies(
        session, company_ids=company_ids, as_of=as_of
    )
    pending_confirmations = await _pending_confirmations(
        session,
        tenant_id=tenant_id,
        companies=companies,
        names=names,
        decisions=decisions,
        as_of=as_of,
    )
    return Dashboard(
        as_of=as_of,
        window_from=window_from,
        window_to=window_to,
        portfolio=PortfolioOverview(
            total_companies=len(companies),
            due_within_30_days=sum(
                1 for e in entries if 0 <= e.days_remaining <= DASHBOARD_HORIZON_DAYS
            ),
            due_within_15_days=sum(1 for e in entries if 0 <= e.days_remaining <= 15),
            due_within_7_days=sum(1 for e in entries if 0 <= e.days_remaining <= 7),
            overdue=sum(1 for e in entries if e.overdue),
            late_fee_exposure_hkd=sum(
                e.late_fee_exposure.fee_hkd for e in entries if e.overdue
            ),
        ),
        upcoming_deadlines=[e for e in entries if 0 <= e.days_remaining <= DASHBOARD_HORIZON_DAYS],
        risk_alerts=[e for e in entries if e.risk is not RiskLevel.GREEN],
        pending_ingestion=pending,
        pending_event_review=events,
        pending_draft_review=draft_review,
        pending_confirmations=pending_confirmations,
        company_cards=[
            CompanyCard(
                company_id=company.id,
                br_number=company.br_number,
                cr_number=company.cr_number,
                company_name=names.get(company.id),
                status=status_by_company.get(company.id),
                next_deadline=next_by_company.get(company.id),
                pending_ingestion_records=pending_by_company.get(company.id, 0),
            )
            for company in companies
        ],
        service_gaps=_service_gaps(decisions),
        not_generated=not_generated,
    )


async def _pending_ingestion(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    companies: Sequence[Company],
    names: Mapping[UUID, str | None],
) -> PendingIngestion:
    """The review queue in counts: ``pending`` extraction records per company.

    Counted in SQL and grouped per company — the widget never loads the
    records themselves. Confirmed and rejected proposals are decisions
    already made and are not pending work.
    """
    rows = (
        await session.execute(
            select(ImportBatch.company_id, func.count())
            .select_from(ImportBatchRecord)
            .join(ImportBatch, ImportBatch.id == ImportBatchRecord.batch_id)
            .where(
                ImportBatchRecord.tenant_id == tenant_id,
                ImportBatch.company_id.in_([company.id for company in companies]),
                ImportBatchRecord.status == RecordStatus.PENDING.value,
            )
            .group_by(ImportBatch.company_id)
        )
    ).all()
    br_by_company = {company.id: company.br_number for company in companies}
    items = [
        PendingIngestionCompany(
            company_id=company_id,
            company_name=names.get(company_id),
            pending_records=count,
        )
        for company_id, count in rows
    ]
    items.sort(key=lambda item: (-item.pending_records, br_by_company[item.company_id]))
    return PendingIngestion(
        pending_records=sum(item.pending_records for item in items), companies=items
    )


async def _pending_event_review(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    companies: Sequence[Company],
    names: Mapping[UUID, str | None],
) -> PendingEventReview:
    """The pending-event-review widget in counts: what awaits staff review.

    Counted in SQL and grouped per company — the widget never loads the events
    themselves (the cross-company queue, ``app.application.change_events.queue``,
    is the read that lists them). The set counted is that queue's own
    (``app.domain.change_events.AWAITING_REVIEW_STATUSES``: ``pending`` and
    ``under_review``), so the widget's number is the queue's length and the two
    cannot drift apart.

    The **current version** is selected in SQL — the highest ``version_no`` per
    event — not by filtering rows: a revision leaves the earlier row's status
    where it stood, so a row-level filter would count a decided event off its
    own history.
    """
    company_ids = [company.id for company in companies]
    latest = (
        select(
            ChangeEvent.id.label("id"),
            func.max(ChangeEvent.version_no).label("version_no"),
        )
        .where(ChangeEvent.tenant_id == tenant_id, ChangeEvent.company_id.in_(company_ids))
        .group_by(ChangeEvent.id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ChangeEvent.company_id, func.count())
            .select_from(ChangeEvent)
            .join(
                latest,
                and_(
                    ChangeEvent.id == latest.c.id,
                    ChangeEvent.version_no == latest.c.version_no,
                ),
            )
            .where(
                ChangeEvent.tenant_id == tenant_id,
                ChangeEvent.status.in_(
                    sorted(status.value for status in EVENT_AWAITING_REVIEW_STATUSES)
                ),
            )
            .group_by(ChangeEvent.company_id)
        )
    ).all()
    br_by_company = {company.id: company.br_number for company in companies}
    items = [
        PendingEventReviewCompany(
            company_id=company_id,
            company_name=names.get(company_id),
            pending_events=count,
        )
        for company_id, count in rows
    ]
    items.sort(key=lambda item: (-item.pending_events, br_by_company[item.company_id]))
    return PendingEventReview(
        pending_events=sum(item.pending_events for item in items), companies=items
    )


async def _pending_draft_review(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    companies: Sequence[Company],
    names: Mapping[UUID, str | None],
) -> PendingDraftReview:
    """The pending-draft-review widget in counts: drafts awaiting staff review.

    Counted in SQL and grouped per company — the widget never loads the drafts
    themselves (the company's own list,
    ``app.application.drafts.list_drafts``, is the read that shows them). The
    set counted is ``app.domain.drafts.AWAITING_REVIEW_STATUSES``
    (``generated`` and ``under_review``), which is the one statement of what
    "awaiting review" means for a draft.

    The **current version** is selected in SQL — the highest ``version_no`` per
    draft — not by filtering rows: a regeneration leaves the earlier row's
    status where it stood, so a row-level filter would count a decided draft
    off its own history.
    """
    company_ids = [company.id for company in companies]
    latest = (
        select(
            Draft.id.label("id"),
            func.max(Draft.version_no).label("version_no"),
        )
        .where(Draft.tenant_id == tenant_id, Draft.company_id.in_(company_ids))
        .group_by(Draft.id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Draft.company_id, func.count())
            .select_from(Draft)
            .join(
                latest,
                and_(
                    Draft.id == latest.c.id,
                    Draft.version_no == latest.c.version_no,
                ),
            )
            .where(
                Draft.tenant_id == tenant_id,
                Draft.status.in_(sorted(status.value for status in DRAFT_AWAITING_REVIEW_STATUSES)),
            )
            .group_by(Draft.company_id)
        )
    ).all()
    br_by_company = {company.id: company.br_number for company in companies}
    items = [
        PendingDraftReviewCompany(
            company_id=company_id,
            company_name=names.get(company_id),
            pending_drafts=count,
        )
        for company_id, count in rows
    ]
    items.sort(key=lambda item: (-item.pending_drafts, br_by_company[item.company_id]))
    return PendingDraftReview(
        pending_drafts=sum(item.pending_drafts for item in items), companies=items
    )


async def _pending_confirmations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    companies: Sequence[Company],
    names: Mapping[UUID, str | None],
    decisions: Mapping[UUID, Mapping[AutomationType, AutomationGateDecision]],
    as_of: date,
) -> PendingConfirmations:
    """The pending-confirmations widget: every ``sent`` request awaiting reply.

    Grant-scoped exactly like the sibling pending_* widgets — the companies
    are the read's own visible set. The chain facts are derived at read time
    from the immutable rows (the send instant from the outbound
    ``Communication``, the reply's presence, the accepted ``reminder`` count),
    through the same domain rules the sweep dispatches with; a request a reply
    has joined is awaiting nothing and is excluded. The §5.1 gate is **not**
    re-run per row: the bulk decision set the service_gaps widget already read
    is reused, so the flag and the gap can never disagree.

    A stored milestone block this read cannot parse degrades to an empty
    cadence rather than taking the dashboard down: the domain rule reads an
    empty cadence as "the chain disabled" (nothing due, nothing exhausted), and
    ``tenant_config_invalid`` stays the panel's and the trigger's refusal —
    §5.1.3 names those two as its whole list.
    """
    try:
        milestones = await tenant_config.get_milestones(session, tenant_id=tenant_id)
        cadence = milestones.reminder_cadence_days
    except MilestoneValidationError:
        cadence = ()
    requests = list(
        (
            await session.scalars(
                select(ConfirmationRequest)
                .where(
                    ConfirmationRequest.tenant_id == tenant_id,
                    ConfirmationRequest.status == RequestStatus.SENT.value,
                    ConfirmationRequest.company_id.in_([company.id for company in companies]),
                )
                .order_by(ConfirmationRequest.company_id, ConfirmationRequest.id)
            )
        ).all()
    )
    if not requests:
        return PendingConfirmations(companies=[])
    ids = [request.id for request in requests]
    sent_at_rows = await session.execute(
        select(Communication.confirmation_request_id, func.min(Communication.received_at))
        .where(
            Communication.confirmation_request_id.in_(ids),
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.CONFIRMATION_REQUEST.value,
        )
        .group_by(Communication.confirmation_request_id)
    )
    sent_at = {request_id: at for request_id, at in sent_at_rows.all()}
    reminder_rows = await session.execute(
        select(Communication.confirmation_request_id, func.count())
        .where(
            Communication.confirmation_request_id.in_(ids),
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.REMINDER.value,
        )
        .group_by(Communication.confirmation_request_id)
    )
    reminders_sent = {request_id: count for request_id, count in reminder_rows.all()}
    replied_rows = await session.scalars(
        select(Communication.confirmation_request_id)
        .where(
            Communication.confirmation_request_id.in_(ids),
            Communication.direction == Direction.INBOUND.value,
        )
        .distinct()
    )
    replied = {request_id for request_id in replied_rows.all() if request_id is not None}

    now = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)
    br_by_company = {company.id: company.br_number for company in companies}
    per_company: dict[UUID, list[PendingConfirmationRequest]] = {}
    for request in requests:
        anchor = sent_at.get(request.id)
        if anchor is None or request.id in replied:
            continue  # queued-but-never-accepted, or answered: not awaiting
        count = reminders_sent.get(request.id, 0)
        gate = decisions.get(request.company_id, {}).get(AutomationType.CONFIRMATION_GENERATION)
        per_company.setdefault(request.company_id, []).append(
            PendingConfirmationRequest(
                confirmation_request_id=request.id,
                days_elapsed=(as_of - anchor.date()).days,
                reminders_sent=count,
                cadence_total=len(cadence),
                next_reminder_at=next_due_instant(
                    sent_at=anchor, cadence_days=cadence, reminders_sent=count
                ),
                exhausted=reminder_state(
                    sent_at=anchor, cadence_days=cadence, has_reply=False, now=now
                )
                is ReminderState.EXHAUSTED,
                automation_blocked=None if gate is None else gate.reason,
            )
        )
    companies_out = [
        PendingConfirmationsCompany(
            company_id=company_id,
            company_name=names.get(company_id),
            requests=per_company[company_id],
        )
        for company_id in sorted(per_company, key=lambda cid: br_by_company[cid])
    ]
    return PendingConfirmations(companies=companies_out)


def _service_gaps(
    decisions: Mapping[UUID, Mapping[AutomationType, AutomationGateDecision]],
) -> list[ServiceGap]:
    """The dashboard's "service not covered" flags, per company and type.

    One entry per denied (company, automation type) with the §5.1 gate's own
    reason — the same decision every automation trigger would get, read in
    bulk (``service_packages.gating_decisions_for_companies``). Companies
    and types keep their stable order, so the list reads top-down.
    """
    gaps: list[ServiceGap] = []
    for company_id, by_type in decisions.items():
        for automation_type, decision in by_type.items():
            reason = decision.reason
            if reason is not None:  # denied decisions always carry one
                gaps.append(
                    ServiceGap(company_id=company_id, automation=automation_type, reason=reason)
                )
    return gaps
