"""The outbox: the intent to send, and what a delivery outcome means
(ADR-0011 §1; contract §5.1.11).

ADR-0011's crash-consistency rule is that the intent to send is a row written
**in the same transaction** as the state change that requests it, and that a
worker delivers it — no in-transaction network calls. This module is the pure
half of that: the row's lifecycle, the vocabulary a failure is named with, and
the two decisions the sender must get right.

**The row's lifecycle** (ADR-0008's third table, behind the same generic
``can_transition`` as the renewal's and the request's): ``pending`` until the
provider accepts the message (``sent``), until the provider refuses it in a way
that will not change (``failed``), or until the release it belongs to no longer
exists (``cancelled`` — a regeneration superseded the request). All three are
terminal, so a row is never reopened: a later attempt is a **new** row, and the
old one stays as the record of what was tried.

**A delivery failure is one of exactly three kinds.** The distinction is the
whole retry policy, and getting it wrong is how a client gets the same letter
twice or never:

- ``TRANSIENT`` — the message did not leave the deployment and a later attempt
  can succeed (the provider unreachable, a rate limit, a 4xx-class refusal).
  The sender retries inside the cycle with the worker's own backoff, and the
  row waits for the next cycle when the retries run out.
- ``UNCONFIRMED`` — the outcome is unknown: the transport died around the
  transfer, so the provider may or may not have taken the message. The row
  waits for a later cycle and is **not** retried at once, because an immediate
  retry is exactly the duplicate this product must not produce. On the Gmail
  leg the next attempt reconciles first (finds the message in Sent by its
  RFC 5322 ``Message-ID`` and records the send instead of repeating it); SMTP
  has no such lookup, and the residual — an ambiguous SMTP outcome can
  duplicate, and the ``Message-ID`` is what makes the duplicate detectable in
  the mailbox — is stated in the contract rather than hidden here.
- ``PERMANENT`` — the provider refused the message in a way that will not
  change (the recipient address is rejected, authentication failed). The row
  ends ``failed`` with the reason, and staff see it; retrying for ever would
  just fill the chain with the same refusal.

The kinds are the *domain's*; which wire answer is which is the adapter's to
say, because only the wire knows (an HTTP 5xx is a retry, an SMTP 5xx is a
permanent refusal — the same digits, opposite meanings).

Pure and framework-free per ADR-0006: the standard library, and the request
status this module reads (``app.domain.confirmation``).
"""

from datetime import UTC, datetime
from enum import StrEnum

from app.domain.confirmation import RequestStatus


class OutboxKind(StrEnum):
    """What a row sends (contract §5.1.12, migration ``0027``, widened
    ``0029`` and ``0033``).

    ``confirmation_request`` is the request's own letter, derived from the
    request row; ``reminder`` is the follow-up the sweep stored, which the row
    names through ``letter_document_id``. ``reconfirmation`` is the
    reconfirmer's letter answering a parsed reply (slice M5b) — like a
    reminder, the row names its own stored letter and answers the same
    request's thread. ``attribution_ask`` (ADR-0019 §1, migration ``0033``)
    is the registration/confirmation ask: the deployment's first
    **request-less** row shape — ``confirmation_request_id`` NULL, the case
    it serves named by ``attribution_case_id``, and its own stored letter in
    ``letter_document_id`` — which is why the pending indexes are one per
    request and kind, and (0033) one per case and kind. The pending indexes
    carry the kind so one request, or one case, may have every kind of row
    waiting at once, and the worker's release idempotency is per anchor
    **and** kind.
    """

    CONFIRMATION_REQUEST = "confirmation_request"
    REMINDER = "reminder"
    RECONFIRMATION = "reconfirmation"
    ATTRIBUTION_ASK = "attribution_ask"


# The hold a ``review_required`` reminder row is parked on until the staff
# release: ``not_before`` set to this named far-future instant, never ``NULL``
# (NULL would mean send-on-release, exactly what a held row must not do). The
# sweep writes it; ``app.application.outbox.release_reminders`` moves the row
# to the press instant. Named — not an arbitrary sentinel — because the route
# answers ``409 already_sent`` only when nothing held remains, and "nothing
# held" is this value compared exactly.
HELD_NOT_BEFORE: datetime = datetime(9999, 12, 31, tzinfo=UTC)


class OutboxStatus(StrEnum):
    """Where a send intent stands (contract §5.1.11).

    ``pending`` is the only state a worker looks at, and it is also the state
    a *blocked* row keeps — one the release gate refused, or one whose provider
    could not be reached yet: those rows are waiting, not finished, and their
    reason is recorded beside them for staff and for the next cycle.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxAction(StrEnum):
    """What an attempt can do to a row (the transition table's second axis)."""

    MARK_SENT = "mark_sent"
    FAIL = "fail"
    CANCEL = "cancel"


# ADR-0008's table, as data: for every status, the actions it admits and the
# status each moves to. Every terminal state appears with no entries.
OUTBOX_TRANSITIONS: dict[OutboxStatus, dict[OutboxAction, OutboxStatus]] = {
    OutboxStatus.PENDING: {
        OutboxAction.MARK_SENT: OutboxStatus.SENT,
        OutboxAction.FAIL: OutboxStatus.FAILED,
        OutboxAction.CANCEL: OutboxStatus.CANCELLED,
    },
    OutboxStatus.SENT: {},
    OutboxStatus.FAILED: {},
    OutboxStatus.CANCELLED: {},
}


class DeliveryFailure(StrEnum):
    """How a send attempt failed, as the retry policy reads it (see the module
    docstring: ``transient`` retries, ``unconfirmed`` waits, ``permanent`` ends
    the row)."""

    TRANSIENT = "transient"
    UNCONFIRMED = "unconfirmed"
    PERMANENT = "permanent"


def row_status_for(failure: DeliveryFailure) -> OutboxStatus:
    """The row's fate after a failed attempt: only a refusal ends it."""
    return OutboxStatus.FAILED if failure is DeliveryFailure.PERMANENT else OutboxStatus.PENDING


def retry_in_cycle(failure: DeliveryFailure) -> bool:
    """Whether the sender may try again immediately, with backoff.

    Only a transient failure: nothing left the deployment, so another attempt
    cannot duplicate anything. An unconfirmed outcome waits for the next cycle
    — the pause is the cheapest protection against the duplicate, and on the
    Gmail leg the reconcile is what makes the next attempt safe.
    """
    return failure is DeliveryFailure.TRANSIENT


def is_deliverable(request_status: RequestStatus) -> bool:
    """Whether a row may be delivered: its request must still be ``queued``.

    The defence behind the release: a request that a regeneration superseded
    (or one that is already ``sent``) must not have its letter go out, whatever
    the row says. ``draft`` is not deliverable either — a draft has not been
    released, and the release is the act that authorises the message.
    """
    return request_status is RequestStatus.QUEUED


def is_deliverable_kind(kind: OutboxKind, request_status: RequestStatus | None) -> bool:
    """Whether a row of this kind may be delivered against a request in this
    status — the kind-aware face of ``is_deliverable`` (contract §5.1.12).

    A request row carries the rule above unchanged: its request must still be
    ``queued``. A reminder row follows the chain it reminds, and a
    reconfirmation row the thread it answers, so both require their request to
    be ``sent`` — a superseded (or never-sent) request has ended the chain,
    and the row is cancelled with the same ``request_superseded`` vocabulary
    rather than delivered.

    An ``attribution_ask`` (ADR-0019 §1) is the request-less kind: its case
    is the anchor, so the status it needs is **no request at all** — ``None``
    is the deliverable answer, and a request status appearing on such a row is
    a row this predicate refuses rather than guesses about.
    """
    if kind is OutboxKind.ATTRIBUTION_ASK:
        return request_status is None
    if request_status is None:
        return False
    if kind in (OutboxKind.REMINDER, OutboxKind.RECONFIRMATION):
        return request_status is RequestStatus.SENT
    return is_deliverable(request_status)


__all__ = [
    "HELD_NOT_BEFORE",
    "OUTBOX_TRANSITIONS",
    "DeliveryFailure",
    "OutboxAction",
    "OutboxKind",
    "OutboxStatus",
    "is_deliverable",
    "is_deliverable_kind",
    "retry_in_cycle",
    "row_status_for",
]
