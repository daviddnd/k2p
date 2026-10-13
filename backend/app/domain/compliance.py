"""Compliance-calendar domain rules (scope §2.4; contract §2.9
``ComplianceEvent``).

The statutory rules behind the calendar, stated once and tested as pure
computation — the application layer composes these, it never re-derives a
date:

- **NAR1 due date** — a local private company must deliver Form NAR1 for
  each year within **42 days after the anniversary of its incorporation**
  in that year. Source: Companies Registry PAM 8A (annual return of a local
  private company, 12/2023) — the NAR1 form itself is made "for the
  purposes of section 662 of Companies Ordinance (Cap. 622)" — and the same
  window appears in scope §2.4. The rule is explicitly **per company
  type**: CR's PAM 8B dates a public company's or guarantee company's
  return from the company's *return date* under the same s.662 (6 months
  after the end of its accounting reference period for a public company, 9
  months for a guarantee company), which is not the incorporation
  anniversary and not held by this product — so those types **refuse**
  (``DueDateRuleNotEstablishedError``) rather than guess an
  anniversary-based date. CR guidance for the non-private types is an open
  item (scope §2.4 flags the dormant treatment the same way). No extension
  applies when the last day falls on a weekend — CR PAM 8A says the
  deadline stands — so the arithmetic is plain calendar days.
- **15-day change window** — the notice of a change (ND2A for a company
  secretary/director appointment or cessation) is due within **15 days
  after the change**, per Cap. 622 ss.645/652 and CR PAM 5; the same 15-day
  window covers the other Wave-1 change forms (registered office, s.658).
  The ``ChangeEvent`` entity itself lands in Phase 4 — these two functions
  are built and tested now, wired to no data source, because the rule is
  fixed and the deadline/countdown arithmetic is exactly what Phase 4 will
  call. The day the window's clock starts on is the change's effective
  date; a decision that the TCSP only *learned* of the change later moves
  the product's reminder, never the statutory deadline.
- **Alert bands** — scope §2.4: alerts are surfaced at **30, 15 and 7
  days** before the due date and risk scores green/amber/red. The mapping
  below is the product decision those words leave open: the colour tracks
  urgency, while the alert kind records which threshold has been crossed —
  a 30-day alert is a green heads-up on the board, 15 days turns amber,
  and 7 days (or any past-due item) is red.
- **Dormant exclusion** — scope §2.4: a company flagged dormant is
  excluded from NAR1 deadline generation, and deadlines regenerate when its
  status returns to active. CR PAM 8A states the requirement does not apply
  to a dormant company within the meaning of Cap. 622 s.5(1). Only that
  documented flag excludes: the treatment of ``striking_off`` / ``dissolved``
  is not established in this slice (CR guidance open item) and is
  deliberately not extended to them.
- **NAR1 fee schedule** (ADR-0010; contract §5.1.4's slice-L note) — the
  registration fee in force for a delivery on a given date is dated policy
  data, not a code constant: ``backend/rules/nar1_fee_schedule.json``
  carries dated entries (``effective_from`` / ``effective_to``) over an
  on-time fee and escalating late bands counted from the anniversary (CR
  PAM 3C section (六)). ``late_fee_exposure`` evaluates the entry applicable
  at the delivery date; the application layer composes it over exactly the
  deadlines ``_generate_entries`` produces — local private companies, the
  dormant excluded — never a second set.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from app.domain.company_master import CompanyStatus, CompanyType

NAR1_FILING_WINDOW_DAYS = 42
CHANGE_FILING_WINDOW_DAYS = 15

_ALERT_7_DAYS = 7
_ALERT_15_DAYS = 15
_ALERT_30_DAYS = 30


class DueDateRuleNotEstablishedError(ValueError):
    """The company type's statutory due-date rule is not established here.

    Refusing is the point: no guessed statutory date. ``company_type``
    carries the refused value (a type whose rule is not the private
    company's 42-day anniversary rule, or a value outside the type
    vocabulary).
    """

    def __init__(self, company_type: object) -> None:
        self.company_type = company_type
        super().__init__(
            f"no established NAR1 due-date rule for company type {company_type!r}"
        )


class ComplianceEventKind(StrEnum):
    """``ComplianceEvent.kind`` vocabulary (contract §2.9)."""

    NAR1_DUE = "nar1_due"
    CHANGE_WINDOW_15D = "change_window_15d"
    ALERT_30D = "alert_30d"
    ALERT_15D = "alert_15d"
    ALERT_7D = "alert_7d"


class RiskLevel(StrEnum):
    """``ComplianceEvent.risk_level`` vocabulary (contract §2.9; scope §2.4)."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class RiskBand:
    """A deadline's position against the 30/15/7-day alert thresholds.

    ``alert`` is the deepest threshold crossed (``None`` while more than 30
    days out); ``level`` is the colour the dashboard paints; ``overdue`` is
    ``days_remaining < 0`` — a past due date is red, never amber or green.
    """

    days_remaining: int
    alert: ComplianceEventKind | None
    level: RiskLevel
    overdue: bool


@dataclass(frozen=True)
class ChangeWindow:
    """One change event's 15-day window, counted as of a date.

    ``is_late`` is False on the deadline day itself — "within 15 days after
    the change" includes the 15th day — and True from the day after.
    """

    deadline: date
    days_remaining: int
    is_late: bool


@dataclass(frozen=True)
class FeeBand:
    """One late-fee band: more than the lower edge, not more than
    ``within_months`` calendar months after the anniversary.

    The lower edge is the source table's wording made data — the first
    band's is the on-time window in days (the evaluation's boundary
    between the on-time fee and the first late band), each later band's is
    the previous band's upper edge in months — so exactly one of
    ``over_days`` / ``over_months`` is set. Evaluation walks the upper
    edges (``within_months``, ``None`` = open-ended), which the loader
    verifies contiguous and ascending.
    """

    over_days: int | None
    over_months: int | None
    within_months: int | None
    fee: int


@dataclass(frozen=True)
class FeeScheduleEntry:
    """One dated fee schedule (ADR-0010): the figures in force from
    ``effective_from`` through ``effective_to`` (``None`` = still in force).
    """

    effective_from: date
    effective_to: date | None
    on_time_fee: int
    late_bands: tuple[FeeBand, ...]
    source: str


@dataclass(frozen=True)
class FeeSchedule:
    """One form's fee schedule: dated entries plus the identity the policy
    applies to. ``form`` is the CR form code (``NAR1``), as in the forms
    registry's manifest."""

    form: str
    company_type: str
    currency: str
    entries: tuple[FeeScheduleEntry, ...]


class FeeScheduleNotEstablishedError(ValueError):
    """No dated schedule entry covers the delivery date.

    Refusing is the point: no guessed fee. Raised by
    ``late_fee_exposure`` when ``today`` falls outside every entry's
    ``effective_from`` / ``effective_to`` range.
    """

    def __init__(self, covered: date) -> None:
        self.covered = covered
        super().__init__(f"no fee schedule entry covers {covered.isoformat()}")


@dataclass(frozen=True)
class LateFeeExposure:
    """The registration fee in force for a delivery on ``as_of`` against the
    anniversary year the entry belongs to (contract §5.1.4's slice-L note).

    ``fee_hkd`` is the ordinary on-time fee while the 42-day window stands
    and the applicable band's fee once it has passed; ``late`` records
    which side of the window the delivery date is on.
    """

    fee_hkd: int
    late: bool


def add_months(anchor: date, months: int) -> date:
    """``anchor`` shifted ``months`` calendar months; the day clamps to the
    target month's last day (a Jan 31 anchor + 3 months lands on Apr 30).

    The clamp is the Feb-29 convention ``anniversary_in_year`` already
    applies to year shifts — one clamp rule for date arithmetic on the
    anniversary anchor, not one per call site.
    """
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, _month_last_day(year, month))
    return date(year, month, day)


def late_fee_exposure(anniversary: date, today: date, schedule: FeeSchedule) -> LateFeeExposure:
    """The NAR1 registration fee in force were the return delivered ``today``.

    ``anniversary`` is the return date the obligation belongs to (the
    anniversary in that return's year); ``today`` is the delivery date
    being priced. The dated entry applicable at ``today`` supplies the
    figures (ADR-0010: the rule pack applicable at the relevant date); a
    delivery within the first band's ``over_days`` of the anniversary pays
    the on-time fee, and each later delivery pays the first band whose
    ``within_months`` edge it has not passed. The bands' contiguity is the
    loader's guarantee, so a matching band always exists past the window.

    Applicability is the deadline's: the application layer composes this
    over exactly the entries ``nar1_due_date`` generates — local private
    companies, the dormant excluded — never a second set.
    """
    entry = _entry_applicable_at(schedule, today)
    first_band = entry.late_bands[0]
    window_end = anniversary + timedelta(days=first_band.over_days or 0)
    if today <= window_end:
        return LateFeeExposure(fee_hkd=entry.on_time_fee, late=False)
    for band in entry.late_bands:
        if band.within_months is None or today <= add_months(anniversary, band.within_months):
            return LateFeeExposure(fee_hkd=band.fee, late=True)
    raise FeeScheduleNotEstablishedError(today)  # unreachable: bands are contiguous


def _entry_applicable_at(schedule: FeeSchedule, today: date) -> FeeScheduleEntry:
    for entry in schedule.entries:
        if entry.effective_from <= today and (
            entry.effective_to is None or today <= entry.effective_to
        ):
            return entry
    raise FeeScheduleNotEstablishedError(today)


def _month_last_day(year: int, month: int) -> int:
    """The last day of a calendar month (28/29 in February), stdlib-only."""
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (following - timedelta(days=1)).day


def nar1_due_date(anniversary_date: date, year: int, *, company_type: CompanyType | str) -> date:
    """The NAR1 deadline for the anniversary falling in ``year``.

    ``anniversary_date`` is the company's anchor (its made-up date); only
    its month/day are used, so the anchor may be stored for any year. A
    February 29 anchor clamps to February 28 in a non-leap year — the
    convention the CSV import already applies to the stored anchor
    (contract §5.1.1). Raises ``DueDateRuleNotEstablishedError`` for every
    company type whose rule is not the private company's 42-day one.
    """
    try:
        private = CompanyType(company_type) is CompanyType.PRIVATE
    except ValueError:
        private = False
    if not private:
        raise DueDateRuleNotEstablishedError(company_type)
    return anniversary_in_year(anniversary_date, year) + timedelta(
        days=NAR1_FILING_WINDOW_DAYS
    )


def change_window_deadline(effective_date: date) -> date:
    """The last day a change event is still within its 15-day window.

    ``effective_date`` is the date the change took effect (the day the
    statutory clock starts, not the day the TCSP learned of it).
    """
    return effective_date + timedelta(days=CHANGE_FILING_WINDOW_DAYS)


def change_window_countdown(effective_date: date, *, as_of: date) -> ChangeWindow:
    """The change's window counted as of ``as_of``: deadline, days, late flag."""
    deadline = change_window_deadline(effective_date)
    days_remaining = (deadline - as_of).days
    return ChangeWindow(
        deadline=deadline, days_remaining=days_remaining, is_late=days_remaining < 0
    )


def risk_band(days_remaining: int) -> RiskBand:
    """The alert band and colour for a deadline ``days_remaining`` away.

    Boundaries are inclusive — on the day exactly 30 (15, 7) days remain,
    that threshold's alert is in force. A past due date is red and
    ``overdue``, with the deepest alert (``alert_7d``) still in force.
    """
    overdue = days_remaining < 0
    if overdue or days_remaining <= _ALERT_7_DAYS:
        return RiskBand(days_remaining, ComplianceEventKind.ALERT_7D, RiskLevel.RED, overdue)
    if days_remaining <= _ALERT_15_DAYS:
        return RiskBand(days_remaining, ComplianceEventKind.ALERT_15D, RiskLevel.AMBER, False)
    if days_remaining <= _ALERT_30_DAYS:
        return RiskBand(days_remaining, ComplianceEventKind.ALERT_30D, RiskLevel.GREEN, False)
    return RiskBand(days_remaining, None, RiskLevel.GREEN, False)


def dormant_excluded(status: str | None) -> bool:
    """Whether this company status excludes the company from NAR1 generation.

    Only the documented dormant flag excludes (scope §2.4; Cap. 622 s.5(1)
    per CR PAM 8A). ``None`` means no status was ever recorded — the
    deadline generates, because a reminder is the safe side of an unknown
    status. ``striking_off`` and ``dissolved`` are not excluded: their
    treatment is not established in this slice.
    """
    return status == CompanyStatus.DORMANT


def anniversary_in_year(anchor: date, year: int) -> date:
    """The anchor's month/day in ``year``; Feb 29 clamps to Feb 28 when the
    year has no Feb 29 (contract §5.1.1 convention).

    Public because it is the convention, not an implementation detail of the
    NAR1 rule: the milestone panel's client-facing dates count back from the
    same anniversary (``app.domain.milestones``), and a second clamp there
    would be a second answer for a Feb 29 anchor.
    """
    try:
        return anchor.replace(year=year)
    except ValueError:  # Feb 29 in a non-leap year
        return anchor.replace(year=year, day=28)
