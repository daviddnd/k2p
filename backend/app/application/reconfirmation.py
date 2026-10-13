"""The reconfirmer's sending half (scope §2.5, §7; the M5b slice; contract
§5.1.12).

The domain module (``app.domain.reconfirmation``) owns the two pure halves —
which parsed reply asks for which letter, and the deterministic bilingual
restatement itself. This module is the seam around them that turns a landed
parse into a client letter riding the M2 machinery and nothing else:
compose → records registry → outbox row → the release the client ``SendMode``
decides. There is no second send path: the letter is stored through
``app.application.documents`` (``generated_draft``, retention
``client_communication`` — the WORM original the sender delivers byte for
byte), queued through ``app.application.outbox`` (``kind=reconfirmation``, the
widened §5.1.12 index pacing one pending row per request and kind), and
delivered by ``app.application.mail_send``'s cycle unchanged — on acceptance
the sender writes the outbound ``Communication`` with ``kind=reconfirmation``
in the request's thread (§5.1.11's pattern, same legs, same reconcile).

``enqueue_letter`` is the parse-driven entry point, called from the parse
cycle's landing tail (``app.application.reply_parsing``) for every reply the
parser landed. It decides on the parse row (``decide`` — the decision is
mode-independent; the send mode is read here, where the release actually
happens) and:

- ``no_letter`` / ``staff_review`` — writes nothing. A clean no-change reply
  is answered by the confirmations themselves (ruling R6); a staff-review
  parse is a person reading the reply, never a letter.
- ``reconfirmation_letter`` / ``clarification_letter`` — composes the letter:
  each parsed confirmation is restated by the checklist's own asked question
  and the client's answer; each proposal the parse landed is restated by
  type, party and effective date (the client's own wording when no ISO date
  parsed); each proposal the grammar could not complete, and every field the
  model flagged uncertain, becomes a per-field clarification question quoting
  the client's own words — the evidence spans the post-checks already proved
  verbatim. A letter decision on a parse with no joined request is a typed
  refusal: the outbox's request reference is NOT NULL, and the recipients,
  the thread anchor and the asked questions all live on the request row —
  nothing is guessed at. A letter that meets an already-pending row for the
  request (a second reply while the thread's letter waits — a wide window
  under ``review_required``) re-reads that row and reports the met condition:
  the thread already owes a letter, and the audit names the letter the row
  will actually deliver — never pairing the fresh, undelivered document with
  the pre-existing row.
- **SendMode governance (ruling R7).** The tenant's client destination
  decides: ``automatic`` releases at once (``not_before NULL``,
  send-on-release, the posture every request row has) and ``review_required``
  is written held on ``HELD_NOT_BEFORE`` until the staff release moves it —
  the reminder release's own path (``outbox.release_reminders``, widened over
  both follow-up kinds), never a second mechanism.

``enqueue_event_letter`` is the event-driven entry point (M5b task 5): the
``send_reconfirmation`` transition's compose-store-release. There is no parse
row behind a staff-pressed re-ask — the letter content composes from the event
itself (``_event_change_line``: the event's type, party and effective date
mapped onto the restatement's change-items shape, the client's own wording
standing in when the event carries no ISO date) and the thread is the one the
event's provenance names: its source reply's request. Everything else — the
stored ``.eml``, the outbox row, the SendMode governance, the met-pending-row
answer, the audit's coordinates-only discipline — is the parse-driven entry's
own machinery, shared, not copied.

**Custody** is the M4/M5a rule this module joins: the stored ``.eml`` is the
one place the letter prose lives (the records registry, WORM); the audit
entries — ``reconfirmation_enqueued`` at the enqueue, ``reconfirmation_released``
at the staff release — and the cycle's report carry ids, counts, modes and
instants, never a span, a quote, or a body.

**Failures are outcomes, never aborts — on the cycle's tail.** ``ReconfirmationError``
carries a bounded reason — ``no_confirmation_request`` | ``company_name_unavailable`` |
``tenant_config_invalid`` | ``mail_not_configured`` | ``nothing_to_restate``
— and the parse cycle's tail contains it: one message's letter failure
reports as that message's outcome (``letter_failed:<reason>``) and the cycle
finishes the replies behind it. A failed letter is deliberately not retried
by the cycle: the parse row's idempotency rule stands (re-parses are new
replies answering a reconfirmation, never re-runs), and the review queue's
send-for-reconfirmation is the staff's re-ask. On the transition path the
same refusal raises instead: the ``send_reconfirmation`` action must not move
the event when its letter cannot compose — never half-performed — and the
route maps the reason the way the confirmation trigger does (a missing record
is a 422; an unconfigured mailbox a 503).

Everything here flushes and never commits: the parse cycle's transaction (or
the API session dependency, or the worker's own edge) commits, so a letter,
its outbox row and its audit entries land with the parse that asked for them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.reply_parsing import ReplyParse
from app.application import audit, company_master, documents, outbox, tenant_config
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.change_events import ChangeEventType
from app.domain.confidence import ConfidenceBand
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.mail_send import HELD_NOT_BEFORE, OutboxKind, OutboxStatus
from app.domain.reconfirmation import (
    ClarificationLine,
    ConfirmationLine,
    ParseOutcome,
    ProposedChangeLine,
    ReconfirmationDecision,
    decide,
    restatement_letter,
    restatement_subject,
)
from app.domain.reply_parsing import ConfirmationAnswer, ReplyStatus, proposal_violations
from app.domain.send_modes import SendDestination, SendMode, SendModes, SendModesValidationError

_NO_LETTER_DECISIONS = (ReconfirmationDecision.NO_LETTER, ReconfirmationDecision.STAFF_REVIEW)


class ReconfirmationError(Exception):
    """The letter could not be composed or enqueued; ``reason`` is the wire code.

    The bounded vocabulary lives in the module docstring; the parse cycle's
    tail maps it to the message's ``letter_failed:<reason>`` outcome.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LetterReport:
    """What the enqueue decided — coordinates only.

    ``decision`` is always set. ``outbox_id``/``document_id``/``held``
    accompany a fresh enqueue (``held`` says the row waits on
    ``HELD_NOT_BEFORE`` for the staff release). ``met_pending_row`` is True
    when the enqueue met an already-pending row for the request: the thread
    already owed a letter, ``outbox_id`` names that row, and ``document_id``
    stays ``None`` — the row will deliver the winner's letter, which the
    audit entry names. No span, no quote, no body travels here — the stored
    ``.eml`` is the letter's only home.
    """

    decision: ReconfirmationDecision
    outbox_id: UUID | None = None
    document_id: UUID | None = None
    held: bool | None = None
    met_pending_row: bool = False


async def enqueue_letter(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    parse_row: ReplyParse,
    communication: Communication,
    request: ConfirmationRequest | None,
    parsed_events: Sequence[Mapping[str, Any]],
    unlanded_events: Sequence[Mapping[str, Any]],
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> LetterReport:
    """Decide one landed parse's letter and enqueue it, or write nothing.

    The parse row is the durable record; ``parsed_events`` and
    ``unlanded_events`` are the caller's projections of what the parse
    proposed (the letter's restatement detail, exactly what
    ``ParseOutcome``'s docstring calls them) — the unlanded entries carry
    their ``index`` into ``parsed_events`` (the clarification field's
    coordinate) and the refusal ``reason`` the landing recorded. A letter
    that meets an already-pending reconfirmation row for the request (a
    second reply parsed while the first's letter still waits) re-reads that
    row — the widened index's idempotent answer that the thread already owes
    a letter: the freshly stored document stands, registered, as the record
    of what this parse asked, and both the audit entry and the report say
    the met condition and name the letter the row will actually deliver.
    """
    at = now if now is not None else datetime.now(UTC)
    send_modes = await _send_modes(session, tenant_id=tenant_id)
    mode = send_modes.mode_for(SendDestination.CLIENT)
    outcome = ParseOutcome(
        status=ReplyStatus(parse_row.status),
        routing=None if parse_row.routing is None else ConfidenceBand(parse_row.routing),
        send_mode=mode,
        parse_valid=True,
        change_event_count=len(parsed_events),
        has_uncertainties=_has_uncertainties(parse_row, parsed_events),
    )
    decision = decide(outcome)
    if decision in _NO_LETTER_DECISIONS:
        return LetterReport(decision=decision)
    if request is None:
        raise ReconfirmationError("no_confirmation_request")
    confirmations = _confirmation_lines(parse_row, request)
    changes = _change_lines(parsed_events, unlanded_events)
    clarifications = _clarification_lines(parse_row, parsed_events, unlanded_events)
    if not (confirmations or changes or clarifications):
        raise ReconfirmationError("nothing_to_restate")
    held = mode is SendMode.REVIEW_REQUIRED
    document = await _compose_and_store(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        company_id=parse_row.company_id,
        request=request,
        confirmations=confirmations,
        changes=changes,
        clarifications=clarifications,
        at=at,
    )
    enqueued = await outbox.enqueue_reconfirmation(
        session,
        tenant_id=tenant_id,
        company_id=parse_row.company_id,
        request_id=request.id,
        letter_document_id=document.id,
        not_before=HELD_NOT_BEFORE if held else None,
        created_by=actor_user_id,
    )
    row = enqueued.row
    met_pending_row = not enqueued.created
    # The audit names the letter the row will actually deliver. On a fresh
    # enqueue that is the document just stored; on a met pending row (a
    # second reply while the thread's letter still waits) the row still names
    # the winner's letter — the widened index's idempotent answer — and this
    # call's stored document stands, registered, as the record of what this
    # parse asked. Pairing the fresh document with the pre-existing row would
    # misdescribe what the client receives.
    delivered_document_id = row.letter_document_id if met_pending_row else document.id
    assert delivered_document_id is not None  # a reconfirmation row names its letter
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.RECONFIRMATION_ENQUEUED,
        actor_user_id=actor_user_id,
        company_id=parse_row.company_id,
        entity_type="confirmation_request",
        entity_id=request.id,
        details={
            "parse_id": str(parse_row.id),
            "communication_id": str(communication.id),
            "request_id": str(request.id),
            "decision": decision.value,
            "confirmation_count": len(confirmations),
            "change_count": len(changes),
            "clarification_count": len(clarifications),
            "send_mode": mode.value,
            "held": held,
            "not_before": HELD_NOT_BEFORE if held else None,
            "destination": outbox.CONFIRMATION_DESTINATION.value,
            "outbox_id": str(row.id),
            "document_id": str(delivered_document_id),
            "met_pending_row": met_pending_row,
        },
    )
    await session.flush()
    return LetterReport(
        decision=decision,
        outbox_id=row.id,
        document_id=None if met_pending_row else document.id,
        held=held,
        met_pending_row=met_pending_row,
    )


async def enqueue_event_letter(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    event: ChangeEvent,
    request: ConfirmationRequest,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> LetterReport:
    """The staff-pressed re-ask (the ``send_reconfirmation`` transition's
    compose-store-release): one event's change restated to the client.

    There is no parse row behind a staff-pressed re-ask, so the letter
    composes from the event itself — ``_event_change_line`` maps the event's
    type, party and effective date onto the restatement's change-items shape —
    and the thread is the one the event's provenance names: its source
    reply's request (the caller resolves it; this module never guesses a
    recipient or a thread). The decision is always the restatement letter: an
    event always carries one change to restate. Everything else — the stored
    ``.eml``, the outbox row naming it, the client destination's ``SendMode``
    release, the met-pending-row answer, the coordinates-only audit — is
    ``enqueue_letter``'s own machinery, shared through ``_compose_and_store``
    and ``outbox.enqueue_reconfirmation``, never copied.

    A refusal raises (``ReconfirmationError``) before anything is written —
    the event must not move to ``client_reconfirming`` when its letter cannot
    compose — and the transition executor lets it propagate to the route.
    """
    at = now if now is not None else datetime.now(UTC)
    send_modes = await _send_modes(session, tenant_id=tenant_id)
    mode = send_modes.mode_for(SendDestination.CLIENT)
    changes = [_event_change_line(event)]
    if not changes:
        raise ReconfirmationError("nothing_to_restate")
    held = mode is SendMode.REVIEW_REQUIRED
    document = await _compose_and_store(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        company_id=event.company_id,
        request=request,
        confirmations=[],
        changes=changes,
        clarifications=[],
        at=at,
    )
    enqueued = await outbox.enqueue_reconfirmation(
        session,
        tenant_id=tenant_id,
        company_id=event.company_id,
        request_id=request.id,
        letter_document_id=document.id,
        not_before=HELD_NOT_BEFORE if held else None,
        created_by=actor_user_id,
    )
    row = enqueued.row
    met_pending_row = not enqueued.created
    # The same fresh-vs-met discipline as the parse-driven entry: on a met
    # pending row the winner's letter is what the client receives, and the
    # audit names that pairing — never the fresh, undelivered document.
    delivered_document_id = row.letter_document_id if met_pending_row else document.id
    assert delivered_document_id is not None  # a reconfirmation row names its letter
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.RECONFIRMATION_ENQUEUED,
        actor_user_id=actor_user_id,
        company_id=event.company_id,
        entity_type="confirmation_request",
        entity_id=request.id,
        details={
            "event_id": str(event.id),
            "request_id": str(request.id),
            "decision": ReconfirmationDecision.RECONFIRMATION_LETTER.value,
            "confirmation_count": 0,
            "change_count": len(changes),
            "clarification_count": 0,
            "send_mode": mode.value,
            "held": held,
            "not_before": HELD_NOT_BEFORE if held else None,
            "destination": outbox.CONFIRMATION_DESTINATION.value,
            "outbox_id": str(row.id),
            "document_id": str(delivered_document_id),
            "met_pending_row": met_pending_row,
        },
    )
    await session.flush()
    return LetterReport(
        decision=ReconfirmationDecision.RECONFIRMATION_LETTER,
        outbox_id=row.id,
        document_id=None if met_pending_row else document.id,
        held=held,
        met_pending_row=met_pending_row,
    )


async def _send_modes(session: AsyncSession, *, tenant_id: UUID) -> SendModes:
    """The tenant's send-mode block, fail-closed — the ruling-R7 read.

    A block nobody can read refuses the letter rather than defaulting to
    sending (silently falling back to ``automatic`` would send client mail on
    the strength of a row the tenant did not choose): the same narrow-read
    discipline the reminder sweep and the generator honour.
    """
    try:
        return await tenant_config.get_send_modes(session, tenant_id=tenant_id)
    except SendModesValidationError as exc:
        raise ReconfirmationError("tenant_config_invalid") from exc


def _has_uncertainties(parse_row: ReplyParse, parsed_events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any parsed confirmation, event or party carries the model's
    ``uncertain`` flag — the decision table's ``has_uncertainties`` leg."""
    if any(bool(item.get("uncertain")) for item in parse_row.confirmations):
        return True
    return any(
        bool(event.get("uncertain")) or bool(event["party"].get("uncertain"))
        for event in parsed_events
    )


def _confirmation_lines(
    parse_row: ReplyParse, request: ConfirmationRequest
) -> list[ConfirmationLine]:
    """The parsed confirmations restated: the checklist's own asked question
    (the parser consumes the checklist, never writes it) and the client's
    answer. A confirmation whose ``fact_id`` the checklist does not carry is
    restated by its coordinate — an unknown fact has no question to quote,
    and staff see the coordinate for what it is."""
    questions = {item.get("fact_id"): item.get("question") for item in request.checklist}
    lines = []
    for item in parse_row.confirmations:
        fact_id = item["fact_id"]
        question = questions.get(fact_id)
        lines.append(
            ConfirmationLine(
                fact_id=fact_id,
                question=question if isinstance(question, str) else fact_id,
                answer=ConfirmationAnswer(item["answer"]),
            )
        )
    return lines


def _change_lines(
    parsed_events: Sequence[Mapping[str, Any]],
    unlanded_events: Sequence[Mapping[str, Any]],
) -> list[ProposedChangeLine]:
    """The proposals the parse landed (every parsed event the grammar
    completed), restated by type, party and effective date — the client's own
    wording stands in when no ISO date parsed."""
    unlanded = {int(entry["index"]) for entry in unlanded_events}
    lines = []
    for index, event in enumerate(parsed_events):
        if index in unlanded:
            continue
        party = event["party"]
        party_name = party.get("name_en") or party.get("name_zh") or party["name_as_written"]
        raw_date = event.get("effective_date")
        lines.append(
            ProposedChangeLine(
                event_type=ChangeEventType(event["type"]),
                party_name=party_name,
                effective_date=None if raw_date is None else date.fromisoformat(raw_date),
                effective_date_text=event.get("effective_date_text") or "",
            )
        )
    return lines


def _event_change_line(event: ChangeEvent) -> ProposedChangeLine:
    """One stored event restated — the staff-pressed re-ask's change line.

    The event's payload grammar is ``app.domain.change_events``'s, and the
    line is the restatement's: an officer event names its officer (English
    name first, the master's own display convention), a registered-office
    change names the address in its filing shape (the four CR boxes, blank
    parts dropped), and the ``other`` escape hatch restates staff's own
    summary line — the one place the grammar admits prose, restated as the
    party the client reads. The event's effective date is the day the
    statutory clock reads from; the verbatim source text stands in when the
    event carries no ISO date, exactly the parse projection's rule.
    """
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    body = payload.get("appointment") or payload.get("cessation") or {}
    officer = body.get("officer") if isinstance(body, Mapping) else None
    if isinstance(officer, Mapping):
        party_name = officer.get("name_en") or officer.get("name_zh")
    elif event.type == ChangeEventType.REGISTERED_OFFICE_CHANGE.value:
        address = (payload.get("registered_office") or {}).get("address") or {}
        party_name = ", ".join(
            str(address[key])
            for key in ("flat", "building", "street", "district")
            if address.get(key)
        )
    elif event.type == ChangeEventType.OTHER.value:
        party_name = payload.get("summary")
    else:
        party_name = None
    return ProposedChangeLine(
        event_type=ChangeEventType(event.type),
        party_name=party_name if isinstance(party_name, str) and party_name.strip() else "",
        effective_date=event.effective_date,
        effective_date_text=event.effective_date_text or "",
    )


def _clarification_lines(
    parse_row: ReplyParse,
    parsed_events: Sequence[Mapping[str, Any]],
    unlanded_events: Sequence[Mapping[str, Any]],
) -> list[ClarificationLine]:
    """The per-field questions: every model uncertainty, and every proposal
    the grammar could not complete — the missing field named, the client's
    own words quoted back (the post-checks proved those spans verbatim)."""
    unlanded = {int(entry["index"]) for entry in unlanded_events}
    lines = []
    for item in parse_row.confirmations:
        if item.get("uncertain"):
            lines.append(
                ClarificationLine(
                    field=f"confirmations[{item['fact_id']}]",
                    quote=item.get("evidence_span") or None,
                )
            )
    for index, event in enumerate(parsed_events):
        if index in unlanded:
            continue  # the unlanded question below covers it
        if event.get("uncertain") or event["party"].get("uncertain"):
            lines.append(
                ClarificationLine(
                    field=f"change_events[{index}]",
                    quote=event.get("evidence_span") or None,
                )
            )
    for entry in unlanded_events:
        index = int(entry["index"])
        if entry.get("reason") == "no_unique_match":
            # A withdrawal/correction reference the matcher refused: which
            # earlier event the client meant is the question.
            field = f"change_events[{index}].referenced_event"
        else:
            violations = proposal_violations(event=entry)
            key = violations[0].split(":", 1)[0].split(".")[-1] if violations else "proposal"
            field = f"change_events[{index}].{key}"
        lines.append(ClarificationLine(field=field, quote=entry.get("evidence_span") or None))
    return lines


async def _compose_and_store(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request: ConfirmationRequest,
    confirmations: Sequence[ConfirmationLine],
    changes: Sequence[ProposedChangeLine],
    clarifications: Sequence[ClarificationLine],
    at: datetime,
) -> Document:
    """Compose the bilingual restatement and register it as the letter's
    WORM original — the reminder letter's assembly, mirrored: plain-text
    UTF-8 with CRLF and RFC 2047 headers (``policy.SMTP``), a fresh
    ``Message-ID`` in the mailbox's own domain, the thread headers carrying
    the request's letter anchor, the request's own recipients (never a fresh
    contact resolution), stored through the records registry with the scan
    rule applied."""
    from_address = _mailbox_address()
    snapshot = await company_master.get_company_master(
        session, tenant_id=tenant_id, company_id=company_id, as_of=at.date()
    )
    name = None if snapshot.name is None else (snapshot.name.name_en or snapshot.name.name_zh)
    if name is None:
        # Scope §7: every client-facing message identifies the company by name
        # and number — a company with no name in force is not written to.
        raise ReconfirmationError("company_name_unavailable")
    cr_number = snapshot.company.cr_number
    br_number = snapshot.company.br_number
    subject = restatement_subject(company_name=name, cr_number=cr_number, br_number=br_number)
    body = restatement_letter(
        company_name=name,
        cr_number=cr_number,
        br_number=br_number,
        confirmations=confirmations,
        changes=changes,
        clarifications=clarifications,
    )
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(
        str(item.get("address", "")) for item in request.recipients if item.get("address")
    )
    message["Subject"] = subject
    message["Date"] = format_datetime(at)
    message["Message-ID"] = make_msgid(domain=from_address.rpartition("@")[2] or None)
    anchor = await _thread_anchor(session, request=request)
    if anchor is not None:
        message["In-Reply-To"] = anchor
        message["References"] = anchor
    message.set_content(body)
    return await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=company_id,
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


async def _thread_anchor(
    session: AsyncSession, *, request: ConfirmationRequest
) -> str | None:
    """The sender's recorded ``Message-ID`` of the request's letter — the
    thread anchor the reconfirmation replies on (the reminder's own rule:
    threading is carried by these headers, nothing else)."""
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


def _mailbox_address() -> str:
    """The mailbox every client-facing message goes out from (scope §2.5).

    The configured address is load-bearing twice over: it is the ``From``
    header and the ``Message-ID``'s domain, so a deployment without one
    refuses rather than composing a letter that could not be recognised as
    the TCSP's own (the generator's and the sweep's own rule)."""
    address = settings.mail_address.strip()
    if not address:
        raise ReconfirmationError("mail_not_configured")
    return address


__all__ = [
    "LetterReport",
    "ReconfirmationError",
    "enqueue_event_letter",
    "enqueue_letter",
]
