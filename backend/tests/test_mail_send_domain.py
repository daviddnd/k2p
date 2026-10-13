"""Pure rules of the sending slice: the send modes, the two transition tables,
and what a delivery failure means for the outbox row.

No database, no provider, no session: the vocabulary and the decisions that
have to be right before anything leaves the deployment. The wire behaviour
lives in ``test_gmail_client.py`` / ``test_smtp_client.py`` and the end-to-end
walk in ``test_mail_send_scenarios_db.py``.

The two rules this file exists for, in the founder's words and the brief's:
**the government destination's mode is not a switch a tenant may turn off**
(it carries ``review_required`` as its only admitted value, because relaxing
it needs AGENTS.md's no-unattended-submission constraint amended and a legal
review), and **a permanent provider refusal is the end of the row while
everything else waits** (ADR-0011's retry-with-backoff, minus the case that
will never succeed).
"""

import pytest

from app.domain.confirmation import (
    LIVE_REQUEST_STATUSES,
    REQUEST_TRANSITIONS,
    RequestAction,
    RequestStatus,
)
from app.domain.mail_send import (
    OUTBOX_TRANSITIONS,
    DeliveryFailure,
    OutboxAction,
    OutboxKind,
    OutboxStatus,
    is_deliverable,
    is_deliverable_kind,
    retry_in_cycle,
    row_status_for,
)
from app.domain.send_modes import (
    DEFAULT_SEND_MODES,
    SendDestination,
    SendMode,
    SendModes,
    SendModesValidationError,
    default_send_modes,
    validate_send_modes,
)
from app.domain.service_renewals import can_transition

# --- The send modes -------------------------------------------------------


def test_the_shipped_defaults_are_the_founders_two_answers() -> None:
    """Client mail goes automatically; anything bound for a government filing
    destination is held for a staff member. The defaults are the ruling."""
    modes = default_send_modes()
    assert modes.client is SendMode.AUTOMATIC
    assert modes.government is SendMode.REVIEW_REQUIRED
    assert modes.as_dict() == {"client": "automatic", "government": "review_required"}
    assert modes == DEFAULT_SEND_MODES


def test_the_client_destination_admits_both_modes() -> None:
    """The client destination carries no constraint: a tenant may hold client
    mail for review as well as send it automatically."""
    modes = validate_send_modes({"client": "review_required", "government": "review_required"})
    assert modes.client is SendMode.REVIEW_REQUIRED
    assert modes.mode_for(SendDestination.CLIENT) is SendMode.REVIEW_REQUIRED


def test_a_stored_block_round_trips_through_the_validator() -> None:
    """What ``as_dict`` writes is what the validator accepts — the stored
    column is read back by the same rule that wrote it."""
    modes = validate_send_modes(DEFAULT_SEND_MODES.as_dict())
    assert modes == DEFAULT_SEND_MODES


def test_the_government_destination_refuses_automatic() -> None:
    """Ruling 2: the e-Registry default is not a switch a tenant may turn off.

    The refusal names the field and the reason, because the operator has to
    learn why this one write is not like the others.
    """
    with pytest.raises(SendModesValidationError) as caught:
        validate_send_modes({"client": "automatic", "government": "automatic"})
    assert caught.value.errors == (
        "send_modes.government: 'automatic' is not admitted — documents bound for a "
        "government filing destination are always held for review (AGENTS.md's "
        "no-unattended-submission constraint; relaxing it needs that constraint "
        "amended and a legal review)",
    )


def test_every_violation_is_reported_at_once() -> None:
    """The PUT answers with the whole list, like the milestone block's rule."""
    with pytest.raises(SendModesValidationError) as caught:
        validate_send_modes({"client": "sometimes", "government": "automatic", "e_registry": "x"})
    errors = caught.value.errors
    assert "send_modes.client: " in errors[0]
    assert errors[1].startswith("send_modes.government: ")
    assert errors[2].startswith("send_modes.e_registry: ")


def test_a_missing_destination_is_a_violation_not_a_key_error() -> None:
    """A hand-edited or truncated stored block fails as a clean validation
    error naming the field — never as a bare ``KeyError``."""
    with pytest.raises(SendModesValidationError) as caught:
        validate_send_modes({"client": "automatic"})
    assert caught.value.errors == ("send_modes.government: is required",)


def test_a_non_mapping_block_is_refused() -> None:
    with pytest.raises(SendModesValidationError) as caught:
        validate_send_modes(["automatic", "review_required"])
    assert caught.value.errors == ("send_modes: must be an object",)


def test_the_vocabulary_is_closed_in_both_directions() -> None:
    with pytest.raises(SendModesValidationError) as caught:
        validate_send_modes({"client": "manual", "government": "review_required"})
    assert caught.value.errors == (
        "send_modes.client: unknown mode 'manual' (one of: automatic, review_required)",
    )
    # The destination vocabulary is closed too: a destination this build does
    # not know is not a value it can store for a later slice to honour.
    assert set(SendDestination) == {SendDestination.CLIENT, SendDestination.GOVERNMENT}


def test_mode_for_an_unknown_destination_is_refused() -> None:
    with pytest.raises(ValueError):
        SendModes(client=SendMode.AUTOMATIC, government=SendMode.REVIEW_REQUIRED).mode_for("ird")


# --- The confirmation request's send lifecycle (ADR-0008's second table) ---


def test_the_request_transition_table_is_exactly_this() -> None:
    """Which moves are legal, as one statement of the lifecycle.

    ``draft → sent`` is **not** in it: a message is released before it is
    delivered, so no caller can send a request without a release having been
    recorded first. ``sent`` admits only ``supersede`` — a regeneration over
    a sent request ends its reminder chain (§5.1.12, the 2026-09-29 Critical
    fix) — and ``superseded`` is terminal.
    """
    assert REQUEST_TRANSITIONS == {
        RequestStatus.DRAFT: {
            RequestAction.RELEASE: RequestStatus.QUEUED,
            RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
        },
        RequestStatus.QUEUED: {
            RequestAction.MARK_SENT: RequestStatus.SENT,
            RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
        },
        RequestStatus.SENT: {
            RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
        },
        RequestStatus.SUPERSEDED: {},
    }


def test_the_guard_reads_a_second_table() -> None:
    """``can_transition`` was written over any table (the renewal slice's own
    promise); this is the second table behind it."""
    assert can_transition(
        REQUEST_TRANSITIONS, status=RequestStatus.DRAFT, action=RequestAction.RELEASE
    )
    assert not can_transition(
        REQUEST_TRANSITIONS, status=RequestStatus.DRAFT, action=RequestAction.MARK_SENT
    )
    assert not can_transition(
        REQUEST_TRANSITIONS, status=RequestStatus.SENT, action=RequestAction.RELEASE
    )


def test_a_queued_request_is_live_until_it_is_sent() -> None:
    """``draft`` and ``queued`` are the two live states — what the "one live
    request per company and form" index covers — so a regeneration supersedes
    a released-but-undelivered request rather than letting two letters race."""
    assert frozenset({RequestStatus.DRAFT, RequestStatus.QUEUED}) == LIVE_REQUEST_STATUSES


# --- The outbox row -------------------------------------------------------


def test_the_outbox_transition_table_is_exactly_this() -> None:
    assert OUTBOX_TRANSITIONS == {
        OutboxStatus.PENDING: {
            OutboxAction.MARK_SENT: OutboxStatus.SENT,
            OutboxAction.FAIL: OutboxStatus.FAILED,
            OutboxAction.CANCEL: OutboxStatus.CANCELLED,
        },
        OutboxStatus.SENT: {},
        OutboxStatus.FAILED: {},
        OutboxStatus.CANCELLED: {},
    }


def test_only_a_queued_request_is_deliverable() -> None:
    assert is_deliverable(RequestStatus.QUEUED)
    for status in (RequestStatus.DRAFT, RequestStatus.SENT, RequestStatus.SUPERSEDED):
        assert not is_deliverable(status)


def test_deliverability_is_kind_aware() -> None:
    """§5.1.12's two gates kept apart: a reminder row follows the chain it
    reminds, so its request must be ``sent`` — a request row's ``queued`` rule
    unchanged, and a reminder to a superseded (or never-sent) request is
    cancelled, not delivered. An attribution ask (ADR-0019 §1) is the
    request-less kind: it is deliverable while it carries no request at all,
    and a request status appearing on it is a row the send path refuses
    rather than guesses about."""
    assert is_deliverable_kind(OutboxKind.CONFIRMATION_REQUEST, RequestStatus.QUEUED)
    for status in (RequestStatus.DRAFT, RequestStatus.SENT, RequestStatus.SUPERSEDED):
        assert not is_deliverable_kind(OutboxKind.CONFIRMATION_REQUEST, status)
    for kind in (OutboxKind.REMINDER, OutboxKind.RECONFIRMATION):
        assert is_deliverable_kind(kind, RequestStatus.SENT)
        for status in (RequestStatus.DRAFT, RequestStatus.QUEUED, RequestStatus.SUPERSEDED):
            assert not is_deliverable_kind(kind, status)
    assert is_deliverable_kind(OutboxKind.ATTRIBUTION_ASK, None)
    for status in (
        RequestStatus.DRAFT,
        RequestStatus.QUEUED,
        RequestStatus.SENT,
        RequestStatus.SUPERSEDED,
    ):
        assert not is_deliverable_kind(OutboxKind.ATTRIBUTION_ASK, status)


def test_a_failure_maps_to_the_rows_fate() -> None:
    """ADR-0011's rule: transient failures retry, and a provider refusal — the
    one that will fail the same way for ever — ends the row."""
    assert row_status_for(DeliveryFailure.TRANSIENT) is OutboxStatus.PENDING
    assert row_status_for(DeliveryFailure.UNCONFIRMED) is OutboxStatus.PENDING
    assert row_status_for(DeliveryFailure.PERMANENT) is OutboxStatus.FAILED


def test_only_a_transient_failure_is_retried_inside_the_cycle() -> None:
    """An ambiguous outcome is not retried at once: the retry of a message
    that may already have gone out is the duplicate this product must not
    produce. It waits for the next cycle (and, on the Gmail leg, reconciles)."""
    assert retry_in_cycle(DeliveryFailure.TRANSIENT)
    assert not retry_in_cycle(DeliveryFailure.UNCONFIRMED)
    assert not retry_in_cycle(DeliveryFailure.PERMANENT)
