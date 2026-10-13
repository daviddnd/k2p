"""Pure rules for the confirmation-discovery sweep (the discovery slice).

The vocabulary the cycle reports in (``generated | refused | skipped``, the
skip ``already_requested``), the eligibility predicate over
``(data_confirmation_date, as_of, has_request_for_year)``, the earliest-year
selection, and the candidate-year range that mirrors the client-actions
panel's own (``range(as_of.year - 1, as_of.year + 3)``).

Always on: no database, no I/O — the standard library only.
"""

from datetime import date

from app.domain.confirmation_discovery import (
    DiscoveryOutcome,
    DiscoverySkip,
    candidate_years,
    earliest_arrived_year,
    earliest_eligible_year,
    is_eligible,
)

AS_OF = date(2026, 10, 3)


def test_the_outcome_vocabulary_is_the_report_shape() -> None:
    assert DiscoveryOutcome.GENERATED == "generated"
    assert DiscoveryOutcome.REFUSED == "refused"
    assert DiscoveryOutcome.SKIPPED == "skipped"
    assert len(set(DiscoveryOutcome)) == 3


def test_the_skip_vocabulary_names_the_panels_done_signal() -> None:
    assert DiscoverySkip.ALREADY_REQUESTED == "already_requested"
    assert len(set(DiscoverySkip)) == 1


def test_candidate_years_mirror_the_panels_range() -> None:
    assert list(candidate_years(AS_OF)) == [2025, 2026, 2027, 2028]
    january = date(2026, 1, 15)
    assert list(candidate_years(january)) == [2025, 2026, 2027, 2028]
    december = date(2026, 12, 31)
    assert list(candidate_years(december)) == [2025, 2026, 2027, 2028]


def test_a_ask_date_on_or_before_as_of_has_arrived() -> None:
    assert is_eligible(date(2026, 10, 3), AS_OF, has_request=False) is True
    assert is_eligible(date(2026, 10, 2), AS_OF, has_request=False) is True
    assert is_eligible(date(2026, 10, 4), AS_OF, has_request=False) is False


def test_an_asked_year_is_not_eligible_whatever_its_date() -> None:
    assert is_eligible(date(2026, 10, 3), AS_OF, has_request=True) is False
    assert is_eligible(date(2020, 1, 1), AS_OF, has_request=True) is False


def test_the_earliest_eligible_year_is_the_earliest_arrived_unasked_one() -> None:
    dates = {
        2025: date(2025, 10, 1),
        2026: date(2026, 10, 1),
        2027: date(2027, 10, 1),
        2028: date(2028, 10, 1),
    }
    # Nothing asked: the earliest arrived year wins.
    assert earliest_eligible_year(dates, set(), AS_OF) == 2025
    # The earliest arrived year asked: the next arrived unasked year wins.
    assert earliest_eligible_year(dates, {2025}, AS_OF) == 2026
    # A request for a later year does not mask the earlier arrived one.
    assert earliest_eligible_year(dates, {2026}, AS_OF) == 2025


def test_the_earliest_eligible_year_ignores_years_that_have_not_arrived() -> None:
    dates = {
        2025: date(2025, 10, 1),
        2026: date(2026, 11, 1),  # after as_of: not arrived
        2027: date(2027, 11, 1),
        2028: date(2028, 11, 1),
    }
    assert earliest_eligible_year(dates, set(), AS_OF) == 2025
    assert earliest_eligible_year(dates, {2025}, AS_OF) is None


def test_the_earliest_arrived_year_is_request_blind() -> None:
    dates = {
        2025: date(2025, 10, 1),
        2026: date(2026, 11, 1),
        2027: date(2027, 11, 1),
        2028: date(2028, 11, 1),
    }
    assert earliest_arrived_year(dates, AS_OF) == 2025
    # Requests do not change which year has arrived.
    assert earliest_arrived_year(dates, AS_OF) == 2025


def test_the_earliest_candidate_year_is_always_arrived() -> None:
    # The earliest candidate year is as_of.year - 1, and its ask date is that
    # year's anniversary minus a positive offset: strictly in the past for
    # any as_of in year Y. The None branches of the selectors are defensive;
    # the reachable "nothing due" shape is every arrived year asked.
    as_of = date(2026, 1, 15)
    dates = {
        2025: date(2025, 12, 1),
        2026: date(2026, 12, 1),
        2027: date(2027, 12, 1),
        2028: date(2028, 12, 1),
    }
    assert earliest_arrived_year(dates, as_of) == 2025
    assert earliest_eligible_year(dates, {2025}, as_of) is None
