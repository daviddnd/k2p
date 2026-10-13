"""The service-renewal lifecycle, the client-release gate, and the panel's
states (scope §2.15; data-model §2.9 ``ServiceRenewal``; contract §5.1.9).

``service_renewals`` (``app.adapters.persistence.service_renewals``) holds one
row per company per anniversary year: whether the client is continuing the
engagement for that period, and whether the fee has arrived (founder
direction, 2026-09-24). ``status`` is a single closed vocabulary over a single
lifecycle — ``pending | confirmed | declined | payment_received`` —
deliberately not two booleans: payment before confirmation is then
*unrepresentable* rather than merely discouraged.

**The record's job is the record; the gate is on what goes out.** The
founder's revision of 2026-09-24 moved the gate: a reminder about a filing or
a renewal needs no gate, and neither do generating and preparing a document —
**the moment that matters is the one where a document actually goes to the
client, filled or not**. So what consults this record is the release:
``release_block_reason`` refuses a client-bound document that is not within a
valid service scope and paid for. A ``pending`` row refuses nothing but that
release — and there it names the missing **answer**, not a missing fee — and the
confirmation-request trigger no longer reads the record at all: nothing a client
has not answered changes what staff may draft.

**The decline case: no side effect on the service assignment (ruled
2026-09-24).** Recording ``declined`` does **not** end, shorten, or otherwise
touch the company's ``ServiceAssignment``: a decline means no renewed coverage,
the §5.1 gate already answers ``out_of_coverage`` once the period ends, and the
dashboard already paints "service not covered — no automated action"
(scope §2.15). Automatically ending the assignment would mutate commercial
state the TCSP owns — staff end it themselves through the routes that already
exist — so the decline is recorded here and nowhere else.

**A record can be corrected, and a correction has to change something.** The
decision route records an answer and refuses a second one (``already_closed``);
the correction route (``…/{year}/correction``) exists because a mis-clicked
``declined`` would otherwise stop a client's whole year, and a wrongly-recorded
payment would leave the release gate wrong for ever (a bounced cheque is the
case). It is admitted from ``confirmed``, ``declined`` and
``payment_received``, requires a reason — a correction to a regulated client
record is a change that needs one — and it rides the same transition table:
``CORRECT_TO_CONFIRMED`` / ``CORRECT_TO_DECLINED`` target the decision the
correction lands on, so the route writes no status the table does not admit and
``payment_received`` stays reachable only from ``confirmed``. A correction that
would re-record the status the row already holds is not admitted at all: it
changes nothing, and the chain would claim a decision was corrected that
was not.

**This is ADR-0008's first transition table.** ADR-0008 requires each lifecycle
to be module-level data plus a single guard and a single executor that writes
its audit entry in the same transaction; nothing implemented it until now. The
renewal lifecycle is the first table because it is small and genuinely linear:
``pending → confirmed → payment_received``, ``pending → declined``, and the two
corrections back onto a decision. ``can_transition`` is written over *any* such
table rather than over this one, so the draft lifecycle (§4.2) can be the
second table behind the same guard when it lands — not a competitor. The
executor is ``app.application.service_renewals``: it locks the row, asks this
guard, writes the state, and appends ``service_renewal_recorded`` on the same
session.

**The release gate, in full** (``release_block_reason``, four scope legs then
the period's — the ruled order): no assignment answers ``no_assignment``, an ended
one ``assignment_ended``, a day outside the coverage period
``out_of_coverage``, a document whose work the assigned package does not cover
``form_not_covered``, and then the period leg: a period with no recorded answer
or a ``pending`` one is ``continuation_not_recorded``, and one the client has
confirmed but not paid is ``payment_not_received``. The first three values are
the §5.1 gating vocabulary itself (``GatingReason``) — the same situation named
the same way — and the three this record adds are new here. **Why the period
leg has two answers**: the two situations behind "the period is not paid" need
different staff remedies — one is "ask the client, then record the answer", the
other is "record the fee" — and a refusal that names the wrong remedy sends
staff chasing money that was never requested. The refusal names the reason and,
for either period leg, the period; the application composes the wire strings.

The gate is not the ADR-0012 licensing gate: manual fill → sign → export is the
customer's own work in every tier, and this is the TCSP's own commercial
precondition applied to what it *sends out*, never to its staff's own working
copies or its own reads.

**The panel's milestone states** are derived from the same things the gate
reads — the record and whether the record that completes a milestone exists —
by ``milestone_state`` for the date's part and ``period_release_block_reason``
for the gate's. ``blocked`` is the gate refusing, so the panel and the gate can
never disagree about what the panel can see, and a blocked milestone **says
which leg refused** (``MilestoneBlockReason``): a status without its remedy
leaves staff guessing, and the reason is the coordinate that names the remedy.

Pure and framework-free per ADR-0006: the standard library and one domain
module (``app.domain.service_packages``, whose vocabulary the gate reuses), and
nothing else.
"""

from collections.abc import Collection, Mapping
from datetime import date
from enum import StrEnum

from app.domain.service_packages import AssignmentStatus, GatingReason


class RenewalStatus(StrEnum):
    """Where a period's continuation stands (contract §5.1.9).

    ``payment_received`` is the terminal state of a continued period, and it is
    reachable only from ``confirmed`` — the guard is what makes the founder's
    ordering a property of the data rather than of a caller's discipline.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    PAYMENT_RECEIVED = "payment_received"


class RenewalAction(StrEnum):
    """What staff can record about a period (the request bodies' vocabulary).

    ``CORRECT_TO_CONFIRMED`` and ``CORRECT_TO_DECLINED`` are the correction's
    two targets, spelled out so the transition table can stay the one
    statement of what is legal: a correction lands on a decision, and which
    one is the request's choice, not a second mechanism beside the guard.
    """

    CONFIRM = "confirm"
    DECLINE = "decline"
    RECORD_PAYMENT = "record_payment"
    CORRECT_TO_CONFIRMED = "correct_to_confirmed"
    CORRECT_TO_DECLINED = "correct_to_declined"


# ADR-0008's transition table, as data: for every status, the actions it admits
# and the status each moves to. A status that appears with no entries is
# terminal (`payment_received` has nothing left to record).
#
# The correction rows are the ruled shape of 2026-09-24: admitted from any
# decided status, and only when the decision *changes* — re-recording the
# answer a row already holds would put a correction in the chain that corrected
# nothing. A paid row admits both targets because either one takes the payment
# back (a bounced cheque is the case the correction exists for), so
# `payment_received` is the target of exactly one pair, `confirmed` +
# `record_payment`, and stays reachable only from there.
RENEWAL_TRANSITIONS: Mapping[RenewalStatus, Mapping[RenewalAction, RenewalStatus]] = {
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


class RenewalTransitionError(ValueError):
    """The row's status does not admit the action; ADR-0008's guard refused.

    Carries both operands, so a caller can name the refusal: the API answers
    ``already_closed`` for a decision on a decided row,
    ``service_not_confirmed`` for a payment on a row the client has not
    confirmed, and — for a correction — ``not_decided`` on an open period or
    ``decision_unchanged`` when the correction would change nothing (contract
    §5.1.9).
    """

    def __init__(self, *, status: RenewalStatus, action: RenewalAction) -> None:
        self.status = status
        self.action = action
        super().__init__(f"a {status.value} renewal does not admit {action.value}")


def can_transition[StatusT: str, ActionT: str](
    table: Mapping[StatusT, Mapping[ActionT, StatusT]], *, status: StatusT, action: ActionT
) -> bool:
    """Whether ``action`` is admitted from ``status`` in this transition table.

    ADR-0008's single guard, asked of whichever table the caller owns — the
    renewal's today, the draft lifecycle's when it lands. It decides legality
    only: *who* may act is the API layer's single RBAC mechanism, and every
    mutation goes through the executor that calls this, so a state never moves
    without an entry in the audit chain (ADR-0009).
    """
    return action in table.get(status, {})


class ReleaseBlockReason(StrEnum):
    """Why a client-bound release is refused, as the wire names it (§5.1.9).

    The vocabulary is closed and short because each value is a remedy a staff
    member can act on: assign or renew the company's service package, widen its
    coverage period, add the work to the package's covered forms, record the
    client's answer, record the payment. The first three values are the §5.1
    gating vocabulary itself (``GatingReason``) — the same situation, named the
    same way — so only ``form_not_covered``, ``continuation_not_recorded`` and
    ``payment_not_received`` are new here.
    """

    NO_ASSIGNMENT = GatingReason.NO_ASSIGNMENT.value
    ASSIGNMENT_ENDED = GatingReason.ASSIGNMENT_ENDED.value
    OUT_OF_COVERAGE = GatingReason.OUT_OF_COVERAGE.value
    FORM_NOT_COVERED = "form_not_covered"
    CONTINUATION_NOT_RECORDED = "continuation_not_recorded"
    PAYMENT_NOT_RECEIVED = "payment_not_received"


def _scope_block_reason(
    *,
    as_of: date,
    assignment_status: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
) -> ReleaseBlockReason | None:
    """The legs read from the assignment, in the §5.1 order (b, then c).

    ``assignment_status``/``coverage_*`` are ``None`` together when the company
    has no assignment at all, exactly as ``decide_automation`` takes them.
    Coverage boundaries are inclusive — a retainer covers its first and last
    day.
    """
    if assignment_status is None:
        return ReleaseBlockReason.NO_ASSIGNMENT
    if assignment_status != AssignmentStatus.ACTIVE:
        return ReleaseBlockReason.ASSIGNMENT_ENDED
    assert coverage_start is not None and coverage_end is not None  # caller contract
    if not coverage_start <= as_of <= coverage_end:
        return ReleaseBlockReason.OUT_OF_COVERAGE
    return None


def _period_block_reason(renewal_status: RenewalStatus | None) -> ReleaseBlockReason | None:
    """The period leg: the client's answer, and the fee.

    Two answers, because the two situations behind them need different staff
    remedies. ``None`` (no row recorded for the period) and ``pending`` are
    both "the client's answer is not on file" — the remedy is to ask, then
    record it — so they answer ``continuation_not_recorded``. A ``confirmed``
    row is the case ``payment_not_received`` is for: the client said yes and
    the fee is the missing piece.

    ``declined`` reaches this leg like any other unpaid status (any client-bound
    download for a declined, unpaid period does), and it keeps
    ``payment_not_received`` rather than getting a third case: what the
    transition table makes unrepresentable is "declined **yet paid**", not a
    declined row at the gate. Its remedy runs through the correction route
    first, which is what the README's table says.
    """
    if renewal_status is RenewalStatus.PAYMENT_RECEIVED:
        return None
    if renewal_status is None or renewal_status is RenewalStatus.PENDING:
        return ReleaseBlockReason.CONTINUATION_NOT_RECORDED
    return ReleaseBlockReason.PAYMENT_NOT_RECEIVED


def release_block_reason(
    *,
    form: str | None,
    covered_forms: Collection[str],
    assignment_status: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
    renewal_status: RenewalStatus | None,
    as_of: date,
) -> ReleaseBlockReason | None:
    """Why this release is refused, or ``None`` — the ruled order, first failure wins.

    The gate the founder's revision of 2026-09-24 left standing: a document
    that goes **out to a client**, filled or not, must be within a valid
    service scope and paid for. Generating and preparing it are not gated, and
    neither is anything that merely notifies.

    In order:

    1. ``no_assignment`` — the company has no service assignment at all.
    2. ``assignment_ended`` — the assignment was ended by hand.
    3. ``out_of_coverage`` — ``as_of`` is outside its coverage period.
    4. ``form_not_covered`` — the document's work is not one the assigned
       package covers. ``form`` is the registry form code of that work
       (``app.domain.service_packages.CoveredForm``); ``None`` — a document
       whose work nothing can name — is not covered either, which is the
       fail-safe answer rather than a permit.
    5. ``continuation_not_recorded`` — no row for the period, or a ``pending``
       one: the client's answer is not on file.
    6. ``payment_not_received`` — the client confirmed the period and the fee
       has not been recorded.

    ``covered_forms`` is the assigned package's list, and is empty when the
    company has no package — by then legs 1 or 2 have already refused. The
    decision is deliberately not read beyond the period's status: ``declined``
    is a record, not a leg of its own, and "declined yet paid" is not a state
    the table can produce.
    """
    reason = _scope_block_reason(
        as_of=as_of,
        assignment_status=assignment_status,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    if reason is not None:
        return reason
    if form is None or form not in covered_forms:
        return ReleaseBlockReason.FORM_NOT_COVERED
    return _period_block_reason(renewal_status)


def period_release_block_reason(
    *,
    as_of: date,
    assignment_status: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
    renewal_status: RenewalStatus | None,
) -> ReleaseBlockReason | None:
    """The release gate's legs that a *period* can be judged by, for the panel.

    The §5.1.9 panel works per company and anniversary year and names no form,
    so it cannot judge the form leg: it judges the assignment, the coverage and
    the period's own record, and a period it reads as releasable can still
    refuse one particular document whose work the package does not cover. That
    leg is judged where a document is — at the download — and the panel's
    docstring says so. The shared legs are one implementation
    (``_scope_block_reason``, ``_period_block_reason``) in the ruled order, so
    the panel and the gate cannot drift on anything both can see.
    """
    reason = _scope_block_reason(
        as_of=as_of,
        assignment_status=assignment_status,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    if reason is not None:
        return reason
    return _period_block_reason(renewal_status)


class MilestoneBlockReason(StrEnum):
    """Which leg refuses a blocked milestone, as the §5.1.9 panel names it.

    The release gate's own vocabulary wherever the gate is the reason — the
    same values ``ReleaseBlockReason`` carries, named the same way, so the
    panel's sentence and the download's ``409`` cannot describe one refusal in
    two vocabularies — plus the two things the gate does not model:
    ``no_confirmation_request``, the reply milestone's **precondition** (no
    letter exists for the period, so there is nothing to reply to), and
    ``reminders_exhausted`` (§5.1.12: the request's reminder chain has run out
    with no reply — the final cadence point passed — so no automated reminder
    will follow and staff take over by hand). Both are milestone block reasons,
    not refusal codes: nothing in the product answers a request with either.

    ``form_not_covered`` is deliberately **absent**: a milestone names no form,
    so the panel can never produce it, and an unreachable value in a closed
    vocabulary is a promise the read does not keep (the same rule that removed
    the withdrawn continuation gate's two codes). The form leg is judged at the
    download, where the document's work is known.
    """

    NO_ASSIGNMENT = ReleaseBlockReason.NO_ASSIGNMENT.value
    ASSIGNMENT_ENDED = ReleaseBlockReason.ASSIGNMENT_ENDED.value
    OUT_OF_COVERAGE = ReleaseBlockReason.OUT_OF_COVERAGE.value
    CONTINUATION_NOT_RECORDED = ReleaseBlockReason.CONTINUATION_NOT_RECORDED.value
    PAYMENT_NOT_RECEIVED = ReleaseBlockReason.PAYMENT_NOT_RECEIVED.value
    NO_CONFIRMATION_REQUEST = "no_confirmation_request"
    REMINDERS_EXHAUSTED = "reminders_exhausted"


class MilestoneState(StrEnum):
    """One milestone's state on the §5.1.9 panel, as of the read's day."""

    PENDING = "pending"
    DUE = "due"
    OVERDUE = "overdue"
    DONE = "done"
    BLOCKED = "blocked"


def milestone_state(*, day: date, as_of: date, done: bool, blocked: bool) -> MilestoneState:
    """One milestone's state: what the record says first, then the date.

    Precedence is deliberate. **``done``** — the record that completes this
    milestone exists, so it reads done even on a day it ran late: it got done.
    **``blocked``** — a gate refuses the work, which is the operative fact and
    what tells staff why nothing is moving (the release gate for the signature
    posting, and a missing letter for the reply nobody can send to), and
    ``MilestoneBlockReason`` names which of them. Otherwise
    the date decides: ``overdue`` once the day has passed, ``due`` on the day
    itself, ``pending`` before it. A milestone whose day has passed is a
    *fact*; whether asking is still worth it is the generator's separate
    judgement (``app.domain.confirmation.reply_deadline``).
    """
    if done:
        return MilestoneState.DONE
    if blocked:
        return MilestoneState.BLOCKED
    if day < as_of:
        return MilestoneState.OVERDUE
    if day == as_of:
        return MilestoneState.DUE
    return MilestoneState.PENDING


__all__ = [
    "RENEWAL_TRANSITIONS",
    "MilestoneBlockReason",
    "MilestoneState",
    "ReleaseBlockReason",
    "RenewalAction",
    "RenewalStatus",
    "RenewalTransitionError",
    "can_transition",
    "milestone_state",
    "period_release_block_reason",
    "release_block_reason",
]
