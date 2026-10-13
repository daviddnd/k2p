"""The send outbox's write path: the release, and each row's outcome.

One module writes ``mail_outbox`` and one module delivers it
(``app.application.mail_send``) — the two halves of ADR-0011's outbox pattern,
split so each has one reason to change. This is the writer:

- ``release`` records the **release** of a confirmation request: the request
  moves ``draft → queued`` through ADR-0008's transition table
  (``app.domain.confirmation.REQUEST_TRANSITIONS``), one pending outbox row
  exists for it, and one audit entry says who released it and under which
  send mode. It is **idempotent by construction**: a request that is already
  ``queued`` gets its pending row back (or a fresh one, when the previous
  attempt failed) rather than a second release entry — the DB's partial unique
  index is what makes two overlapping releases impossible anyway.
- ``supersede`` closes the request a regeneration replaced, live or already
  ``sent``: the request moves to ``superseded`` and **all** of its pending rows
  — the request's own send and every reminder row the sweep has queued — are
  cancelled in the same transaction, so neither letter can leave afterwards.
  No audit entry of its own — the generation that superseded it already
  records that act.
- ``enqueue_reminder`` / ``enqueue_reconfirmation`` / ``enqueue_attribution_ask``
  / ``release_reminders`` are the follow-up half of the outbox (§5.1.12; the
  ask is ADR-0019 §1's): the sweep writes a reminder row and the reconfirmer a
  reconfirmation row through the matching enqueue (held on ``HELD_NOT_BEFORE``
  under the tenant's ``review_required`` mode, released with the queue under
  ``automatic``), the attribution ask writes its request-less row pending
  send-on-release (a notice: no mode holds it), and the staff release moves a
  sent request's held rows of **both** kinds to the press instant through
  ``release_reminders`` — one release mechanism, whichever follow-up is held.
  A follow-up row is never the request's release row: ``_pending_job``
  filters ``kind == confirmation_request``, so the widened pending index —
  one row per request **and** kind — cannot make a follow-up answer for the
  request. A released reconfirmation row gets its own
  ``reconfirmation_released`` entry (the enqueue it releases was audited;
  the press is the staff act the chain must record); a released reminder
  still writes none (§5.1.12's rule stands).
- ``mark_sent`` / ``fail`` / ``cancel`` are the row's own outcomes, called by
  the sender once it knows what happened. ``mark_sent`` is also the request's
  ``queued → sent`` move, so the two rows cannot disagree about whether a
  letter went out — and it re-reads the row under its lock first
  (``lock_job``): a row superseded while the provider had the message refuses
  the move rather than being overwritten.

**The request's status moves nowhere else.** The generator calls ``release``
instead of writing ``queued`` itself, a regeneration calls ``supersede``
instead of flipping the column, and the sender calls ``mark_sent``; that is
what makes "no state moves without the guard" true for this lifecycle rather
than aspirational.

Every function here flushes and never commits: the API session dependency (or
the worker's own edge) commits, so a change and its audit entry land together.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.mail_outbox import MailOutbox
from app.application import audit
from app.domain.audit import AuditAction
from app.domain.confirmation import (
    REQUEST_TRANSITIONS,
    RequestAction,
    RequestStatus,
)
from app.domain.mail_send import (
    HELD_NOT_BEFORE,
    OUTBOX_TRANSITIONS,
    OutboxAction,
    OutboxKind,
    OutboxStatus,
)
from app.domain.send_modes import SendDestination
from app.domain.service_renewals import can_transition

# A confirmation request is a **client** letter: it goes to the company's own
# contacts, which is the destination the founder's ruling defaults to
# ``automatic`` and the one that carries no constraint. The government
# destination exists in the configuration vocabulary (pinned to
# ``review_required``) and has no artifact yet — Phase 6 owns export.
CONFIRMATION_DESTINATION = SendDestination.CLIENT


class RequestNotFoundError(Exception):
    """No such confirmation request on that company (cross-tenant → unknown)."""


class RequestNotSendableError(Exception):
    """The request's status does not admit a release; ``reason`` is the wire code.

    Two situations, both answered 409 by the send route: ``already_sent`` (the
    message has left; a second send would be a second email) and
    ``request_superseded`` (a regeneration replaced it, and its letter is not
    the live one any more).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OutboxTransitionError(Exception):
    """The row's status does not admit the action — a caller's bug, not a case.

    ADR-0008's guard is asked before every write; reaching a move the table
    does not carry would mean two writers disagreed about a row, and the
    refusal is what keeps a wrong write from being an untyped surprise.
    """

    def __init__(self, *, status: OutboxStatus, action: OutboxAction) -> None:
        self.status = status
        self.action = action
        super().__init__(f"a {status.value} outbox row does not admit {action.value}")


@dataclass(frozen=True)
class Release:
    """What a release produced: the live request and its pending send."""

    request: ConfirmationRequest
    job: MailOutbox


@dataclass(frozen=True)
class EnqueuedReconfirmation:
    """What ``enqueue_reconfirmation`` produced: the pending row, and whether
    this call created it.

    ``created=False`` is the widened index's re-read — a reconfirmation row
    for this request already pends, so this call's stored letter is **not**
    what the row will deliver (``row.letter_document_id`` still names the
    winner's). The caller's record must name that pairing honestly, so the
    flag travels with the row.
    """

    row: MailOutbox
    created: bool


@dataclass(frozen=True)
class EnqueuedAttributionAsk:
    """What ``enqueue_attribution_ask`` produced: the pending row, and whether
    this call created it (ADR-0019 §1).

    Same fresh-vs-met rule as the reconfirmation enqueue, over the ask's own
    partial index (one pending row per case and kind): ``created=False`` means
    a row for this case already pends and the winner's letter is what the row
    will deliver.
    """

    row: MailOutbox
    created: bool


async def get_request(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, request_id: UUID
) -> ConfirmationRequest:
    """One confirmation request on that company, or ``RequestNotFoundError``.

    Unknown, cross-tenant and wrong-company ids are one answer, deliberately:
    a request id is not a capability, and the route's 404 should not say
    whether some other tenant's request exists.
    """
    row: ConfirmationRequest | None = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.id == request_id,
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id == company_id,
        )
    )
    if row is None:
        raise RequestNotFoundError
    return row


async def release(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    actor_user_id: UUID | None,
    now: datetime | None = None,
) -> Release:
    """Release a request: record the intent to send, and hand back its row.

    Raises ``RequestNotFoundError`` for an unknown or cross-tenant request and
    ``RequestNotSendableError`` for one that is already ``sent`` or
    ``superseded``. A ``draft`` is released exactly once — the transition goes
    through the guard and one ``confirmation_request_released`` entry is
    written; a ``queued`` request simply gets its pending row, which is the
    retry path (a previous attempt failed, and the release already stands, so
    no second entry claims one happened twice).

    ``actor_user_id`` is ``None`` when the generator releases a draft itself
    under the tenant's ``automatic`` mode — the entry then reads
    ``released_by: system`` and the generator's own draft entry carries the
    mode that decided it.
    """
    request = await get_request(
        session, tenant_id=tenant_id, company_id=company_id, request_id=request_id
    )
    status = RequestStatus(request.status)
    if status is RequestStatus.SENT:
        raise RequestNotSendableError("already_sent")
    if status is RequestStatus.SUPERSEDED:
        raise RequestNotSendableError("request_superseded")
    at = now if now is not None else datetime.now(UTC)
    if status is RequestStatus.DRAFT:
        _request_transition(request, action=RequestAction.RELEASE)
        request.status = RequestStatus.QUEUED.value
    job = await _pending_job(session, tenant_id=tenant_id, request_id=request.id)
    if job is None:
        # Either the release above, or a retry after a failed attempt: the
        # request is released and nothing pending is left to deliver. The
        # savepoint keeps a racing insert (two overlapping sends of one
        # request) from poisoning the transaction: the partial unique index
        # lets exactly one through, and the loser re-reads the winner's row —
        # which is what the caller asked for — without a second release entry,
        # because the winner already wrote the one release there is.
        job = MailOutbox(
            tenant_id=tenant_id,
            company_id=request.company_id,
            confirmation_request_id=request.id,
            destination=CONFIRMATION_DESTINATION.value,
            status=OutboxStatus.PENDING.value,
            created_by=actor_user_id,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError:
            existing = await _pending_job(session, tenant_id=tenant_id, request_id=request.id)
            assert existing is not None  # the index refused this insert because of it
            job = existing
    if status is RequestStatus.DRAFT:
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.CONFIRMATION_REQUEST_RELEASED,
            actor_user_id=actor_user_id,
            company_id=request.company_id,
            entity_type="confirmation_request",
            entity_id=request.id,
            details={
                "destination": CONFIRMATION_DESTINATION.value,
                "version_no": request.version_no,
                "outbox_id": str(job.id),
                "released_by": "staff" if actor_user_id is not None else "system",
                "released_at": at,
            },
        )
    await session.flush()
    return Release(request=request, job=job)


async def supersede(
    session: AsyncSession, *, request: ConfirmationRequest, reason: str = "superseded"
) -> None:
    """Close the request a regeneration replaced, and cancel its pending rows.

    Called by the generator **inside its own transaction**, before the new
    version is written and flushed (the partial unique index allows one live
    request per company and form, so the old one has to leave that state
    first). The request may be live (``draft``/``queued``) or already ``sent``
    — a regeneration over a sent request supersedes it the same way, because
    its reminder chain ends with it (contract §5.1.12). One audit entry
    already covers the act — the generation's own, which names the superseded
    request — so this writes none: the row is the record of the
    supersession, and a second entry would say the same thing.

    **Every** pending row of the request is cancelled in the same transaction —
    the request's own send and any reminder row the sweep has queued (the
    index admits one pending row per request and kind, so both can wait at
    once) — which is what keeps a superseded letter, of either kind, from
    leaving afterwards. The sender checks the request's status as well
    (``app.domain.mail_send.is_deliverable_kind``): this is the ordinary
    path, and that check is the defence in depth behind it.
    """
    status = RequestStatus(request.status)
    if status is RequestStatus.SUPERSEDED:
        return  # already closed: nothing to close
    # The transition table is the authority on what may be superseded — a
    # ``sent`` request is admitted too (§5.1.12: its reminder chain ends with
    # it), and anything the table refuses raises below rather than passing.
    _request_transition(request, action=RequestAction.SUPERSEDE)
    request.status = RequestStatus.SUPERSEDED.value
    jobs = await _pending_rows(session, tenant_id=request.tenant_id, request_id=request.id)
    for job in jobs:
        _row_transition(job, action=OutboxAction.CANCEL)
        job.status = OutboxStatus.CANCELLED.value
        job.last_error = reason
    await session.flush()


async def mark_sent(
    session: AsyncSession,
    *,
    job: MailOutbox,
    request: ConfirmationRequest | None,
    communication_id: UUID,
    provider_message_id: str | None,
    message_id: str | None,
    now: datetime | None = None,
) -> None:
    """Record that the provider accepted the message: row, request, and moment.

    Called by the sender in the same transaction as the outbound
    ``Communication`` row it names, so the exchange, the request's status and
    the queue's outcome are one fact. ``provider_message_id`` is the provider's
    own id when it reports one (Gmail does, SMTP does not) and ``message_id``
    is the RFC 5322 ``Message-ID`` the deployment stamped — the anchor the
    reconciliation looks for, and the identity a message keeps on a leg whose
    provider gives it none. ``request`` is ``None`` for a request-less row
    (the attribution ask, ADR-0019 §1): its case is the anchor, there is no
    request status to move, and the row's own outcome is the whole record.

    The provider call this records happened **outside** the database, and the
    row can have left ``pending`` while it was in flight — a regeneration's
    ``supersede`` cancels every pending row of the old request. The row is
    therefore re-read under its lock before the write (``lock_job``'s select;
    the identity map refreshes ``job`` in place): a row no longer ``pending``
    refuses the move with the guard's typed ``OutboxTransitionError`` instead
    of being overwritten, and the sender reports the row ``skipped``.
    """
    at = now if now is not None else datetime.now(UTC)
    current = await lock_job(session, job=job)
    assert current is not None  # the row's own anchor pins its existence
    _row_transition(current, action=OutboxAction.MARK_SENT)
    current.status = OutboxStatus.SENT.value
    current.sent_at = at
    current.attempts = current.attempts + 1
    current.blocked_reason = None
    current.last_error = None
    current.provider_message_id = provider_message_id
    current.message_id = message_id
    current.communication_id = communication_id
    if request is not None and RequestStatus(request.status) is RequestStatus.QUEUED:
        _request_transition(request, action=RequestAction.MARK_SENT)
        request.status = RequestStatus.SENT.value
    await session.flush()


async def fail(
    session: AsyncSession, *, job: MailOutbox, reason: str, blocked_reason: str | None = None
) -> None:
    """End a row the provider refused in a way that will not change.

    The reason is a bounded code (``mail_send_failed:<provider code>``); the
    request stays ``queued``, because the *release* is not what failed — the
    letter still has not gone out, the row records why, and staff retry it
    through the send route.
    """
    _row_transition(job, action=OutboxAction.FAIL)
    job.status = OutboxStatus.FAILED.value
    job.attempts = job.attempts + 1
    job.last_error = reason
    job.blocked_reason = blocked_reason
    await session.flush()


async def note_attempt(
    session: AsyncSession,
    *,
    job: MailOutbox,
    reason: str,
    blocked_reason: str | None = None,
) -> None:
    """Record an attempt that neither sent the message nor ended the row.

    A transient failure, an unconfirmed outcome, or the release gate refusing
    while the row waits: the row stays ``pending`` with the reason beside it,
    the next cycle (or the next press of the send route) tries again, and the
    count of attempts is what a reader uses to see that it kept trying. No
    audit entry — the chain records decisions, not every retry (the ingestion
    cursor's rule) — except the release gate's refusal, which §5.1.9 makes a
    record: the sender writes that one through
    ``app.application.service_renewals.record_release_refusal``.
    """
    job.attempts = job.attempts + 1
    job.last_error = reason
    job.blocked_reason = blocked_reason
    await session.flush()


async def cancel(session: AsyncSession, *, job: MailOutbox, reason: str) -> None:
    """Close a pending row whose request is no longer deliverable.

    The defence behind ``supersede``: a request that is no longer ``queued``
    (a regeneration won the race, or the row outlived its request) must not
    have its letter go out, whatever the row says.
    """
    _row_transition(job, action=OutboxAction.CANCEL)
    job.status = OutboxStatus.CANCELLED.value
    job.last_error = reason
    await session.flush()


async def pending_jobs(
    session: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[MailOutbox]:
    """The rows waiting to be delivered, oldest first — the worker's queue.

    §5.1.12's scheduling leg: a pending row with a ``not_before`` in the future
    is owed but not yet due, and the cycle skips it — ``NULL`` (send-on-release,
    every request row) and a past instant both deliver. ``tenant_id`` narrows
    it to the actor's tenant when the API triggers a cycle; the background
    loop runs for the deployment's one tenant (ADR-0002) and resolves it
    itself.

    The select carries the row lock, ``SKIP LOCKED``: its answer decides which
    rows a cycle will deliver, and a row a concurrent cycle is already
    delivering must not appear in a second cycle's batch — the loser finds it
    gone rather than queueing a second copy of one letter. ``SKIP LOCKED``
    rather than plain ``FOR UPDATE`` because the cycle commits per row: a
    blocking loser would wait out another cycle's provider round-trips only
    to re-read the same "gone" answer, while the skipper can work the rest of
    the queue. This lock is the scheduling guard, not the correctness
    mechanism — it dies with the batch read's first per-row commit, and the
    lock that actually covers a delivery is the delivery-time re-read
    (``lock_job``), with ``record_outbound_message``'s caught
    ``IntegrityError`` and ``mark_sent``'s locked re-check as the two
    backstops behind it.
    """
    at = now if now is not None else datetime.now(UTC)
    query = (
        select(MailOutbox)
        .where(
            MailOutbox.status == OutboxStatus.PENDING.value,
            MailOutbox.not_before.is_(None) | (MailOutbox.not_before <= at),
        )
        .order_by(MailOutbox.created_at, MailOutbox.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if tenant_id is not None:
        query = query.where(MailOutbox.tenant_id == tenant_id)
    if limit is not None:
        query = query.limit(limit)
    return list((await session.scalars(query)).all())


async def lock_job(session: AsyncSession, *, job: MailOutbox) -> MailOutbox | None:
    """Re-read one outbox row under its row lock — the delivery-time lock.

    ``deliver_one`` calls this before anything leaves the deployment. The
    batch read's lock cannot cover a delivery — the cycle commits per row, so
    it dies with the first commit — and the lock that makes "one delivering
    consumer per row" true is this one, taken in the row's own transaction
    and held until the caller commits the outcome. ``populate_existing``
    refreshes a stale batch reference in place: a second consumer racing the
    same row blocks here, then re-reads the winner's transaction and finds
    the row gone from ``pending`` — the honest ``skipped`` answer, before a
    second copy of the letter reaches the provider. ``None`` would mean the
    row no longer exists, which the row's own anchors — the confirmation
    request or the attribution case — make impossible in practice.
    """
    row: MailOutbox | None = await session.scalar(
        select(MailOutbox)
        .where(MailOutbox.id == job.id, MailOutbox.tenant_id == job.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row


async def enqueue_reminder(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    letter_document_id: UUID,
    not_before: datetime,
    created_by: UUID | None = None,
) -> MailOutbox:
    """Write one pending reminder row for a sent request (the sweep's enqueue).

    The row names the reminder letter it will deliver (``letter_document_id`` —
    a reminder never sends the request's own document), and ``not_before`` is
    the cadence instant under the tenant's ``automatic`` mode or
    ``HELD_NOT_BEFORE`` under ``review_required`` (the sweep decides; this
    function stores what it is handed). The pending index admits one reminder
    row per request, and a concurrent second sweep meets it the way a second
    release does: the loser re-reads the winner's row, which is the idempotent
    answer to "the reminder is queued".
    """
    row = MailOutbox(
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=request_id,
        destination=CONFIRMATION_DESTINATION.value,
        status=OutboxStatus.PENDING.value,
        kind=OutboxKind.REMINDER.value,
        letter_document_id=letter_document_id,
        not_before=not_before,
        created_by=created_by,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await _pending_reminder(session, tenant_id=tenant_id, request_id=request_id)
        assert existing is not None  # the index refused this insert because of it
        return existing
    return row


async def enqueue_reconfirmation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    letter_document_id: UUID,
    not_before: datetime | None,
    created_by: UUID | None = None,
) -> EnqueuedReconfirmation:
    """Write one pending reconfirmation row for the request the letter answers
    (the reconfirmer's enqueue).

    The mirror of ``enqueue_reminder``: the row names the stored letter it
    will deliver (``letter_document_id`` — a reconfirmation never sends the
    request's own document), and ``not_before`` is the caller's release
    posture — ``NULL`` under the tenant's ``automatic`` mode (send-on-release)
    or ``HELD_NOT_BEFORE`` under ``review_required``. The pending index admits
    one reconfirmation row per request, so it pends beside the request's own
    row and its reminder without collision.

    The return reports fresh-vs-met: a concurrent second enqueue (a second
    reply parsed while the thread's letter still waits — a wide window under
    ``review_required``) loses the race, re-reads the winner's row, and
    answers ``created=False``: the letter this call stored is the record of
    what its parse asked, but the row will still deliver the winner's letter,
    and the caller's audit must not pair them the other way.
    """
    row = MailOutbox(
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=request_id,
        destination=CONFIRMATION_DESTINATION.value,
        status=OutboxStatus.PENDING.value,
        kind=OutboxKind.RECONFIRMATION.value,
        letter_document_id=letter_document_id,
        not_before=not_before,
        created_by=created_by,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await _pending_reconfirmation(
            session, tenant_id=tenant_id, request_id=request_id
        )
        assert existing is not None  # the index refused this insert because of it
        return EnqueuedReconfirmation(row=existing, created=False)
    return EnqueuedReconfirmation(row=row, created=True)


async def enqueue_attribution_ask(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID | None,
    case_id: UUID,
    letter_document_id: UUID,
    created_by: UUID | None = None,
) -> EnqueuedAttributionAsk:
    """Write one pending attribution-ask row for the case it serves
    (ADR-0019 §1; the case-lifecycle module's enqueue).

    The first **request-less** row shape (migration ``0033``; the company
    column nullable since ``0035`` for the clarification loop's company-less
    asks): no confirmation request behind it — its case is the anchor,
    carried by ``attribution_case_id`` — and its own stored letter in
    ``letter_document_id``. ``not_before`` is always ``NULL``: an ask is a
    notice, released the moment it is written, and deliberately never read
    through the tenant's ``SendMode`` (ruling R4 — the client destination
    holds letters for staff, not this). The pending index admits one ask row
    per case, so two overlapping enqueues answer fresh-vs-met the way the
    reconfirmation enqueue does: the loser re-reads the winner's row.
    """
    row = MailOutbox(
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=None,
        attribution_case_id=case_id,
        destination=CONFIRMATION_DESTINATION.value,
        status=OutboxStatus.PENDING.value,
        kind=OutboxKind.ATTRIBUTION_ASK.value,
        letter_document_id=letter_document_id,
        not_before=None,
        created_by=created_by,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await pending_attribution_ask(
            session, tenant_id=tenant_id, case_id=case_id
        )
        assert existing is not None  # the index refused this insert because of it
        return EnqueuedAttributionAsk(row=existing, created=False)
    return EnqueuedAttributionAsk(row=row, created=True)


async def release_reminders(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    actor_user_id: UUID | None,
    now: datetime | None = None,
) -> tuple[MailOutbox, ...]:
    """Release a sent request's held follow-up rows: the press moves them up.

    Despite the name — the send route's release path since M2, and the one
    mechanism this is — the held set is every pending row of the two
    follow-up kinds: the request's reminder **and** its reconfirmation letter
    (the reconfirmer reuses the reminder release posture; there is no second
    release path). Every pending follow-up row parked on ``HELD_NOT_BEFORE``
    gets ``not_before`` = the press instant and is returned, so the caller can
    deliver what it released. The select takes the row lock: two interleaved
    presses of one request's send route must not both read the row as held
    and both deliver — the loser blocks, re-reads the winner's transaction,
    and finds the row released or sent, which is what makes the second press
    answer ``409 already_sent`` rather than send a second copy (ADR-0011: a
    duplicated client letter is a trust incident). Idempotent — a second press
    with nothing contending finds nothing held and returns the empty tuple,
    the route's existing "nothing held" leg.

    The audit rule is per kind: a released reconfirmation row writes one
    ``reconfirmation_released`` entry (coordinates only — the request, the
    row, who pressed, when); a released reminder still writes none, the
    §5.1.12 rule that the enqueue it releases was already audited.
    """
    at = now if now is not None else datetime.now(UTC)
    held = await _pending_followups(session, tenant_id=tenant_id, request_id=request_id)
    released: list[MailOutbox] = []
    for row in held:
        if row.not_before != HELD_NOT_BEFORE:
            continue
        row.not_before = at
        released.append(row)
        if OutboxKind(row.kind) is OutboxKind.RECONFIRMATION:
            await audit.record_event(
                session,
                tenant_id=tenant_id,
                action=AuditAction.RECONFIRMATION_RELEASED,
                actor_user_id=actor_user_id,
                company_id=company_id,
                entity_type="confirmation_request",
                entity_id=request_id,
                details={
                    "request_id": str(request_id),
                    "outbox_id": str(row.id),
                    "released_by": "staff" if actor_user_id is not None else "system",
                    "released_at": at,
                },
            )
    if released:
        await session.flush()
    return tuple(released)


async def _pending_job(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> MailOutbox | None:
    """A request's pending **release** row, if it has one — never a reminder.

    The release row is the one with ``kind == confirmation_request``; a
    reminder row (``kind == reminder``) must never be returned as the answer
    to "what delivers this request?", which is what the widened pending index
    — one row per request and kind — made possible to get wrong.
    """
    job: MailOutbox | None = await session.scalar(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == OutboxKind.CONFIRMATION_REQUEST.value,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
    )
    return job


async def _pending_reminder(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> MailOutbox | None:
    """A request's pending reminder row, if one exists (at most one, by the index).

    The select carries the row lock — the quarantine retry path's
    ``with_for_update`` precedent — because both callers use the answer to
    decide a write: ``release_reminders`` moves ``not_before`` when the row is
    held, and an unlocked read lets two interleaved presses both see "held"
    and both deliver. The loser blocks here, then re-reads the winner's
    transaction and sees the row released (or sent), which is the honest
    "nothing to release" answer.
    """
    row: MailOutbox | None = await session.scalar(
        select(MailOutbox)
        .where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == OutboxKind.REMINDER.value,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
        .with_for_update()
    )
    return row


async def _pending_reconfirmation(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> MailOutbox | None:
    """A request's pending reconfirmation row, if one exists (at most one, by
    the index) — the IntegrityError re-read inside ``enqueue_reconfirmation``.
    """
    row: MailOutbox | None = await session.scalar(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == OutboxKind.RECONFIRMATION.value,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
    )
    return row


async def pending_attribution_ask(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID
) -> MailOutbox | None:
    """A case's pending ask row, if one exists (at most one, by the partial
    index) — the IntegrityError re-read inside ``enqueue_attribution_ask``,
    and the row the case's own lifecycle cancels when the case settles.

    The ask's key is its case, not a request: the read filters on
    ``attribution_case_id`` because that is the column the partial index is
    over, and a request-less row can hold no request to mistake it for.
    """
    row: MailOutbox | None = await session.scalar(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.attribution_case_id == case_id,
            MailOutbox.kind == OutboxKind.ATTRIBUTION_ASK.value,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
    )
    return row


async def _pending_followups(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> list[MailOutbox]:
    """The request's pending follow-up rows, both kinds — ``release_reminders``'
    held set. The select carries the row lock for the presses' interleaving
    rule (the ``_pending_reminder`` docstring's case, over both rows now).
    """
    rows = await session.scalars(
        select(MailOutbox)
        .where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind.in_(
                [OutboxKind.REMINDER.value, OutboxKind.RECONFIRMATION.value]
            ),
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
        .with_for_update()
    )
    return list(rows.all())


async def _pending_rows(
    session: AsyncSession, *, tenant_id: UUID, request_id: UUID
) -> list[MailOutbox]:
    """Every pending row of the request, both kinds — ``supersede``'s set."""
    rows = await session.scalars(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
    )
    return list(rows.all())


def _request_transition(request: ConfirmationRequest, *, action: RequestAction) -> None:
    """Ask ADR-0008's guard over the request's table before moving it."""
    status = RequestStatus(request.status)
    if not can_transition(REQUEST_TRANSITIONS, status=status, action=action):
        raise RequestNotSendableError(
            "already_sent" if status is RequestStatus.SENT else "request_superseded"
        )


def _row_transition(job: MailOutbox, *, action: OutboxAction) -> None:
    """Ask the same guard over the outbox row's table before moving it."""
    status = OutboxStatus(job.status)
    if not can_transition(OUTBOX_TRANSITIONS, status=status, action=action):
        raise OutboxTransitionError(status=status, action=action)


__all__ = [
    "CONFIRMATION_DESTINATION",
    "EnqueuedAttributionAsk",
    "EnqueuedReconfirmation",
    "OutboxTransitionError",
    "Release",
    "RequestNotFoundError",
    "RequestNotSendableError",
    "cancel",
    "enqueue_attribution_ask",
    "enqueue_reconfirmation",
    "enqueue_reminder",
    "fail",
    "get_request",
    "lock_job",
    "mark_sent",
    "note_attempt",
    "pending_attribution_ask",
    "pending_jobs",
    "release",
    "release_reminders",
    "supersede",
]
