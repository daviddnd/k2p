"""Service-renewal domain tests (data-model §2.9; scope §2.15; contract §5.1.9).

Pure rules, no I/O: the transition table ADR-0008 asks for — including the two
correction targets — the guard that reads it, the release gate's four scope legs
and its period leg (which answers the missing answer and the missing fee
separately), the panel's projection of that gate, and the per-milestone state
derivation. The wire behaviour lives in ``test_api_service_renewals.py`` and the
record itself in ``test_service_renewals_scenarios_db.py``.
"""

from datetime import date
from enum import StrEnum

import pytest

from app.domain.service_renewals import (
    RENEWAL_TRANSITIONS,
    MilestoneBlockReason,
    MilestoneState,
    ReleaseBlockReason,
    RenewalAction,
    RenewalStatus,
    RenewalTransitionError,
    can_transition,
    milestone_state,
    period_release_block_reason,
    release_block_reason,
)

STATUSES = tuple(RenewalStatus)
ACTIONS = tuple(RenewalAction)

# The ruled lifecycle, pinned: pending → confirmed → payment_received,
# pending → declined, and the corrections back onto a decision. Written out
# here as data — the same shape ADR-0008 requires of §2.8's tables — so a
# change to the table is a change to this test, not a silent widening.
EXPECTED_TABLE: dict[RenewalStatus, dict[RenewalAction, RenewalStatus]] = {
    RenewalStatus.PENDING: {
        RenewalAction.CONFIRM: RenewalStatus.CONFIRMED,
        RenewalAction.DECLINE: RenewalStatus.DECLINED,
    },
    RenewalStatus.CONFIRMED: {
        RenewalAction.RECORD_PAYMENT: RenewalStatus.PAYMENT_RECEIVED,
        RenewalAction.CORRECT_TO_DECLINED: RenewalStatus.DECLINED,
    },
    RenewalStatus.DECLINED: {
        RenewalAction.CORRECT_TO_CONFIRMED: RenewalStatus.CONFIRMED,
    },
    RenewalStatus.PAYMENT_RECEIVED: {
        RenewalAction.CORRECT_TO_CONFIRMED: RenewalStatus.CONFIRMED,
        RenewalAction.CORRECT_TO_DECLINED: RenewalStatus.DECLINED,
    },
}


# --- The vocabulary is the database's ---


def test_the_status_vocabulary_is_the_one_the_database_checks() -> None:
    """The CHECK constraint in migration ``0020`` is the backstop; these are
    the values it admits, pinned as strings so a rename cannot quietly leave
    the column accepting a status the code no longer produces."""
    assert [status.value for status in RenewalStatus] == [
        "pending",
        "confirmed",
        "declined",
        "payment_received",
    ]


def test_the_release_refusal_vocabulary_is_the_one_the_contract_names() -> None:
    """These strings cross the wire and are the contract's §5.1.3 vocabulary, so
    they are pinned rather than derived.

    The three scope values are the §5.1 ``GatingReason`` vocabulary itself —
    the same situation named the same way — and ``form_not_covered``,
    ``continuation_not_recorded`` and ``payment_not_received`` are the ones this
    record adds beside them. ``continuation_not_recorded`` is fixed here rather
    than in the application layer: it is the refusal's *identity*, and the wire
    string is composed from it. The two continuation-gate values of the previous
    revision (``service_not_renewed``, ``service_continuation_declined``) are
    **gone**: the decision is a record, not a gate, and a refusal that no longer
    exists must not stay in the vocabulary a caller could depend on.
    """
    assert [reason.value for reason in ReleaseBlockReason] == [
        "no_assignment",
        "assignment_ended",
        "out_of_coverage",
        "form_not_covered",
        "continuation_not_recorded",
        "payment_not_received",
    ]


def test_the_milestone_state_vocabulary_is_the_one_the_contract_names() -> None:
    assert [state.value for state in MilestoneState] == [
        "pending",
        "due",
        "overdue",
        "done",
        "blocked",
    ]


def test_the_panel_block_vocabulary_names_every_leg_it_can_judge() -> None:
    """The panel's own answer to "which leg refused", pinned.

    It is the gate's vocabulary for every leg a *period* can be judged by, so
    the panel and the download cannot describe one refusal in two words, plus
    the reply milestone's two non-gate reasons — its precondition and its
    spent reminder chain (§5.1.12) — milestone block reasons, never refusal
    codes. ``form_not_covered`` is deliberately absent: a milestone names no
    form, an unreachable value in a closed vocabulary is a promise the read
    does not keep, and the application layer's conversion raises rather than
    let the form leg reach the wire.
    """
    assert [reason.value for reason in MilestoneBlockReason] == [
        "no_assignment",
        "assignment_ended",
        "out_of_coverage",
        "continuation_not_recorded",
        "payment_not_received",
        "no_confirmation_request",
        "reminders_exhausted",
    ]


def test_the_one_gate_leg_the_panel_omits_is_the_form_leg() -> None:
    """The pin that keeps the two vocabularies in step: every gate reason but
    ``form_not_covered`` has a panel value with the same string.

    A leg added to the gate without a decision about whether a period can judge
    it fails here — and the panel's conversion would raise on it at runtime —
    rather than leaving the panel silently unable to name the refusal it just
    showed.
    """
    panel = {reason.value for reason in MilestoneBlockReason}
    missing = {reason.value for reason in ReleaseBlockReason} - panel

    assert missing == {"form_not_covered"}
    for reason in ReleaseBlockReason:
        if reason is not ReleaseBlockReason.FORM_NOT_COVERED:
            assert MilestoneBlockReason(reason.value).value == reason.value


# --- The transition table (ADR-0008) ---


def test_the_transition_table_is_the_ruled_lifecycle() -> None:
    """The renewal is small and linear, and that is the point: the only path to
    ``payment_received`` runs through ``confirmed``, so "paid but not
    confirmed" is not a state this product can be in. A correction is a row of
    the same table, never a second mechanism beside it."""
    assert {
        status: dict(actions) for status, actions in RENEWAL_TRANSITIONS.items()
    } == EXPECTED_TABLE


def test_payment_received_is_reachable_only_from_confirmed() -> None:
    """The invariant the whole ordering rests on, asserted as an edge scan
    rather than by reading the table: exactly one (status, action) pair targets
    ``payment_received``, and its status is ``confirmed``. A correction targets
    a decision, so no correction can ever land a period in the paid state."""
    inbound = [
        (status, action)
        for status, actions in EXPECTED_TABLE.items()
        for action, target in actions.items()
        if target is RenewalStatus.PAYMENT_RECEIVED
    ]
    assert inbound == [(RenewalStatus.CONFIRMED, RenewalAction.RECORD_PAYMENT)]


def test_a_correction_always_changes_the_decision() -> None:
    """The correction's own rule, as data: from ``confirmed`` only the decline
    is admitted (and the other way round), from a paid row both are — because
    there the payment stamp is what changes — and from ``pending`` neither,
    since there is no decision to put right."""
    assert RenewalAction.CORRECT_TO_CONFIRMED not in RENEWAL_TRANSITIONS[RenewalStatus.CONFIRMED]
    assert RenewalAction.CORRECT_TO_DECLINED not in RENEWAL_TRANSITIONS[RenewalStatus.DECLINED]
    assert set(RENEWAL_TRANSITIONS[RenewalStatus.PAYMENT_RECEIVED]) == {
        RenewalAction.CORRECT_TO_CONFIRMED,
        RenewalAction.CORRECT_TO_DECLINED,
    }
    assert set(RENEWAL_TRANSITIONS[RenewalStatus.PENDING]) == {
        RenewalAction.CONFIRM,
        RenewalAction.DECLINE,
    }


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("action", ACTIONS)
def test_can_transition_answers_for_every_pair(
    status: RenewalStatus, action: RenewalAction
) -> None:
    """Every one of the twenty (status, action) pairs, against the pinned
    table: seven allowed transitions and thirteen refusals, including the two
    that would break the founder's ordering (``record_payment`` from
    ``pending`` or ``declined``) and every correction that would change
    nothing."""
    expected = action in EXPECTED_TABLE[status]
    assert can_transition(RENEWAL_TRANSITIONS, status=status, action=action) is expected


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (RenewalStatus.CONFIRMED, RenewalAction.CONFIRM),
        (RenewalStatus.DECLINED, RenewalAction.CONFIRM),
        (RenewalStatus.PAYMENT_RECEIVED, RenewalAction.DECLINE),
        (RenewalStatus.PENDING, RenewalAction.RECORD_PAYMENT),
        (RenewalStatus.DECLINED, RenewalAction.RECORD_PAYMENT),
        (RenewalStatus.PAYMENT_RECEIVED, RenewalAction.RECORD_PAYMENT),
        # A correction is not a way to record a first answer, and it never
        # re-records the answer the row already holds.
        (RenewalStatus.PENDING, RenewalAction.CORRECT_TO_CONFIRMED),
        (RenewalStatus.PENDING, RenewalAction.CORRECT_TO_DECLINED),
        (RenewalStatus.CONFIRMED, RenewalAction.CORRECT_TO_CONFIRMED),
        (RenewalStatus.DECLINED, RenewalAction.CORRECT_TO_DECLINED),
    ],
)
def test_payment_before_confirmation_and_every_second_answer_are_refused(
    status: RenewalStatus, action: RenewalAction
) -> None:
    """The refusals that carry weight, named one by one rather than left to the
    parametrized sweep: a payment against a period the client has not confirmed
    is the founder's ordering, the rest are a second answer to a question the
    record already holds — and the corrections among them are the ones that
    would have written a correction correcting nothing."""
    assert can_transition(RENEWAL_TRANSITIONS, status=status, action=action) is False


def test_the_guard_reads_a_table_that_does_not_carry_the_pair() -> None:
    """A status or action outside a table admits nothing rather than raising:
    fail closed, whatever made the value — a hand-edited row, a lifecycle's own
    vocabulary drifting from its table's."""
    table = {"pending": {"confirm": "confirmed"}}

    assert can_transition(table, status="pending", action="confirm") is True
    assert can_transition(table, status="elsewhere", action="confirm") is False
    assert can_transition(table, status="pending", action="elsewhere") is False
    assert can_transition({"pending": {}}, status="pending", action="confirm") is False


def test_the_guard_is_a_function_of_any_table_not_of_this_one() -> None:
    """ADR-0008's guard is generic: the draft lifecycle can be the second table
    behind the same function when it lands, rather than a competitor. Here a
    differently-shaped table — its own statuses and actions — is read by the
    same call, which is what "one guard" has to mean for the claim to hold."""

    class DraftStatus(StrEnum):
        GENERATED = "generated"
        APPROVED = "approved"

    class DraftAction(StrEnum):
        APPROVE = "approve"
        SEND = "send"

    draft_table = {
        DraftStatus.GENERATED: {DraftAction.APPROVE: DraftStatus.APPROVED},
        DraftStatus.APPROVED: {},
    }

    assert can_transition(draft_table, status=DraftStatus.GENERATED, action=DraftAction.APPROVE)
    assert not can_transition(draft_table, status=DraftStatus.APPROVED, action=DraftAction.SEND)


def test_the_transition_error_carries_both_operands() -> None:
    """The API's refusal names are derived from these two values
    (``service_not_confirmed``, ``not_decided``, ``decision_unchanged``,
    ``already_closed``), so the error has to carry them rather than a prose
    sentence."""
    error = RenewalTransitionError(
        status=RenewalStatus.DECLINED, action=RenewalAction.RECORD_PAYMENT
    )

    assert error.status is RenewalStatus.DECLINED
    assert error.action is RenewalAction.RECORD_PAYMENT
    assert "declined" in str(error) and "record_payment" in str(error)


# --- The release gate ---


COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
GATE_AS_OF = date(2026, 6, 30)
COVERED = ("NAR1", "ND2A")


def gate(**overrides: object) -> ReleaseBlockReason | None:
    """The release gate with every leg passing; ``overrides`` break one leg."""
    kwargs: dict[str, object] = {
        "form": "NAR1",
        "covered_forms": COVERED,
        "assignment_status": "active",
        "coverage_start": COVERAGE_START,
        "coverage_end": COVERAGE_END,
        "renewal_status": RenewalStatus.PAYMENT_RECEIVED,
        "as_of": GATE_AS_OF,
    }
    kwargs.update(overrides)
    return release_block_reason(**kwargs)  # type: ignore[arg-type]


def panel_gate(**overrides: object) -> ReleaseBlockReason | None:
    """The panel's projection of the same gate; ``overrides`` break one leg."""
    kwargs: dict[str, object] = {
        "assignment_status": "active",
        "coverage_start": COVERAGE_START,
        "coverage_end": COVERAGE_END,
        "renewal_status": RenewalStatus.PAYMENT_RECEIVED,
        "as_of": GATE_AS_OF,
    }
    kwargs.update(overrides)
    return period_release_block_reason(**kwargs)  # type: ignore[arg-type]


def test_all_four_legs_and_the_payment_pass_allows() -> None:
    assert gate() is None


def test_no_assignment_denies_and_needs_no_coverage() -> None:
    """A company with no assignment has no coverage dates to read, exactly as
    the §5.1 gate takes them, and the reason names the situation a staff member
    can fix: assign (or renew) a package."""
    assert (
        gate(assignment_status=None, coverage_start=None, coverage_end=None, covered_forms=())
        is ReleaseBlockReason.NO_ASSIGNMENT
    )


def test_an_ended_assignment_denies() -> None:
    assert gate(assignment_status="ended") is ReleaseBlockReason.ASSIGNMENT_ENDED


@pytest.mark.parametrize(
    "as_of",
    [COVERAGE_START - date.resolution, COVERAGE_END + date.resolution],
)
def test_a_day_outside_the_coverage_period_denies(as_of: date) -> None:
    assert gate(as_of=as_of) is ReleaseBlockReason.OUT_OF_COVERAGE


def test_the_coverage_boundaries_are_inclusive() -> None:
    """A retainer covers its first and last day — the §5.1 gate's own
    convention, reused rather than restated."""
    assert gate(as_of=COVERAGE_START) is None
    assert gate(as_of=COVERAGE_END) is None


def test_a_form_the_package_does_not_cover_denies() -> None:
    """The founder's work dimension: paying for the annual return covers NAR1
    work and not an ND2A change, so the ND2A document waits for a package that
    covers it — and the remedy is on the package, not on the record."""
    assert gate(form="ND2A", covered_forms=("NAR1",)) is ReleaseBlockReason.FORM_NOT_COVERED
    assert gate(form="NAR1", covered_forms=("ND2A",)) is ReleaseBlockReason.FORM_NOT_COVERED
    assert gate(form="ND2A", covered_forms=("NAR1", "ND2A")) is None


def test_a_work_nothing_can_name_is_not_covered() -> None:
    """``None`` — a document whose form nothing resolves — is refused, not
    permitted: the fail-safe direction, the way an unknown reason never allows
    an automation."""
    assert gate(form=None) is ReleaseBlockReason.FORM_NOT_COVERED
    assert gate(covered_forms=()) is ReleaseBlockReason.FORM_NOT_COVERED


@pytest.mark.parametrize("status", [None, RenewalStatus.PENDING])
def test_the_period_leg_names_an_unrecorded_answer_distinctly(
    status: RenewalStatus | None,
) -> None:
    """Two situations behind "the period is not paid" need different staff
    remedies, so the gate gives them different refusals: no row and a
    ``pending`` row both mean the client's answer is not on file — ask, then
    record it — and the refusal says that. Answering "payment not received" for
    a period nobody has asked the client about sends staff to chase money that
    was never requested, which is the defect this distinction fixes.
    """
    assert gate(renewal_status=status) is ReleaseBlockReason.CONTINUATION_NOT_RECORDED


def test_a_confirmed_and_unpaid_period_is_the_payment_refusal() -> None:
    """The case the payment refusal is *for*: the client said yes and the fee
    is the missing piece, so the remedy the refusal names — record the fee — is
    the right one. The transition table is what makes this the only
    confirmed-and-unpaid reading."""
    assert gate(renewal_status=RenewalStatus.CONFIRMED) is ReleaseBlockReason.PAYMENT_NOT_RECEIVED


def test_a_declined_period_reaches_the_period_leg() -> None:
    """A ``declined`` row **is** reachable at the period leg — any client-bound
    download for a declined, unpaid period reaches it — and it keeps the
    payment refusal rather than getting a third case of its own: what the
    transition table makes unrepresentable is "declined **yet paid**", not a
    declined row at the gate. Its remedy runs through the correction route
    first (a wrongly-recorded decline is corrected), then the fee, which is
    what the README's table says.
    """
    assert gate(renewal_status=RenewalStatus.DECLINED) is ReleaseBlockReason.PAYMENT_NOT_RECEIVED


def test_a_paid_period_is_the_only_one_that_passes_the_period_leg() -> None:
    assert gate(renewal_status=RenewalStatus.PAYMENT_RECEIVED) is None


def test_the_first_failing_leg_wins_in_the_ruled_order() -> None:
    """Every leg can be broken at once; the answer is the first in the order
    the founder ruled, because that is the remedy a caller can act on first —
    an unassigned company is not "unpaid", it is unassigned."""
    assert (
        gate(
            assignment_status=None,
            coverage_start=None,
            coverage_end=None,
            covered_forms=(),
            form=None,
            renewal_status=None,
        )
        is ReleaseBlockReason.NO_ASSIGNMENT
    )
    assert gate(assignment_status="ended", covered_forms=(), renewal_status=None) is (
        ReleaseBlockReason.ASSIGNMENT_ENDED
    )
    assert gate(as_of=date(2027, 1, 1), covered_forms=(), renewal_status=None) is (
        ReleaseBlockReason.OUT_OF_COVERAGE
    )
    assert gate(covered_forms=(), renewal_status=None) is ReleaseBlockReason.FORM_NOT_COVERED


def test_a_pending_row_refuses_nothing_but_the_release() -> None:
    """R3 as a rule: with every other leg passing, a ``pending`` period's only
    answer is the period leg — and because a refusal must name the operator's
    remedy, it names the missing **answer** rather than a fee nobody asked for.
    Drafting and preparing are not gated at all."""
    assert (
        gate(renewal_status=RenewalStatus.PENDING)
        is ReleaseBlockReason.CONTINUATION_NOT_RECORDED
    )
    assert not hasattr(ReleaseBlockReason, "SERVICE_NOT_RENEWED")
    assert not hasattr(ReleaseBlockReason, "SERVICE_CONTINUATION_DECLINED")


@pytest.mark.parametrize("status", [None, *RenewalStatus])
@pytest.mark.parametrize(
    ("assignment_status", "coverage_start", "coverage_end", "as_of"),
    [
        ("active", COVERAGE_START, COVERAGE_END, GATE_AS_OF),
        ("active", COVERAGE_START, COVERAGE_END, date(2027, 1, 1)),
        ("ended", COVERAGE_START, COVERAGE_END, GATE_AS_OF),
        (None, None, None, GATE_AS_OF),
    ],
)
def test_the_panel_projection_agrees_with_the_gate_it_can_see(
    status: RenewalStatus | None,
    assignment_status: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
    as_of: date,
) -> None:
    """The two functions cannot drift on a leg both can judge: with a covered
    form (the leg only the gate has), the full gate and the panel's projection
    answer the same thing for every status and every assignment case."""
    overrides: dict[str, object] = {
        "as_of": as_of,
        "assignment_status": assignment_status,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "renewal_status": status,
    }
    assert gate(**overrides) is panel_gate(**overrides)


def test_the_panel_projection_does_not_judge_the_form_leg() -> None:
    """And the one leg it cannot judge is skipped, not guessed: the panel names
    no form, so a period it reads as releasable is releasable *for the work the
    package covers* — a document whose form is outside that list is still
    refused at the download, where the form is known."""
    assert panel_gate() is None
    assert gate(form="NAR1", covered_forms=("ND2A",)) is ReleaseBlockReason.FORM_NOT_COVERED


# --- The panel's states ---


AS_OF = date(2026, 12, 1)


def test_the_record_completing_a_milestone_wins_over_the_date() -> None:
    """A milestone that got done reads ``done`` even when it ran late: the
    panel is a work list, and work that is finished is not work. The date is
    still in the row for a reader who wants to see how late it was."""
    assert (
        milestone_state(day=date(2026, 10, 1), as_of=AS_OF, done=True, blocked=False)
        is MilestoneState.DONE
    )
    # Even when a gate would refuse the *next* step, the milestone itself is
    # done — the two are different facts about the same period.
    assert (
        milestone_state(day=date(2026, 10, 1), as_of=AS_OF, done=True, blocked=True)
        is MilestoneState.DONE
    )


def test_a_blocked_milestone_says_so_instead_of_reading_overdue() -> None:
    """A gate refusing is the operative fact: "the client has not confirmed the
    period" is actionable, "the day passed" is not, and a panel that showed
    only the date would send staff to chase a client for work that cannot
    proceed at all."""
    assert (
        milestone_state(day=date(2026, 10, 1), as_of=AS_OF, done=False, blocked=True)
        is MilestoneState.BLOCKED
    )
    assert (
        milestone_state(day=date(2026, 12, 1), as_of=AS_OF, done=False, blocked=True)
        is MilestoneState.BLOCKED
    )
    assert (
        milestone_state(day=date(2027, 1, 1), as_of=AS_OF, done=False, blocked=True)
        is MilestoneState.BLOCKED
    )


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 30), MilestoneState.OVERDUE),
        (date(2026, 12, 1), MilestoneState.DUE),
        (date(2026, 12, 2), MilestoneState.PENDING),
    ],
)
def test_the_date_decides_the_rest(day: date, expected: MilestoneState) -> None:
    """The day itself is ``due`` (not overdue and not still pending), the day
    after it is overdue, and the day before it is pending — the boundary is
    inclusive, like every other deadline in this product."""
    assert milestone_state(day=day, as_of=AS_OF, done=False, blocked=False) is expected
