"""Milestone-panel domain tests (data-model §2.9 ``TenantConfig``; scope §2.15).

Pure rules, no I/O: the shipped defaults, the full-replace validation that
every write and every read of a stored block goes through, and the
anniversary arithmetic the panel's dates come from. The API/DB round trips
live in ``test_api_tenant_config.py`` and
``test_tenant_config_scenarios_db.py``.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from app.domain.compliance import NAR1_FILING_WINDOW_DAYS
from app.domain.milestones import (
    ANCHOR_ANNIVERSARY,
    MAX_DAYS_BEFORE,
    MAX_REMINDER_CADENCE_ENTRIES,
    MAX_REPLY_WINDOW_DAYS,
    MIN_REPLY_WINDOW_DAYS,
    MilestoneKind,
    MilestoneValidationError,
    default_milestones,
    milestone_dates,
    validate_milestones,
)

ANNIVERSARY = date(2026, 3, 1)


def payload(**overrides: object) -> dict[str, Any]:
    """The shipped defaults as a wire/stored mapping, with overrides applied."""
    values = default_milestones().as_dict()
    values.update(overrides)
    return values


# --- The bounds are decisions: pinned as numbers ---


def test_the_bounds_are_the_ones_this_slice_shipped() -> None:
    """A day count is bounded, and the bounds are part of the rule rather than
    an implementation detail: a year is longer than any annual period's
    milestone, the reply window is a matter of days, and the cadence is a short
    list. Pinned as numbers so a change to any of them is a decision — the
    out-of-bounds cases below use literals for the same reason. The window's
    bound is ``NAR1_FILING_WINDOW_DAYS - 2``: the offset bound it feeds must
    stay at or above the chain's own floor of two days (the client's reply must
    exceed the signature posting's offset, which is itself at least a day),
    which is the interaction the tests below pin."""
    assert MAX_DAYS_BEFORE == 365
    assert MIN_REPLY_WINDOW_DAYS == 1
    assert MAX_REPLY_WINDOW_DAYS == NAR1_FILING_WINDOW_DAYS - 2 == 40
    assert MAX_REMINDER_CADENCE_ENTRIES == 6


# --- The shipped defaults (founder-ruled 2026-09-24) ---


def test_the_shipped_defaults_are_the_founders_numbers() -> None:
    """The panel exists to let a TCSP set these; the shipped numbers are the
    ruled ones, so a change to any of them is a decision, not an edit."""
    milestones = default_milestones()

    assert milestones.continuation_request_days_before == 60
    assert milestones.data_confirmation_days_before == 30
    assert milestones.client_reply_days_before == 20
    assert milestones.signature_post_days_before == 14
    assert milestones.minimum_reply_window_days == 7
    assert milestones.reminder_cadence_days == (3, 7, 10)
    assert milestones.anchor == ANCHOR_ANNIVERSARY


def test_the_shipped_defaults_sit_inside_the_chain_and_validate() -> None:
    """The defect this pins: the defaults once put the signature posting (21 days
    before the anniversary) a day *before* the client's reply deadline (20 days
    before) — asking for a signature before the data the signature is about had
    been confirmed. A larger offset is an earlier date, so the shipped four must
    fall like the chain they configure, and the block the tenant reads before
    ever opening the panel must pass the same rule a PUT does."""
    milestones = default_milestones()

    assert (
        milestones.continuation_request_days_before
        > milestones.data_confirmation_days_before
        > milestones.client_reply_days_before
        > milestones.signature_post_days_before
    )
    assert validate_milestones(milestones.as_dict()) == milestones


def test_the_stored_shape_round_trips_through_validation() -> None:
    """The block the column stores is the block the validator accepts: one
    shape, so a stored row and a PUT body are validated by the same rule."""
    stored = default_milestones().as_dict()

    assert validate_milestones(stored) == default_milestones()
    assert stored["reminder_cadence_days"] == [3, 7, 10]
    assert isinstance(stored["reminder_cadence_days"], list)


# --- What the validator accepts ---


def test_a_reordered_but_coherent_config_is_accepted() -> None:
    """The rules are orderings, not the shipped numbers: a TCSP that asks
    earlier is configuring its own panel, which is what the panel is for."""
    milestones = validate_milestones(
        payload(
            continuation_request_days_before=90,
            data_confirmation_days_before=45,
            client_reply_days_before=30,
            signature_post_days_before=25,
            minimum_reply_window_days=12,
            reminder_cadence_days=[1],
        )
    )

    assert milestones.continuation_request_days_before == 90
    assert milestones.reminder_cadence_days == (1,)


def test_an_empty_reminder_cadence_is_accepted() -> None:
    """No reminders is a coherent cadence (and the sending slice is what would
    honour it); the validator refuses contradictions, not quiet tenants."""
    assert validate_milestones(payload(reminder_cadence_days=[])).reminder_cadence_days == ()


def test_the_shipped_values_survive_a_read_only_mapping() -> None:
    """The validator reads a mapping (a PUT body or a stored JSON block), so a
    valid block passed as a plain read-only mapping is still accepted."""
    stored: Mapping[str, object] = dict(default_milestones().as_dict())

    assert validate_milestones(stored) == default_milestones()


# --- Ordering ---


@pytest.mark.parametrize(
    "override",
    [
        {"continuation_request_days_before": 30},  # == data_confirmation
        {"continuation_request_days_before": 29},  # < data_confirmation
        {"data_confirmation_days_before": 20},  # == client_reply
        {"data_confirmation_days_before": 19},  # < client_reply
        {"client_reply_days_before": 14},  # == signature_post
        {"client_reply_days_before": 13},  # < signature_post
        # The pair the shipped defaults carried before the fix: the posting's
        # day arrived a day *before* the reply's.
        {"client_reply_days_before": 20, "signature_post_days_before": 21},
    ],
)
def test_an_incoherent_ordering_is_refused(override: dict[str, int]) -> None:
    """The chain the work follows, and every link of it is strict: a larger
    offset is an *earlier* date, so the client cannot be asked to reply after
    the documents went out for signature, be asked to confirm data after they
    were asked to reply, nor be asked to continue after the reply window
    closed."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(**override))

    assert any("must be greater than" in error for error in caught.value.errors)


def test_a_reply_at_or_before_the_signature_posting_names_the_reply_field() -> None:
    """Which field the refusal names: the reply is the one that has to move, so
    the message says so rather than blaming the posting — the same
    one-field-per-violation shape the other links use."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(client_reply_days_before=10))

    assert caught.value.errors == (
        "milestones.client_reply_days_before: must be greater than signature_post_days_before",
    )


def test_equal_milestone_days_are_refused() -> None:
    """Strictly greater, not greater-or-equal: two milestones on one day is
    one milestone, and the panel would show two names for it."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(
            payload(
                continuation_request_days_before=60,
                data_confirmation_days_before=60,
                client_reply_days_before=20,
            )
        )

    assert any("continuation_request_days_before" in error for error in caught.value.errors)
    assert any("data_confirmation_days_before" in error for error in caught.value.errors)


# --- Bounds and types ---


@pytest.mark.parametrize(
    "field",
    [
        "continuation_request_days_before",
        "data_confirmation_days_before",
        "client_reply_days_before",
        "signature_post_days_before",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 366])
def test_a_day_offset_outside_its_bounds_is_refused(field: str, value: int) -> None:
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(**{field: value}))

    assert any(error.startswith(f"milestones.{field}:") for error in caught.value.errors)


@pytest.mark.parametrize("value", [0, -1, 42, 61])
def test_a_reply_window_outside_its_bounds_is_refused(value: int) -> None:
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(minimum_reply_window_days=value))

    assert any(
        error.startswith("milestones.minimum_reply_window_days:") for error in caught.value.errors
    )


def test_a_window_that_leaves_no_reply_offset_is_refused_as_a_window() -> None:
    """The two bounds have to agree, or the tenant reads nonsense.

    A reply window of ``NAR1_FILING_WINDOW_DAYS`` would make the offset bound
    ``42 - 42 = 0`` — no valid offset at all — and a wider one would make it
    negative. The refusal names the **window** that caused it (the field the
    tenant has to change), and no violation may reach them as a negative offset
    bound."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(minimum_reply_window_days=NAR1_FILING_WINDOW_DAYS))

    assert caught.value.errors == (
        f"milestones.minimum_reply_window_days: must be between "
        f"{MIN_REPLY_WINDOW_DAYS} and {MAX_REPLY_WINDOW_DAYS}",
    )
    assert not any("client_reply_days_before" in error for error in caught.value.errors), (
        caught.value.errors
    )


def test_the_reply_offset_bound_is_reachable_at_every_window() -> None:
    """For every window the block admits, the bound `42 - window` is a value a
    tenant can actually configure: the offset at the bound is accepted and one
    day past it is refused with that positive bound in the message. Swept over
    the whole window range, because the two bounds are computed from each other
    and a change to either can make the other unreachable."""
    for window in range(MIN_REPLY_WINDOW_DAYS, MAX_REPLY_WINDOW_DAYS + 1):
        bound = NAR1_FILING_WINDOW_DAYS - window
        # Two days is the chain's own floor: the reply offset must exceed the
        # signature posting's, which is itself at least a day.
        assert bound >= 2
        # The ordering rules are the other half of the block: the offsets above
        # the reply stay above it, and the posting stays below.
        accepted = validate_milestones(
            payload(
                client_reply_days_before=bound,
                data_confirmation_days_before=bound + 1,
                continuation_request_days_before=bound + 2,
                signature_post_days_before=1,
                minimum_reply_window_days=window,
            )
        )
        assert accepted.client_reply_days_before == bound

        with pytest.raises(MilestoneValidationError) as caught:
            validate_milestones(
                payload(
                    client_reply_days_before=bound + 1,
                    data_confirmation_days_before=bound + 2,
                    continuation_request_days_before=bound + 3,
                    signature_post_days_before=1,
                    minimum_reply_window_days=window,
                )
            )
        assert any(
            error.startswith("milestones.client_reply_days_before:")
            and f"must not exceed {bound}" in error
            for error in caught.value.errors
        ), caught.value.errors


@pytest.mark.parametrize("value", ["20", 20.5, None, True])
@pytest.mark.parametrize(
    "field",
    ["continuation_request_days_before", "minimum_reply_window_days", "anchor"],
)
def test_a_value_that_is_not_the_declared_type_is_refused(field: str, value: object) -> None:
    """A stored row a hand edit corrupted must fail as a clean violation, not
    as a ``TypeError`` from arithmetic on a string."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(**{field: value}))

    assert any(error.startswith(f"milestones.{field}:") for error in caught.value.errors)


@pytest.mark.parametrize(
    "field",
    [
        "continuation_request_days_before",
        "data_confirmation_days_before",
        "client_reply_days_before",
        "signature_post_days_before",
        "minimum_reply_window_days",
        "reminder_cadence_days",
        "anchor",
    ],
)
def test_a_missing_field_is_refused(field: str) -> None:
    values = default_milestones().as_dict()
    del values[field]

    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(values)

    assert any(error.startswith(f"milestones.{field}:") for error in caught.value.errors)


# --- The reminder cadence ---


@pytest.mark.parametrize(
    "cadence",
    [[7, 3, 10], [3, 3], [0, 1], [1, -1], [1, 366], ["3"]],
)
def test_an_incoherent_cadence_is_refused(cadence: list[object]) -> None:
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(reminder_cadence_days=cadence))

    assert any(
        error.startswith("milestones.reminder_cadence_days:") for error in caught.value.errors
    )


def test_a_cadence_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(
            payload(reminder_cadence_days=list(range(1, MAX_REMINDER_CADENCE_ENTRIES + 2)))
        )

    assert any(
        error.startswith("milestones.reminder_cadence_days:") for error in caught.value.errors
    )


def test_the_cadence_bound_itself_is_accepted() -> None:
    cadence = list(range(1, MAX_REMINDER_CADENCE_ENTRIES + 1))

    assert validate_milestones(
        payload(reminder_cadence_days=cadence)
    ).reminder_cadence_days == tuple(cadence)


# --- The anchor ---


@pytest.mark.parametrize("anchor", ["planned_filing_date", "Anniversary", "", None])
def test_an_anchor_outside_the_vocabulary_is_refused(anchor: object) -> None:
    """The anchor is a closed, one-value vocabulary today: the anniversary is
    the ruled anchor, and a stored row naming another one is not a rule this
    product has."""
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(payload(anchor=anchor))

    assert any(error.startswith("milestones.anchor:") for error in caught.value.errors)


# --- The reply offset may not reach past the statutory window ---


def test_a_reply_offset_that_leaves_no_minimum_window_is_refused() -> None:
    """The bound is ``NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days``.

    It is the panel's coherence rule — the offset and the minimum window are two
    numbers measured against the one statutory window — and deliberately not a
    promise about the generator, whose refusal depends on the day it runs rather
    than on the offset (``test_the_offset_bound_does_not_promise_the_generator``
    in ``test_confirmation_domain.py`` pins that difference).
    """
    over_the_bound = NAR1_FILING_WINDOW_DAYS - 7 + 1  # 36, the bound is 35

    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(
            payload(client_reply_days_before=over_the_bound, data_confirmation_days_before=45)
        )

    assert any(
        error.startswith("milestones.client_reply_days_before:") and "35" in error
        for error in caught.value.errors
    )


def test_the_reply_offset_bound_itself_is_accepted() -> None:
    at_the_bound = NAR1_FILING_WINDOW_DAYS - 7

    assert at_the_bound == 35
    assert (
        validate_milestones(
            payload(client_reply_days_before=at_the_bound, data_confirmation_days_before=45)
        ).client_reply_days_before
        == at_the_bound
    )


def test_the_bound_moves_with_the_configured_reply_window() -> None:
    """It is ``42 - minimum_reply_window_days``, not the shipped 35: a tenant
    widening its window must move the bound with it."""
    with pytest.raises(MilestoneValidationError):
        validate_milestones(
            payload(
                client_reply_days_before=31,
                data_confirmation_days_before=45,
                minimum_reply_window_days=12,  # the bound is now 30
            )
        )
    assert (
        validate_milestones(
            payload(
                client_reply_days_before=30,
                data_confirmation_days_before=45,
                minimum_reply_window_days=12,
            )
        ).client_reply_days_before
        == 30
    )


# --- Every violation, in one refusal ---


def test_every_violation_is_reported_not_only_the_first() -> None:
    with pytest.raises(MilestoneValidationError) as caught:
        validate_milestones(
            {
                "continuation_request_days_before": 0,
                "data_confirmation_days_before": "thirty",
                "client_reply_days_before": 40,
                "signature_post_days_before": 21,
                "minimum_reply_window_days": 7,
                "reminder_cadence_days": [3, 3],
                "anchor": "planned_filing_date",
            }
        )

    errors = caught.value.errors
    assert len(errors) == 5
    assert any("continuation_request_days_before" in error for error in errors)
    assert any("data_confirmation_days_before" in error for error in errors)
    assert any("client_reply_days_before" in error for error in errors)
    assert any("reminder_cadence_days" in error for error in errors)
    assert any("anchor" in error for error in errors)


# --- The anniversary arithmetic ---


def test_the_milestones_are_counted_back_from_the_anniversary() -> None:
    """The anchor is the company's made-up date: every client-facing date is
    the anniversary minus its configured offset, and none of them is CR's
    last day (``anniversary + 42``). The dates fall in the order the work runs,
    which the offsets make so: bigger offsets, earlier dates."""
    dates = milestone_dates(ANNIVERSARY, 2026, milestones=default_milestones())

    assert dates.anniversary == date(2026, 3, 1)
    assert dates.continuation_request == date(2025, 12, 31)
    assert dates.data_confirmation == date(2026, 1, 30)
    assert dates.client_reply == date(2026, 2, 9)
    assert dates.signature_post == date(2026, 2, 15)
    assert dates.client_reply < dates.signature_post < ANNIVERSARY


def test_the_year_selects_the_anniversary_the_milestones_count_back_from() -> None:
    dates = milestone_dates(ANNIVERSARY, 2027, milestones=default_milestones())

    assert dates.anniversary == date(2027, 3, 1)
    assert dates.client_reply == date(2027, 2, 9)


def test_a_february_29_anchor_clamps_like_the_statutory_rule_does() -> None:
    """One clamp, one convention (contract §5.1.1): the milestone dates of a
    Feb 29 anchor use Feb 28 in a non-leap year, exactly as the NAR1 due date
    does — a second arithmetic here would be a second answer."""
    leap = milestone_dates(date(2024, 2, 29), 2024, milestones=default_milestones())
    ordinary = milestone_dates(date(2024, 2, 29), 2025, milestones=default_milestones())

    assert leap.anniversary == date(2024, 2, 29)
    assert ordinary.anniversary == date(2025, 2, 28)
    assert ordinary.client_reply == date(2025, 2, 8)


def test_a_configured_offset_moves_only_its_own_milestone() -> None:
    dates = milestone_dates(
        ANNIVERSARY, 2026, milestones=validate_milestones(payload(signature_post_days_before=15))
    )

    assert dates.client_reply == date(2026, 2, 9)
    assert dates.signature_post == date(2026, 2, 14)


def test_each_milestone_kind_is_paired_with_its_own_date_once() -> None:
    """The panel's read model (``app.application.service_renewals``) walks the
    dates through ``ordered()``, so the pairing is pinned here: four kinds, each
    with the date its offset produced, in the order the client meets them."""
    dates = milestone_dates(ANNIVERSARY, 2026, milestones=default_milestones())

    assert [kind for kind, _ in dates.ordered()] == [
        MilestoneKind.CONTINUATION_REQUEST,
        MilestoneKind.DATA_CONFIRMATION,
        MilestoneKind.CLIENT_REPLY,
        MilestoneKind.SIGNATURE_POST,
    ]
    assert dict(dates.ordered()) == {
        MilestoneKind.CONTINUATION_REQUEST: date(2025, 12, 31),
        MilestoneKind.DATA_CONFIRMATION: date(2026, 1, 30),
        MilestoneKind.CLIENT_REPLY: date(2026, 2, 9),
        MilestoneKind.SIGNATURE_POST: date(2026, 2, 15),
    }


def test_the_milestone_kind_vocabulary_is_the_contracts() -> None:
    """The values cross the wire on the panel read, so they are pinned as
    strings rather than left to the enum's member names."""
    assert [kind.value for kind in MilestoneKind] == [
        "continuation_request",
        "data_confirmation",
        "client_reply",
        "signature_post",
    ]
