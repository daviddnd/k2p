"""Delivering the outbox: the release gate, the provider, and the record.

The one module that delivers a send intent (``app.application.outbox`` is the
one that writes it). One cycle picks up pending rows, oldest first, and each
row is delivered in this order:

1. **The row, re-read under its row lock.** The batch read's lock cannot
   cover a delivery — the cycle commits per row, so it dies with the first
   commit — so ``deliver_one`` re-reads the row under its own lock before
   anything leaves the deployment, and the lock is held until the caller
   commits the outcome. A concurrent consumer racing the same row blocks
   here and then re-reads the winner's transaction: gone from ``pending``,
   the row answers ``skipped`` rather than shipping a second copy. A stale
   reference from an earlier batch read is refreshed in place, so the check
   decides on the locked row, not on memory.
2. **Is the request still deliverable?** Deliverability is kind-aware
   (§5.1.12): a request row delivers only while its request is ``queued`` (a
   regeneration superseded it), and a reminder or reconfirmation row delivers
   only while its request is ``sent`` — a superseded, draft or still-queued
   request has ended the chain the follow-up rides. Whatever the row says,
   the request having left the deliverable state closes the row ``cancelled``
   with the ``request_superseded`` reason. An ``attribution_ask`` row
   (ADR-0019 §1) is the request-less kind: its case is the anchor, there is
   no request state to consult, and the row delivers as written.
3. **The release gate** — ``app.application.service_renewals``'s predicate, the
   founder's ruling 3 and the third caller ADR-0017 names, consulted **before
   the message leaves** and with the request's work and period resolved exactly
   as the download route resolves them — **for request rows only** (§5.1.12's
   "two gates, kept apart": a reminder needs no release, so its row never
   runs this predicate, and an attribution ask — a notice with no request
   behind it — never does either). A refusal records the gate's own reason
   on the row (so the next cycle still tries) and writes **one**
   ``document_release_refused`` entry per distinct reason — the same discipline
   §5.1.9 records for the download, minus a second entry for a retry that met
   the same refusal, which would fill an append-only chain with one fact.
4. **The stored message** — the row's own letter, resolved once: a reminder,
   reconfirmation or attribution-ask row names the stored ``.eml`` it will
   deliver (``letter_document_id``); a request row's letter is the request's
   stored draft, as since migration 0023. The same id feeds the bytes read
   (through the records registry, so the scan rule applies: only clean content
   is processed), the outbound ``Communication`` row and the audit entry's
   details. The letter is parsed for the RFC 5322 ``Message-ID``.
5. **The provider** — one rule, per the controller's ruling: **a connected Gmail
   credential means the Gmail API; otherwise SMTP settings; otherwise nothing
   sends** and the row waits (a deployment that has not configured sending yet
   is not an error — it is an unconfigured deployment, and the same
   ``mail_not_configured`` refusal the API answers for it).
6. **The reconcile, then the send** (Gmail): before repeating an attempt the
   sender asks the provider whether Sent already holds this ``Message-ID``. The
   check is unconditional rather than "after an ambiguous outcome" because the
   row cannot always tell — a crash between the provider's accept and the
   row's record leaves no mark at all — and because it is exactly the question
   that must be answered before a client's letter can be duplicated. A message
   found there is **recorded as sent** rather than sent again (ADR-0011: the
   provider message id is recorded and reconciled on retry, so a crash cannot
   double-send). SMTP has no such lookup; its residual — an ambiguous outcome
   can duplicate, and the ``Message-ID`` is what makes the duplicate detectable
   in the mailbox — is stated in the contract.
7. **The record** — the outbound ``Communication`` row (the exchange that
   actually happened, linked back to the request, ``kind`` naming the letter's
   role: ``confirmation_request`` for the request's own row, ``reminder`` for
   a reminder's, ``reconfirmation`` for the reconfirmer's, ``attribution_ask``
   for the request-less ask's — §2.9's vocabulary, so the reconfirmation lands
   in the request's thread as its own kind of exchange), the request's
   ``queued → sent`` move when the row being sent is
   the request's own release (a follow-up's delivery moves nothing — the
   request is already ``sent`` — and a request-less ask has no request to
   move), the row's ``sent`` outcome and one
   ``mail_message_sent`` entry carrying the outbox row id, the request id
   (``None`` for a request-less row), the provider message id, the recipient
   addresses and the outcome. Or, for a failure, ``mail_send_failed`` and the
   row's fate.

**At-least-once, and honest about it.** ADR-0011's pattern is at-least-once with
an idempotency key, not exactly-once: the reconcile narrows the duplicate window
on the Gmail leg to nothing, and the SMTP leg's residual is stated rather than
hidden. The 2026-09-25 follow-up — two overlapping cycles reading one pending
row and both delivering it — is closed by the locked queue read
(``outbox.pending_jobs``), the delivery-time row lock (``outbox.lock_job``), the
caught ``IntegrityError`` in ``record_outbound_message`` that answers a raced
duplicate with the winner's row, and ``mark_sent``'s locked re-check refusing a
row superseded mid-delivery (the sender reports it ``skipped``): the loser of
the race never reaches the provider, and where the race is already decided —
the SMTP leg's one identity, recorded twice — the record stays single. What
remains residual is what no lock can prevent: a crash or an ambiguous outcome
while the provider has the message, which is the reconcile's case.

**What an entry never carries**: a subject, a body, an attachment name, a
recipient's message, an access token, the app password, or the mail server's
own refusal text. The audit chain records ids, addresses, codes and outcomes —
never the letter, and never a credential.

**The cycle commits after each row** — deliberately, and unlike the ingestion
cycle's one transaction: each row is an effect *outside* the deployment, so its
record must survive the next row's failure, and a refusal's entry must be
committed rather than rolled back silently (§5.1.9's own discipline). The
session's caller owns nothing else; ``deliver_one`` flushes and never commits.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.errors import (
    MailDeliveryFailed,
    MailProviderRefused,
    MailProviderUnavailable,
)
from app.adapters.mail.smtp_client import SmtpClient
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant
from app.application import (
    audit,
    communications,
    documents,
    mail,
    outbox,
    service_renewals,
)
from app.application.documents import DocumentNotAvailableError, DocumentStore
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.communications import CommunicationKind
from app.domain.confirmation import RequestStatus
from app.domain.mail import MailboxCredential, MailResponseInvalid
from app.domain.mail_send import (
    DeliveryFailure,
    OutboxKind,
    OutboxStatus,
    is_deliverable_kind,
    retry_in_cycle,
    row_status_for,
)

# How many times one row may be attempted inside a single cycle. The first
# attempt plus two retries: a transient provider hiccup is what this is for,
# and anything longer belongs to the next cycle (where the loop's own backoff,
# imported from the command worker, paces the cycles).
ATTEMPTS_PER_CYCLE = 3

# The pause before an in-cycle retry: one second, doubling per attempt, capped.
# Deliberately **not** the command worker's ``backoff_delay`` — that one is
# measured in cycle intervals and paces *cycles*, while this is a bounded pause
# inside one cycle, and the cap is what keeps a synchronous send route from
# holding a request open for a configured interval that may be a minute or
# more. A transient failure that outlasts these attempts is not lost: the row
# stays pending with its reason, and the next cycle tries again.
RETRY_PAUSE_SECONDS = 1.0
MAX_RETRY_PAUSE_SECONDS = 5.0

# The reason codes an outcome carries. Bounded, no provider text, and the same
# names the contract's error reference lists.
REASON_NOT_CONFIGURED = "mail_not_configured"
REASON_UNCONFIRMED = "mail_send_unconfirmed"
REASON_REQUEST_SUPERSEDED = "request_superseded"
# The two reasons the *provider's* failures answer with, and deliberately the
# strings the ingestion trigger already uses for them: `mail_read_refused:<code>`
# is a read the provider refused with a usable token, and `mail_unavailable` is a
# provider that could not be reached. One vocabulary for one situation, wherever
# the deployment was when it happened.
REASON_READ_REFUSED = "mail_read_refused"
REASON_UNAVAILABLE = "mail_unavailable"
REASON_INVALID_RESPONSE = "invalid_response"


class SendDisposition(StrEnum):
    """What happened to one row in one cycle (the report's vocabulary).

    ``sent`` the provider accepted it (or Sent already held it and it was
    recorded instead of repeated); ``refused`` the release gate refused and the
    row waits; ``failed`` the provider refused it permanently and the row ends;
    ``waiting`` nothing was decided — no provider configured, an unconfirmed
    outcome, or a transient failure whose retries ran out; ``cancelled`` the
    request is no longer deliverable; ``skipped`` the row was not pending any
    more (another delivery had it).
    """

    SENT = "sent"
    REFUSED = "refused"
    FAILED = "failed"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SendOutcome:
    """What one row's delivery decided — ids, a code, and the outcome.

    ``reason`` is the bounded code for anything that did not send (the gate's
    own detail string for a refusal, ``mail_send_failed:<code>`` for a provider
    refusal, ``mail_not_configured`` / ``mail_send_unconfirmed`` /
    ``mail_send_deferred:<code>`` for the ways a row waits), and
    ``provider_message_id`` is set only when there is one.
    ``confirmation_request_id`` is the request the row's letter belongs to —
    NULL for a request-less row (the attribution ask, migration ``0033``),
    whose anchor is its case. ``company_id`` is NULL for the same row when
    its case is company-less (migration ``0034``, the clarification loop).
    """

    outbox_id: UUID
    confirmation_request_id: UUID | None
    company_id: UUID | None
    disposition: SendDisposition
    reason: str | None = None
    provider_message_id: str | None = None
    message_id: str | None = None
    reconciled: bool = False


@dataclass(frozen=True)
class SendCycleReport:
    """One cycle's account of itself: what it looked at, and what it did."""

    examined: int
    sent: int
    outcomes: tuple[SendOutcome, ...] = ()

    def count(self, disposition: SendDisposition) -> int:
        return sum(1 for outcome in self.outcomes if outcome.disposition is disposition)


async def run_cycle(
    session: AsyncSession,
    store: DocumentStore,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
    limit: int | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> SendCycleReport:
    """Deliver every pending send of one tenant, oldest first, and report.

    The caller names the tenant: the route passes the actor's, and the worker
    resolves the deployment's single one (ADR-0002). Each row is committed on
    its own — see the module docstring — so an outage in the middle of a queue
    leaves the rows before it recorded rather than rolled back.
    """
    at = now if now is not None else datetime.now(UTC)
    jobs = await outbox.pending_jobs(
        session,
        tenant_id=tenant_id,
        limit=limit if limit is not None else settings.mail_send_max_messages,
    )
    outcomes: list[SendOutcome] = []
    for job in jobs:
        outcome = await deliver_one(
            session, store, job=job, now=at, sleeper=sleeper
        )
        outcomes.append(outcome)
        await session.commit()
    return SendCycleReport(
        examined=len(outcomes),
        sent=sum(1 for outcome in outcomes if outcome.disposition is SendDisposition.SENT),
        outcomes=tuple(outcomes),
    )


# What each outbox kind's accepted delivery records the exchange as: the
# Communication kind names the letter's role in the request's thread (§2.9's
# vocabulary), one row per outbox kind, so a reconfirmation never counts as a
# second request, an attribution ask records the notice it is, and the thread
# reads honestly.
_COMMUNICATION_KIND_BY_OUTBOX_KIND = {
    OutboxKind.CONFIRMATION_REQUEST: CommunicationKind.CONFIRMATION_REQUEST,
    OutboxKind.REMINDER: CommunicationKind.REMINDER,
    OutboxKind.RECONFIRMATION: CommunicationKind.RECONFIRMATION,
    OutboxKind.ATTRIBUTION_ASK: CommunicationKind.ATTRIBUTION_ASK,
}


async def deliver_one(
    session: AsyncSession,
    store: DocumentStore,
    *,
    job: MailOutbox,
    now: datetime | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> SendOutcome:
    """Deliver one outbox row (the whole per-row order, above).

    Never raises for a refusal, a delivery failure **or a provider failure
    around the credential or the reconciliation**: those are outcomes a report
    carries and the row records, because a bare 500 would tell an operator
    nothing and, in a cycle, would abort the rows behind this one. It flushes
    and never commits, so the caller decides when the outcome is durable.
    """
    at = now if now is not None else datetime.now(UTC)
    # The delivery-time lock (``lock_job``'s docstring is the rule): the batch
    # read's lock dies with the cycle's first per-row commit, so the row is
    # re-read under its own lock here, before anything leaves the deployment,
    # and the lock is held until the caller commits the outcome. A second
    # consumer racing the same row blocks here and then re-reads the winner's
    # transaction — gone from ``pending``, the row answers ``skipped`` rather
    # than shipping a second copy of one letter.
    locked = await outbox.lock_job(session, job=job)
    assert locked is not None  # the row's own anchor pins its existence
    job = locked
    if OutboxStatus(job.status) is not OutboxStatus.PENDING:
        return _outcome(job, disposition=SendDisposition.SKIPPED, reason="not_pending")
    # The row's company is its own either way — company-bound for request and
    # ① ask rows, NULL for the clarification loop's company-less asks
    # (migration 0035) — and every read below (the stored letter's document
    # read, the exchange record, the audit entry) accepts both shapes.
    # The request-less kind (ADR-0019 §1's attribution ask) has no request to
    # resolve: its case is the anchor, and the row delivers as written. Every
    # other kind reads its request, and a missing one — impossible behind the
    # foreign key — is answered as a cancellation rather than a crash, so a
    # hand-edited database cannot stop the queue.
    kind = OutboxKind(job.kind)
    request = None if kind is OutboxKind.ATTRIBUTION_ASK else await _request_of(session, job=job)
    request_status = None if request is None else RequestStatus(request.status)
    if not is_deliverable_kind(kind, request_status):
        await outbox.cancel(session, job=job, reason=REASON_REQUEST_SUPERSEDED)
        return _outcome(
            job, disposition=SendDisposition.CANCELLED, reason=REASON_REQUEST_SUPERSEDED
        )
    # One resolution point for the letter this row delivers: a reminder,
    # reconfirmation or ask row names its own stored letter
    # (``letter_document_id``); a request row's letter is the request's stored
    # draft, as since migration 0023. The same id feeds the bytes read, the
    # outbound ``Communication``'s ``raw_document_id`` and the send entry's
    # details. The company is the row's own either way.
    letter_document_id: UUID | None
    if request is not None:
        letter_document_id = (
            job.letter_document_id
            if job.letter_document_id is not None
            else request.document_id
        )
    else:
        letter_document_id = job.letter_document_id
    if letter_document_id is None:
        # A request-less row naming no letter is a row with nothing to
        # deliver; it waits, the way a request row's missing document does.
        await outbox.note_attempt(session, job=job, reason="document_missing")
        return _outcome(job, disposition=SendDisposition.WAITING, reason="document_missing")
    try:
        # The document row first (one cheap read — the gate needs it and its
        # bytes are fetched only once the gate has let the message through).
        document = await documents.get_document(
            session,
            tenant_id=job.tenant_id,
            company_id=job.company_id,
            document_id=letter_document_id,
        )
    except documents.DocumentNotFoundError:
        await outbox.note_attempt(session, job=job, reason="document_missing")
        return _outcome(job, disposition=SendDisposition.WAITING, reason="document_missing")
    if kind is OutboxKind.CONFIRMATION_REQUEST:
        # §5.1.12's "two gates, kept apart": the release gate is consulted
        # for the request's own row only. A reminder needs no release — the
        # founder's ruling a reminder about a filing or a renewal names no
        # gate — so its row never runs this predicate; neither does the
        # request-less ask, whose own gate is the enqueue's.
        assert request is not None  # only a request row reaches this branch
        refusal = await _release_refusal(
            session, job=job, request=request, document=document, as_of=at.date()
        )
        if refusal is not None:
            return refusal
    try:
        documents.ensure_usable(document)
        raw = await store.get_bytes(document.storage_key)
    except DocumentNotAvailableError as exc:
        reason = f"document_not_available:{exc.scan_status}"
        await outbox.note_attempt(session, job=job, reason=reason)
        return _outcome(job, disposition=SendDisposition.WAITING, reason=reason)
    message_id = _message_id_of(raw)
    try:
        provider = await _provider(now=at)
    except mail.MailRefreshFailed as exc:
        # The connected mailbox's grant is dead (revoked, expired, or another
        # client's): the ingestion trigger's own typed refusal, and deliberately
        # **not** a silent fallback to the SMTP leg — an operator who made the
        # connection has to see that it broke, and the row waits rather than
        # ends, because the letter is still owed.
        return await _waiting(
            session, job=job, reason=f"mail_refresh_failed:{exc.reason}"
        )
    except MailProviderUnavailable:
        # Google could not be reached while renewing the access token: nothing
        # was attempted, and the next cycle (or the next press of send) asks
        # again.
        return await _waiting(session, job=job, reason=REASON_UNAVAILABLE)
    if provider is None:
        return await _waiting(session, job=job, reason=REASON_NOT_CONFIGURED)
    credential, smtp = provider
    return await _deliver(
        session,
        job=job,
        request=request,
        letter_document_id=letter_document_id,
        communication_kind=_COMMUNICATION_KIND_BY_OUTBOX_KIND[kind],
        raw=raw,
        message_id=message_id,
        credential=credential,
        smtp=smtp,
        now=at,
        sleeper=sleeper,
    )


async def _deliver(
    session: AsyncSession,
    *,
    job: MailOutbox,
    request: ConfirmationRequest | None,
    letter_document_id: UUID,
    communication_kind: CommunicationKind,
    raw: bytes,
    message_id: str | None,
    credential: MailboxCredential | None,
    smtp: SmtpClient | None,
    now: datetime,
    sleeper: Callable[[float], Awaitable[None]] | None,
) -> SendOutcome:
    """Send (or reconcile), with the retry policy the classification implies.

    ``request`` is ``None`` for the request-less ask: there is no request to
    move and no request id to record, and the recipients come from the stored
    message itself — what a client receives is what was composed, never a
    fresh resolution (the same discipline ``_recipients`` states for a
    request's own letter).
    """
    recipients = _recipients(request) if request is not None else _recipients_of(raw)
    request_id = None if request is None else request.id
    # The mailbox the message goes out from: the connected credential's own
    # address (that is the mailbox it was consented for), else the configured
    # one the SMTP leg authenticates as.
    sender = (
        credential.address if credential is not None else settings.mail_address.strip()
    )
    attempt = 0
    while True:
        attempt += 1
        failure: DeliveryFailure | None = None
        reason = ""
        try:
            if credential is not None:
                sent = await _send_via_gmail(
                    raw=raw, message_id=message_id, credential=credential
                )
            else:
                assert smtp is not None  # the provider rule never returns neither
                await smtp.send_message(sender=sender, recipients=recipients, raw=raw)
                sent = _Sent(provider_message_id=None, thread_id=None, reconciled=False)
        except MailDeliveryFailed as exc:
            failure = exc.failure
            reason = _failure_reason(failure=failure, code=exc.reason)
        except MailProviderRefused as exc:
            # The provider refused the **reconciliation lookup** (a rate limit,
            # a revoked scope): nothing was sent, the check could not be made,
            # and this sender never sends without it — so the row waits with
            # the code the ingestion trigger already answers for a refused read.
            failure = DeliveryFailure.TRANSIENT
            reason = f"{REASON_READ_REFUSED}:{exc.reason}"
        except MailProviderUnavailable:
            # Google could not be reached during the lookup. Same rule: no
            # send without the check, and no untyped failure either.
            failure = DeliveryFailure.TRANSIENT
            reason = REASON_UNAVAILABLE
        except MailResponseInvalid as exc:
            # A 200 whose body is not the shape the wire contract fixes: the
            # reconciliation cannot be trusted, so it is treated exactly like a
            # refused lookup (`invalid_response:<reason>`, the ingestion
            # trigger's own string) rather than sending on a guess.
            failure = DeliveryFailure.TRANSIENT
            reason = f"{REASON_INVALID_RESPONSE}:{exc.reason}"
        if failure is not None:
            if retry_in_cycle(failure) and attempt < ATTEMPTS_PER_CYCLE:
                await _pause(attempt=attempt, sleeper=sleeper)
                continue
            if row_status_for(failure) is OutboxStatus.FAILED:
                await outbox.fail(session, job=job, reason=reason)
                await audit.record_event(
                    session,
                    tenant_id=job.tenant_id,
                    action=AuditAction.MAIL_SEND_FAILED,
                    actor_user_id=None,
                    company_id=job.company_id,
                    entity_type="mail_outbox",
                    entity_id=job.id,
                    details={
                        "confirmation_request_id": _request_id_text(request_id),
                        "outbox_id": str(job.id),
                        "destination": job.destination,
                        "recipients": recipients,
                        "reason": reason,
                    },
                )
                return _outcome(job, disposition=SendDisposition.FAILED, reason=reason)
            await outbox.note_attempt(session, job=job, reason=reason)
            return _outcome(job, disposition=SendDisposition.WAITING, reason=reason)
        break
    # The message is with the provider (or was already): one transaction writes
    # the exchange, the request's status and the row's outcome together.
    row = await communications.record_outbound_message(
        session,
        tenant_id=job.tenant_id,
        company_id=job.company_id,
        confirmation_request_id=request_id,
        mailbox_message_id=sent.provider_message_id or message_id,
        thread_id=sent.thread_id,
        sender_address=sender,
        recipient_addresses=", ".join(recipients),
        received_at=now,
        raw_document_id=letter_document_id,
        kind=communication_kind,
    )
    try:
        await outbox.mark_sent(
            session,
            job=job,
            request=request,
            communication_id=row.id,
            provider_message_id=sent.provider_message_id,
            message_id=message_id,
            now=now,
        )
    except outbox.OutboxTransitionError:
        # The row left ``pending`` while the provider had the message — a
        # regeneration superseded it mid-delivery. The exchange above is the
        # record that the provider accepted it (the caller's commit makes it
        # durable); the row keeps the outcome it was given, un-overwritten,
        # and the report answers ``skipped``.
        return _outcome(job, disposition=SendDisposition.SKIPPED, reason="not_pending")
    await audit.record_event(
        session,
        tenant_id=job.tenant_id,
        action=AuditAction.MAIL_MESSAGE_SENT,
        actor_user_id=None,
        company_id=job.company_id,
        entity_type="communication",
        entity_id=row.id,
        details={
            "confirmation_request_id": _request_id_text(request_id),
            "outbox_id": str(job.id),
            "communication_id": str(row.id),
            "destination": job.destination,
            "message_id": message_id,
            "provider_message_id": sent.provider_message_id,
            "recipients": recipients,
            "document_id": str(letter_document_id),
            "reconciled": sent.reconciled,
            "attempts": job.attempts,
        },
    )
    return _outcome(
        job,
        disposition=SendDisposition.SENT,
        provider_message_id=sent.provider_message_id,
        message_id=message_id,
        reconciled=sent.reconciled,
    )


@dataclass(frozen=True)
class _Sent:
    """What one accepted send produced: the provider's ids, if it has any."""

    provider_message_id: str | None
    thread_id: str | None
    reconciled: bool


async def _waiting(session: AsyncSession, *, job: MailOutbox, reason: str) -> SendOutcome:
    """Record an attempt that could not even be made, and report it.

    The row's reason without a delivery classification: these are the failures
    *before* the delivery (no provider configured, a grant that cannot be
    renewed, a provider that could not be reached) and the delivery failures the
    retry policy defers. The row stays ``pending`` in every one of them — the
    letter is still owed — and the next cycle, or the next press of the send
    route, tries again with the reason beside it for whoever reads the queue.
    """
    await outbox.note_attempt(session, job=job, reason=reason)
    return _outcome(job, disposition=SendDisposition.WAITING, reason=reason)


def _failure_reason(*, failure: DeliveryFailure, code: str) -> str:
    """One failed attempt as the wire and the row name it.

    Three shapes, one per classification: an unconfirmed outcome has no code
    worth carrying (the code would be the transport's, and the point is that
    nothing is known), a transient failure is *deferred* (the same attempt
    later can work), and a permanent one *failed* (it will not).
    """
    if failure is DeliveryFailure.UNCONFIRMED:
        return REASON_UNCONFIRMED
    prefix = "mail_send_deferred" if failure is DeliveryFailure.TRANSIENT else "mail_send_failed"
    return f"{prefix}:{code}"


async def _send_via_gmail(
    *,
    raw: bytes,
    message_id: str | None,
    credential: MailboxCredential,
) -> _Sent:
    """The Gmail leg: reconcile first, then send (the module docstring's rule)."""
    client = mail.client()
    if message_id is not None:
        existing = await client.find_sent_message_id(
            access_token=credential.access_token, message_id=message_id
        )
        if existing is not None:
            metadata = await client.fetch_message_metadata(
                access_token=credential.access_token, message_id=existing
            )
            return _Sent(
                provider_message_id=existing,
                thread_id=metadata.get("threadId") if isinstance(metadata, dict) else None,
                reconciled=True,
            )
    answer = await client.send_message(access_token=credential.access_token, raw=raw)
    return _Sent(
        provider_message_id=_answer_text(answer, "id"),
        thread_id=_answer_text(answer, "threadId"),
        reconciled=False,
    )


async def _release_refusal(
    session: AsyncSession,
    *,
    job: MailOutbox,
    request: ConfirmationRequest,
    document: Document,
    as_of: date,
) -> SendOutcome | None:
    """Consult the release gate before the message leaves, and record a refusal.

    The predicate is the download route's own (``release_refusal`` resolves the
    request's work and period exactly as ``ensure_release_permitted`` does, and
    they share one implementation), and the entry is written **once per
    distinct reason**: a queue that retried every minute would otherwise fill
    an append-only chain with one unchanged fact, while a reason that *changes*
    — staff widened the package but have not recorded the fee — is a new fact
    and is recorded as one.
    """
    refusal = await service_renewals.release_refusal(
        session, tenant_id=job.tenant_id, document=document, as_of=as_of
    )
    if refusal is None:
        return None
    if job.blocked_reason != refusal.detail:
        await service_renewals.record_release_refusal(
            session,
            tenant_id=job.tenant_id,
            document=document,
            refusal=refusal,
            actor_user_id=None,
            outbox_id=job.id,
        )
    await outbox.note_attempt(
        session, job=job, reason=refusal.detail, blocked_reason=refusal.detail
    )
    return _outcome(job, disposition=SendDisposition.REFUSED, reason=refusal.detail)


async def _provider(
    *, now: datetime
) -> tuple[MailboxCredential | None, SmtpClient | None] | None:
    """Which leg sends, by the one ruled rule — or ``None`` when nothing can.

    A connected Gmail credential means the provider API; otherwise the SMTP
    settings (address, host, app password) mean the fallback; otherwise the
    deployment has no mailbox to send from at all and the row waits. There is
    deliberately **no** "choose a provider" setting.

    A credential whose grant can no longer be renewed is not a silent fallback
    to SMTP: the failure is reported (``mail_refresh_failed:<code>``) so an
    operator sees that the connection they made is broken, and the remedy —
    reconnect, or disconnect to fall back — is theirs.
    """
    try:
        credential = await mail.usable_credential(now=now)
    except mail.MailNotConnected:
        credential = None
    if credential is not None:
        return credential, None
    host = settings.smtp_host.strip()
    password = settings.mail_app_password.strip()
    address = settings.mail_address.strip()
    if host and password and address:
        return None, SmtpClient(
            host=host,
            port=settings.smtp_port,
            username=address,
            password=password,
            timeout=settings.mail_request_timeout_seconds,
        )
    return None


async def _pause(*, attempt: int, sleeper: Callable[[float], Awaitable[None]] | None) -> None:
    """Wait before an in-cycle retry: one second, doubling, capped."""
    delay = min(RETRY_PAUSE_SECONDS * (2.0 ** (attempt - 1)), MAX_RETRY_PAUSE_SECONDS)
    if sleeper is None:
        await asyncio.sleep(delay)
    else:
        await sleeper(delay)


async def _request_of(
    session: AsyncSession, *, job: MailOutbox
) -> ConfirmationRequest | None:
    """The request this row's letter belongs to."""
    row: ConfirmationRequest | None = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.id == job.confirmation_request_id,
            ConfirmationRequest.tenant_id == job.tenant_id,
        )
    )
    return row


def _recipients(request: ConfirmationRequest) -> list[str]:
    """The addresses the stored message was composed for.

    The request row's own ``recipients`` — the same list the message's ``To``
    header carries and the draft's audit entry recorded, so the exchange
    records exactly who the letter was addressed to. Nothing here re-resolves
    contacts: what goes out is what was drafted.
    """
    return [str(item.get("address", "")) for item in request.recipients if item.get("address")]


def _recipients_of(raw: bytes) -> list[str]:
    """The addresses a request-less row's stored message was composed for.

    The ask has no request row to carry its recipients (ADR-0019 §1), so they
    are read from the message's own ``To`` header — the addresses
    ``attribution.enqueue_ask`` composed it for. The same rule as
    ``_recipients``: what goes out is what was drafted, and on a met pending
    row the winner's letter is the one whose header is read. An empty answer
    is a message addressed to nobody, which the provider leg refuses like any
    other.
    """
    header = message_from_bytes(raw, policy=default_policy).get("To")
    if header is None:
        return []
    return [address for _name, address in getaddresses([str(header)]) if address]


def _request_id_text(request_id: UUID | None) -> str | None:
    """A request id as an audit entry carries it — JSON-safe, ``None`` kept."""
    return None if request_id is None else str(request_id)


def _message_id_of(raw: bytes) -> str | None:
    """The RFC 5322 ``Message-ID`` of the stored message — the anchor.

    The M3 generator stamps one in the deployment's own domain; a stored
    message without one (an older draft, or a hand-edited blob) simply has no
    anchor, and the sender then cannot reconcile and must not pretend to.
    """
    value = message_from_bytes(raw, policy=default_policy).get("Message-ID")
    return None if value is None else str(value)


def _answer_text(answer: dict[str, object], key: str) -> str | None:
    value = answer.get(key)
    return value if isinstance(value, str) and value else None


def _outcome(
    job: MailOutbox,
    *,
    disposition: SendDisposition,
    reason: str | None = None,
    provider_message_id: str | None = None,
    message_id: str | None = None,
    reconciled: bool = False,
) -> SendOutcome:
    return SendOutcome(
        outbox_id=job.id,
        confirmation_request_id=job.confirmation_request_id,
        company_id=job.company_id,
        disposition=disposition,
        reason=reason,
        provider_message_id=provider_message_id,
        message_id=message_id,
        reconciled=reconciled,
    )


async def resolve_local_tenant(session: AsyncSession) -> UUID | None:
    """The deployment's one tenant, for the background loop (ADR-0002).

    A deployment is single-tenant: "the tenant" is the one row the local
    ``tenants`` table holds. ``None`` when this deployment has none (or has
    somehow more than one) — the loop then delivers nothing, which is what an
    uninitialised deployment should do.
    """
    tenant_ids = list(
        (await session.scalars(select(Tenant.id).order_by(Tenant.created_at).limit(2))).all()
    )
    return tenant_ids[0] if len(tenant_ids) == 1 else None


__all__ = [
    "ATTEMPTS_PER_CYCLE",
    "MAX_RETRY_PAUSE_SECONDS",
    "REASON_INVALID_RESPONSE",
    "REASON_NOT_CONFIGURED",
    "REASON_READ_REFUSED",
    "REASON_REQUEST_SUPERSEDED",
    "REASON_UNAVAILABLE",
    "REASON_UNCONFIRMED",
    "RETRY_PAUSE_SECONDS",
    "SendCycleReport",
    "SendDisposition",
    "SendOutcome",
    "deliver_one",
    "resolve_local_tenant",
    "run_cycle",
]
