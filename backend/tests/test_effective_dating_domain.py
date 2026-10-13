"""Unit tests for the as-of resolution rule (``app.domain.effective_dating``).

Pure domain code, no database: the single ADR-0007 resolution rule — per
fact group, the record with the latest ``effective_date`` not after the
as-of date — including its boundary and tie-break behaviour.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.domain.effective_dating import resolve_as_of


@dataclass(frozen=True)
class Fact:
    """Minimal effective-dated record standing in for any fact-group row."""

    effective_date: date
    created_at: datetime
    label: str = ""


CREATED_EARLY = datetime(2025, 1, 10, 9, 0, tzinfo=UTC)
CREATED_LATE = datetime(2025, 2, 10, 9, 0, tzinfo=UTC)

# --- the resolution rule ---


def test_no_records_resolves_to_none() -> None:
    assert resolve_as_of([], date(2025, 6, 1)) is None


def test_nothing_in_effect_yet_resolves_to_none() -> None:
    future = Fact(effective_date=date(2025, 7, 1), created_at=CREATED_EARLY)
    assert resolve_as_of([future], date(2025, 6, 1)) is None


def test_single_record_in_effect_resolves_to_it() -> None:
    record = Fact(effective_date=date(2025, 1, 1), created_at=CREATED_EARLY, label="only")
    assert resolve_as_of([record], date(2025, 6, 1)) is record


def test_latest_effective_date_not_after_as_of_wins() -> None:
    old = Fact(effective_date=date(2024, 1, 1), created_at=CREATED_EARLY, label="old")
    current = Fact(effective_date=date(2025, 1, 1), created_at=CREATED_EARLY, label="current")
    assert resolve_as_of([old, current], date(2025, 6, 1)) is current
    assert resolve_as_of([old, current], date(2024, 6, 1)) is old


def test_as_of_on_the_effective_date_includes_the_record() -> None:
    # "Not after the as-of date": a record takes effect on its effective_date.
    record = Fact(effective_date=date(2025, 3, 1), created_at=CREATED_EARLY)
    assert resolve_as_of([record], date(2025, 3, 1)) is record


def test_records_effective_after_as_of_are_ignored_even_if_created_earlier() -> None:
    # Creation time never pulls a future-dated record into effect.
    future = Fact(effective_date=date(2025, 12, 1), created_at=CREATED_EARLY, label="future")
    current = Fact(effective_date=date(2025, 1, 1), created_at=CREATED_LATE, label="current")
    assert resolve_as_of([future, current], date(2025, 6, 1)) is current


def test_resolution_is_independent_of_input_order() -> None:
    records = [
        Fact(effective_date=date(2024, 1, 1), created_at=CREATED_EARLY, label="a"),
        Fact(effective_date=date(2025, 1, 1), created_at=CREATED_LATE, label="b"),
        Fact(effective_date=date(2023, 1, 1), created_at=CREATED_EARLY, label="c"),
    ]
    expected = resolve_as_of(records, date(2025, 6, 1))
    assert expected is not None and expected.label == "b"
    assert resolve_as_of(list(reversed(records)), date(2025, 6, 1)) is expected


def test_same_day_correction_supersedes_by_created_at() -> None:
    # Two records share an effective_date (a same-day correction): the one
    # the system learned about later wins, so re-running resolution over the
    # same rows is deterministic.
    original = Fact(effective_date=date(2025, 3, 1), created_at=CREATED_EARLY, label="original")
    correction = Fact(effective_date=date(2025, 3, 1), created_at=CREATED_LATE, label="correction")
    assert resolve_as_of([original, correction], date(2025, 6, 1)) is correction
    assert resolve_as_of([correction, original], date(2025, 6, 1)) is correction
