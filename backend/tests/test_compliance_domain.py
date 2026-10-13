"""Unit tests for the compliance-calendar domain rules (scope §2.4;
contract §2.9 ``ComplianceEvent``).

Pure computation, no database and no app: the 42-day NAR1 window per
company type (including the refusal for types whose rule is not
established), the 15-day change window and its countdown boundaries, the
30/15/7-day alert bands with their green/amber/red scoring, the dormant
exclusion with the ``None`` (no status recorded) case, and the NAR1
late-fee exposure over the dated schedule (ADR-0010; contract §5.1.4's
slice-L note) — the on-time figure inside the 42-day window, the
escalating bands at their month edges with day clamping, and the dated
entry selection.
"""

from datetime import date, timedelta

import pytest

from app.domain.company_master import CompanyStatus, CompanyType
from app.domain.compliance import (
    CHANGE_FILING_WINDOW_DAYS,
    NAR1_FILING_WINDOW_DAYS,
    ComplianceEventKind,
    DueDateRuleNotEstablishedError,
    FeeBand,
    FeeSchedule,
    FeeScheduleEntry,
    FeeScheduleNotEstablishedError,
    LateFeeExposure,
    RiskLevel,
    add_months,
    change_window_countdown,
    change_window_deadline,
    dormant_excluded,
    late_fee_exposure,
    nar1_due_date,
    risk_band,
)

# --- NAR1 due date: the private-company 42-day rule ---


def test_private_company_due_date_is_42_days_after_the_anniversary() -> None:
    due = nar1_due_date(date(2026, 3, 1), 2026, company_type=CompanyType.PRIVATE)
    assert due == date(2026, 4, 12)
    assert (due - date(2026, 3, 1)).days == NAR1_FILING_WINDOW_DAYS


def test_due_date_uses_the_anniversary_in_the_requested_year() -> None:
    assert nar1_due_date(date(2026, 3, 1), 2027, company_type=CompanyType.PRIVATE) == date(
        2027, 4, 12
    )
    assert nar1_due_date(date(2026, 12, 20), 2027, company_type=CompanyType.PRIVATE) == date(
        2028, 1, 31
    )


def test_leap_day_anniversary_keeps_feb_29_in_a_leap_year() -> None:
    due = nar1_due_date(date(2020, 2, 29), 2024, company_type=CompanyType.PRIVATE)
    assert due == date(2024, 4, 11)
    assert (due - date(2024, 2, 29)).days == NAR1_FILING_WINDOW_DAYS


def test_leap_day_anniversary_clamps_to_feb_28_in_a_non_leap_year() -> None:
    due = nar1_due_date(date(2020, 2, 29), 2023, company_type=CompanyType.PRIVATE)
    assert due == date(2023, 4, 11)
    assert (due - date(2023, 2, 28)).days == NAR1_FILING_WINDOW_DAYS


@pytest.mark.parametrize(
    "company_type",
    [CompanyType.PUBLIC, CompanyType.COMPANY_LIMITED_BY_GUARANTEE, "not-a-type"],
)
def test_types_without_an_established_rule_refuse(company_type: object) -> None:
    """No guessed statutory date: the rule is per type, and only the private
    rule is established (CR PAM 8B dates public/guarantee returns from the
    return date, which the product does not hold)."""
    with pytest.raises(DueDateRuleNotEstablishedError) as excinfo:
        nar1_due_date(date(2026, 3, 1), 2026, company_type=company_type)  # type: ignore[arg-type]
    assert excinfo.value.company_type == company_type


# --- Change window: the 15-day statutory window ---


def test_change_window_deadline_is_15_days_after_the_effective_date() -> None:
    deadline = change_window_deadline(date(2026, 3, 1))
    assert deadline == date(2026, 3, 16)
    assert (deadline - date(2026, 3, 1)).days == CHANGE_FILING_WINDOW_DAYS


def test_change_window_countdown_boundaries() -> None:
    effective = date(2026, 3, 1)
    on_effective_day = change_window_countdown(effective, as_of=effective)
    assert (on_effective_day.deadline, on_effective_day.days_remaining) == (date(2026, 3, 16), 15)
    assert on_effective_day.is_late is False

    # The deadline day itself is still within the window; the next day is late.
    last_day = change_window_countdown(effective, as_of=date(2026, 3, 16))
    assert (last_day.days_remaining, last_day.is_late) == (0, False)
    next_day = change_window_countdown(effective, as_of=date(2026, 3, 17))
    assert (next_day.days_remaining, next_day.is_late) == (-1, True)


# --- Risk bands: 30/15/7-day alerts and green/amber/red scoring ---


@pytest.mark.parametrize(
    ("days_remaining", "expected_alert", "expected_level", "expected_overdue"),
    [
        (31, None, RiskLevel.GREEN, False),
        (30, ComplianceEventKind.ALERT_30D, RiskLevel.GREEN, False),
        (16, ComplianceEventKind.ALERT_30D, RiskLevel.GREEN, False),
        (15, ComplianceEventKind.ALERT_15D, RiskLevel.AMBER, False),
        (8, ComplianceEventKind.ALERT_15D, RiskLevel.AMBER, False),
        (7, ComplianceEventKind.ALERT_7D, RiskLevel.RED, False),
        (0, ComplianceEventKind.ALERT_7D, RiskLevel.RED, False),
        (-1, ComplianceEventKind.ALERT_7D, RiskLevel.RED, True),
    ],
)
def test_risk_band_boundaries(
    days_remaining: int,
    expected_alert: ComplianceEventKind | None,
    expected_level: RiskLevel,
    expected_overdue: bool,
) -> None:
    band = risk_band(days_remaining)
    assert band.days_remaining == days_remaining
    assert band.alert == expected_alert
    assert band.level == expected_level
    assert band.overdue == expected_overdue


# --- Dormant exclusion ---


@pytest.mark.parametrize(
    ("status", "excluded"),
    [
        (CompanyStatus.DORMANT, True),
        ("dormant", True),
        (CompanyStatus.ACTIVE, False),
        (CompanyStatus.STRIKING_OFF, False),
        (CompanyStatus.DISSOLVED, False),
        (None, False),  # no status recorded: the deadline generates
        ("mystery", False),  # only the documented dormant flag excludes
    ],
)
def test_dormant_excluded(status: str | None, excluded: bool) -> None:
    assert dormant_excluded(status) is excluded


# --- Late-fee exposure: the dated schedule evaluated at a delivery date ---

_PAM3C = FeeSchedule(
    form="NAR1",
    company_type="private",
    currency="HKD",
    entries=(
        FeeScheduleEntry(
            effective_from=date(2023, 12, 1),
            effective_to=None,
            on_time_fee=105,
            late_bands=(
                FeeBand(over_days=42, over_months=None, within_months=3, fee=870),
                FeeBand(over_days=None, over_months=3, within_months=6, fee=1740),
                FeeBand(over_days=None, over_months=6, within_months=9, fee=2610),
                FeeBand(over_days=None, over_months=9, within_months=None, fee=3480),
            ),
            source="test",
        ),
    ),
)


def test_add_months_shifts_calendar_months_and_clamps_the_day() -> None:
    assert add_months(date(2026, 3, 1), 3) == date(2026, 6, 1)
    # A 31st clamps to the target month's last day (April has 30).
    assert add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)
    # Feb 29 clamps to Feb 28 in a non-leap year, the anniversary convention.
    assert add_months(date(2025, 2, 28), 12) == date(2026, 2, 28)


def test_exposure_inside_the_42_day_window_is_the_on_time_fee() -> None:
    anniversary = date(2026, 3, 1)

    exposure = late_fee_exposure(anniversary, anniversary, _PAM3C)
    assert exposure == LateFeeExposure(fee_hkd=105, late=False)

    # On the due day itself the delivery is still "within 42 days" (PAM 3C wording).
    on_the_due_day = late_fee_exposure(
        anniversary, anniversary + timedelta(days=NAR1_FILING_WINDOW_DAYS), _PAM3C
    )
    assert on_the_due_day == LateFeeExposure(fee_hkd=105, late=False)

    # The day after is "more than 42 days" — the first late band.
    first_late_day = late_fee_exposure(
        anniversary, anniversary + timedelta(days=NAR1_FILING_WINDOW_DAYS + 1), _PAM3C
    )
    assert first_late_day == LateFeeExposure(fee_hkd=870, late=True)


@pytest.mark.parametrize(
    ("delivery", "expected_fee"),
    [
        (date(2026, 6, 1), 870),  # not more than 3 months: the edge day itself
        (date(2026, 6, 2), 1740),  # more than 3 months
        (date(2026, 9, 1), 1740),  # not more than 6 months
        (date(2026, 9, 2), 2610),
        (date(2026, 12, 1), 2610),  # not more than 9 months
        (date(2026, 12, 2), 3480),
        (date(2027, 6, 1), 3480),  # the top band is open-ended
    ],
)
def test_exposure_walks_the_late_bands_at_their_month_edges(
    delivery: date, expected_fee: int
) -> None:
    exposure = late_fee_exposure(date(2026, 3, 1), delivery, _PAM3C)
    assert exposure.fee_hkd == expected_fee
    assert exposure.late is True


def test_exposure_month_edges_respect_day_clamping() -> None:
    """A Jan 31 anniversary's 3-month edge is Apr 30, not May 1."""
    assert late_fee_exposure(date(2026, 1, 31), date(2026, 4, 30), _PAM3C) == (
        LateFeeExposure(fee_hkd=870, late=True)
    )
    assert late_fee_exposure(date(2026, 1, 31), date(2026, 5, 1), _PAM3C) == (
        LateFeeExposure(fee_hkd=1740, late=True)
    )


def test_exposure_evaluates_the_entry_applicable_at_the_delivery_date() -> None:
    """ADR-0010: the rule pack applicable at the relevant date — a fee
    revision mid-stream prices from its own effective_from."""
    revised = FeeSchedule(
        form="NAR1",
        company_type="private",
        currency="HKD",
        entries=(
            FeeScheduleEntry(
                effective_from=date(2020, 1, 1),
                effective_to=date(2026, 5, 31),
                on_time_fee=105,
                late_bands=(
                    FeeBand(over_days=42, over_months=None, within_months=3, fee=870),
                    FeeBand(over_days=None, over_months=3, within_months=None, fee=1740),
                ),
                source="old",
            ),
            FeeScheduleEntry(
                effective_from=date(2026, 6, 1),
                effective_to=None,
                on_time_fee=140,
                late_bands=(
                    FeeBand(over_days=42, over_months=None, within_months=3, fee=900),
                    FeeBand(over_days=None, over_months=3, within_months=None, fee=1800),
                ),
                source="new",
            ),
        ),
    )
    anniversary = date(2026, 3, 1)

    before = late_fee_exposure(anniversary, date(2026, 5, 31), revised)
    assert before == LateFeeExposure(fee_hkd=870, late=True)
    after = late_fee_exposure(anniversary, date(2026, 6, 1), revised)
    assert after == LateFeeExposure(fee_hkd=900, late=True)
    # A delivery before the revision prices under the schedule then in force,
    # even when that schedule's figures have since been superseded.
    on_time_before = late_fee_exposure(anniversary, date(2026, 3, 20), revised)
    assert on_time_before == LateFeeExposure(fee_hkd=105, late=False)
    on_time_after = late_fee_exposure(date(2026, 6, 15), date(2026, 6, 20), revised)
    assert on_time_after == LateFeeExposure(fee_hkd=140, late=False)


def test_exposure_refuses_a_date_no_entry_covers() -> None:
    """No applicable dated entry: refuse, never guess a fee."""
    with pytest.raises(FeeScheduleNotEstablishedError):
        late_fee_exposure(date(2026, 3, 1), date(2019, 1, 1), _PAM3C)
