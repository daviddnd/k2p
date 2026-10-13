"""Mailbox endpoints (ADR-0005; Phase 3 mailbox and ingestion slices).

The dedicated mailbox the product talks to clients from, connected once by an
Administrator: start the consent flow, come back to the loopback callback,
read the status, run the probe, ingest what has arrived, run one reminder
cycle, parse the replies clients sent, run one confirmation-discovery cycle,
deliver the queued sends, or forget the connection.

The callback is the second **unauthenticated** route in this service
(``app.api.activation`` has the first), and for the same reason: the browser
arrives from Google's site carrying no session, and the whole point of the
exchange is that it is the browser that holds the one-time code. Three
properties make that safe, and all three are required:

- the ``state`` must be the one this deployment minted for the attempt, so a
  callback that was not started here is refused before anything else;
- the code is redeemable **only** with the PKCE verifier, which was minted
  here and never left this process — Google stores only its S256 challenge —
  so a stolen code is useless to whoever stole it;
- the code is single-use at Google, and the attempt here is consumed by the
  success, so a replayed callback finds nothing to complete.

What the callback must *not* do is trust the mailbox: consent is granted by
whoever is signed in to the browser, so the connected address is checked
against the deployment's configured one and a different mailbox is refused
rather than stored.

Administrator-only: starting a connection, reading it, probing it, ingesting
from it, running the reply-parse cycle, and disconnecting it — a mailbox
credential is deployment plumbing of the same class as the entitlement lease,
not per-company staff work. The callback is not, by necessity. The probe reads
the mailbox and never a message body: the metadata request format is the
client's, and this response carries only the envelope. The ingestion route
answers with ids, counts, scan outcomes, and — for a message the multi-company
resolver decided — the decision it reached (its case, evidence tier, score,
routing band and candidate company ids; never the text that matched, which
stays in the §2.7 assessment) — never a message, never an attachment, never a
filename; the mail itself goes to the records registry, and what has already
been ingested is read through the per-company communication routes, which are
ungated (the customer's own data). A message that opened an attribution case
(ADR-0019 §1) reports the case's id and nothing of the message: its thread
bound the company, its sender is not that company's registered channel, and
the bytes were never read. The parse cycle's report answers the same
way: ids, the cycle's bounded outcome codes, status and routing — never a
span of a client's words, which stays in the customer's own database with the
parse row and its §2.7 assessment. A message the cycle could not attribute is
not in a company's communications at all: it is read, retried, or dismissed
through the §2.5 review surface (``app.api.quarantine``).
"""

from datetime import date, datetime
from html import escape
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.api.documents import get_storage
from app.application import (
    confirmation_discovery,
    mail,
    mail_ingest,
    mail_send,
    reminders,
    reply_parsing,
    scanning,
)
from app.core.config import settings
from app.domain.communications import IngestOutcome
from app.domain.mail import MailResponseInvalid
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/mail", tags=["mail"])

# The page is a hand-off, not an application: no scripts, no client data, and
# never cached (a browser holding it could re-post a spent code on back).
_PAGE_HEADERS = {"Cache-Control": "no-store"}

NOT_CONNECTED = "not_connected"


class ConnectStartResponse(BaseModel):
    """The URL the administrator opens, and when the attempt stops working."""

    authorization_url: str
    expires_at: datetime


class ConnectionResponse(BaseModel):
    """The connection as the operator reads it.

    There is deliberately no field that could carry a token: the address and
    the grant are what an operator checks, and the credential stays in its
    file (ADR-0014 custody). ``scopes`` is what Google granted, which the
    consent screen lets a user narrow.
    """

    connected: bool
    address: str | None
    scopes: list[str]
    connected_at: datetime | None
    token_expires_at: datetime | None


class MessageMetadataResponse(BaseModel):
    """One message's envelope: subject, sender, date — never its body.

    ``from`` is a Python keyword, so the field is named ``from_`` and
    serializes as the header's own name; the response is what a reviewer
    reads, not a Python object.
    """

    id: str
    subject: str | None
    from_: str | None = Field(default=None, serialization_alias="from")
    date: str | None


class ProbeResponse(BaseModel):
    """The proof the read path works: the mailbox and its newest envelopes."""

    address: str
    messages_total: int
    messages: list[MessageMetadataResponse]


class AttachmentOutcomeResponse(BaseModel):
    """One attachment's outcome: the document it became, or why it did not."""

    document_id: UUID | None
    disposition: str
    scan_status: str | None
    reason: str | None


class MessageResolutionResponse(BaseModel):
    """What the multi-company resolver saw about one message — never its words.

    The rule decides which of the sender's own companies a message is about
    from the message's text (scope §2.5), and the text each candidate matched
    on stays in the customer's own database, in the §2.7 assessment's
    ``signal_log``. What a reviewer reads here is the decision: the ``case``
    it reached (``resolved``/``ambiguous``/``no_match``), the
    ``evidence_tier`` that decided it (``brn_exact``/``cr_exact``/
    ``name_exact``, null when nothing decisive matched), the ``score``,
    the §2.7 ``routing`` band the answer falls into, and the
    ``candidate_company_ids`` the message matched — ids, never names, because
    a name here would be a fragment of the mail.
    """

    case: str
    evidence_tier: str | None
    score: float
    routing: str | None
    candidate_company_ids: list[UUID]


class MessageOutcomeResponse(BaseModel):
    """One message's ingestion outcome — ids and scan results only.

    There is deliberately nothing here that could carry what a client wrote:
    no subject, no sender, no filename. The stored message and its
    attachments are in the records registry, reached through the documents
    and communication routes.

    ``quarantine_id`` is the §2.5 record a message that could not be
    attributed leaves behind; the row itself is what staff act on, and this
    is how a report says which message became which row.

    ``resolution`` is the resolver's decision whenever the resolver ran — a
    refusal it reached, a message it attributed, one it attributed and the
    §5.1 gate then denied, or one whose bytes turned out not to be a message
    after the rule had already decided — and null when it did not: an
    unregistered sender, or one reaching a single company, never needs the
    rule (see ``MessageResolutionResponse`` for why its fields carry no
    matched text).

    ``attribution_case_id`` is the case one of ADR-0019 §1's two awaits
    opened — the threaded message whose sender is not the chain company's
    registered channel — and null on every other outcome: an ingested
    message, a refusal, a threaded message the chain attributed (there is
    nothing to ask), or one with no chain at all.
    """

    mailbox_message_id: str
    outcome: str
    reason: str | None
    company_id: UUID | None
    communication_id: UUID | None
    raw_document_id: UUID | None
    scan_status: str | None
    quarantine_id: UUID | None
    attribution_case_id: UUID | None
    # The split parent workstream ③ opened for this message (the ``split``
    # outcome); ``reason`` carries a park or failure code when the division
    # did not complete.
    split_parent_id: UUID | None
    resolution: MessageResolutionResponse | None
    attachments: list[AttachmentOutcomeResponse]


class IngestCycleResponse(BaseModel):
    """One ingestion cycle's account of itself.

    ``examined`` is every message the cycle looked at, and it equals
    ``ingested + skipped_known + refused``. ``refused`` is the remainder
    bucket — **every outcome that is neither ``ingested`` nor
    ``skipped_known``** — so it folds in ADR-0019 §1's two ``awaiting_*``
    outcomes and ``attribution_confirmed`` beside the genuine refusals: the
    per-message outcomes below say which one it was and why.
    """

    cursor_received_at: datetime | None
    query: str | None
    examined: int
    ingested: int
    skipped_known: int
    refused: int
    outcomes: list[MessageOutcomeResponse]


class SendOutcomeResponse(BaseModel):
    """One queued send's outcome — ids, a code, and what happened.

    ``disposition`` is ``sent | refused | failed | waiting | cancelled |
    skipped``; ``reason`` is the bounded code behind anything that did not send
    (the release gate's own detail string for ``refused``), and
    ``reconciled`` says a message already in the provider's Sent was recorded
    rather than sent again. **Never** a subject, a body, an attachment or a
    recipient's message: a send report carries coordinates, exactly as the
    ingestion report does.

    ``confirmation_request_id`` is null only for a request-less row's outcome
    (the attribution ask, migration ``0033``) — every row a client letter
    rides links the request it belongs to. ``company_id`` is null for the
    same row when its case is company-less (migration ``0034``).
    """

    outbox_id: UUID
    confirmation_request_id: UUID | None
    company_id: UUID | None
    disposition: str
    reason: str | None
    provider_message_id: str | None
    message_id: str | None
    reconciled: bool


class SendCycleResponse(BaseModel):
    """One send cycle's account of itself.

    ``examined`` is every pending row the cycle took; the counts are the
    dispositions and always sum to it. A row that could not be delivered stays
    **pending** (``waiting``) unless the provider refused it permanently
    (``failed``) — the queue is the record of what is still owed to a client,
    and a cycle report is not the place to lose it.
    """

    examined: int
    sent: int
    failed: int
    refused: int
    waiting: int
    cancelled: int
    skipped: int
    outcomes: list[SendOutcomeResponse]


class ReminderOutcomeResponse(BaseModel):
    """One request's sweep outcome — ids, the outcome, and its own detail.

    ``outcome`` is the sweep's vocabulary (``enqueued | denied | exhausted |
    skipped``); ``cadence_index`` and ``held`` accompany ``enqueued``,
    ``reason`` the gate's own vocabulary for ``denied``, and ``skip_reason``
    the sweep's ``skip`` set. **Never** a body, never a letter, never client
    data beyond ids: a reminder report carries coordinates, exactly as the
    send cycle's report does.
    """

    confirmation_request_id: UUID
    company_id: UUID
    outcome: str
    cadence_index: int | None
    held: bool | None
    reason: str | None
    skip_reason: str | None


class ReminderCycleResponse(BaseModel):
    """One reminder cycle's account of itself (contract §5.1.12).

    ``examined`` is every ``sent`` request the sweep looked at; the counts are
    its outcome vocabulary and sum to it. ``held`` counts the enqueued rows a
    ``review_required`` deployment parked on ``HELD_NOT_BEFORE`` — they wait
    for the per-request send route's press, not for this cycle.
    """

    examined: int
    enqueued: int
    held: int
    denied: int
    exhausted: int
    skipped: int
    outcomes: list[ReminderOutcomeResponse]


class ConfirmationDiscoveryOutcomeResponse(BaseModel):
    """One company's cycle outcome — ids, the period, and its own detail.

    ``outcome`` is the cycle's vocabulary (``generated | refused | skipped``);
    ``request_id`` accompanies ``generated`` (the drafted request, already
    queued under the tenant's ``automatic`` client mode), ``reason`` the
    generation trigger's own refusal vocabulary for ``refused`` (a refusal
    writes nothing), and ``skip`` the cycle's one skip reason,
    ``already_requested`` — the client-actions panel's own done-signal.
    **Never** a body, never a letter: a discovery report carries coordinates,
    exactly as the reminder cycle's report does.
    """

    company_id: UUID
    year: int
    outcome: str
    request_id: UUID | None
    reason: str | None
    skip: str | None


class ConfirmationDiscoveryCycleResponse(BaseModel):
    """One discovery cycle's account of itself.

    ``examined`` is every company the cycle looked at — one with at least one
    arrived candidate year; the counts are its outcome vocabulary and sum to
    it. Only companies the cycle decided something about appear in
    ``outcomes``; a refusal or a skip is a report entry, never a write.
    """

    examined: int
    generated: int
    refused: int
    skipped: int
    outcomes: list[ConfirmationDiscoveryOutcomeResponse]


class ParseOutcomeResponse(BaseModel):
    """One reply's parse outcome — coordinates and the cycle's own code.

    ``outcome`` is the cycle's bounded vocabulary: ``parsed`` | ``invalid``
    | ``unavailable`` | ``refused:<reason>`` (a gateway pre-flight refusal) |
    ``denied:<gating reason>`` (the §5.1 gate) | ``skipped:<reason>`` |
    ``letter_failed:<reason>`` — never anything the sender typed. A refusal,
    a denial, a skip and a letter failure are each one message's row, never
    an HTTP error the cycle answers. ``status``/``routing``/``event_ids``/
    ``withdrawn_event_ids`` accompany a run that produced a parse row, and
    **never** a span of the client's words or a body: a parse report carries
    coordinates, exactly as the other cycles' reports do.
    """

    communication_id: UUID
    company_id: UUID
    outcome: str
    parse_id: UUID | None
    status: str | None
    routing: str | None
    event_ids: list[UUID]
    withdrawn_event_ids: list[UUID]


class ParseCycleResponse(BaseModel):
    """One parse cycle's account of itself (slice M5b; contract §5.1.13).

    ``examined`` is every ingested inbound reply the cycle looked at —
    ``parsed`` counts the runs that produced a parse row, and the per-message
    outcomes below say which refusal, denial, skip or failure each of the
    rest met. One reply's outcome never becomes the cycle's HTTP error.
    """

    examined: int
    parsed: int
    outcomes: list[ParseOutcomeResponse]


def _connection_response(view: mail.ConnectionView) -> ConnectionResponse:
    return ConnectionResponse(
        connected=view.connected,
        address=view.address,
        scopes=view.scopes,
        connected_at=view.connected_at,
        token_expires_at=view.token_expires_at,
    )


def message_outcome_response(item: mail_ingest.MessageOutcome) -> MessageOutcomeResponse:
    """One per-message outcome as the wire shape.

    Shared with the §2.5 quarantine retry (``app.api.quarantine``): both
    report a message with the same words, and a second copy of these fields
    would be a second thing to keep in step.
    """
    return MessageOutcomeResponse(
        mailbox_message_id=item.mailbox_message_id,
        outcome=item.outcome.value,
        reason=item.reason,
        company_id=item.company_id,
        communication_id=item.communication_id,
        raw_document_id=item.raw_document_id,
        scan_status=item.scan_status,
        quarantine_id=item.quarantine_id,
        attribution_case_id=item.attribution_case_id,
        split_parent_id=item.split_parent_id,
        resolution=(
            None if item.resolution is None else _resolution_response(item.resolution)
        ),
        attachments=[
            AttachmentOutcomeResponse(
                document_id=attachment.document_id,
                disposition=attachment.disposition.value,
                scan_status=attachment.scan_status,
                reason=attachment.reason,
            )
            for attachment in item.attachments
        ],
    )


def _resolution_response(summary: mail_ingest.ResolutionSummary) -> MessageResolutionResponse:
    """One resolver decision as the wire shape, field for field.

    A mapping, not a dump: the summary's fields are named for the audit entry
    they also fill, and this renames the two that read better to a reviewer
    (``evidence_tier``, ``candidate_company_ids``). Everything here is a
    value the summary already holds — nothing reads the message again.
    """
    return MessageResolutionResponse(
        case=summary.case,
        evidence_tier=summary.evidence,
        score=summary.score,
        routing=summary.routing,
        candidate_company_ids=list(summary.candidate_ids),
    )


@router.post("/connection", status_code=201)
async def start_connection(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
) -> ConnectStartResponse:
    """Register a consent attempt and return the Google URL to open.

    503 when this deployment has no Google OAuth client or no expected
    mailbox configured — the flow refuses rather than consenting for
    whichever account the browser happens to be signed in to.
    """
    try:
        started = await mail.begin(local_tenant_id=actor.tenant_id, local_user_id=actor.user_id)
    except mail.MailNotConfigured:
        raise HTTPException(status_code=503, detail="mail_not_configured") from None
    return ConnectStartResponse(
        authorization_url=started.authorization_url,
        expires_at=started.expires_at,
    )


@router.get("/connection")
async def get_connection(
    _: Annotated[ActorContext, Depends(require_admin())],
) -> ConnectionResponse:
    """What this deployment is connected to, from local state only.

    Never calls Google, so it answers during an outage; the probe is what
    asks the mailbox itself.
    """
    return _connection_response(mail.status())


@router.delete("/connection")
async def disconnect_mailbox(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionResponse:
    """Forget the mailbox credential; the audit chain records the disconnect.

    The grant at Google is not revoked by this — the TCSP's own Google
    account page is where that happens — and nothing else in the deployment
    changes: ingestion and sending report their own typed refusals
    (``409 not_connected``, or SMTP settings when they are configured), and the
    ungated manual path never depended on a mailbox in the first place.
    """
    await mail.disconnect(session, local_tenant_id=actor.tenant_id, local_user_id=actor.user_id)
    return _connection_response(mail.status())


@router.get("/probe")
async def probe_mailbox(
    _: Annotated[ActorContext, Depends(require_admin())],
) -> ProbeResponse:
    """Read the mailbox: profile plus the newest messages' metadata, nothing else.

    409 while no mailbox is connected. A 502 with ``mail_refresh_failed``
    means the grant is gone (revoked, expired, or issued to another client)
    and the mailbox has to be connected again; ``mail_unavailable`` means
    Google could not be reached, and trying again is the whole remedy;
    ``mail_probe_refused`` is Google refusing a read with a usable token,
    carrying its own error code.
    """
    try:
        result = await mail.probe()
    except mail.MailNotConnected:
        raise HTTPException(status_code=409, detail=NOT_CONNECTED) from None
    except mail.MailRefreshFailed as exc:
        raise HTTPException(status_code=502, detail=f"mail_refresh_failed:{exc.reason}") from None
    except mail.MailProbeRefused as exc:
        raise HTTPException(status_code=502, detail=f"mail_probe_refused:{exc.reason}") from None
    except MailProviderUnavailable:
        raise HTTPException(status_code=502, detail="mail_unavailable") from None
    return ProbeResponse(
        address=result.profile.address,
        messages_total=result.profile.messages_total,
        messages=[
            MessageMetadataResponse(
                id=message.message_id,
                subject=message.subject,
                from_=message.sender,
                date=message.date,
            )
            for message in result.messages
        ],
    )


@router.post("/ingest")
async def ingest_mail(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> IngestCycleResponse:
    """Run one ingestion cycle over the connected mailbox, now.

    Administrator-only, and the same cycle the background loop runs: read the
    mailbox from the stored cursor, and for each new message attribute it to
    a company, consult the §5.1 gate for ``email_ingestion``, scan the message
    and its attachments, store them through the records registry, and record
    a ``Communication``. A message for a company the gate refuses is **not**
    ingested, and its outcome carries the gate's own reason. A message whose
    sender is registered against several companies is attributed from its own
    subject and body; one that cannot be attributed at all — no company
    registered for the address, or several with nothing to choose between
    them — is refused with its own reason and **recorded for staff review**
    (scope §2.5), and its outcome carries the ``quarantine_id`` of that row.
    A message whose own thread binds it to a company the sender is not a
    registered channel of is not ingested and is not a refusal — it opens an
    attribution case (ADR-0019 §1), reports one of the two ``awaiting_*``
    outcomes and the ``attribution_case_id``, and its bytes are never read
    (the report's ``refused`` count, a remainder bucket, folds the outcome
    in).
    An outcome also carries the resolver's decision (``resolution``) whenever
    the resolver ran — a refusal it reached, a message it attributed, one it
    attributed and the gate then denied — and null when it did not.
    Runs one cycle whether or not the loop is enabled; the cycle commits when
    this request returns.

    409 ``not_connected`` when the deployment has no mailbox to read: no
    connected credential and no configured IMAP settings. 502
    ``mail_refresh_failed:<reason>`` when the grant is dead,
    ``mail_read_refused:<reason>`` when the provider refuses a read with a
    usable credential, ``mail_unavailable`` when it cannot be reached, and
    ``invalid_response:<reason>`` when a response is not the shape the wire
    contract fixes — the same vocabulary whichever leg raised it. A provider
    failure aborts the cycle: nothing is recorded half-way, and re-running it
    re-reads the same window (the message-id check makes that harmless).
    """
    try:
        report = await mail_ingest.run_cycle(
            session,
            store=store,
            scanner=scanning.scanner(),
            tenant_id=actor.tenant_id,
        )
    except mail.MailNotConnected:
        raise HTTPException(status_code=409, detail=NOT_CONNECTED) from None
    except mail_ingest.NoLocalTenantError as exc:
        raise HTTPException(status_code=409, detail=f"ingest_unavailable:{exc.reason}") from None
    except mail.MailRefreshFailed as exc:
        raise HTTPException(status_code=502, detail=f"mail_refresh_failed:{exc.reason}") from None
    except MailProviderRefused as exc:
        raise HTTPException(status_code=502, detail=f"mail_read_refused:{exc.reason}") from None
    except MailProviderUnavailable:
        raise HTTPException(status_code=502, detail="mail_unavailable") from None
    except MailResponseInvalid as exc:
        raise HTTPException(status_code=502, detail=f"invalid_response:{exc.reason}") from None
    return IngestCycleResponse(
        cursor_received_at=report.cursor_received_at,
        query=report.query,
        examined=report.examined,
        ingested=report.ingested,
        skipped_known=sum(
            1 for item in report.outcomes if item.outcome is IngestOutcome.SKIPPED_KNOWN
        ),
        refused=sum(
            1
            for item in report.outcomes
            if item.outcome is not IngestOutcome.INGESTED
            and item.outcome is not IngestOutcome.SKIPPED_KNOWN
        ),
        outcomes=[message_outcome_response(item) for item in report.outcomes],
    )


@router.post("/send")
async def send_queued_mail(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> SendCycleResponse:
    """Deliver the queued sends now — one cycle, the same one the loop runs.

    Administrator-only, and the manual trigger for the automatic path: a
    request released by the generator's ``automatic`` mode waits for a cycle,
    and this is one. Each row's outcome is reported and **every** provider
    failure is one of them — a grant that cannot be renewed, a refused
    reconciliation lookup, an unreachable provider, the send's own refusals —
    so one unusable row never hides the rows behind it and no row answers a
    bare 500 (the ingestion trigger's shape, with the same vocabulary). Rows
    stay pending unless the provider refused them permanently, so running this
    twice is safe: the second run sees the rows that are still owed.
    """
    report = await mail_send.run_cycle(session, store, tenant_id=actor.tenant_id)
    return SendCycleResponse(
        examined=report.examined,
        sent=report.count(mail_send.SendDisposition.SENT),
        failed=report.count(mail_send.SendDisposition.FAILED),
        refused=report.count(mail_send.SendDisposition.REFUSED),
        waiting=report.count(mail_send.SendDisposition.WAITING),
        cancelled=report.count(mail_send.SendDisposition.CANCELLED),
        skipped=report.count(mail_send.SendDisposition.SKIPPED),
        outcomes=[
            SendOutcomeResponse(
                outbox_id=outcome.outbox_id,
                confirmation_request_id=outcome.confirmation_request_id,
                company_id=outcome.company_id,
                disposition=outcome.disposition.value,
                reason=outcome.reason,
                provider_message_id=outcome.provider_message_id,
                message_id=outcome.message_id,
                reconciled=outcome.reconciled,
            )
            for outcome in report.outcomes
        ],
    )


@router.post("/reminders")
async def run_reminder_cycle(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ReminderCycleResponse:
    """Run one reminder cycle now — the sweep contract §5.1.12 defines.

    Administrator-only, and the same cycle the opt-in background loop
    (``AICS_MAIL_REMINDERS_ENABLED``) runs: every ``sent``, unanswered request
    of the tenant whose next cadence instant has passed gets the sweep's
    decision — the bilingual letter stored through the records registry and an
    outbox row written with ``not_before`` (released with the queue under
    ``automatic``, held for the per-request send route under
    ``review_required``), or a per-request outcome naming why not: the §5.1
    gate's own reason for a denial (nothing written), the chain's exhaustion,
    or the skip that applies. A gate denial is a report entry, never a
    refusal: a sweep is system-side, and one request's outcome never becomes
    another request's error — no row's outcome is a 500. A stored milestone or
    send-mode block nobody can read stops the cycle before it starts: 409
    ``tenant_config_invalid: <violations>`` (§5.1.3's vocabulary), nothing
    written. The report carries ids, counts and bounded codes only — never a
    body, never a letter.
    """
    report = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id
    )
    if report.config_error is not None:
        raise HTTPException(
            status_code=409, detail=f"tenant_config_invalid: {report.config_error}"
        )
    return ReminderCycleResponse(
        examined=len(report.outcomes),
        enqueued=report.enqueued,
        held=sum(1 for item in report.outcomes if item.held is True),
        denied=report.denied,
        exhausted=report.exhausted,
        skipped=report.skipped,
        outcomes=[
            ReminderOutcomeResponse(
                confirmation_request_id=item.request_id,
                company_id=item.company_id,
                outcome=item.outcome.value,
                cadence_index=item.cadence_index,
                held=item.held,
                reason=item.reason,
                skip_reason=None if item.skip is None else item.skip.value,
            )
            for item in report.outcomes
        ],
    )


@router.post("/confirmation-discovery")
async def run_confirmation_discovery_cycle(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ConfirmationDiscoveryCycleResponse:
    """Run one confirmation-discovery cycle now — the due-company sweep.

    Administrator-only, and the same cycle the opt-in background loop
    (``AICS_MAIL_CONFIRMATION_DISCOVERY_ENABLED``) runs: every company of the
    tenant whose data-confirmation ask date has arrived with no confirmation
    request for the period gets the generation trigger's decision (contract
    §5.1.8, unchanged) — a drafted request, stored and (under the tenant's
    ``automatic`` client mode) already queued when it returns, or the
    trigger's own typed refusal, caught and named in the report:
    ``automation_denied: <reason>``, ``no_client_contact``,
    ``no_client_reply_window`` and the rest of the trigger's vocabulary. A
    refusal is a report entry and never a write — no request row, no outbox
    row, no audit entry — and one company's outcome never becomes another
    company's error. A period with any request row already — whatever its
    status, the client-actions panel's own done-signal — is a skip named
    ``already_requested``.

    503 ``mail_not_configured`` when the deployment has no mailbox address to
    write from — the generation trigger's own precondition, checked once
    before the cycle starts, so an unconfigured deployment refuses without
    sweeping anything. 409 ``tenant_config_invalid: <violations>`` when a
    stored milestone or send-mode block nobody can read stops the cycle (the
    §5.1.3 vocabulary, exactly as the reminder cycle answers). The report
    carries ids, the period year and bounded codes only — never a body, never
    a letter. Runs one cycle whether or not the loop is enabled; the cycle
    commits when this request returns.
    """
    if not settings.mail_address.strip():
        raise HTTPException(status_code=503, detail="mail_not_configured")
    report = await confirmation_discovery.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id
    )
    if report.config_error is not None:
        raise HTTPException(
            status_code=409, detail=f"tenant_config_invalid: {report.config_error}"
        )
    return ConfirmationDiscoveryCycleResponse(
        examined=report.examined,
        generated=report.generated,
        refused=report.refused,
        skipped=report.skipped,
        outcomes=[
            ConfirmationDiscoveryOutcomeResponse(
                company_id=item.company_id,
                year=item.year,
                outcome=item.outcome.value,
                request_id=item.request_id,
                reason=item.reason,
                skip=None if item.skip is None else item.skip.value,
            )
            for item in report.outcomes
        ],
    )


@router.post("/parse-replies")
async def parse_replies(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ParseCycleResponse:
    """Run one reply-parse cycle now — every stored reply, one gateway call each.

    Administrator-only, and the same cycle the opt-in background loop
    (``AICS_MAIL_PARSE_REPLIES_ENABLED``) runs: every ingested inbound reply
    with no parse row gets the deterministic pre-pass, the per-company §5.1
    gate (``ai_reply_parsing``), and — when the gate allows — the single M6
    gateway call whose structured answer lands its parse row, its assessment
    and its proposed change events through the change-event write path.
    Each message's outcome is reported — ``parsed`` with its routing and the
    event ids it proposed or withdrew; ``refused:<reason>`` for a gateway
    pre-flight refusal; ``denied:<gating reason>`` when the §5.1 gate refuses
    the company (the reply waits for staff, nothing is written);
    ``skipped:<reason>`` for the cheap legs (not a reply, already parsed, no
    usable text); ``invalid`` for an answer outside the closed grammar (one
    staff-review assessment, nothing else); ``unavailable`` when the gateway
    booked a failed attempt; ``letter_failed:<reason>`` when the parse stood
    but its reconfirmation letter could not go out. A message's outcome is
    data, never a 500 — one refused or denied reply never costs the replies
    behind it, and the report carries coordinates only: ids, codes, status
    and routing, never a span, never a body.
    """
    report = await reply_parsing.run_cycle(
        session,
        store=store,
        scanner=scanning.scanner(),
        tenant_id=actor.tenant_id,
        as_of=date.today(),
        actor_user_id=actor.user_id,
    )
    return ParseCycleResponse(
        examined=report.examined,
        parsed=report.parsed,
        outcomes=[
            ParseOutcomeResponse(
                communication_id=item.communication_id,
                company_id=item.company_id,
                outcome=item.outcome,
                parse_id=item.parse_id,
                status=item.status,
                routing=item.routing,
                event_ids=list(item.event_ids),
                withdrawn_event_ids=list(item.withdrawn_event_ids),
            )
            for item in report.outcomes
        ],
    )


@router.get("/oauth/callback", response_class=HTMLResponse)
async def mail_callback(
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """The loopback redirect target: redeem the code, connect, show a page.

    Refusals are pages, not JSON: this route is reached by a browser, which
    should be told in words what happened. ``error`` is Google's own refusal
    (a user who clicked "cancel" is the ordinary case); the text is rendered
    escaped and never carries a code to redeem.
    """
    if error is not None:
        return _page(
            "Mailbox not connected",
            f"Google refused the consent ({error}). Nothing changed; start again "
            "from the administrator console.",
            status_code=400,
        )
    if code is None:
        return _page(
            "Mailbox not connected",
            "This deployment received no authorization code.",
            status_code=400,
        )
    try:
        credential = await mail.complete(session, code=code, state=state)
    except mail.MailConnectionRefused as exc:
        return _page("Mailbox not connected", f"Reason: {exc.reason}", status_code=400)
    except MailProviderUnavailable:
        return _page(
            "Mailbox not connected",
            "Google could not be reached. Nothing changed; start again from the "
            "administrator console.",
            status_code=502,
        )
    return _page(
        "Mailbox connected",
        f"{credential.address} is connected. You can close this window.",
    )


def _page(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html>\n"
        '<html lang="en">\n<head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head>\n"
        f"<body><h1>{escape(title)}</h1><p>{escape(message)}</p></body>\n</html>\n"
    )
    return HTMLResponse(content=body, status_code=status_code, headers=_PAGE_HEADERS)
