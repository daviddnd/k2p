"""Inbound mail ingestion: the mailbox's messages become records (ADR-0005;
contract §5.1.7; data-model §2.9).

One cycle reads the dedicated mailbox from the tenant's cursor and turns each
new message into documents and a ``Communication`` row — repeatable, bounded,
and visible, the way the command worker turns envelopes into outcomes. The
mailbox is read through the one reader the deployment has (``mail.reader``):
a connected Gmail credential's provider API, or the configured IMAP settings
— ADR-0005's fallback for a TCSP on corporate Exchange — with the same
per-message surface either way, so the order below is one code path.

Per message, in this order and for these reasons — ``examine_message`` below
is that order, and it is the same code for a cycle's window and for the §2.5
review surface's retry of one message (``app.application.quarantine``):

1. **Already recorded?** (by provider message id) — skip without a provider
   call. This is the idempotency rule, and the database's partial unique
   index is its backstop.
2. **The envelope** — sender, receipt time, thread, subject. The cheap read,
   and everything the next two steps need.
3. **Attribution** — first the thread evidence (ADR-0019 §1): when the
   message's provider thread id or its ``In-Reply-To``/``References`` match
   exactly one company across the tenant's stored outbound exchanges, the
   message is *threaded* and that company is the chain it binds to. The
   sender is then judged as a registered channel of **that** company: one it
   is registered for proceeds (the thread resolving an address that reaches
   several companies — no resolver runs), and one it is not is the
   registration/confirmation case: the message opens an attribution case and
   is neither read nor stored, awaiting an instruction from a registered
   channel or that channel's confirmation (``app.application.attribution``).
   The case's ask — the one thing that case writes — is composed from
   envelope facts and queued through the same outbox every other client
   letter rides (``attribution.enqueue_ask``; ruling R4), so the unregistered
   sender's own bytes are still never read.
   **A message that is a registered channel's does two things besides
   proceed** (rulings R5/R7/M2, ``_registered_channel_message``): when a case
   on the thread awaits a confirmation, its first non-empty line is read just
   in time and, if it is the confirm act, the message is stored as the
   authorising exchange (``attribution_confirmation``) and every open awaiting
   case on that thread is processed — the parked originals are ingested for
   the first time — while the confirming message itself is never parsed as a
   reply (the parse cycle's listing leaves that kind out); an *ordinary*
   registered channel's message on the thread closes the cases still awaiting
   one, whose remedy is exactly that message
   (``close_registration_cases``), cancelling the asks they queued. The
   closure is the ordinary path's alone: the act has already attempted every
   open awaiting case, so closing behind it would strand a 2.1 case whose
   processing the gate denied rather than leave it awaiting for staff (M2).
   With no chain — no thread evidence, or evidence naming none or several
   companies — today's flow runs unchanged, except where the clarification
   loop's two unambiguously deterministic-but-unattributed classes replace its
   quarantine landings (workstream ②, ruling D4, ``decide_unthreaded``): the
   sender's address must first resolve through the tenant's registered contact
   channels, and a message already carrying an open case's anchor is answered
   against that case first — the A-class answer grammar (a reply whose first
   line names exactly one of the sender's companies resolves the case and
   re-runs the ingestion tail for the parked original, ruling D6), the bounded
   re-ask (a later unresolvable message from the same sender on the anchor may
   ask again, one re-ask maximum, ruling D7), and the B-class closure (a
   message from a registered channel of any company closes the company-less
   first-contact case, ruling D8, and then follows its own normal path).
   Exactly one company is the attribution and the message proceeds; none is ②'s
   first-contact ask when the message carries its own RFC ``Message-ID`` (the
   thread anchor a reply's ``In-Reply-To``/``References`` carries back), else
   §2.5's unregistered sender; several is the multi-company case, which the
   resolver then decides from the message's own subject and body — ②'s
   which-company ask when the resolver matches none and the anchor exists
   (else §2.5's ambiguous sender, unchanged). The several case splits further
   (workstream ③, ADR-0019 §3): when the resolver names **two or more**
   decisive candidates, the message is the split's E1 — one parent, one
   indivisible call dividing the tokenized content across the sender's own
   companies, per-company work items, no anchor required and nothing asked
   of the client; the outcome is ``split`` with the parent id riding it. **A
   message without its own RFC ``Message-ID`` opens no case**, either class:
   an anchor-less case could never be found again, which is ①'s anchor-less
   rule extended verbatim. **Neither remaining refusal is a guess**: both are
   recorded for staff in the §2.5 quarantine table
   (``quarantined_messages``), and the reasons differ because the remedies
   do — ``unregistered_sender`` is "register this address",
   ``multiple_companies`` is "work out which company this message is about".
4. **The §5.1 automation gate** — ``check_automation(EMAIL_INGESTION)`` for
   that company, failing closed with the gate's own reason. A denial means
   nothing is scanned or written.
5. **ADR-0009's ingestion order, per file** — the raw message and then every
   attachment go through the single document write path, which scans before
   it writes: ``clean``, ``infected`` (stored as the record of what arrived,
   never served or processed), or ``pending`` (the scanner could not answer —
   fail closed, and the retro-scan resolves it). An attachment outside the
   registry's allowed types is refused and reported; it is not stored, and
   the rest of the message still is.
6. **The ``Communication`` row** — written last, so the message id becomes
   the idempotency key only once everything it refers to exists. The row
   carries the deterministic reply join (§5.1.12): when the message's
   thread id or ``In-Reply-To``/``References`` headers match exactly one
   stored outbound exchange, the row names the request it answers, and a
   zero-or-ambiguous match leaves the link null rather than guessing.

**What is fetched, and what is stored.** Four paths read a message the
cycle has not accepted yet, and each reads it only to decide. The
multi-company step decides from the words a message carries, so it fetches
the raw message to have its subject and body (``message_text``) resolved
against the sender's own companies. A registered channel's message on a
thread with a case awaiting a confirmation is fetched to read the body's
first non-empty line (``app.domain.communications.first_body_line``, a read
that keeps the line structure the confirm grammar is written for), and the
bytes are reused for that message's own landing rather than fetched twice.
A message carrying an ``awaiting_clarification`` case's anchor, from the
case's own sender, is fetched for the answer grammar's first line — ①'s
just-in-time grammar fetch, extended — and the bytes are reused for the
answer's own landing when the grammar resolves the case (ruling D6). The
attribution branch (ADR-0019 §1) otherwise decides from metadata alone,
and a message that only opens a case is **never fetched at all**: the asks
quote envelope facts the envelope already carries (ruling R2), so there is
nothing to read the bytes for. The rule that holds across all four is
therefore *never stored for a message the cycle did not accept* — no object,
no document row of the message, and no ``Communication``, for a refusal and
an awaiting case alike. The one document an awaiting case does store is the
ask's own letter: a new outbound draft composed from the envelope's facts,
which is the deployment's writing, not a copy of the mail. An authorised
original is read by the case it belongs to, after the confirmation, never
before (``attribution.process_case`` → ``ingest_case_message``, this order's
own tail).

The cycle is one transaction: it flushes, never commits (the caller's session
edge commits), and a provider failure anywhere aborts it — a retry re-reads
the same window, and the message-id check makes the replay harmless. The
message's audit entries all go through the one write path:
``mail_message_ingested`` for a message that landed,
carrying the message id, the document ids, the scan outcomes, and — when the
resolver ran — its decision (case, evidence tier, score, routing, candidate
ids); ``mail_message_quarantined`` for one that could not be attributed,
carrying the quarantine row's id and the sender's own reason;
``attribution_case_opened`` for one that opened a case (ADR-0019 §1),
carrying the case id, the sender and the status; and
``attribution_ask_enqueued`` for the ask that case then queued, carrying the
case's coordinates, the outbox row and the stored letter — never the
letter's content. The resolver's
§2.7 assessment rides that same entry rather than adding a second one.
**The confirming act writes two more** (ruling R5): the
``attribution_confirmed`` entry names the company, the stored confirming
exchange when the gate let it be stored, and every case the act authorised
with the exchange each resolved to — written whether or not the storage and
the processing succeeded, because the act is the authority and the report is
what says the rest; and ``attribution_case_closed`` names the 2.1 cases a
registered channel's own message closed (ruling R7), one entry for the one
message that brought the remedy. **The same decision travels on the message's
outcome**
(``MessageOutcome.resolution``), projected once (``_resolution_summary``) and
used for both the entry and every report built from that outcome, so the two
cannot disagree about what the rule saw. An outcome whose sender reached one
company, or none, never saw the resolver at all and carries ``None`` — the
honest answer, not an omission. **No mail body, no attachment content, no
attachment filename, and no text a resolver candidate matched on** ever
reaches an audit entry, a log, or a response.

The cursor is the mailbox's own receipt time of the newest message the cycle
examined — Gmail's ``internalDate``, IMAP's ``INTERNALDATE`` — stored per
tenant, and the next read asks for everything after it minus a short overlap
(the search expression that means that differs by leg; the reader builds it).
It advances over refused messages too, which is what keeps a burst larger
than one cycle's bound from being skipped: nothing above the cursor is missed,
and re-reading the overlap costs the id checks it would cost anyway. Its
limits are stated in the contract (§5.1.7) — and the important one is that
the cursor only ever moves forward: a message the cycle refused is never in a
later window, and nothing here re-examines it. The §2.5 review surface's
retry is what does, for one message at a time, by calling ``examine_message``
— the same order, the same idempotency key, and the quarantine row the
message already has rather than a second one. Moving that row (its status,
its resolution) belongs to that surface (``app.application.quarantine``);
nothing here resolves one.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.attribution import open_cases_for_anchor, open_cases_for_thread
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.mail_ingestion import MailIngestionState
from app.adapters.persistence.models import AttributionCase, Tenant
from app.adapters.persistence.quarantine import QuarantinedMessage
from app.application import (
    attribution,
    audit,
    communications,
    company_master,
    contacts,
    documents,
    mail,
    reply_resolver,
    service_packages,
    split,
)
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.attribution import (
    AttributionAction,
    AttributionCaseStatus,
    CompanyReference,
    UnthreadedAction,
    decide,
    decide_unthreaded,
    is_confirmation,
)
from app.domain.audit import AuditAction
from app.domain.communications import (
    AttachmentDisposition,
    CommunicationKind,
    Direction,
    IngestOutcome,
    MessageAttachment,
    first_body_line,
    message_text,
    normalized_address,
    parse_inbound_message,
    sender_address,
)
from app.domain.contacts import ChannelType
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    ContentTypeMismatchError,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
    UnsupportedContentTypeError,
    UploadTooLargeError,
)
from app.domain.mail import MessageMetadata
from app.domain.prior_filing import AssessmentSubject
from app.domain.quarantine import QuarantineReason, QuarantineStatus
from app.domain.reminders import match_outbound
from app.domain.reply_resolver import Resolution, ResolutionOutcome, decisive_candidate_ids
from app.domain.service_packages import AutomationType

# Refusals are codes, not the domain's prose: a report is for an operator, and
# nothing a sender typed belongs in one.
_UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
_CONTENT_TYPE_MISMATCH = "content_type_mismatch"
_TOO_LARGE = "too_large"
_TOO_MANY_ATTACHMENTS = "too_many_attachments"

_MESSAGE_REFUSALS: dict[type[Exception], str] = {
    UnsupportedContentTypeError: _UNSUPPORTED_CONTENT_TYPE,
    ContentTypeMismatchError: _CONTENT_TYPE_MISMATCH,
    UploadTooLargeError: _TOO_LARGE,
}

# The two awaiting outcomes, keyed by the ask ``decide`` chose (ADR-0019 §1):
# the decision's own vocabulary, projected for the cycle's report.
_AWAITING_OUTCOME: dict[AttributionAction, IngestOutcome] = {
    AttributionAction.ASK_REGISTERED_CHANNEL: IngestOutcome.AWAITING_REGISTERED_CHANNEL,
    AttributionAction.ASK_CONFIRMATION: IngestOutcome.AWAITING_CONFIRMATION,
}

# The clarification loop's two awaiting outcomes, keyed by the ask
# ``decide_unthreaded`` chose (ruling D4) — the first-contact ask reuses ①'s
# outcome, the which-company ask carries the new one.
_UNTHREADED_AWAITING_OUTCOME: dict[UnthreadedAction, IngestOutcome] = {
    UnthreadedAction.ASK_FIRST_CONTACT: IngestOutcome.AWAITING_REGISTERED_CHANNEL,
    UnthreadedAction.ASK_CLARIFICATION: IngestOutcome.AWAITING_CLARIFICATION,
}


class NoLocalTenantError(Exception):
    """The deployment cannot say which tenant's mailbox this cycle reads."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AttachmentOutcome:
    """What became of one attachment: a document, or a bounded refusal code."""

    disposition: AttachmentDisposition
    document_id: UUID | None = None
    scan_status: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ResolutionSummary:
    """The resolver's decision as a report carries it — never its matched text.

    The §2.7 assessment is the record of what the rule saw, and the text each
    candidate matched on lives in its ``signal_log``, in the customer's own
    database (``app.application.reply_resolver``). This is that same decision
    projected for a reader outside it, so every field is a verdict or an
    identifier: the case the rule reached, the evidence tier behind a
    decision, the score, the §2.7 band it routes to, and the ids of the
    companies the message matched. A staff member reading a refusal can see
    what the rule saw, which is the difference between "this needs a human"
    and "the rule is missing something we can supply".

    ``as_details`` is the same projection in the shape an audit entry's
    ``details`` stores — the entry and the response are built from one
    summary, so they cannot disagree about the decision. The key names differ
    (``evidence``/``candidates`` in an entry, the fuller names on the wire):
    the entry's shape is the one the ingestion slice already writes, and an
    append-only chain is the wrong place to rename a field for readability.
    """

    case: str
    evidence: str | None
    score: float
    routing: str | None
    candidate_ids: tuple[UUID, ...]

    def as_details(self) -> dict[str, object]:
        """The projection in the shape an audit entry stores it."""
        return {
            "case": self.case,
            "evidence": self.evidence,
            "score": self.score,
            "routing": self.routing,
            "candidates": [str(company_id) for company_id in self.candidate_ids],
        }


@dataclass(frozen=True)
class MessageOutcome:
    """What one cycle decided about one message — ids and outcomes only."""

    mailbox_message_id: str
    outcome: IngestOutcome
    reason: str | None = None
    company_id: UUID | None = None
    communication_id: UUID | None = None
    raw_document_id: UUID | None = None
    scan_status: str | None = None
    # The §2.5 quarantine row, when the message could not be attributed: the
    # id a staff member acts on. ``None`` for every other outcome.
    quarantine_id: UUID | None = None
    # The attribution case ADR-0019 §1 or the clarification loop opened, when
    # the message's evidence bound it to an ask instead of an attribution: the
    # row the ask hangs from. ``None`` for every other outcome — including a
    # threaded message the chain attributed (there is nothing to ask), one with
    # no chain that today's flow ran or quarantined, and a B-class closure's
    # closing message (it follows its own path; the case id rode the outcome
    # that opened it).
    attribution_case_id: UUID | None = None
    # The split parent workstream ③ opened for this message (E1's decisive
    # several-match, E2's several-answer handoff): the row the work items
    # hang from. ``reason`` carries a park or failure code when the division
    # did not complete. ``None`` for every other outcome.
    split_parent_id: UUID | None = None
    # What the multi-company resolver saw, whenever it ran: a sender whose
    # address reaches exactly one company (or none) never reaches the rule, so
    # ``None`` here is the honest "the resolver did not run", not a gap.
    resolution: ResolutionSummary | None = None
    attachments: tuple[AttachmentOutcome, ...] = ()


@dataclass(frozen=True)
class CycleReport:
    """One cycle's account of itself: the window it read, and what it did."""

    cursor_received_at: datetime | None
    query: str | None
    examined: int
    ingested: int
    outcomes: tuple[MessageOutcome, ...] = ()


async def run_cycle(
    session: AsyncSession,
    *,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID | None = None,
    now: datetime | None = None,
) -> CycleReport:
    """Read the mailbox once, from the tenant's cursor, and ingest what is new.

    ``tenant_id`` is the actor's tenant when an administrator triggers the
    cycle and resolved locally when the background loop runs it: a deployment
    is single-tenant (ADR-0002), so "the tenant" is the one row the local
    ``tenants`` table holds.

    Raises ``mail.MailNotConnected`` when the deployment has no mailbox to
    read — no connected credential and no configured IMAP settings — and
    ``NoLocalTenantError`` when this deployment cannot name its tenant; the
    provider's own typed errors and ``MailResponseInvalid`` propagate: a
    mailbox that cannot be read is not a cycle that found nothing, and the API
    and the loop each report it as what it is.
    """
    at = now if now is not None else datetime.now(UTC)
    mailbox = await mail.reader(now=at)
    if tenant_id is None:
        tenant_id = await _resolve_local_tenant(session)
    cursor = await _cursor(session, tenant_id=tenant_id)
    listing = await mailbox.list_message_ids(
        cursor=cursor,
        max_results=settings.mail_ingest_max_messages,
    )

    outcomes: list[MessageOutcome] = []
    newest = cursor
    # Oldest first (the reader's own order): the cursor only ever moves
    # forward, so a cycle that is cut short (the per-cycle bound, or a
    # provider failure) has advanced it over exactly the messages it finished
    # and never over one it did not reach.
    for message_id in listing.message_ids:
        examined = await examine_message(
            session,
            store,
            scanner,
            at=at,
            tenant_id=tenant_id,
            mailbox=mailbox,
            message_id=message_id,
        )
        outcomes.append(examined.outcome)
        if examined.received_at is not None and (newest is None or examined.received_at > newest):
            newest = examined.received_at
    await _advance_cursor(session, tenant_id=tenant_id, cursor=newest)
    return CycleReport(
        cursor_received_at=newest,
        query=listing.query,
        examined=len(outcomes),
        ingested=sum(1 for item in outcomes if item.outcome is IngestOutcome.INGESTED),
        outcomes=tuple(outcomes),
    )


@dataclass(frozen=True)
class ExaminedMessage:
    """One message's outcome, plus the cursor position it moves to."""

    outcome: MessageOutcome
    received_at: datetime | None = None


async def examine_message(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    mailbox: mail.MailboxReader,
    message_id: str,
) -> ExaminedMessage:
    """Decide and act on one message (the cycle's whole per-message order).

    The cycle calls this once per message it lists, and the §2.5 review
    surface (``app.application.quarantine``) calls it for the one provider
    message id staff are retrying: the order below is one function, so the
    retry can never drift from what a cycle does with the same message.

    ``at`` is the decision's day — the date the resolver reads the master data
    as of — and ``mailbox`` is the leg the message is read through, whose
    ``address`` is the connected mailbox: that address is what makes a message
    from it ``self_sent`` rather than a client reply.
    """
    if await _recorded(session, tenant_id=tenant_id, message_id=message_id):
        return ExaminedMessage(
            outcome=MessageOutcome(
                mailbox_message_id=message_id, outcome=IngestOutcome.SKIPPED_KNOWN
            )
        )
    metadata = await mailbox.fetch_message_metadata(message_id=message_id)
    outcome = await _ingest(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        mailbox=mailbox,
        metadata=metadata,
    )
    return ExaminedMessage(outcome=outcome, received_at=metadata.received_at)


@dataclass(frozen=True)
class OutboundEvidence:
    """The tenant's outbound exchanges, indexed for the two thread questions.

    One read of the outbound ``Communication`` rows per examined message
    (ruling R1), shaped into the four mappings ``match_outbound`` matches
    against: the message-id and thread-id signals, asked once about the
    company (every outbound row — the chain the thread binds) and once about
    the confirmation request (request-linked rows only — the §5.1.12 join).
    A signal whose rows name more than one value is left out of the index:
    it names no single exchange, which is the exactly-one rule's own answer —
    and the safe one, because a first-wins collapse would bind the message to
    whichever row a query happened to return first.
    """

    company_by_message_id: dict[str, UUID]
    company_by_thread_id: dict[str, UUID]
    request_by_message_id: dict[str, UUID]
    request_by_thread_id: dict[str, UUID]


async def _ingest(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    mailbox: mail.MailboxReader,
    metadata: MessageMetadata,
) -> MessageOutcome:
    """Everything that happens to one accepted-looking message."""
    message_id = metadata.message_id
    sender = sender_address(metadata.sender)
    if sender is None:
        return MessageOutcome(
            mailbox_message_id=message_id, outcome=IngestOutcome.FAILED, reason="sender_missing"
        )
    if normalized_address(sender) == normalized_address(mailbox.address):
        # Our own outbound mail: the sending slice records what this deployment
        # sends, and the mailbox must not read it back as a client reply.
        return MessageOutcome(mailbox_message_id=message_id, outcome=IngestOutcome.SELF_SENT)
    # ADR-0019 §1: one read of the tenant's outbound exchanges, two questions
    # asked of it — which company the thread binds the message to (the
    # chain), and which request the message answers (§5.1.12's join). Both
    # are metadata over headers the product already stores.
    evidence = await _outbound_evidence(session, tenant_id=tenant_id)
    chain_company_id = _match_chain_company(
        evidence,
        thread_id=metadata.thread_id,
        in_reply_to=metadata.in_reply_to,
        references=metadata.references,
    )
    raw: bytes | None = None
    resolution: ResolutionOutcome | None = None
    if chain_company_id is not None:
        # The thread binds the company, and the sender is judged as a
        # registered channel of *that* company only (ruling R1): the thread
        # resolves an address that reaches several companies, and an address
        # registered elsewhere is not a channel of this chain.
        sender_is_channel = await contacts.is_channel_for_company(
            session,
            tenant_id=tenant_id,
            company_id=chain_company_id,
            identifier=sender,
        )
        if not sender_is_channel:
            return await _ask(
                session,
                store,
                scanner,
                at=at,
                tenant_id=tenant_id,
                company_id=chain_company_id,
                metadata=metadata,
                sender=sender,
            )
        # A registered channel's message on a chain it is a channel of: it may
        # be the confirming act for cases awaiting one, and it is the remedy
        # the 2.1 cases asked for (ADR-0019 §1, rulings R5/R7). Either way it
        # is then processed like any registered channel's message.
        return await _registered_channel_message(
            session,
            store,
            scanner,
            at=at,
            tenant_id=tenant_id,
            company_id=chain_company_id,
            mailbox=mailbox,
            metadata=metadata,
            sender=sender,
            evidence=evidence,
        )
    else:
        companies = await contacts.companies_for_channel_identifier(
            session, tenant_id=tenant_id, channel_type=ChannelType.EMAIL, identifier=sender
        )
        # The clarification loop's anchor (workstream ②): before the plain
        # path decides, a message carrying an open case's anchor is answered
        # against that case — the A-class answer grammar (ruling D6) or the
        # B-class closure (ruling D8) — or told the case is still waiting (the
        # bounded re-ask, ruling D7). Cases whose anchor nothing carries fall
        # through to today's flow untouched.
        anchor_cases = await open_cases_for_anchor(
            session,
            tenant_id=tenant_id,
            thread_id=metadata.thread_id,
            rfc_message_ids=(*metadata.in_reply_to, *metadata.references),
        )
        if anchor_cases:
            handled = await _anchor_message(
                session,
                store,
                scanner,
                at=at,
                tenant_id=tenant_id,
                mailbox=mailbox,
                metadata=metadata,
                sender=sender,
                companies=companies,
                cases=anchor_cases,
                evidence=evidence,
            )
            if handled is not None:
                return handled
        has_anchor = metadata.rfc_message_id is not None
        if not companies:
            # Nothing to decide between: this address is registered against no
            # company at all. ②'s first-contact class replaces the
            # ``UNATTRIBUTED_SENDER`` quarantine with an ask — when the
            # message carries its own RFC ``Message-ID`` (the anchor a case
            # hangs on; without one, no case could ever be found again, and
            # ①'s anchor-less rule extends verbatim to the quarantine below).
            action = decide_unthreaded(
                registered_company_count=0, resolver_matched=False, has_anchor=has_anchor
            )
            if action is UnthreadedAction.ASK_FIRST_CONTACT:
                return await _ask_unthreaded(
                    session,
                    store,
                    scanner,
                    at=at,
                    tenant_id=tenant_id,
                    metadata=metadata,
                    sender=sender,
                    action=action,
                )
            return await _refuse(
                session,
                tenant_id=tenant_id,
                metadata=metadata,
                sender=sender,
                outcome=IngestOutcome.UNATTRIBUTED_SENDER,
                reason=QuarantineReason.UNREGISTERED_SENDER,
            )
        if len(companies) > 1:
            # The decision needs the message's own words (scope §2.5 has the
            # company identified from the subject and the body), so this is the
            # path that reads a message the cycle has not accepted yet. What it
            # reads is never *stored* unless the message is ingested below.
            raw = await _fetch_raw(mailbox=mailbox, message_id=message_id)
            resolution = await reply_resolver.resolve_message(
                session,
                tenant_id=tenant_id,
                company_ids=companies,
                subject=metadata.subject,
                text=message_text(raw),
                as_of=at.date(),
            )
            if resolution.company_id is None:
                # The rule could not choose one — and the case names which
                # refusal this is (the two are not interchangeable):
                # ``ambiguous`` is a decisive several-match, workstream ③'s
                # split: the involved set is the resolver's decisive
                # candidates (the sender's own companies), the body this
                # branch already read is what the call divides, and no anchor
                # is required — nothing is asked of the client, and the
                # parent is processed in one pass. ``no_match`` is ②'s
                # which-company class (the ask, when the anchor exists) or
                # today's ``MULTIPLE_COMPANIES`` quarantine, unchanged.
                if resolution.resolution is Resolution.AMBIGUOUS:
                    return await _split_ambiguous(
                        session,
                        store,
                        scanner,
                        at=at,
                        tenant_id=tenant_id,
                        metadata=metadata,
                        sender=sender,
                        raw=raw,
                        resolution=resolution,
                    )
                action = decide_unthreaded(
                    registered_company_count=len(companies),
                    resolver_matched=False,
                    has_anchor=has_anchor,
                )
                if action is UnthreadedAction.ASK_CLARIFICATION:
                    return await _ask_unthreaded(
                        session,
                        store,
                        scanner,
                        at=at,
                        tenant_id=tenant_id,
                        metadata=metadata,
                        sender=sender,
                        action=action,
                        resolution=resolution,
                    )
                return await _refuse(
                    session,
                    tenant_id=tenant_id,
                    metadata=metadata,
                    sender=sender,
                    outcome=IngestOutcome.AMBIGUOUS_SENDER,
                    reason=QuarantineReason.MULTIPLE_COMPANIES,
                    resolution=resolution,
                )
            company_id = resolution.company_id
        else:
            company_id = companies[0]
    return await _ingest_for_company(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox=mailbox,
        metadata=metadata,
        sender=sender,
        evidence=evidence,
        raw=raw,
        resolution=resolution,
    )


async def _ingest_for_company(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    company_id: UUID,
    mailbox: mail.MailboxReader,
    metadata: MessageMetadata,
    sender: str,
    evidence: OutboundEvidence,
    raw: bytes | None = None,
    resolution: ResolutionOutcome | None = None,
    kind: CommunicationKind = CommunicationKind.REPLY,
    actor_user_id: UUID | None = None,
) -> MessageOutcome:
    """Everything that happens to one message once its company is fixed.

    Steps 4 to 6 of the module's order, and the one tail both callers reach:
    the ingestion cycle hands it the company its attribution step resolved,
    and the case path (``ingest_case_message``, for ``attribution.process_case``)
    hands it the company the case carries. ``raw`` is the message's bytes when
    the caller already has them — the multi-company resolver fetched them to
    decide, a confirmation fetched them to read the grammar, and ordinary
    processing reuses them rather than asking the provider twice — and ``None``
    when this tail is the first to need them. ``kind`` is the exchange's role
    (``reply`` unless a registered channel's confirmation is being stored).

    ``actor_user_id`` is ``None`` for the system actor every cycle runs as,
    and the pressing staff member when a case is processed from the surface;
    it travels to the documents and the audit entry this tail writes.
    """
    message_id = metadata.message_id
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        automation_type=AutomationType.EMAIL_INGESTION,
        as_of=at.date(),
    )
    if not decision.allowed:
        assert decision.reason is not None  # denied decisions always carry one
        return MessageOutcome(
            mailbox_message_id=message_id,
            outcome=IngestOutcome.DENIED,
            reason=decision.reason.value,
            company_id=company_id,
            # The rule ran and decided; the gate refused the company it
            # decided on. The decision is reported even though a denial
            # writes no assessment to carry it — the reader still needs to
            # know the refusal was not a failure to attribute.
            resolution=None if resolution is None else _resolution_summary(resolution),
        )

    if raw is None:
        raw = await _fetch_raw(mailbox=mailbox, message_id=message_id)
    try:
        raw_document = await documents.register_upload(
            session,
            store,
            tenant_id=tenant_id,
            company_id=company_id,
            actor_user_id=actor_user_id,
            kind=DocumentKind.CLIENT_REPLY,
            origin=DocumentOrigin.EMAIL_ATTACHMENT,
            content_type=MESSAGE_CONTENT_TYPE,
            data=raw,
            scanner=scanner,
        )
    except (UnsupportedContentTypeError, ContentTypeMismatchError, UploadTooLargeError) as exc:
        return MessageOutcome(
            mailbox_message_id=message_id,
            outcome=IngestOutcome.FAILED,
            reason=f"invalid_message:{_MESSAGE_REFUSALS[type(exc)]}",
            company_id=company_id,
            # The bytes were unusable, but the decision was made: with the
            # resolver's evidence on the message, a report that dropped it
            # would leave the reader unable to tell that apart from a message
            # nothing could attribute.
            resolution=None if resolution is None else _resolution_summary(resolution),
        )

    parsed = parse_inbound_message(raw)
    attachments = [
        await _store_attachment(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            company_id=company_id,
            attachment=part,
            actor_user_id=actor_user_id,
        )
        for part in parsed.attachments
    ]
    if parsed.attachment_count > len(parsed.attachments):
        # Reported, not dropped: the message carried more parts than the cap
        # exists to bound, and the record has to say so.
        attachments.append(
            AttachmentOutcome(
                disposition=AttachmentDisposition.REFUSED, reason=_TOO_MANY_ATTACHMENTS
            )
        )
    # The deterministic reply join (§5.1.12): which request, if any, this
    # message answers — the same evidence read the attribution branch used.
    # It rides on the same row the cycle is about to write — the join is
    # metadata over headers the product already stores, decided before the
    # Communication exists, and recorded on it. No audit entry: the row is
    # the record.
    matched_request_id = _match_reply_request(
        evidence,
        thread_id=metadata.thread_id,
        in_reply_to=metadata.in_reply_to,
        references=metadata.references,
    )
    row = await communications.record_inbound_message(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox_message_id=message_id,
        thread_id=metadata.thread_id,
        sender_address=sender,
        received_at=metadata.received_at,
        raw_document_id=raw_document.id,
        confirmation_request_id=matched_request_id,
        kind=kind,
    )
    details: dict[str, object] = {
        "mailbox_message_id": message_id,
        "thread_id": metadata.thread_id,
        "sender_address": sender,
        "raw_document_id": str(raw_document.id),
        "raw_scan_status": raw_document.scan_status,
        "attachments": [
            {
                "document_id": str(item.document_id) if item.document_id else None,
                "disposition": item.disposition.value,
                "scan_status": item.scan_status,
                "reason": item.reason,
            }
            for item in attachments
        ],
    }
    summary: ResolutionSummary | None = None
    if resolution is not None:
        # The resolver's §2.7 record, attached to the Communication it
        # decided, and its summary on this message's one audit entry — no
        # second entry for the assessment. The entry and the report below are
        # both built from ``summary``: one code path, so they cannot disagree.
        await reply_resolver.record_assessment(
            session,
            tenant_id=tenant_id,
            subject_type=AssessmentSubject.COMMUNICATION,
            subject_id=row.id,
            outcome=resolution,
        )
        summary = _resolution_summary(resolution)
        details["resolution"] = summary.as_details()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.MAIL_MESSAGE_INGESTED,
        actor_user_id=actor_user_id,
        company_id=company_id,
        entity_type="communication",
        entity_id=row.id,
        details=details,
    )
    return MessageOutcome(
        mailbox_message_id=message_id,
        outcome=IngestOutcome.INGESTED,
        company_id=company_id,
        communication_id=row.id,
        raw_document_id=raw_document.id,
        scan_status=raw_document.scan_status,
        resolution=summary,
        attachments=tuple(attachments),
    )


async def ingest_case_message(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    company_id: UUID,
    mailbox_message_id: str,
    actor_user_id: UUID | None = None,
) -> MessageOutcome:
    """Re-ingest one case's original message, into the case's own company.

    The case path's entry into the tail above (ruling R6): the original's
    provider message id and the company the case fixed, with the envelope and
    the bytes re-read from the mailbox — the same leg the cycle reads through
    — because the ``Communication`` row carries the message's own receipt
    time and thread headers. ``attribution.process_case`` reaches this for
    every case a confirmation authorises and for the staff-forced processing
    route, so the tail is one code path either way: one order, one set of
    writes, one audit entry.

    The idempotency key comes first, as it does for every message: a provider
    message id that already has a ``Communication`` — the §2.5 retry that
    ingested the original after its sender was registered, say — is not
    written twice. Its row is returned as ``skipped_known`` **with its id**,
    which is what lets the case resolve to the exchange the original already
    became.
    """
    recorded = await _recorded_communication(
        session, tenant_id=tenant_id, message_id=mailbox_message_id
    )
    if recorded is not None:
        return MessageOutcome(
            mailbox_message_id=mailbox_message_id,
            outcome=IngestOutcome.SKIPPED_KNOWN,
            company_id=company_id,
            communication_id=recorded,
        )
    mailbox = await mail.reader(now=at)
    metadata = await mailbox.fetch_message_metadata(message_id=mailbox_message_id)
    sender = sender_address(metadata.sender)
    if sender is None:
        return MessageOutcome(
            mailbox_message_id=mailbox_message_id,
            outcome=IngestOutcome.FAILED,
            reason="sender_missing",
            company_id=company_id,
        )
    evidence = await _outbound_evidence(session, tenant_id=tenant_id)
    return await _ingest_for_company(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox=mailbox,
        metadata=metadata,
        sender=sender,
        evidence=evidence,
        actor_user_id=actor_user_id,
    )


async def _registered_channel_message(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    company_id: UUID,
    mailbox: mail.MailboxReader,
    metadata: MessageMetadata,
    sender: str,
    evidence: OutboundEvidence,
) -> MessageOutcome:
    """A registered channel's message on the chain's thread (R5/R7).

    Two questions are asked of one read of the anchor, and the message is then
    processed like any other from a registered channel of the company — the
    one thing the thread changes is that the message may also *act*:

    - **Is it a confirmation?** When a case on the anchor awaits one, the
      message's bytes are fetched just in time and its first non-empty line —
      read by ``first_body_line``, which keeps the lines ``message_text``
      collapses — is the grammar's
      (``app.domain.attribution.is_confirmation``). A confirmed
      act stores the exchange as ``attribution_confirmation``, pins it to the
      cases and processes every open awaiting case on the anchor.
    - **Is it the 2.1 remedy?** Any *other* message from a registered channel
      on the thread closes the cases still ``awaiting_registered_channel`` —
      the ask invited exactly this message (ruling R7) — and cancels their
      asks. The closure is the ordinary path's alone (ruling M2): the act has
      already attempted every open awaiting case on the anchor, so running the
      closure behind it would close a 2.1 case its processing could not take
      (a gate denial, a fetch failure) and strand the parked original, where
      R6's failure semantics and the case surface's retry keep it awaiting.

    The raw fetch happens only for the grammar, and only when some case
    actually awaits a confirmation; ordinary processing reuses the bytes
    rather than asking the provider twice. Nothing else of the message is read
    before the tail accepts it.
    """
    cases = await open_cases_for_thread(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        thread_id=metadata.thread_id,
        rfc_message_ids=(*metadata.in_reply_to, *metadata.references),
    )
    raw: bytes | None = None
    if any(case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value for case in cases):
        raw = await _fetch_raw(mailbox=mailbox, message_id=metadata.message_id)
        if is_confirmation(first_body_line(raw)):
            return await _confirm_cases(
                session,
                store,
                scanner,
                at=at,
                tenant_id=tenant_id,
                company_id=company_id,
                mailbox=mailbox,
                metadata=metadata,
                sender=sender,
                evidence=evidence,
                cases=cases,
                raw=raw,
            )
    outcome = await _ingest_for_company(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox=mailbox,
        metadata=metadata,
        sender=sender,
        evidence=evidence,
        raw=raw,
    )
    await attribution.close_registration_cases(
        session, tenant_id=tenant_id, cases=cases, now=at
    )
    return outcome


async def _confirm_cases(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    company_id: UUID,
    mailbox: mail.MailboxReader,
    metadata: MessageMetadata,
    sender: str,
    evidence: OutboundEvidence,
    cases: Sequence[AttributionCase],
    raw: bytes,
) -> MessageOutcome:
    """Store the confirming exchange and process the cases it authorises.

    In order: the confirming message's own row (the ordinary tail with
    ``kind=attribution_confirmation`` — the §5.1 gate applies to storing it
    exactly as it applies to any message), the pin of that exchange to every
    case still awaiting a confirmation, the processing of **every** open
    awaiting case on the anchor (ruling R5), and one ``attribution_confirmed``
    entry naming the company, the stored exchange and the cases with what each
    resolved to. The entry is written whether or not the gate let the message
    be stored and whether or not the processing succeeded: the act is the
    authority, and the report is what says the rest. The outcome is
    ``attribution_confirmed``, carrying the tail's own reason when the gate
    denied the storage — the reader then knows why nothing was written.

    The confirming message is not parsed as a reply: it is recorded as
    ``attribution_confirmation``, and the parse cycle's listing leaves that
    kind out.
    """
    stored = await _ingest_for_company(
        session,
        store,
        scanner,
        at=at,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox=mailbox,
        metadata=metadata,
        sender=sender,
        evidence=evidence,
        raw=raw,
        kind=CommunicationKind.ATTRIBUTION_CONFIRMATION,
    )
    for case in cases:
        if case.status != AttributionCaseStatus.AWAITING_CONFIRMATION.value:
            continue
        await attribution.confirm_case(
            session,
            tenant_id=tenant_id,
            case=case,
            confirmation_communication_id=stored.communication_id,
        )
    resolved: list[tuple[AttributionCase, UUID | None]] = []
    for case in cases:
        report = await attribution.process_case(
            session, store, scanner, tenant_id=tenant_id, case=case, now=at
        )
        resolved.append((case, report.communication_id if report.resolved else None))
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CONFIRMED,
        actor_user_id=None,
        company_id=company_id,
        details={
            "company_id": str(company_id),
            "confirmation_communication_id": (
                None if stored.communication_id is None else str(stored.communication_id)
            ),
            "cases": [
                {
                    "case_id": str(case.id),
                    "communication_id": (
                        None if communication_id is None else str(communication_id)
                    ),
                }
                for case, communication_id in resolved
            ],
        },
    )
    return replace(
        stored,
        outcome=IngestOutcome.ATTRIBUTION_CONFIRMED,
        attribution_case_id=None,
    )


async def _ask(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    company_id: UUID,
    metadata: MessageMetadata,
    sender: str,
) -> MessageOutcome:
    """Park a threaded message whose sender is not the chain's registered channel.

    ADR-0019 §1's registration/confirmation branch: the thread evidence bound
    the company and the sender is not one of its registered channels, so the
    message opens an attribution case and is asked about rather than
    quarantined or attributed. The decision is metadata-only — ``decide``
    over the sender's status and the registered channels found among the
    To/Cc addresses — and nothing of the message is read or stored here: no
    raw fetch, no document of the message, no ``Communication`` (ruling R2).
    The case is the record, and its ask is composed from the envelope facts
    this function already holds and queued through the same outbox every
    other client letter rides (``attribution.enqueue_ask``) — the one thing
    the awaiting path writes, and an ask that the suppression read, the §5.1
    gate, an anti-abuse header or the tenant's hourly cap keeps off leaves the
    case open with ``ask_sent_at`` NULL for staff.

    A case with **no usable anchor** is not opened. The confirmation read and
    the closure read both find a case by its provider thread id or the
    original's ``Message-ID`` (ADR-0019 §1's two anchors, of which the second
    must fit ``attribution_cases.original_rfc_message_id``,
    ``app.domain.mail``'s ``MAX_RFC_ANCHOR_CHARS``), so a case with neither
    could never be found again: every later cycle's overlap re-read would ask
    again and store another letter. Such a message falls back to the §2.5
    refusal ``unregistered_sender``, which is staff-visible — most often on
    the IMAP leg (``thread_id`` always NULL) when the message's own
    ``Message-ID`` is absent or unusable.
    """
    if metadata.thread_id is None and metadata.rfc_message_id is None:
        return await _refuse(
            session,
            tenant_id=tenant_id,
            metadata=metadata,
            sender=sender,
            outcome=IngestOutcome.UNATTRIBUTED_SENDER,
            reason=QuarantineReason.UNREGISTERED_SENDER,
        )
    registered_in_copy = await _registered_channels_in_copy(
        session, tenant_id=tenant_id, company_id=company_id, metadata=metadata
    )
    decision = decide(
        threaded=True,
        sender_is_channel=False,
        registered_channels_in_copy=registered_in_copy,
    )
    case, _created = await attribution.open_case(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox_message_id=metadata.message_id,
        sender_address=sender,
        action=decision.action,
        thread_id=metadata.thread_id,
        original_rfc_message_id=metadata.rfc_message_id,
    )
    # The 2.1 ask goes back to the sender — the decision carries no recipients
    # for it; the 2.2 ask goes to the registered channels it found on copy.
    # The chain tokens the message carries ride along: on the IMAP leg they
    # are the only evidence that this message has an ancestor case, and the
    # suppression read matches the case's own id against the set.
    await attribution.enqueue_ask(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        case=case,
        recipients=decision.recipients or (sender,),
        rfc_message_ids=(*metadata.in_reply_to, *metadata.references),
        original_subject=metadata.subject or "",
        original_received_at=metadata.received_at,
        auto_submitted=metadata.auto_submitted,
        return_path=metadata.return_path,
        now=at,
    )
    return MessageOutcome(
        mailbox_message_id=metadata.message_id,
        outcome=_AWAITING_OUTCOME[decision.action],
        company_id=company_id,
        attribution_case_id=case.id,
    )


async def _ask_unthreaded(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    metadata: MessageMetadata,
    sender: str,
    action: UnthreadedAction,
    resolution: ResolutionOutcome | None = None,
) -> MessageOutcome:
    """Park an unthreaded message as one of the clarification loop's classes.

    ``decide_unthreaded`` has already chosen the ask, and the caller has
    already established the anchor: the original's own RFC ``Message-ID`` is
    what a reply's ``In-Reply-To``/``References`` carries back, and a case
    without it could never be found again. The case opens company-less
    (ruling D2) and its ask is composed and enqueued through the same one
    path ①'s asks ride (``attribution.enqueue_ask``) — the sender alone is
    the recipient (ruling D8: no thread evidence, no bothering registered
    channels about cold mail, even when To/Cc holds them). The message itself
    is never read or stored (ruling R2 holds for the ask's composition):
    the case is the record, exactly as ①'s asks. ``resolution`` is the
    resolver's verdict for the A-class (the only class whose decision reads
    the body): it rides the outcome the way a refusal's does, so the cycle's
    report keeps what the rule saw.

    The ask rides the outbox like ①'s (migration ``0035`` made the registry
    company-less-capable): a guard or cap that trips leaves the case
    staff-visible with ``ask_sent_at`` NULL — the same visible-but-unasked
    posture ①'s suppressions use (ruling R3).
    """
    case, _created = await attribution.open_case(
        session,
        tenant_id=tenant_id,
        company_id=None,
        mailbox_message_id=metadata.message_id,
        sender_address=sender,
        action=action,
        thread_id=metadata.thread_id,
        original_rfc_message_id=metadata.rfc_message_id,
    )
    await attribution.enqueue_ask(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        case=case,
        recipients=(sender,),
        rfc_message_ids=(*metadata.in_reply_to, *metadata.references),
        original_subject=metadata.subject or "",
        original_received_at=metadata.received_at,
        auto_submitted=metadata.auto_submitted,
        return_path=metadata.return_path,
        now=at,
    )
    return MessageOutcome(
        mailbox_message_id=metadata.message_id,
        outcome=_UNTHREADED_AWAITING_OUTCOME[action],
        attribution_case_id=case.id,
        resolution=None if resolution is None else _resolution_summary(resolution),
    )


async def _split_ambiguous(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    metadata: MessageMetadata,
    sender: str,
    raw: bytes,
    resolution: ResolutionOutcome,
) -> MessageOutcome:
    """Workstream ③'s E1: the resolver's decisive several-match.

    The sender's message names ≥2 of their own companies decisively — the
    split divides it instead of asking or quarantining. The involved set is
    the resolver's decisive candidates (ruling D1: the sender's own, never
    the provider's say); the body this branch already read for the resolver
    is the bytes the call divides; and no anchor is required, because nothing
    is asked of the client — the parent is processed in one pass. The split's
    own guards (the anti-abuse headers, the all-or-nothing pre-flight, the
    grammar) park the parent rather than fail the cycle, and the outcome
    carries the parent id with the park or failure code in ``reason``.
    """
    involved_ids = decisive_candidate_ids(resolution)
    assert len(involved_ids) >= 2  # AMBIGUOUS is a decisive several-match
    references = await _company_references(
        session, tenant_id=tenant_id, company_ids=involved_ids, as_of=at.date()
    )
    report = await split.split_message(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        mailbox_message_id=metadata.message_id,
        sender_address=sender,
        subject=metadata.subject or "",
        received_at=metadata.received_at,
        raw=raw,
        involved=references,
        auto_submitted=metadata.auto_submitted,
        return_path=metadata.return_path,
        daily_splits_for_sender=await split.splits_for_sender_count(
            session, tenant_id=tenant_id, sender_address=sender, at=at
        ),
        now=at,
    )
    return MessageOutcome(
        mailbox_message_id=metadata.message_id,
        outcome=IngestOutcome.SPLIT,
        reason=report.reason,
        split_parent_id=report.parent_id,
        resolution=_resolution_summary(resolution),
    )


async def _anchor_message(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    mailbox: mail.MailboxReader,
    metadata: MessageMetadata,
    sender: str,
    companies: Sequence[UUID],
    cases: Sequence[AttributionCase],
    evidence: OutboundEvidence,
) -> MessageOutcome | None:
    """A message carrying an open case's anchor: the clarification loop's
    three answers (ruling D6/D7/D8).

    The cases are the tenant's open ones on the anchor (``open_cases_for_anchor``),
    oldest first. For each, the message is tried in order:

    - **The A-class answer** — the case awaits a clarification and the sender
      is the case's own: the bytes are fetched just in time and the first
      non-empty line is read (the same just-in-time grammar fetch ① makes for
      the confirm act). Exactly one of the sender's companies named → the case
      resolves (``attribution.resolve_clarification_case`` stamps the company
      and re-runs the ingestion tail for the parked original), and the answer
      itself then lands as ordinary mail for the company it named — the client
      often carries the real instruction in the same message. Zero or several
      → the case stays awaiting and the bounded re-ask is driven by the same
      enqueue the first ask rode: one ask, one re-ask, then never again
      (ruling D7).
    - **The B-class closure** — the case awaits a registered channel (the
      company-less 2.1 variant) and the sender is a registered channel of any
      company: the remedy Letter B asked for has arrived. Every company-less
      such case closes (its pending ask cancelled), and the message is **not
      consumed** — it returns ``None`` and follows its own plain-path fate,
      because it is ordinary registered-sender mail with its own company to
      reach (ruling D8).

    Nothing matched → ``None``: the message opens a new case or follows
    today's flow, and neither the A-case of another sender nor the company-bound
    2.1 cases of ① are touched here.
    """
    for case in cases:
        if case.status != AttributionCaseStatus.AWAITING_CLARIFICATION.value:
            continue
        if normalized_address(sender) != normalized_address(case.sender_address):
            continue
        raw = await _fetch_raw(mailbox=mailbox, message_id=metadata.message_id)
        references = await _company_references(
            session, tenant_id=tenant_id, company_ids=companies, as_of=at.date()
        )
        result = await attribution.resolve_clarification_case(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            case=case,
            first_line=first_body_line(raw),
            companies=references,
            now=at,
        )
        if result.handoff:
            # E2's several-answer: the case closed with the handoff named in
            # its audit; the parked original — never stored, so the bytes
            # are fetched now, the way the forced processing re-reads it —
            # hands to the split with the matched set as the involved set.
            original_metadata = await mailbox.fetch_message_metadata(
                message_id=case.mailbox_message_id
            )
            original_raw = await _fetch_raw(
                mailbox=mailbox, message_id=case.mailbox_message_id
            )
            references = await _company_references(
                session,
                tenant_id=tenant_id,
                company_ids=result.matched_company_ids,
                as_of=at.date(),
            )
            report = await split.split_message(
                session,
                store,
                scanner,
                tenant_id=tenant_id,
                mailbox_message_id=case.mailbox_message_id,
                sender_address=case.sender_address,
                subject=original_metadata.subject or "",
                received_at=original_metadata.received_at,
                raw=original_raw,
                involved=references,
                auto_submitted=metadata.auto_submitted,
                return_path=metadata.return_path,
                daily_splits_for_sender=await split.splits_for_sender_count(
                    session, tenant_id=tenant_id, sender_address=sender, at=at
                ),
                now=at,
            )
            return MessageOutcome(
                mailbox_message_id=metadata.message_id,
                outcome=IngestOutcome.SPLIT,
                reason=report.reason,
                attribution_case_id=case.id,
                split_parent_id=report.parent_id,
            )
        if result.resolved:
            assert result.company_id is not None  # a resolution names its company
            return await _ingest_for_company(
                session,
                store,
                scanner,
                at=at,
                tenant_id=tenant_id,
                company_id=result.company_id,
                mailbox=mailbox,
                metadata=metadata,
                sender=sender,
                evidence=evidence,
                raw=raw,
            )
        if result.reason != attribution.ANSWER_UNRESOLVED:
            # The answer named a company but the tail refused it (the §5.1
            # gate, an unusable original): the case stays awaiting with its
            # company stamped — the staff surface's retry is its road, and a
            # re-ask would be ①'s letter, not this case's.
            return MessageOutcome(
                mailbox_message_id=metadata.message_id,
                outcome=IngestOutcome.AWAITING_CLARIFICATION,
                reason=result.reason,
                company_id=result.company_id,
                attribution_case_id=case.id,
            )
        ask_report = await attribution.enqueue_ask(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            case=case,
            recipients=(sender,),
            rfc_message_ids=(*metadata.in_reply_to, *metadata.references),
            original_subject=metadata.subject or "",
            original_received_at=metadata.received_at,
            auto_submitted=metadata.auto_submitted,
            return_path=metadata.return_path,
            now=at,
        )
        return MessageOutcome(
            mailbox_message_id=metadata.message_id,
            outcome=IngestOutcome.AWAITING_CLARIFICATION,
            reason=ask_report.reason,
            attribution_case_id=case.id,
        )
    if companies:
        await attribution.close_first_contact_cases(
            session, tenant_id=tenant_id, cases=cases, now=at
        )
    return None


async def _company_references(
    session: AsyncSession, *, tenant_id: UUID, company_ids: Sequence[UUID], as_of: date
) -> list[CompanyReference]:
    """The answer grammar's input: the sender's own companies, each with the
    name in force and its CR/BRN numbers (the master data as of the decision's
    day). Matching never leaves the sender's set — the grammar resolves the
    case to a company the sender is registered for, and no other."""
    references: list[CompanyReference] = []
    for company_id in company_ids:
        snapshot = await company_master.get_company_master(
            session, tenant_id=tenant_id, company_id=company_id, as_of=as_of
        )
        name = None if snapshot.name is None else (snapshot.name.name_en or snapshot.name.name_zh)
        references.append(
            CompanyReference(
                company_id=snapshot.company.id,
                name=name or "",
                cr_number=snapshot.company.cr_number,
                br_number=snapshot.company.br_number,
            )
        )
    return references


async def _registered_channels_in_copy(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, metadata: MessageMetadata
) -> list[str]:
    """The To/Cc addresses that are registered channels of the company.

    First-seen order, each address judged once: the list ``decide`` turns
    into the confirmation ask's recipients. The addresses arrive in their
    comparison form (``app.domain.communications.addresses_in_header``), so
    the channel read here and the decision's own normalization agree.
    """
    channels: list[str] = []
    seen: set[str] = set()
    for address in (*metadata.recipients_to, *metadata.recipients_cc):
        if not address or address in seen:
            continue
        seen.add(address)
        if await contacts.is_channel_for_company(
            session, tenant_id=tenant_id, company_id=company_id, identifier=address
        ):
            channels.append(address)
    return channels


async def _outbound_evidence(session: AsyncSession, *, tenant_id: UUID) -> OutboundEvidence:
    """Read the tenant's outbound exchanges once, indexed for both calls.

    Every outbound row contributes its ``mailbox_message_id`` and
    ``thread_id`` to the company question; the request-linked ones contribute
    the same two signals to the request question. Values are collected per
    key, and ``_single_values`` keeps only the keys that name exactly one —
    an ambiguous signal is dropped rather than resolved by row order.
    """
    rows = await session.execute(
        select(
            Communication.mailbox_message_id,
            Communication.thread_id,
            Communication.company_id,
            Communication.confirmation_request_id,
        ).where(
            Communication.tenant_id == tenant_id,
            Communication.direction == Direction.OUTBOUND.value,
        )
    )
    companies_by_message_id: dict[str, set[UUID]] = {}
    companies_by_thread_id: dict[str, set[UUID]] = {}
    requests_by_message_id: dict[str, set[UUID]] = {}
    requests_by_thread_id: dict[str, set[UUID]] = {}
    for mailbox_message_id, thread_id, company_id, request_id in rows.all():
        if mailbox_message_id is not None:
            companies_by_message_id.setdefault(mailbox_message_id, set()).add(company_id)
            if request_id is not None:
                requests_by_message_id.setdefault(mailbox_message_id, set()).add(request_id)
        if thread_id is not None:
            companies_by_thread_id.setdefault(thread_id, set()).add(company_id)
            if request_id is not None:
                requests_by_thread_id.setdefault(thread_id, set()).add(request_id)
    return OutboundEvidence(
        company_by_message_id=_single_values(companies_by_message_id),
        company_by_thread_id=_single_values(companies_by_thread_id),
        request_by_message_id=_single_values(requests_by_message_id),
        request_by_thread_id=_single_values(requests_by_thread_id),
    )


def _single_values(index: dict[str, set[UUID]]) -> dict[str, UUID]:
    """The keys naming exactly one value, as a plain mapping for the matcher."""
    return {
        key: next(iter(values)) for key, values in index.items() if len(values) == 1
    }


def _match_chain_company(
    evidence: OutboundEvidence,
    *,
    thread_id: str | None,
    in_reply_to: tuple[str, ...],
    references: tuple[str, ...],
) -> UUID | None:
    """The company the message's thread evidence names, or ``None``.

    ADR-0019 §1's chain call (ruling R1): the value is the outbound row's
    ``company_id``, drawn from every stored outbound exchange, and exactly
    one distinct company must match the message's three signals. Zero (not
    threaded) and several (ambiguous evidence) alike return ``None``, and the
    caller then runs today's flow. The evidence was already read; nothing
    here touches a database.
    """
    return match_outbound(
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        message_id_to_value=evidence.company_by_message_id,
        thread_id_to_value=evidence.company_by_thread_id,
    )


def _match_reply_request(
    evidence: OutboundEvidence,
    *,
    thread_id: str | None,
    in_reply_to: tuple[str, ...],
    references: tuple[str, ...],
) -> UUID | None:
    """The request an inbound message answers, by deterministic metadata match.

    The §5.1.12 join, unchanged in meaning: provider ``thread_id`` equality,
    or RFC 5322 ``In-Reply-To``/``References`` carrying a stored outbound
    ``mailbox_message_id`` (the ``Message-ID`` the deployment stamped on the
    SMTP leg, the provider id on the Gmail one). The candidate maps come from
    the tenant's **outbound** rows only — the request-linked ones, value =
    ``confirmation_request_id`` — and the pure rule
    (``app.domain.reminders.match_outbound``) refuses zero or more-than-one
    distinct matches: a guessed join would end the wrong chain. This is
    metadata processing over headers the product already stores, not reply
    parsing; the parser (M5b) later enriches the same rows.
    """
    return match_outbound(
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        message_id_to_value=evidence.request_by_message_id,
        thread_id_to_value=evidence.request_by_thread_id,
    )


async def _store_attachment(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    company_id: UUID,
    attachment: MessageAttachment,
    actor_user_id: UUID | None = None,
) -> AttachmentOutcome:
    """Store one attachment, or refuse it by code (the message still lands).

    An attachment outside the registry's allowed types is refused rather than
    stored as something it is not; it stays in the mailbox, which is where the
    TCSP's own copy lives (ADR-0005). ``actor_user_id`` rides to the document
    registry's own audit entry — ``None`` is the system actor a cycle runs as,
    and the pressing staff member when a case is processed from the surface.
    """
    try:
        document = await documents.register_upload(
            session,
            store,
            tenant_id=tenant_id,
            company_id=company_id,
            actor_user_id=actor_user_id,
            kind=DocumentKind.CLIENT_DOCUMENT,
            origin=DocumentOrigin.EMAIL_ATTACHMENT,
            content_type=attachment.content_type,
            data=attachment.data,
            retention_class=RetentionClass.CLIENT_COMMUNICATION,
            scanner=scanner,
        )
    except UnsupportedContentTypeError:
        return AttachmentOutcome(
            disposition=AttachmentDisposition.REFUSED, reason=_UNSUPPORTED_CONTENT_TYPE
        )
    except ContentTypeMismatchError:
        return AttachmentOutcome(
            disposition=AttachmentDisposition.REFUSED, reason=_CONTENT_TYPE_MISMATCH
        )
    except UploadTooLargeError:
        return AttachmentOutcome(disposition=AttachmentDisposition.REFUSED, reason=_TOO_LARGE)
    return AttachmentOutcome(
        disposition=AttachmentDisposition.STORED,
        document_id=document.id,
        scan_status=document.scan_status,
    )


async def _refuse(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    metadata: MessageMetadata,
    sender: str,
    outcome: IngestOutcome,
    reason: QuarantineReason,
    resolution: ResolutionOutcome | None = None,
) -> MessageOutcome:
    """Record a message that cannot be attributed, and report the refusal.

    The §2.5 quarantine row is what staff work from; ``resolution`` is the
    resolver's decision when one ran (the multi-company case, the only case
    whose bytes were read), and it is written as that message's §2.7
    assessment. Nothing is scanned or stored either way — the mail stays in
    the mailbox (ADR-0005).

    One quarantine row and one audit entry per message, ever: a later cycle
    that re-reads the message (the cursor's overlap does that by design)
    finds the row instead of writing a second one, and the entry records the
    quarantine rather than the examination.
    """
    row, created = await _quarantine(
        session, tenant_id=tenant_id, metadata=metadata, sender=sender, reason=reason
    )
    summary = None if resolution is None else _resolution_summary(resolution)
    if created:
        details: dict[str, object] = {
            "mailbox_message_id": metadata.message_id,
            "thread_id": metadata.thread_id,
            "sender_address": sender,
            "reason": reason.value,
        }
        if resolution is not None and summary is not None:
            await reply_resolver.record_assessment(
                session,
                tenant_id=tenant_id,
                subject_type=AssessmentSubject.QUARANTINE,
                subject_id=row.id,
                outcome=resolution,
            )
            details["resolution"] = summary.as_details()
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.MAIL_MESSAGE_QUARANTINED,
            actor_user_id=None,
            company_id=None,
            entity_type="quarantined_message",
            entity_id=row.id,
            details=details,
        )
    return MessageOutcome(
        mailbox_message_id=metadata.message_id,
        outcome=outcome,
        reason=reason.value,
        quarantine_id=row.id,
        # Reported whether or not this examination is the one that wrote the
        # row: the refusal is the decision made *now*, and a retry reports
        # what the rule reaches today (the row's stored reason is history).
        resolution=summary,
    )


async def _quarantine(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    metadata: MessageMetadata,
    sender: str,
    reason: QuarantineReason,
) -> tuple[QuarantinedMessage, bool]:
    """The tenant's quarantine row for one message, and whether it is new.

    Keyed by the provider message id — the same idempotency key ingestion
    uses, and the same one whose unique index is the backstop: this
    read-then-insert is what makes the ordinary re-run a no-op rather than a
    conflict.

    An existing row is returned as it stands, deliberately: its ``reason``
    and ``received_at`` are the historical facts of why and when that message
    *was* quarantined, not a live state to refresh, and the resolver's new
    decision travels in the report (and in the retry surface's own reason
    when staff act on the row).
    """
    existing = await session.scalar(
        select(QuarantinedMessage).where(
            QuarantinedMessage.tenant_id == tenant_id,
            QuarantinedMessage.mailbox_message_id == metadata.message_id,
        )
    )
    if existing is not None:
        return existing, False
    row = QuarantinedMessage(
        tenant_id=tenant_id,
        mailbox_message_id=metadata.message_id,
        thread_id=metadata.thread_id,
        sender_address=sender,
        received_at=metadata.received_at,
        reason=reason.value,
        status=QuarantineStatus.PENDING.value,
    )
    session.add(row)
    await session.flush()
    return row, True


async def _fetch_raw(*, mailbox: mail.MailboxReader, message_id: str) -> bytes:
    """One message's bytes, exactly as the mailbox hands them over."""
    return await mailbox.fetch_message_raw(message_id=message_id)


def _resolution_summary(outcome: ResolutionOutcome) -> ResolutionSummary:
    """The resolver's decision projected for a report — never the text that matched.

    The case, the evidence tier, the score, the routing band, and the
    candidate company ids. The matched text is a §2.7 diagnostic and stays in
    the assessment's ``signal_log``, in the customer's own database: no audit
    entry, log line, or response carries a fragment of the mail.

    The band comes from ``reply_resolver.band_for`` — the same call the §2.7
    record stores its ``routing`` with — so the entry written beside this
    summary and the report built from it agree by construction. One summary
    per decision, built once by the caller, is what keeps the two from
    drifting: this function is the only place the projection is made.
    """
    return ResolutionSummary(
        case=outcome.resolution.value,
        evidence=None if outcome.evidence is None else outcome.evidence.value,
        score=outcome.score,
        routing=reply_resolver.band_for(outcome),
        candidate_ids=tuple(candidate.company_id for candidate in outcome.candidates),
    )


async def _recorded(session: AsyncSession, *, tenant_id: UUID, message_id: str) -> bool:
    """Whether this mailbox message id is already recorded (the idempotency key)."""
    return (
        await _recorded_communication(session, tenant_id=tenant_id, message_id=message_id)
        is not None
    )


async def _recorded_communication(
    session: AsyncSession, *, tenant_id: UUID, message_id: str
) -> UUID | None:
    """The exchange this mailbox message id already became, or ``None``.

    The idempotency key every mail path shares (the partial unique index is
    its backstop), returned as the row's id rather than a bare "known": a case
    whose original another road ingested resolves to that exchange instead of
    writing a second ``Communication`` for one message.
    """
    found: UUID | None = await session.scalar(
        select(Communication.id).where(
            Communication.tenant_id == tenant_id,
            Communication.mailbox_message_id == message_id,
        )
    )
    return found


async def _cursor(session: AsyncSession, *, tenant_id: UUID) -> datetime | None:
    """The tenant's read position, or ``None`` before the first cycle."""
    return await session.scalar(
        select(MailIngestionState.cursor_received_at).where(
            MailIngestionState.tenant_id == tenant_id
        )
    )


async def _advance_cursor(
    session: AsyncSession, *, tenant_id: UUID, cursor: datetime | None
) -> None:
    """Move the tenant's read position forward, never backward."""
    state = await session.get(MailIngestionState, tenant_id)
    if state is None:
        if cursor is None:
            return
        session.add(MailIngestionState(tenant_id=tenant_id, cursor_received_at=cursor))
        await session.flush()
        return
    current = state.cursor_received_at
    if cursor is not None and (current is None or cursor > current):
        state.cursor_received_at = cursor
        await session.flush()


async def _resolve_local_tenant(session: AsyncSession) -> UUID:
    """The deployment's one tenant, as the command worker resolves it.

    A deployment is single-tenant (ADR-0002): "the tenant" is the one row the
    local ``tenants`` table holds, and a cycle that cannot say whose mailbox
    it is reading ingests nothing and says why.
    """
    tenant_ids = list(
        (await session.scalars(select(Tenant.id).order_by(Tenant.created_at).limit(2))).all()
    )
    if not tenant_ids:
        raise NoLocalTenantError("no_local_tenant")
    if len(tenant_ids) > 1:
        raise NoLocalTenantError("local_tenant_ambiguous")
    return tenant_ids[0]
