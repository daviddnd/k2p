"""The §2.5 quarantine review surface's service half (scope §2.5; data-model
§2.9; contract §5.1.7).

A message the ingestion cycle cannot attribute to exactly one company is
recorded in ``quarantined_messages``
(``app.adapters.persistence.quarantine``) and waits for staff. This module is
what they work from:

- ``list_quarantined`` is the read behind the review list: the tenant's rows,
  newest receipt first, metadata only, filtered to one status on request. The
  table holds no subject and no body — the message itself stays in the mailbox
  (ADR-0005) — and nothing here adds one. The filter is here rather than in
  the route because it is the same query either way; what the *default* should
  be is the route's call (see ``app.api.quarantine``), and this read answers
  for whatever status it is given, ``None`` meaning every row.
- ``retry`` is the action that works the message: re-examine **that one
  provider message id** through the ingestion cycle's own per-message order
  (``app.application.mail_ingest.examine_message``), never a copy of it. The
  message is still in the mailbox, so a retry re-reads it from the provider and
  re-runs the same deterministic decision — it takes no company id, and it
  never guesses one: the remedy for a message the rule cannot attribute is to
  change what the rule reads (register the sender's contact channel, record the
  company name the message uses), not to let a human assert an attribution the
  record cannot show.
- ``dismiss`` is the action that closes a message staff have decided needs no
  further action. A retry needs the message to still be in the mailbox, and the
  classic case for a dismissal is exactly that it is not: the client deleted
  it, the provider answers a not-found refusal, and the row would stay
  ``pending`` in the queue for good. A dismissal takes no reason and reads no
  mail — it is a statement about the row, and the row keeps the reason it was
  quarantined with.

Why a retry exists at all: the cycle's cursor only moves forward, so a message
it refused is never re-examined by a later cycle's window (the limit is stated
in contract §5.1.7) — while the staff remedy for an unattributable message
happens *after* it was refused. A retry is what makes that remedy reach the
message.

What each outcome does to the row:

- **attributed and ingested** → the row becomes ``resolved``: ``resolved_at``,
  and ``resolved_communication_id`` pointing at the ``Communication`` the
  message became, with one audit entry (``mail_quarantine_resolved``);
- **already recorded** (a cycle attributed the message after the row was
  written) → the same resolution, pointing at that ``Communication``: the
  row's premise — this message has not been attributed — no longer holds, and
  ``pending`` for a message that is in the flow is not a state staff can act
  on;
- **still unattributable** → the row stays ``pending`` and the attempt reports
  the reason the decision reaches **now**, which is not always the row's stored
  one: ``reason`` is the historical fact of why the message was quarantined,
  and the two differ when staff changed what the decision reads;
- **a case opened instead** (ADR-0019 §1's threaded asks, or the clarification
  loop's unthreaded classes: the message now sits on a thread bound to exactly
  one company and its sender is not that company's registered channel, or it
  is one of the two deterministic-but-unattributed shapes an ask replaces the
  quarantine for) → the row stays ``pending``, and the attempt reports one of
  the ``awaiting_*`` outcomes with the ``attribution_case_id`` it opened: the
  message is no longer an unattributed one — it is parked on the
  registration/confirmation or clarification exchange, whose surface
  (``app.application.attribution``) is where it is worked, and the row's own
  way out is the dismissal below (or a later retry, once the confirmed
  processing has recorded the message);
- **attributed, but the §5.1 gate denies the company** → the row stays ``pending``
  and the attempt reports the gate's own reason (``service_not_included`` and
  its siblings) while the row keeps its stored reason (``unregistered_sender``
  or ``multiple_companies``): the message was attributed, but the gate refused
  to act on it, and that refusal writes no assessment, no audit entry and no
  quarantine row — there is nothing to hang them on;
- **dismissed** (one audit entry, ``mail_quarantine_dismissed``) → the row
  closes with ``dismissed_at`` and nothing else. Only a ``pending`` row can be
  dismissed, and a dismissed row is closed to the retry too: reopening one
  would be a second decision about the same row, with the first one's audit
  entry still standing.

Both actions read the row **under the same lock** (``FOR UPDATE``) and refuse
the same two terminal states with the same typed errors — ``retry`` and
``dismiss`` must answer for a closed row identically, or the two would
disagree about a row either of them could have closed.

Nothing here carries mail content. The row has no subject and no body, the
report is the cycle's own ids-and-outcomes shape, and no audit entry written
here holds a fragment of the message. The resolver's §2.7 assessment rides the
per-message entries the ingestion path already writes, so a retry adds no
second one — and a retry that cannot attribute writes none at all, because the
refusal it reaches is the same record the first refusal left (the ingestion
cycle's one-entry-per-message rule).

Services flush but never commit — the API session dependency commits at the
request edge.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.communications import Communication
from app.adapters.persistence.quarantine import QuarantinedMessage
from app.application import audit, mail, mail_ingest
from app.application.documents import DocumentStore
from app.application.mail_ingest import MessageOutcome
from app.application.scanning import Scanner
from app.domain.audit import AuditAction
from app.domain.communications import IngestOutcome
from app.domain.quarantine import QuarantineStatus


class QuarantineNotFoundError(Exception):
    """No quarantine row with that id in the actor's tenant."""


class QuarantineAlreadyResolvedError(Exception):
    """The row is already resolved; there is nothing left to do with it."""


class QuarantineAlreadyDismissedError(Exception):
    """The row was already dismissed; staff decided, and the decision stands."""


@dataclass(frozen=True)
class RetryResult:
    """One retry's account of itself: the row as it now stands, and the outcome."""

    row: QuarantinedMessage
    outcome: MessageOutcome


async def list_quarantined(
    session: AsyncSession, *, tenant_id: UUID, status: QuarantineStatus | None = None
) -> list[QuarantinedMessage]:
    """The tenant's quarantine rows, newest receipt first, metadata only.

    ``received_at`` is the provider's own receipt time — the value the row was
    written with and the ``Communication`` ordering uses — so "newest first"
    means the same thing here as it does for a company's communications.

    ``status`` filters to one of the row's own statuses; ``None`` is every row.
    The statuses are the caller's to choose and the vocabulary is closed
    (``app.domain.quarantine.QuarantineStatus``), so a value that is not a
    status cannot reach this far — the API's ``all`` is this function's
    ``None``, which is why it is expressed here as the absence of a filter
    rather than as a fourth value.
    """
    statement = (
        select(QuarantinedMessage)
        .where(QuarantinedMessage.tenant_id == tenant_id)
        .order_by(QuarantinedMessage.received_at.desc(), QuarantinedMessage.id)
    )
    if status is not None:
        statement = statement.where(QuarantinedMessage.status == status.value)
    return list((await session.scalars(statement)).all())


async def retry(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    quarantine_id: UUID,
    actor_user_id: UUID | None = None,
) -> RetryResult:
    """Re-examine one quarantined message and move its row if it lands.

    The row is read tenant-scoped, so another tenant's id is simply not found
    (a deployment must not be able to tell the two apart, contract §5.1.7).
    A closed row is refused before the mailbox is touched — resolved or
    dismissed, each by its own error (``_refuse_if_closed``): there is nothing
    left to examine, and re-examining would be a provider call for an answer
    already recorded or a decision already taken.

    The read **takes the row lock** (``FOR UPDATE``), and that is the whole of
    the concurrency story. A retry spends a metadata read, a possible raw
    fetch, a scan and an object-store write between reading the row and moving
    it, so two overlapping requests — a double-click, a browser retry, two
    administrators — would both read ``pending`` and both close the row: the
    loser would find the winner's ``Communication`` and append a second
    ``mail_quarantine_resolved`` entry to an append-only chain, a duplicated
    fact in the record. Serialized by the lock, the second request reads
    ``resolved`` and is refused exactly as it would be a moment later.

    Raises the mailbox's own typed errors unchanged (``mail.MailNotConnected``,
    ``MailRefreshFailed``, and the provider's) — an unreachable mailbox is not
    a retry that found nothing.
    """
    row = await session.scalar(
        select(QuarantinedMessage)
        .where(
            QuarantinedMessage.id == quarantine_id,
            QuarantinedMessage.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if row is None:
        raise QuarantineNotFoundError
    _refuse_if_closed(row)
    at = datetime.now(UTC)
    mailbox = await mail.reader(now=at)
    examined = await mail_ingest.examine_message(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        mailbox=mailbox,
        message_id=row.mailbox_message_id,
    )
    outcome = examined.outcome
    communication_id = outcome.communication_id
    company_id = outcome.company_id
    if communication_id is None and outcome.outcome is IngestOutcome.SKIPPED_KNOWN:
        recorded = await _recorded_communication(
            session, tenant_id=tenant_id, message_id=row.mailbox_message_id
        )
        if recorded is not None:
            communication_id, company_id = recorded
    if communication_id is not None:
        await _resolve(
            session,
            row=row,
            communication_id=communication_id,
            company_id=company_id,
            actor_user_id=actor_user_id,
            at=at,
        )
    return RetryResult(row=row, outcome=outcome)


async def dismiss(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    quarantine_id: UUID,
    actor_user_id: UUID | None = None,
) -> QuarantinedMessage:
    """Close one pending row that staff have decided needs no further action.

    The case this exists for: the client deleted the mail, so the retry meets
    the provider's not-found refusal for ever and the row would wait in a queue
    that never empties. A dismissal is the staff decision that the queue entry
    is not a to-do, and it is deliberately not available for any other row — a
    resolved message has a record pointing at it and a dismissed one does not,
    so neither state may be entered from the other.

    Reads no mail and takes no reason: nothing is fetched, scanned, or stored,
    and the row keeps the reason it was quarantined with (that is history, and
    the dismissal does not change why the message needed staff). The read takes
    the row lock, the same one the retry takes and for the same reason: two
    overlapping dismissals must not both close the row, and neither may race a
    retry that is attributing the message.

    Raises ``QuarantineNotFoundError`` for an unknown or another tenant's id
    (one answer for both, contract §5.1.7) and ``QuarantineAlreadyResolvedError``
    / ``QuarantineAlreadyDismissedError`` for a row that is already closed.
    """
    at = datetime.now(UTC)
    row = await session.scalar(
        select(QuarantinedMessage)
        .where(
            QuarantinedMessage.id == quarantine_id,
            QuarantinedMessage.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if row is None:
        raise QuarantineNotFoundError
    _refuse_if_closed(row)
    row.status = QuarantineStatus.DISMISSED.value
    row.dismissed_at = at
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.MAIL_QUARANTINE_DISMISSED,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="quarantined_message",
        entity_id=row.id,
        details={
            "mailbox_message_id": row.mailbox_message_id,
            "reason": row.reason,
        },
    )
    await session.flush()
    return row


def _refuse_if_closed(row: QuarantinedMessage) -> None:
    """Refuse a row that is already closed, naming which way it is closed.

    The two terminal states are different answers because they are different
    situations: a resolved row has a ``Communication`` behind it, a dismissed
    one is a decision not to look further. One function rather than two
    checks, because the retry and the dismiss must agree exactly — a caller
    that could dismiss a row the other refuses would be a hole, not a
    difference.
    """
    if row.status == QuarantineStatus.RESOLVED.value:
        raise QuarantineAlreadyResolvedError
    if row.status == QuarantineStatus.DISMISSED.value:
        raise QuarantineAlreadyDismissedError


async def _resolve(
    session: AsyncSession,
    *,
    row: QuarantinedMessage,
    communication_id: UUID,
    company_id: UUID | None,
    actor_user_id: UUID | None,
    at: datetime,
) -> None:
    """Point a pending row at the record the message became, and audit it.

    ``resolved_at`` is our clock — when the row stopped waiting for staff — not
    the provider's receipt time (``received_at``, which never changes).
    """
    row.status = QuarantineStatus.RESOLVED.value
    row.resolved_at = at
    row.resolved_communication_id = communication_id
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.MAIL_QUARANTINE_RESOLVED,
        actor_user_id=actor_user_id,
        company_id=company_id,
        entity_type="quarantined_message",
        entity_id=row.id,
        details={
            "mailbox_message_id": row.mailbox_message_id,
            "reason": row.reason,
            "communication_id": str(communication_id),
        },
    )
    await session.flush()


async def _recorded_communication(
    session: AsyncSession, *, tenant_id: UUID, message_id: str
) -> tuple[UUID, UUID | None] | None:
    """The ``Communication`` this provider message id already became, and its company.

    The same idempotency key the ingestion order's first step reads: a retry
    that finds it has nothing to attribute, because the message is already in
    the flow. The company comes back with the id because the resolution the
    caller writes has to stay attributable: a ``skipped_known`` outcome carries
    no company of its own, and an audit entry without one is missing from the
    per-company compliance report (scope §8), which filters on it. The company
    is nullable on the row since migration 0035 (a delivered company-less ask's
    outbound exchange); no retried quarantined message resolves to one — the
    recorded match of an unattributed message's id is always company-bound —
    but the read stays honest about the column it selects.
    """
    found = (
        (
            await session.execute(
                select(Communication.id, Communication.company_id).where(
                    Communication.tenant_id == tenant_id,
                    Communication.mailbox_message_id == message_id,
                )
            )
        )
        .tuples()
        .one_or_none()
    )
    if found is None:
        return None
    return found[0], found[1]
