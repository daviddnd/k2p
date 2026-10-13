"""The reminder sweep: the only path that enqueues a reminder (contract
§5.1.12, slice R).

One cycle selects the tenant's ``sent`` confirmation requests, derives each
chain's facts from the immutable rows — ``sent_at`` from the request's
outbound ``Communication`` (the moment the letter left), a reply's presence
from any inbound row the deterministic join linked, ``reminders_sent`` from
the request's accepted ``reminder`` rows — and dispatches, slot by slot,
independently of the read-side state:

- **A slot is owed** when ``next_due_instant`` says the next cadence point has
  passed. The chain's read state (``due``/``exhausted``) never gates dispatch:
  a spent chain is time-bound, not send-bound, so the sweep keeps sending an
  owed final slot even while the read says ``exhausted``. The widened pending
  index admits one reminder row per request, and that row is what paces the
  ladder — a request whose reminder already pends skips with ``pending``.
- **The §5.1 gate** (``confirmation_generation`` — the founder's ruling: a
  reminder exists only as a follow-up to a request the product sent) is
  consulted before anything is written; a denial is an outcome carrying the
  gate's own reason and nothing else, the gate's standing semantics.
- **The letter** is the founder-approved bilingual copy
  (``app.domain.reminders``) assembled here the way the request letter is
  assembled (plain-text UTF-8, CRLF, RFC 2047 headers, ``Message-ID`` minted
  in the mailbox's own domain), threaded on the request: ``In-Reply-To`` and
  ``References`` carry the sender's recorded ``Message-ID``. The Gmail leg's
  thread reuse is the provider's business — nothing here threads beyond the
  headers. Stored through the records registry exactly as the request letter
  is (the registry's same-day same-form key suffix keeps letters unique).
- **The outbox row** is written through ``app.application.outbox`` — held on
  ``HELD_NOT_BEFORE`` under the tenant's ``review_required`` mode, released
  with the queue under ``automatic`` — and one ``reminder_enqueued`` entry
  records the request id, the cadence point and ``not_before`` on the same
  transaction.
- **Exhaustion** is observed, never stored as state: the first sweep past the
  final cadence point with no reply writes one ``reminders_exhausted`` entry
  (the request id, the reminders sent, the days unanswered), and the read
  surfaces derive the rest.

A stored configuration block the sweep cannot read stops the whole cycle
cleanly: the report names the block's violations (the R5 route answers
``tenant_config_invalid``), nothing is written, and one unreadable block can
never raise out of a cycle — the send cycle's own posture. An empty cadence
disables the chain: nothing is ever due, nothing ever exhausts.

**Overlapping cycles serialize.** The cycle begins by taking a
transaction-scoped advisory lock keyed to the tenant, before any read or
write. Without it, two overlapping cycles (the background loop plus an
administrator's press, or two administrators) both derive the same facts and
both enqueue: the widened index backstops the outbox row — the loser's insert
loses and re-reads the winner's — but the loser has already stored its own
letter document and then writes a ``reminder_enqueued`` entry naming the
winner's outbox row with its own orphan document, and both cycles record
``reminders_exhausted``, breaking §5.1.12's one-entry rule. The lock makes
the second cycle wait and re-derive from the winner's committed transaction.
Stated residual, as §5.1.11 records its concurrent-delivery window: the key
hashes the tenant id's first four bytes, so two tenants whose ids collide on
those bytes serialize against each other — exclusion is preserved either way;
only a little latency is shared.

Everything here flushes and never commits: the API session dependency (or the
worker's own edge) commits, so a letter, its outbox row and its audit entry
land together.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.application import (
    audit,
    compliance,
    documents,
    outbox,
    service_packages,
    tenant_config,
)
from app.application.confirmation import MailNotConfiguredError
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.communications import CommunicationKind, Direction
from app.domain.confirmation import RequestStatus
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.mail_send import HELD_NOT_BEFORE, OutboxKind, OutboxStatus
from app.domain.milestones import MilestoneValidationError
from app.domain.reminders import (
    cadence_instant,
    next_due_instant,
    reminder_body,
    reminder_subject,
)
from app.domain.send_modes import SendDestination, SendMode, SendModesValidationError
from app.domain.service_packages import AutomationType


class SweepOutcome(StrEnum):
    """What the sweep decided about one request (the report's vocabulary)."""

    ENQUEUED = "enqueued"
    DENIED = "denied"
    EXHAUSTED = "exhausted"
    SKIPPED = "skipped"


class SweepSkip(StrEnum):
    """Why a request owed nothing, when the outcome is ``skipped``."""

    ANSWERED = "answered"
    PENDING = "pending"
    NOT_DUE = "not_due"
    NO_CADENCE = "no_cadence"
    NO_SENT_COMMUNICATION = "no_sent_communication"


# The advisory lock's high word — "RMSW" (reminder sweep) — so a sweep's key
# can never collide with a different feature's xact lock on the same database.
_SWEEP_LOCK_SPACE = 0x524D5357


def _sweep_lock_key(tenant_id: UUID) -> int:
    """The per-tenant advisory key serializing overlapping reminder cycles."""
    return (_SWEEP_LOCK_SPACE << 32) | int.from_bytes(tenant_id.bytes[:4], "big")


@dataclass(frozen=True)
class RequestReport:
    """One request's outcome: ids, the outcome, and the outcome's own detail.

    ``cadence_index``/``held`` accompany ``enqueued`` (which point the reminder
    answers, and whether the row waits for the staff release);
    ``reason`` accompanies ``denied`` (the §5.1 gate's own vocabulary);
    ``skip`` accompanies ``skipped``.
    """

    request_id: UUID
    company_id: UUID
    outcome: SweepOutcome
    cadence_index: int | None = None
    held: bool | None = None
    reason: str | None = None
    skip: SweepSkip | None = None


@dataclass(frozen=True)
class SweepReport:
    """One cycle's account of itself, per request, in (company, request) order.

    ``config_error`` is set when a stored configuration block could not be
    read: the cycle enqueued nothing and examined nothing, and the R5 route
    maps the value to the ``tenant_config_invalid`` refusal.
    """

    now: datetime
    outcomes: tuple[RequestReport, ...]
    config_error: str | None = None

    @property
    def enqueued(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is SweepOutcome.ENQUEUED)

    @property
    def denied(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is SweepOutcome.DENIED)

    @property
    def exhausted(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is SweepOutcome.EXHAUSTED)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is SweepOutcome.SKIPPED)


async def sweep(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
) -> SweepReport:
    """Run one reminder cycle for the tenant and report what it decided.

    Never raises for one request: every candidate gets an outcome, and a
    configuration block nobody can read stops the cycle before it starts,
    named in the report rather than thrown. ``now`` pins the clock (the
    background loop and the API route both pass it in tests).
    """
    at = now if now is not None else datetime.now(UTC)
    # Serialize overlapping cycles per tenant before any read or write: the
    # module docstring states the duplicated-fact failure mode and the
    # lock-key residual. Transaction-scoped, so a rolled-back cycle (the
    # test harness) releases it with its transaction.
    await session.execute(select(func.pg_advisory_xact_lock(_sweep_lock_key(tenant_id))))
    try:
        milestones = await tenant_config.get_milestones(session, tenant_id=tenant_id)
        send_modes = await tenant_config.get_send_modes(session, tenant_id=tenant_id)
    except (MilestoneValidationError, SendModesValidationError) as exc:
        # The cycle cannot derive cadence or hold from a block nobody can
        # read, and guessing either would send client mail on the strength of
        # a row the tenant did not choose. Nothing is written; the route
        # names the refusal.
        return SweepReport(now=at, outcomes=(), config_error=str(exc))
    cadence = milestones.reminder_cadence_days
    held_by_default = send_modes.mode_for(SendDestination.CLIENT) is SendMode.REVIEW_REQUIRED

    requests = list(
        (
            await session.scalars(
                select(ConfirmationRequest)
                .where(
                    ConfirmationRequest.tenant_id == tenant_id,
                    ConfirmationRequest.status == RequestStatus.SENT.value,
                )
                .order_by(ConfirmationRequest.company_id, ConfirmationRequest.id)
            )
        ).all()
    )
    if not requests:
        return SweepReport(now=at, outcomes=())

    ids = [request.id for request in requests]
    sent_at = await _sent_at_by_request(session, ids)
    replied = await _replied_requests(session, ids)
    reminders_sent = await _reminders_sent_by_request(session, ids)
    pending_reminders = await _pending_reminders(session, ids)
    exhausted_recorded = await _exhausted_recorded(session, tenant_id, ids)
    names = await compliance.display_names(session, _unique(requests), as_of=at.date())

    reports: list[RequestReport] = []
    for request in requests:
        reports.append(
            await _sweep_one(
                session,
                store,
                scanner,
                request=request,
                cadence=cadence,
                sent_at=sent_at.get(request.id),
                has_reply=request.id in replied,
                reminders_sent=reminders_sent.get(request.id, 0),
                has_pending_reminder=request.id in pending_reminders,
                exhaustion_recorded=request.id in exhausted_recorded,
                company_name=names.get(request.company_id),
                held_by_default=held_by_default,
                tenant_id=tenant_id,
                now=at,
            )
        )
    return SweepReport(now=at, outcomes=tuple(reports))


async def _sweep_one(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    request: ConfirmationRequest,
    cadence: tuple[int, ...],
    sent_at: datetime | None,
    has_reply: bool,
    reminders_sent: int,
    has_pending_reminder: bool,
    exhaustion_recorded: bool,
    company_name: str | None,
    held_by_default: bool,
    tenant_id: UUID,
    now: datetime,
) -> RequestReport:
    """One candidate's whole decision, in the order the contract fixes."""
    if sent_at is None:
        # Queued but never accepted: nothing has left, so there is nothing to
        # count the cadence from.
        return RequestReport(
            request_id=request.id,
            company_id=request.company_id,
            outcome=SweepOutcome.SKIPPED,
            skip=SweepSkip.NO_SENT_COMMUNICATION,
        )
    if has_reply:
        return RequestReport(
            request_id=request.id,
            company_id=request.company_id,
            outcome=SweepOutcome.SKIPPED,
            skip=SweepSkip.ANSWERED,
        )
    if not cadence:
        return RequestReport(
            request_id=request.id,
            company_id=request.company_id,
            outcome=SweepOutcome.SKIPPED,
            skip=SweepSkip.NO_CADENCE,
        )
    # The boundary is time-bound, not send-bound: past the final cadence point
    # the chain is spent whether or not the final letter has delivered.
    chain_spent = now >= cadence_instant(sent_at, cadence[-1])

    due = next_due_instant(sent_at=sent_at, cadence_days=cadence, reminders_sent=reminders_sent)
    if due is not None and due <= now:
        if has_pending_reminder:
            # The index admits one pending reminder row per request, and it is
            # what paces the ladder: this slot is already queued.
            if chain_spent and not exhaustion_recorded:
                await _record_exhaustion(
                    session,
                    request=request,
                    sent_at=sent_at,
                    reminders_sent=reminders_sent,
                    now=now,
                )
            return RequestReport(
                request_id=request.id,
                company_id=request.company_id,
                outcome=SweepOutcome.SKIPPED,
                skip=SweepSkip.PENDING,
            )
        decision = await service_packages.check_automation(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            automation_type=AutomationType.CONFIRMATION_GENERATION,
            as_of=now.date(),
        )
        if not decision.allowed:
            assert decision.reason is not None  # denied decisions always carry one
            return RequestReport(
                request_id=request.id,
                company_id=request.company_id,
                outcome=SweepOutcome.DENIED,
                reason=decision.reason.value,
            )
        # Dispatch the owed slot even while the read says exhausted: the final
        # letter is still owed, and the gate having allowed it is what matters.
        held = held_by_default
        index = reminders_sent
        document = await _store_letter(
            session,
            store,
            scanner,
            request=request,
            company_name=company_name,
            sent_at=sent_at,
            now=now,
        )
        row = await outbox.enqueue_reminder(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            request_id=request.id,
            letter_document_id=document.id,
            not_before=HELD_NOT_BEFORE if held else due,
        )
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.REMINDER_ENQUEUED,
            actor_user_id=None,
            company_id=request.company_id,
            entity_type="confirmation_request",
            entity_id=request.id,
            details={
                "request_id": str(request.id),
                "cadence_index": index,
                "not_before": HELD_NOT_BEFORE if held else due,
                "held": held,
                "outbox_id": str(row.id),
                "document_id": str(document.id),
            },
        )
        if chain_spent and not exhaustion_recorded:
            await _record_exhaustion(
                session,
                request=request,
                sent_at=sent_at,
                reminders_sent=reminders_sent,
                now=now,
            )
        return RequestReport(
            request_id=request.id,
            company_id=request.company_id,
            outcome=SweepOutcome.ENQUEUED,
            cadence_index=index,
            held=held,
        )
    if chain_spent:
        if not exhaustion_recorded:
            await _record_exhaustion(
                session,
                request=request,
                sent_at=sent_at,
                reminders_sent=reminders_sent,
                now=now,
            )
        return RequestReport(
            request_id=request.id,
            company_id=request.company_id,
            outcome=SweepOutcome.EXHAUSTED,
        )
    return RequestReport(
        request_id=request.id,
        company_id=request.company_id,
        outcome=SweepOutcome.SKIPPED,
        skip=SweepSkip.NOT_DUE,
    )


async def _store_letter(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    request: ConfirmationRequest,
    company_name: str | None,
    sent_at: datetime,
    now: datetime,
) -> Document:
    """Compose, render, and register the bilingual reminder letter.

    The mirror of the request letter's assembly: one plain-text UTF-8 part
    carrying both languages, CRLF line endings and RFC 2047 headers
    (``policy.SMTP``), a fresh ``Message-ID`` in the mailbox's own domain, and
    the thread headers carrying the sender's recorded ``Message-ID`` of the
    request's letter. The recipients are the request row's own list, in its
    order — never a fresh contact resolution.
    """
    from_address = _mailbox_address()
    anchor = await _request_message_id(session, request=request)
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(
        str(item.get("address", "")) for item in request.recipients if item.get("address")
    )
    subject = reminder_subject(
        company_name=company_name or "",
        anniversary_year=request.target_date.year,
        language="en",
    )
    message["Subject"] = subject
    message["Date"] = format_datetime(now)
    message["Message-ID"] = make_msgid(domain=from_address.rpartition("@")[2] or None)
    if anchor is not None:
        message["In-Reply-To"] = anchor
        message["References"] = anchor
    body_en = reminder_body(
        company_name=company_name or "",
        sent_date=sent_at.date(),
        reply_by=request.reply_by,
        language="en",
    )
    body_zh = reminder_body(
        company_name=company_name or "",
        sent_date=sent_at.date(),
        reply_by=request.reply_by,
        language="zh-hk",
    )
    message.set_content(f"{body_en}\n\n{body_zh}")
    return await documents.register_upload(
        session,
        store,
        tenant_id=request.tenant_id,
        company_id=request.company_id,
        actor_user_id=None,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=message.as_bytes(),
        scanner=scanner,
        form=request.form_type,
        retention_class=RetentionClass.CLIENT_COMMUNICATION,
        version=request.version_no,
    )


async def _record_exhaustion(
    session: AsyncSession,
    *,
    request: ConfirmationRequest,
    sent_at: datetime,
    reminders_sent: int,
    now: datetime,
) -> None:
    """The one ``reminders_exhausted`` entry a spent chain is worth."""
    await audit.record_event(
        session,
        tenant_id=request.tenant_id,
        action=AuditAction.REMINDERS_EXHAUSTED,
        actor_user_id=None,
        company_id=request.company_id,
        entity_type="confirmation_request",
        entity_id=request.id,
        details={
            "request_id": str(request.id),
            "reminders_sent": reminders_sent,
            "days_unanswered": (now - sent_at).days,
        },
    )


def _mailbox_address() -> str:
    """The mailbox every client-facing message goes out from (scope §2.5).

    The sweep composes a letter, so the same rule the generator honours
    applies: no configured address, no letter. Reaching this raise means the
    request's letter left without a configured address, which no leg allows;
    it is a deployment bug, not a case, and the cycle's caller reports it.
    """
    address = settings.mail_address.strip()
    if not address:
        raise MailNotConfiguredError
    return address


async def _sent_at_by_request(
    session: AsyncSession, request_ids: list[UUID]
) -> dict[UUID, datetime]:
    """Each request's send instant: its outbound exchange's acceptance moment."""
    rows = await session.execute(
        select(Communication.confirmation_request_id, func.min(Communication.received_at))
        .where(
            Communication.confirmation_request_id.in_(request_ids),
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.CONFIRMATION_REQUEST.value,
        )
        .group_by(Communication.confirmation_request_id)
    )
    return {request_id: received_at for request_id, received_at in rows.all()}


async def _replied_requests(session: AsyncSession, request_ids: list[UUID]) -> set[UUID]:
    """The requests a reply has already joined — the chain is over."""
    rows = await session.scalars(
        select(Communication.confirmation_request_id)
        .where(
            Communication.confirmation_request_id.in_(request_ids),
            Communication.direction == Direction.INBOUND.value,
        )
        .distinct()
    )
    return {request_id for request_id in rows.all() if request_id is not None}


async def _reminders_sent_by_request(
    session: AsyncSession, request_ids: list[UUID]
) -> dict[UUID, int]:
    """Accepted reminders per request — the immutable rows the ladder counts."""
    rows = await session.execute(
        select(Communication.confirmation_request_id, func.count())
        .where(
            Communication.confirmation_request_id.in_(request_ids),
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.REMINDER.value,
        )
        .group_by(Communication.confirmation_request_id)
    )
    return {request_id: count for request_id, count in rows.all()}


async def _pending_reminders(session: AsyncSession, request_ids: list[UUID]) -> set[UUID]:
    """The requests whose reminder row is already waiting (the index's pacing)."""
    rows = await session.scalars(
        select(MailOutbox.confirmation_request_id)
        .where(
            MailOutbox.confirmation_request_id.in_(request_ids),
            MailOutbox.kind == OutboxKind.REMINDER.value,
            MailOutbox.status == OutboxStatus.PENDING.value,
        )
        .distinct()
    )
    return {request_id for request_id in rows.all() if request_id is not None}


async def _exhausted_recorded(
    session: AsyncSession, tenant_id: UUID, request_ids: list[UUID]
) -> set[UUID]:
    """The requests whose exhaustion the chain already records (one entry ever)."""
    rows = await session.scalars(
        select(AuditLog.entity_id).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.REMINDERS_EXHAUSTED.value,
            AuditLog.entity_type == "confirmation_request",
            AuditLog.entity_id.in_(request_ids),
        )
    )
    return {entity_id for entity_id in rows.all() if entity_id is not None}


def _unique(requests: list[ConfirmationRequest]) -> list[UUID]:
    """The candidate companies, deduplicated, order kept — the name read's key set."""
    return list(dict.fromkeys(request.company_id for request in requests))


async def _request_message_id(session: AsyncSession, *, request: ConfirmationRequest) -> str | None:
    """The sender's recorded ``Message-ID`` of the request's letter.

    The thread anchor the reminder replies on: the RFC 5322 id the deployment
    stamped and the sender recorded on the sent row (the reconcile's own key).
    """
    row: MailOutbox | None = await session.scalar(
        select(MailOutbox)
        .where(
            MailOutbox.confirmation_request_id == request.id,
            MailOutbox.kind == OutboxKind.CONFIRMATION_REQUEST.value,
            MailOutbox.status == OutboxStatus.SENT.value,
        )
        .order_by(MailOutbox.sent_at, MailOutbox.id)
        .limit(1)
    )
    return None if row is None else row.message_id


__all__ = [
    "RequestReport",
    "SweepOutcome",
    "SweepReport",
    "SweepSkip",
    "sweep",
]
