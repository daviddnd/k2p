"""The reply parser's pipeline: one stored reply becomes structured
confirmations and change-event proposals (scope §2.5, §7; data-model §2.7;
the M5b slice; contract §5.1.13).

A client reply arrives as free text on a ``Communication``; this module is
what turns it into the structured record the review queue and the
reconfirmer work from. It is the product's **first LLM caller**, and the
rules that make that defensible:

**One gateway call per reply, never batched.** Every model call routes
through the single M6 gateway (``app.application.ai_gateway.infer`` — the
only path that may call an LLM), with ``task_type=reply_parsing`` and the
prompt-binding selector ``language="en"`` (ruling R5: the channel grammar
admits ``en | zh-hk`` only, and the parser prompt is trilingual by
instruction). The *detected* reply language from the model's output is
written to ``communications.language``. The snippet is the minimized
context the counsel-adopted profile admits — the reply text, the checklist
as ``role_reference``, the known officer names as ``officer_name``; the
gateway anchors it on the company row's own BRN, scrubs it, tokenizes the
names against the typed master-data lexicon this module supplies (both
script forms of each officer, the company's own names and its BRN — used as
replacement sources only, never sent), meters it, and books the closed Q1.5
audit facts. None of that is repeated here. The call also declares this
grammar's closed enum vocabulary as the strict restore's
``allowed_literals`` (``PARSE_LITERALS``): a bare enum word is admissible,
every other value the answer returns must be a token from that call's table,
and a value literal the model could not have seen refuses the answer
(``invented_value``) rather than reaching the record.

**The deterministic half is code.** The model's output is judged by
``app.domain.reply_parsing`` and nothing outside the closed grammar is
trusted: a grammar-invalid answer lands exactly one ``staff_review``
assessment — no parse row (there is no valid status to write), no events,
no language fill — and waits for staff. A grammar-valid answer is scored
and routed by the domain rules; the no-guessing rule bends any parse with
an uncompletable proposal to ``client_clarification`` regardless of score,
and that proposal is **not** created as an event — the client is asked,
never guessed at. Ruling R-D is the second bend, and it is a cap: a proposal
naming a natural person the company's appointments do not carry caps an
otherwise-``auto_accept`` run at ``staff_review`` (``registered_names``,
read here beside the lexicon and passed to the domain's one routing rule).

**Landing the candidate proposal.** Complete proposals enter the review
queue through the change-event write path (``app.application.change_events``)
at ``pending``, version 1, ``source_type=communication`` with the reply's
id — the same queue manual entry lands in; staff review is the only gate
between a proposal and the record. A ``withdrawal`` parse moves the matched
earlier event (same type, same party, a status the action can still land
on — the domain matcher refuses zero or several) to ``withdrawn`` through
the executor's external-contact leg, with the reply recorded as the
withdrawal's source; a ``correction`` parse creates the new event carrying
``relation: corrects`` linked to the match. A match that refuses bends the
routing to ``client_clarification`` like any other uncompletable proposal —
a guessed link would withdraw or correct the wrong event.

**Two homes, one session.** The parse run writes the business home
(``reply_parses`` — the parsed confirmations and the proposed event ids)
and the diagnostic home (an ``extraction_assessments`` row,
``component=reply_parser``, ``model`` pinned from the gateway's envelope
facts as ``<model_id>/<prompt_name>@<prompt_version>``) on the caller's
session, plus exactly one ``reply_parsed`` audit entry through the single
write path — ids, status, routing and counts, never spans or bodies. The
assessment's ``signal_log`` carries the parsed proposals and fired grammar
violations: diagnostics, in the customer's own database, the way the
resolver's ``signal_log`` carries the matched text. It is also where R-D's
register read reports itself: whenever a proposal names a person the company's
appointments do not carry, the ``unregistered_person_names`` entry names the
forms at issue — **any** miss, not only the one that capped the band, because
the entry is a diagnostic and the routing is what decides who looks. The
parser never writes the checklist (the M3 generator owns it); the letter a
parse's decision asks for is the reconfirmer's write
(``app.application.reconfirmation``, invoked from this module's landing tail —
Task 4), never this module's.
The same tail fires §4.1's system ``client_reply`` (Task 5): a reply that
joined a request's thread returns every event of the company standing
``client_reconfirming`` to ``pending`` through the change-event write
path, on this same session — the reply is the client's answer arriving,
and staff review exists again.

**Gates and refusals are outcomes, never aborts.** The §5.1 automation
gate (``AutomationType.AI_REPLY_PARSING``) is consulted per company before
any call: a denial reports ``denied:<gating_reason>`` and parses nothing.
The gateway's typed errors map to per-communication outcomes — a
pre-flight ``InferenceRefused`` means nothing transited and nothing was
written (``refused:<reason>``; the register and flags are the record), an
``InferenceUnavailable`` means the gateway already booked the failed
attempt (``unavailable``; the parser adds nothing of its own), and a
``RedactionRefused`` refusal is the same shape whether it came from the
compose (before the attempt, nothing sent) or from the answer's strict
restore — an ``invented_value``, where the snippet left and the attempt is
audited but the model returned a value or placeholder nothing can account
for (``refused:<reason>``; the reply waits for staff). The reconfirmer's
typed errors are outcomes the
same way: a letter the landing tail cannot compose or enqueue reports
``letter_failed:<reason>`` for that message alone — the parse and its
proposals stand, the letter waits for staff's re-ask (the review queue's
send-for-reconfirmation), and the cycle finishes the replies behind it.
One message's outcome never raises into the cycle: ``run_cycle`` reports
every message and finishes.

**The resolver's model fallback** (the M5a-designed answer for the
multi-company messages the deterministic rule cannot call, writing the
assessment pair ``raw_model_resolution``/``enforced_resolution``) is named
here and deliberately not built: the honest hook does not exist yet — the
gateway's four task types have no multi-company resolution binding (the
counsel-adopted Q1.2 profile table cannot grow without a contract change),
``infer`` is scoped to one company while the fallback decides among the
sender's own several, and the contract leaves the raw-vs-enforced
reconciliation rule open. Building it needs a controller ruling, not a
guess; the columns wait, NULL, until then.

**Overlapping parses serialize.** Every entry point — the cycle and a
direct single-message parse — begins by taking a transaction-scoped
advisory lock keyed to the tenant, before any read or write. Without it,
two overlapping cycles (the background loop plus an administrator's press,
or two of either) both list the same un-parsed replies under READ
COMMITTED, both pass the already-parsed check, both call the gateway, and
both insert parse rows and land change-event proposals: duplicate
review-queue entries, two audit chains for one reply, and a wasted model
call. The lock makes the second transaction wait and re-derive from the
winner's committed rows: a losing cycle lists nothing and parses nothing,
and a reply met twice meets the ``skipped:already_parsed`` outcome. The
key's low word hashes the tenant id's first four bytes, so two tenants
whose ids collide serialize spuriously — a harmless one-time wait, the
reminders sweep's stated residual. The lock stacks within one transaction
(a cycle holds it while its per-message parses re-acquire it), so the
cycle never deadlocks against itself.

**Custody** is the M4/M5a rule this module joins: evidence spans live in
``reply_parses.confirmations`` and the assessment's ``signal_log`` (the
customer's own database), the reply body lives on the WORM document, and
no audit entry, log line, or report carries a span, a body, or a planted
marker — coordinates only.

**The reads** (``latest_parse``, ``latest_parses``) serve the per-company
communications surface: the newest parse row of one reply, or the newest
per reply for the list. They carry the same custody bar as the cycle's
report — status, language, routing, the ``fact_id``/``answer`` join and
the proposed event ids — and nothing else; the spans the rows hold never
reach an API response.

Services flush but never commit — the API session dependency commits at
the request edge, and the cycle's transaction is the caller's.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.contacts import Person
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import SplitWorkItem
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.reply_parsing import ReplyParse
from app.application import (
    ai_gateway,
    audit,
    change_events,
    company_master,
    officers,
    reconfirmation,
    service_packages,
)
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.application.split import SplitWorkItemStatus
from app.domain.ai_gateway import TaskType
from app.domain.audit import AuditAction
from app.domain.change_events import (
    ChangeEventRelation,
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
)
from app.domain.communications import CommunicationKind, Direction, message_text
from app.domain.confidence import ConfidenceBand, overall_score
from app.domain.documents import ScanStatus, is_usable
from app.domain.prior_filing import AssessmentComponent
from app.domain.redaction import RedactionRefused
from app.domain.reply_parsing import (
    PARSE_LITERALS,
    EarlierEvent,
    ReplyLanguage,
    ReplyStatus,
    field_scores,
    joined_english_name,
    match_earlier_event,
    parse_violations,
    payload_proposal,
    proposal_violations,
    route_parse,
    unregistered_person_names,
)
from app.domain.service_packages import AutomationType

# The prompt-binding selector (ruling R5): the channel grammar admits
# ``en | zh-hk`` only, and the parser prompt itself is trilingual by
# instruction — the reply's detected language comes back in the parse and
# is written to the Communication, never sent outbound as the binding.
_PROMPT_LANGUAGE = "en"

# The outcome vocabulary's prefixes. Outcomes are bounded machine strings:
# ``parsed`` | ``invalid`` | ``unavailable`` | ``refused:<closed reason>`` |
# ``denied:<gating reason>`` | ``skipped:<reason>`` |
# ``letter_failed:<closed reason>`` — never anything the sender typed.
_OUTCOME_PARSED = "parsed"
_OUTCOME_INVALID = "invalid"
_OUTCOME_UNAVAILABLE = "unavailable"

# The advisory lock's high word — "RPRS" (reply parse) — so a parse cycle's
# key can never collide with a different feature's xact lock on the same
# database (the reminders sweep's "RMSW" space is the precedent).
_PARSE_LOCK_SPACE = 0x52505253


def _parse_lock_key(tenant_id: UUID) -> int:
    """The per-tenant advisory key serializing overlapping parse cycles."""
    return (_PARSE_LOCK_SPACE << 32) | int.from_bytes(tenant_id.bytes[:4], "big")


async def _serialize(session: AsyncSession, *, tenant_id: UUID) -> None:
    """Take the per-tenant advisory xact lock before any read or write.

    Transaction-scoped, so a rolled-back cycle (the test harness) releases
    it with its transaction; re-entrant, so a cycle holding it never
    deadlocks against its own per-message parses. The module docstring
    states the failure mode this prevents and the lock-key residual.
    """
    await session.execute(select(func.pg_advisory_xact_lock(_parse_lock_key(tenant_id))))


class ReplyNotFoundError(Exception):
    """No communication with that id in the tenant (cross-tenant → unknown)."""


class CommunicationNotFoundError(Exception):
    """No communication with that id for the company in the tenant
    (cross-company and cross-tenant resolve to one answer, the tenant-scoped
    read's own rule)."""


class ParseNotFoundError(Exception):
    """The communication has no parse row yet — no cycle has parsed it."""


@dataclass(frozen=True)
class ParseReport:
    """What one parse run decided about one reply — coordinates only.

    Built for the cycle's report and the §5.1 row's per-message surface:
    ids, the parsed status and routing, counts carried as the id tuples
    themselves. No span, no body, no fragment of the reply travels here.
    """

    communication_id: UUID
    company_id: UUID
    outcome: str
    parse_id: UUID | None = None
    status: str | None = None
    routing: str | None = None
    event_ids: tuple[UUID, ...] = ()
    withdrawn_event_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ParseCycleReport:
    """One cycle's account of itself: what it examined and what it did."""

    examined: int
    parsed: int
    outcomes: tuple[ParseReport, ...] = ()


async def run_cycle(
    session: AsyncSession,
    *,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID,
    as_of: date,
    actor_user_id: UUID | None = None,
) -> ParseCycleReport:
    """Parse every ingested inbound reply that has no parse row, oldest first.

    A registered channel's confirming reply is not one of them: ADR-0019 §1's
    ``attribution_confirmation`` rows are left out of the listing entirely
    (ruling R5) — the confirmation is an authorisation its cases already
    acted on, and the parser's closed grammar has nothing to make of it.

    The parse cycle is deliberately its own cycle (ruling R3): the ingestion
    cycle stays free of LLM involvement, and the §5.1 gate is consulted per
    company here, before any call. Each message's outcome is reported —
    per-message refusals, denials, skips and letter failures are data, never
    exceptions that abort the cycle; a message the parser cannot classify
    leaves its reply for staff and the cycle finishes the rest.

    ``scanner`` is the same seam the generator and the sweep take: a parsed
    reply whose decision is a letter stores the reconfirmer's letter through
    the records registry, where the scan rule applies.
    """
    await _serialize(session, tenant_id=tenant_id)
    parsed_ids = select(ReplyParse.communication_id).where(ReplyParse.tenant_id == tenant_id)
    rows = await session.scalars(
        select(Communication)
        .where(
            Communication.tenant_id == tenant_id,
            Communication.direction == Direction.INBOUND.value,
            # ADR-0019 §1's confirming reply is an authorisation, not a client
            # reply (ruling R5): its words are the confirm grammar the
            # ingestion cycle already read, and nothing here may parse them
            # into confirmations or change-event proposals.
            Communication.kind != CommunicationKind.ATTRIBUTION_CONFIRMATION.value,
            Communication.id.not_in(parsed_ids),
        )
        .order_by(Communication.received_at, Communication.id)
    )
    outcomes = [
        await parse_reply(
            session,
            store=store,
            scanner=scanner,
            tenant_id=tenant_id,
            communication_id=row.id,
            as_of=as_of,
            actor_user_id=actor_user_id,
        )
        for row in rows.all()
    ]
    # The split's work-item leg (workstream ③): slices owed a parse join the
    # same cycle, one parse path, the per-company gate inside.
    item_rows = await session.scalars(
        select(SplitWorkItem)
        .where(
            SplitWorkItem.tenant_id == tenant_id,
            SplitWorkItem.status == SplitWorkItemStatus.AWAITING_PARSE.value,
        )
        .order_by(SplitWorkItem.created_at, SplitWorkItem.id)
    )
    for row in item_rows.all():
        outcomes.append(
            await parse_work_item(
                session,
                store=store,
                scanner=scanner,
                tenant_id=tenant_id,
                work_item_id=row.id,
                as_of=as_of,
                actor_user_id=actor_user_id,
            )
        )
    return ParseCycleReport(
        examined=len(outcomes),
        parsed=sum(1 for item in outcomes if item.outcome == _OUTCOME_PARSED),
        outcomes=tuple(outcomes),
    )


@dataclass(frozen=True)
class _ParseSubject:
    """The one parse path's subject (workstream ③): a stored reply, or a
    split work item's per-company slice. Exactly one leg is set — the reply
    leg carries the communication (and its request thread); the work-item
    leg carries the item. Everything downstream — the gate, the stored
    text, the gateway call, the landing — is one code path either way."""

    company_id: UUID
    source_type: str  # ChangeEventSourceType value: the event source leg
    source_id: UUID  # communication.id | work_item.id
    communication: Communication | None = None
    request: ConfirmationRequest | None = None
    work_item: SplitWorkItem | None = None


class SplitWorkItemNotFoundError(Exception):
    """No split work item with that id in the actor's tenant (cross-tenant → unknown)."""


async def parse_reply(
    session: AsyncSession,
    *,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID,
    communication_id: UUID,
    as_of: date,
    actor_user_id: UUID | None = None,
) -> ParseReport:
    """Parse one stored reply end to end — the per-message order below.

    Steps, in order and for the reasons the module docstring states: the
    communication (tenant-scoped; ``ReplyNotFoundError`` when unknown), the
    cheap skips, the §5.1 gate, the stored reply text, the one gateway call,
    and the landing — where a parse whose decision is a letter hands the
    reconfirmer its compose-store-release (the ``scanner`` seam included).
    Typed gateway errors map to outcomes; anything the domain refuses to
    trust maps to ``invalid`` with its one assessment; a letter the
    reconfirmer refuses maps to ``letter_failed:<reason>`` — the parse and
    its proposals stand either way.
    """
    await _serialize(session, tenant_id=tenant_id)
    communication = await _communication(
        session, tenant_id=tenant_id, communication_id=communication_id
    )
    company_id = communication.company_id
    # The parse cycle's subjects are stored replies, and a reply is always
    # company-bound: the only company-less exchange kind (migration 0035) is
    # the outbound attribution ask, which this cycle's listing never selects.
    assert company_id is not None
    if communication.direction != Direction.INBOUND.value:
        return ParseReport(communication_id, company_id, "skipped:not_a_reply")
    if await _parsed(session, tenant_id=tenant_id, communication_id=communication.id):
        return ParseReport(communication_id, company_id, "skipped:already_parsed")
    request = None
    if communication.confirmation_request_id is not None:
        request = await session.scalar(
            select(ConfirmationRequest).where(
                ConfirmationRequest.id == communication.confirmation_request_id,
                ConfirmationRequest.tenant_id == tenant_id,
            )
        )
    subject = _ParseSubject(
        company_id=company_id,
        source_type=ChangeEventSourceType.COMMUNICATION.value,
        source_id=communication.id,
        communication=communication,
        request=request,
    )
    return await _parse_subject(
        session,
        store=store,
        scanner=scanner,
        tenant_id=tenant_id,
        subject=subject,
        as_of=as_of,
        actor_user_id=actor_user_id,
    )


async def parse_work_item(
    session: AsyncSession,
    *,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID,
    work_item_id: UUID,
    as_of: date,
    actor_user_id: UUID | None = None,
) -> ParseReport:
    """Parse one split work item's stored slice — the one parse path's
    work-item leg (workstream ③, ruling D7).

    The subject is the item, not a communication: the stored slice stands in
    for the reply text, the XOR leg carries the parse row, the assessment
    hangs off the item, and the proposals land per company through the
    unchanged change-event write path with the ``split_work_item`` source
    (0036's widening). The per-company §5.1 gate, the grammar, the scoring
    and the routing are the reply leg's own — one code path, two subjects.
    A slice is not a thread: the reconfirmation return and the letter tail
    are the reply leg's, and the work-item leg does not run them (a slice
    joins no request); a withdrawal decision does not move events here —
    the proposals stay visible in the parse's signal log, never guessed.
    """
    await _serialize(session, tenant_id=tenant_id)
    item: SplitWorkItem | None = await session.scalar(
        select(SplitWorkItem).where(
            SplitWorkItem.id == work_item_id, SplitWorkItem.tenant_id == tenant_id
        )
    )
    if item is None:
        raise SplitWorkItemNotFoundError
    if item.status != SplitWorkItemStatus.AWAITING_PARSE.value:
        return ParseReport(item.id, item.company_id, "skipped:already_parsed")
    subject = _ParseSubject(
        company_id=item.company_id,
        source_type=ChangeEventSourceType.SPLIT_WORK_ITEM.value,
        source_id=item.id,
        work_item=item,
    )
    return await _parse_subject(
        session,
        store=store,
        scanner=scanner,
        tenant_id=tenant_id,
        subject=subject,
        as_of=as_of,
        actor_user_id=actor_user_id,
    )


async def _parse_subject(
    session: AsyncSession,
    *,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID,
    subject: _ParseSubject,
    as_of: date,
    actor_user_id: UUID | None,
) -> ParseReport:
    """The one parse path, from the gate down: the §5.1 gate for the
    subject's company, the stored text (the reply's raw, or the item's
    slice), the single gateway call, and the landing. Typed gateway errors
    map to outcomes; anything the domain refuses to trust maps to
    ``invalid`` with its one assessment."""
    company_id = subject.company_id
    source_id = subject.source_id
    gate = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        automation_type=AutomationType.AI_REPLY_PARSING,
        as_of=as_of,
    )
    if not gate.allowed:
        assert gate.reason is not None  # denied decisions always carry one
        return ParseReport(source_id, company_id, f"denied:{gate.reason.value}")
    document_id = (
        subject.communication.raw_document_id
        if subject.communication is not None
        else subject.work_item.slice_document_id  # type: ignore[union-attr]
    )
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if document is None or not is_usable(ScanStatus(document.scan_status)):
        # ADR-0009: only scanned-clean content is processed — an infected or
        # still-pending document is the retro-scan's or staff's job, never
        # the parser's.
        return ParseReport(source_id, company_id, "skipped:document_unavailable")
    raw = await store.get_bytes(document.storage_key)
    reply_text = message_text(raw)
    if not reply_text:
        # An HTML-only or undecodable body reads as nothing (the domain
        # reader's own rule): there is no reply to parse, and a model asked
        # to structure silence could only hallucinate — the message waits
        # for staff, exactly where the ingestion cycle's resolver left it.
        return ParseReport(source_id, company_id, "skipped:no_reply_text")
    checklist = [] if subject.request is None else list(subject.request.checklist)
    context = await _content(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        as_of=as_of,
        checklist=checklist,
        reply_text=reply_text,
    )
    try:
        inference = await ai_gateway.infer(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            task_type=TaskType.REPLY_PARSING,
            language=_PROMPT_LANGUAGE,
            content=context.fields,
            tokenize_values=context.tokenize_values,
            # The strict restore's literal vocabulary (review focus 6): the
            # grammar's closed enums are admissible as they stand, and every
            # other value the answer returns must be a token from this call's
            # own table — a value literal the model never saw is refused.
            allowed_literals=PARSE_LITERALS,
            actor_user_id=actor_user_id,
        )
    except ai_gateway.InferenceRefused as exc:
        # Pre-flight (or the transport's own configuration refusal): nothing
        # transited, nothing was audited — the register and flags are the
        # record. The reply waits for staff.
        return ParseReport(source_id, company_id, f"refused:{exc.reason}")
    except RedactionRefused as exc:
        # Two origins, one outcome. Before the attempt: the compose could not
        # make the content defensible, so nothing transited. After it: the
        # strict restore refused an ``invented_value`` in the answer — the
        # snippet left and the gateway has already booked the attempt. Either
        # way the answer is not landed; the reply waits for staff.
        return ParseReport(source_id, company_id, f"refused:{exc.reason}")
    except ai_gateway.InferenceUnavailable:
        # The snippet left and the gateway booked the failed attempt; the
        # parser adds nothing of its own.
        return ParseReport(source_id, company_id, _OUTCOME_UNAVAILABLE)
    return await _land(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        subject=subject,
        reply_text=reply_text,
        checklist_fact_ids={item.get("fact_id") for item in checklist},
        registered_names=context.registered_names,
        parse=inference,
        store=store,
        scanner=scanner,
        as_of=as_of,
        actor_user_id=actor_user_id,
    )


async def latest_parse(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, communication_id: UUID
) -> ReplyParse:
    """The newest parse row of one of the company's replies — the read behind
    ``GET …/communications/{id}/parse``.

    Metadata only by construction: the caller maps status, language, routing,
    the confirmations' ``fact_id``/``answer`` pair and the proposed event ids
    — the ``evidence_span`` strings the row carries never leave the database.
    ``CommunicationNotFoundError`` when the id is not one of the company's
    communications (one answer for cross-company and cross-tenant, the
    tenant-scoped read's own rule); ``ParseNotFoundError`` when no cycle has
    parsed it yet. Re-parses append rows; the newest wins.
    """
    communication = await session.scalar(
        select(Communication).where(
            Communication.id == communication_id,
            Communication.tenant_id == tenant_id,
            Communication.company_id == company_id,
        )
    )
    if communication is None:
        raise CommunicationNotFoundError
    row = await session.scalar(
        select(ReplyParse)
        .where(ReplyParse.tenant_id == tenant_id, ReplyParse.communication_id == communication_id)
        .order_by(ReplyParse.created_at.desc(), ReplyParse.id.desc())
        .limit(1)
    )
    if row is None:
        raise ParseNotFoundError
    return row


async def latest_parses(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> dict[UUID, ReplyParse]:
    """The newest parse row per communication, for the company's communications
    list — one query for the whole list, the same newest-wins rule as
    ``latest_parse``. Communications nothing has parsed are simply absent."""
    rows = await session.scalars(
        select(ReplyParse)
        .where(
            ReplyParse.tenant_id == tenant_id,
            ReplyParse.company_id == company_id,
            # The reply leg only: a split work item's parse (migration 0036)
            # has no communication and is no company's communications-list
            # entry.
            ReplyParse.communication_id.isnot(None),
        )
        .order_by(ReplyParse.communication_id, ReplyParse.created_at.desc(), ReplyParse.id.desc())
        .distinct(ReplyParse.communication_id)
    )
    return {
        row.communication_id: row
        for row in rows.all()
        if row.communication_id is not None  # the where above already confines the read
    }


async def _communication(
    session: AsyncSession, *, tenant_id: UUID, communication_id: UUID
) -> Communication:
    """The tenant's communication row, or ``ReplyNotFoundError`` — one answer
    for unknown and cross-tenant ids, like every other tenant-scoped read."""
    row = await session.scalar(
        select(Communication).where(
            Communication.id == communication_id, Communication.tenant_id == tenant_id
        )
    )
    if row is None:
        raise ReplyNotFoundError
    return row


async def _parsed(session: AsyncSession, *, tenant_id: UUID, communication_id: UUID) -> bool:
    """Whether a parse row already exists for this reply (the idempotency rule:
    re-parses are new replies answering a reconfirmation, never a re-run)."""
    existing = await session.scalar(
        select(ReplyParse.id).where(
            ReplyParse.tenant_id == tenant_id, ReplyParse.communication_id == communication_id
        )
    )
    return existing is not None


@dataclass(frozen=True)
class _ReplyContext:
    """The reply-parsing call's minimized content and its typed lexicon.

    ``fields`` is the profile-admitted field mapping (``reply_text``,
    ``role_reference``, ``officer_name``) — the payload that may transit.
    ``tokenize_values`` is the master-data lexicon keyed ``PERSON`` /
    ``COMPANY`` / ``BRN``; the gateway uses it as the tokenizer's replacement
    sources only, so it is not a field and never transits (the Q1.2
    allow-list is unchanged). ``registered_names`` is the company's in-office
    appointment name forms — the same values the ``PERSON`` lexicon is built
    from, kept here as their own thing because ruling R-D reads them locally:
    a proposal naming a person this list does not know can never auto-accept,
    and the names are compared, never sent.
    """

    fields: Mapping[str, str]
    tokenize_values: Mapping[str, Sequence[str]]
    registered_names: tuple[str, ...]


async def _content(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    as_of: date,
    checklist: Sequence[Mapping[str, Any]],
    reply_text: str,
) -> _ReplyContext:
    """The minimized context the reply-parsing profile admits, and its lexicon.

    The checklist and the company context travel as ``role_reference`` and
    the known in-office officer names as ``officer_name`` — the profile's
    three-field allow-list is the counsel-adopted Q1.2 bar, and nothing
    outside it can be sent. The company anchor (the BRN) is the gateway's own
    derivation from the company row, not a field here.

    The typed lexicon is supplied separately and keyed ``PERSON`` /
    ``COMPANY`` / ``BRN``: every script form of each in-office officer (the
    English name and the Chinese name as separate values when the record has
    both), the company's own names, and its BRN. The gateway tokenizes the
    snippet's values from it, so a name the reply writes inside the free-text
    field is replaced wherever it appears — not only when the ``officer_name``
    field happens to match word for word. Those same officer name forms are
    returned a second time as ``registered_names``: ruling R-D judges a
    proposal's named person against the register this read defines, so the
    two questions ("which names may transit" and "which names the company
    knows") are answered from one read and cannot drift apart.
    """
    master = await company_master.get_company_master(
        session, tenant_id=tenant_id, company_id=company_id, as_of=as_of
    )
    appointments = await officers.list_appointments(
        session, tenant_id=tenant_id, company_id=company_id, as_of=as_of
    )
    role_reference = {
        "checklist": [
            {"fact_id": item.get("fact_id"), "question": item.get("question")}
            for item in checklist
        ],
        "company_name_en": None if master.name is None else master.name.name_en,
        "company_name_zh": None if master.name is None else master.name.name_zh,
    }
    known_names = [_person_name(item.person) for item in appointments]
    officer_names = [
        name
        for item in appointments
        for name in officers.person_name_forms(item.person)
    ]
    company_names = [
        name
        for name in (
            None if master.name is None else master.name.name_en,
            None if master.name is None else master.name.name_zh,
        )
        if name
    ]
    return _ReplyContext(
        fields={
            "reply_text": reply_text,
            "role_reference": json.dumps(role_reference, ensure_ascii=False),
            "officer_name": json.dumps(known_names, ensure_ascii=False),
        },
        tokenize_values={
            "PERSON": officer_names,
            "COMPANY": company_names,
            "BRN": [master.company.br_number],
        },
        registered_names=tuple(dict.fromkeys(officer_names)),
    )


def _person_name(person: Person) -> str:
    """The display name the parser offers the model for one known officer.

    English name first (the master record's own display convention), else the
    joined split pair, else the Chinese name; one human is one entry in the
    ``officer_name`` list. The join is ``joined_english_name``'s — R-D compares
    the register's forms, so the register must not spell them its own way.
    """
    if person.name_en:
        return person.name_en
    joined = joined_english_name(
        surname_en=person.surname_en, other_names_en=person.other_names_en
    )
    if joined:
        return joined
    return person.name_zh or ""


async def _land(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    subject: _ParseSubject,
    reply_text: str,
    checklist_fact_ids: set[Any],
    registered_names: Sequence[str],
    parse: ai_gateway.InferenceResult,
    store: DocumentStore,
    scanner: Scanner,
    as_of: date,
    actor_user_id: UUID | None,
) -> ParseReport:
    """Validate the model's answer and land everything the parse produced.

    The fail-closed branch comes first: output outside the closed grammar is
    never trusted — one ``staff_review`` assessment, one audit entry, and
    nothing else. The valid branch scores and routes in the domain — the band
    the field scores fall into, capped by ruling R-D when a proposal names a
    person ``registered_names`` (the company's in-office appointments) does
    not carry — then lands what the grammar can complete, fills the detected
    language, and writes the parse row, the assessment and the single audit
    entry on this session. Its tail is the reconfirmation exchange's: a reply
    that joined a request's thread returns the company's
    ``client_reconfirming`` events to ``pending`` (the system
    ``client_reply``, through the change-event write path, on this session) —
    an event that left the status between the read and the move's lock is
    skipped, per event, never an abort — and a parse whose decision is a
    letter gets the compose-store-release
    (``app.application.reconfirmation``) on this same session — a letter
    refusal is this message's outcome, and the parse and its proposals stand
    either way.
    """
    model_pin = f"{parse.model_id}/{parse.prompt_name}@{parse.prompt_version}"
    raw_parse = parse.result
    violations = parse_violations(raw_parse)
    if violations:
        assessment = _assessment(
            tenant_id=tenant_id,
            subject_type=subject.source_type,
            subject_id=subject.source_id,
            model_pin=model_pin,
            field_scores={},
            signal_log={
                "violations": violations,
                "provider": parse.provider,
            },
            routing=ConfidenceBand.STAFF_REVIEW.value,
        )
        session.add(assessment)
        await session.flush()
        await _audit(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            entity_type=subject.source_type,
            entity_id=subject.source_id,
            parse_id=None,
            status=None,
            routing=ConfidenceBand.STAFF_REVIEW.value,
            language=None,
            confirmation_count=0,
            event_ids=(),
            withdrawn_event_ids=(),
            actor_user_id=actor_user_id,
        )
        return ParseReport(
            subject.source_id,
            company_id,
            _OUTCOME_INVALID,
            routing=ConfidenceBand.STAFF_REVIEW.value,
        )

    assert isinstance(raw_parse, Mapping)
    status = ReplyStatus(raw_parse["status"])
    language = ReplyLanguage(raw_parse["language"])
    parsed_events = list(raw_parse["change_events"])
    scores = field_scores(
        parse=raw_parse,
        reply_text=reply_text,
        checklist_fact_ids=checklist_fact_ids,
    )
    band = route_parse(
        scores=scores, change_events=parsed_events, registered_names=registered_names
    )
    routing = None if band is None else band.value
    event_ids, withdrawn_ids, unlanded = await _land_events(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        subject=subject,
        status=status,
        parsed_events=parsed_events,
        as_of=as_of,
    )
    if unlanded:
        # A proposal the grammar cannot complete, or a withdrawal/correction
        # reference the matcher refused: the client is asked, never guessed
        # at — the no-guessing bend, stated once, here.
        routing = ConfidenceBand.CLIENT_CLARIFICATION.value
    if subject.communication is not None:
        subject.communication.language = language.value
    parse_row = ReplyParse(
        tenant_id=tenant_id,
        company_id=company_id,
        communication_id=None if subject.communication is None else subject.communication.id,
        split_work_item_id=None if subject.work_item is None else subject.work_item.id,
        confirmation_request_id=(
            None if subject.request is None else subject.request.id
        ),
        status=status.value,
        language=language.value,
        routing=routing,
        confirmations=list(raw_parse["confirmations"]),
        event_ids=[str(event_id) for event_id in event_ids],
    )
    session.add(parse_row)
    score = overall_score(scores)
    signal_log: dict[str, Any] = {
        "change_events": parsed_events,
        "multi_company_signals": raw_parse["multi_company_signals"],
        "unlanded_proposals": unlanded,
        "provider": parse.provider,
    }
    # The diagnostic home for Ruling R-D's register read: the same question the
    # domain's band rule asks, reported here so staff can read which proposed
    # names the company's appointments do not carry — kept out of the audit
    # chain, whose vocabulary is closed. It is recorded on **any** miss, not
    # only on the miss that capped the band: this is a diagnostic, and the
    # routing is what says whether a person must look.
    register_misses = unregistered_person_names(
        change_events=parsed_events, registered_names=registered_names
    )
    if register_misses:
        signal_log["unregistered_person_names"] = list(register_misses)
    assessment = _assessment(
        tenant_id=tenant_id,
        subject_type=subject.source_type,
        subject_id=subject.source_id,
        model_pin=model_pin,
        field_scores=dict(scores),
        signal_log=signal_log,
        routing=routing,
        overall=None if score is None else Decimal(str(score)).quantize(Decimal("0.001")),
    )
    session.add(assessment)
    await session.flush()
    if subject.work_item is not None:
        subject.work_item.status = SplitWorkItemStatus.PARSED.value
    await _audit(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        entity_type=subject.source_type,
        entity_id=subject.source_id,
        parse_id=parse_row.id,
        status=status.value,
        routing=routing,
        language=language.value,
        confirmation_count=len(raw_parse["confirmations"]),
        event_ids=event_ids,
        withdrawn_event_ids=withdrawn_ids,
        actor_user_id=actor_user_id,
    )
    if subject.request is not None:
        # A reply on a request's thread answers every reconfirmation out with
        # the client on the company's threads: each such event returns to
        # ``pending`` (§4.1's system ``client_reply``) on this same session,
        # so the parse and the review-queue return commit together. An
        # unsolicited reply (no thread joined) is not an answer and moves
        # nothing — and a withdrawal that already took its event out of
        # ``client_reconfirming`` above is not returned: the trigger reads
        # current versions, which no longer stand at the reconfirming status.
        assert subject.communication is not None  # a request implies the reply leg
        for waiting in await change_events.reconfirming_events(
            session, tenant_id=tenant_id, company_id=company_id
        ):
            try:
                await change_events.client_reply_from_parse(
                    session,
                    tenant_id=tenant_id,
                    event_id=waiting.id,
                    reply_communication_id=subject.communication.id,
                    as_of=as_of,
                )
            except (
                change_events.ChangeEventTransitionError,
                change_events.ChangeEventNotFoundError,
            ):
                # Contained per event, the function's own documented contract:
                # the event left ``client_reconfirming`` between this read and
                # the move's lock (a staff move, an overlapping cycle), so the
                # reply has nothing to return for it — data, never an abort:
                # one message's race must not cost the cycle its other
                # messages. Nothing was written before the raise, so the
                # session is clean to continue on.
                continue
    if subject.communication is None:
        # The work-item leg: a slice joins no request thread, so there is no
        # reconfirmation to answer and no letter to send — the parse and its
        # proposals stand alone, and the unlanded list (a withdrawal decision
        # moves nothing on this leg) is staff's read in the signal log.
        return ParseReport(
            subject.source_id,
            company_id,
            _OUTCOME_PARSED,
            parse_id=parse_row.id,
            status=status.value,
            routing=routing,
            event_ids=tuple(event_ids),
            withdrawn_event_ids=tuple(withdrawn_ids),
        )
    try:
        await reconfirmation.enqueue_letter(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            parse_row=parse_row,
            communication=subject.communication,
            request=subject.request,
            parsed_events=parsed_events,
            unlanded_events=unlanded,
            actor_user_id=actor_user_id,
        )
    except reconfirmation.ReconfirmationError as exc:
        # The letter is this message's last step, and its failure is this
        # message's outcome — never the cycle's: the parse row and its
        # proposals stand, the letter waits for staff's re-ask, and the
        # replies behind this one still get parsed.
        return ParseReport(
            subject.source_id,
            company_id,
            f"letter_failed:{exc.reason}",
            parse_id=parse_row.id,
            status=status.value,
            routing=routing,
            event_ids=tuple(event_ids),
            withdrawn_event_ids=tuple(withdrawn_ids),
        )
    return ParseReport(
        subject.source_id,
        company_id,
        _OUTCOME_PARSED,
        parse_id=parse_row.id,
        status=status.value,
        routing=routing,
        event_ids=tuple(event_ids),
        withdrawn_event_ids=tuple(withdrawn_ids),
    )


async def _land_events(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    subject: _ParseSubject,
    status: ReplyStatus,
    parsed_events: Sequence[Mapping[str, Any]],
    as_of: date,
) -> tuple[list[UUID], list[UUID], list[Mapping[str, Any]]]:
    """Propose what the parse reported, through the change-event write path.

    Returns the created event ids, the withdrawn event ids, and the proposals
    that did not land (the unlanded list is diagnostic data and the
    reconfirmer's clarification input: each entry carries its ``index`` into
    ``parsed_events`` — the field coordinate the letter's question names — and
    the refusal ``reason``). A withdrawal moves the matched earlier event; a
    correction creates the linked new event; any other status proposes new
    events. Grammar-incomplete proposals, refused matches, and a match that
    moved under a concurrent writer between the read and the move's lock all
    land nothing — never a guessed row.
    """
    event_ids: list[UUID] = []
    withdrawn_ids: list[UUID] = []
    unlanded: list[Mapping[str, Any]] = []
    needs_match = status in (ReplyStatus.WITHDRAWAL, ReplyStatus.CORRECTION)
    earlier: Sequence[EarlierEvent] = ()
    if needs_match:
        earlier = await _earlier_events(session, tenant_id=tenant_id, company_id=company_id)
    for index, event in enumerate(parsed_events):
        if status is ReplyStatus.WITHDRAWAL:
            if proposal_violations(event=event, reference_only=True):
                # The one date home, consulted for the reference too: a
                # withdrawal that STATES a calendar-impossible date is asked,
                # never filed — the event is NOT withdrawn on this path, the
                # routing bends to client_clarification, and the letter's
                # restatement never meets the impossible date. A withdrawal
                # that states nothing needs nothing: reference_only waives
                # the filing legs (a null date, the payload grammar) and
                # keeps the calendar refusals.
                unlanded.append({**event, "reason": "grammar_incomplete", "index": index})
                continue
            match = match_earlier_event(event=event, earlier_events=earlier)
            if match is None:
                unlanded.append({**event, "reason": "no_unique_match", "index": index})
                continue
            if subject.work_item is not None:
                # A slice's withdrawal decision does not move events on this
                # leg: the withdraw path names a reply source, and the work
                # item has none. Staff read the proposal in the parse's
                # signal log; nothing is guessed, and the routing bends to
                # staff below.
                unlanded.append({**event, "reason": "work_item_withdrawal", "index": index})
                continue
            try:
                view = await change_events.client_withdraw_from_reply(
                    session,
                    tenant_id=tenant_id,
                    event_id=match.id,
                    reply_communication_id=subject.communication.id,  # type: ignore[union-attr]
                    as_of=as_of,
                )
            except (
                change_events.ChangeEventTransitionError,
                change_events.ChangeEventNotFoundError,
            ):
                # Contained per event, the executor's own documented contract:
                # the matched event left the withdrawable set between the
                # matcher's unlocked read and the move's lock (a staff
                # delete/reject/withdraw, an overlapping cycle) — the reply
                # has nothing to withdraw for it, so the reference cannot
                # land: data, never an abort. One message's race must not
                # cost the cycle its other messages.
                unlanded.append({**event, "reason": "match_moved", "index": index})
                continue
            withdrawn_ids.append(view.id)
            continue
        if proposal_violations(event=event):
            unlanded.append({**event, "reason": "grammar_incomplete", "index": index})
            continue
        related_event_id = None
        relation = None
        if status is ReplyStatus.CORRECTION:
            match = match_earlier_event(event=event, earlier_events=earlier)
            if match is None:
                unlanded.append({**event, "reason": "no_unique_match", "index": index})
                continue
            related_event_id = match.id
            relation = ChangeEventRelation.CORRECTS
        try:
            view = await change_events.create(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                event_type=ChangeEventType(event["type"]),
                effective_date=date.fromisoformat(event["effective_date"]),
                payload=payload_proposal(event=event),
                as_of=as_of,
                effective_date_text=event.get("effective_date_text"),
                informed_date=(
                    None
                    if event.get("informed_date") is None
                    else date.fromisoformat(event["informed_date"])
                ),
                related_event_id=related_event_id,
                relation=relation,
                source_type=ChangeEventSourceType(subject.source_type),
                source_id=subject.source_id,
            )
        except (
            change_events.ChangeEventInvalidError,
            change_events.ChangeEventProtectedClassError,
        ):
            # Grammar-valid but sanity-refused (a date rule, a protected-class
            # value the proposal mapping carried): the same bend as an
            # incomplete proposal — ask, never land a guessed row.
            unlanded.append({**event, "reason": "grammar_incomplete", "index": index})
            continue
        event_ids.append(view.id)
    return event_ids, withdrawn_ids, unlanded


async def _earlier_events(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[EarlierEvent]:
    """The company's events as the withdrawal/correction matcher reads them:
    each event's current version, its type, status, and the party names its
    payload carries (officer events only — nothing else names a party)."""
    latest = (
        select(
            ChangeEvent.id.label("id"),
            func.max(ChangeEvent.version_no).label("version_no"),
        )
        .where(ChangeEvent.tenant_id == tenant_id, ChangeEvent.company_id == company_id)
        .group_by(ChangeEvent.id)
        .subquery()
    )
    rows = await session.scalars(
        select(ChangeEvent).join(
            latest,
            (ChangeEvent.id == latest.c.id) & (ChangeEvent.version_no == latest.c.version_no),
        )
    )
    return [
        EarlierEvent(
            id=row.id,
            event_type=ChangeEventType(row.type),
            status=ChangeEventStatus(row.status),
            party_names=_payload_party_names(row.payload),
        )
        for row in rows.all()
    ]


def _payload_party_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The party names an officer event's payload carries, as the matcher
    normalizes them; every other payload shape names no party at all."""
    officer = payload.get("appointment", {}).get("officer") or payload.get("cessation", {}).get(
        "officer"
    ) or {}
    return tuple(
        name
        for name in (officer.get("name_en"), officer.get("name_zh"))
        if isinstance(name, str) and name.strip()
    )


def _assessment(
    *,
    tenant_id: UUID,
    subject_type: str,
    subject_id: UUID,
    model_pin: str,
    field_scores: Mapping[str, float],
    signal_log: Mapping[str, Any],
    routing: str | None,
    overall: Decimal | None = None,
) -> ExtractionAssessment:
    """The §2.7 diagnostic row for one parse run — the assessment's own shape
    is data; the resolver pair stays NULL here (the fallback's columns, not
    the parser's)."""
    return ExtractionAssessment(
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        component=AssessmentComponent.REPLY_PARSER.value,
        model=model_pin,
        field_scores=dict(field_scores),
        signal_log=dict(signal_log),
        overall_score=overall,
        routing=routing,
    )


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    entity_type: str,
    entity_id: UUID,
    parse_id: UUID | None,
    status: str | None,
    routing: str | None,
    language: str | None,
    confirmation_count: int,
    event_ids: Sequence[UUID],
    withdrawn_event_ids: Sequence[UUID],
    actor_user_id: UUID | None,
) -> None:
    """The one ``reply_parsed`` entry: what the run decided, in coordinates.

    Ids, status, routing, counts — never a span, never a body. The proposed
    and withdrawn event ids are coordinates a reader follows into the review
    queue; the parse row and the assessment carry everything else.
    """
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.REPLY_PARSED,
        actor_user_id=actor_user_id,
        company_id=company_id,
        entity_type=entity_type,
        entity_id=entity_id,
        details={
            "parse_id": None if parse_id is None else str(parse_id),
            "status": status,
            "routing": routing,
            "language": language,
            "confirmation_count": confirmation_count,
            "event_count": len(event_ids),
            "withdrawn_count": len(withdrawn_event_ids),
        },
    )
    await session.flush()


__all__ = [
    "CommunicationNotFoundError",
    "ParseCycleReport",
    "ParseNotFoundError",
    "ParseReport",
    "ReplyNotFoundError",
    "SplitWorkItemNotFoundError",
    "latest_parse",
    "latest_parses",
    "parse_reply",
    "parse_work_item",
    "run_cycle",
]
