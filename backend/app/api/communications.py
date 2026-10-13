"""Client-communication endpoints (data-model §2.9; contract §5.1, mailbox
area).

The read behind the mailbox: what this deployment has exchanged with a company,
newest first. Both directions appear — an inbound message the ingestion cycle
recorded, and an outbound one the sender recorded when a confirmation request's
message was accepted — so one read answers "what has passed between us".
Metadata only: the message id and thread, the counterparty addresses, the
provider's timestamp, the request an outbound row answers, and the id of the
stored raw message. The text of a letter is in the records registry
(``raw_document_id``) and is reached through the document routes; it is never
rendered here, so no response in this service carries a mail body.

The list rows and the per-message parse read carry the same bar one level
deeper: a reply the parse cycle has read gains its parse's status, language
routing band, the ``fact_id`` → ``answer`` join and the proposed change-event
ids — coordinates a reviewer follows into the review queue. The client's
verbatim words (the ``evidence_span`` strings) stay in ``reply_parses`` in the
customer's own database, exactly as the parsed body's stay on the WORM
document; no field of either shape could carry one.

Grant-gated via ``require_company_access``: reading a client's correspondence
is ordinary staff work on the company's own data (the same rule as master-data
reads and document listings). It is **not** gated by the automation gate —
what has already been ingested is the customer's data, and a lapsed licence
must not lock them out of it (ADR-0012).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.communications import Communication
from app.adapters.persistence.reply_parsing import ReplyParse
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_company_access
from app.application import communications, reply_parsing
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/companies", tags=["communications"])


class CommunicationResponse(BaseModel):
    """One communication as the operator reads it — never its content.

    The counterparty is the row's own: ``sender_address`` for an inbound
    message, and for an outbound one the mailbox it went out from with the
    client addresses it went to in ``recipient_addresses``. An outbound row
    that answers a confirmation request carries the request's id, which is how
    a reader gets from the exchange back to the checklist the client was asked
    about.

    ``parse_status``/``parse_routing`` are the newest parse's status and
    confidence band — ``None`` until a parse cycle has read the reply, set on
    every later read. Coordinates only: the parse row and the review queue
    carry what happened; this response never carries what the client wrote.
    """

    id: UUID
    # NULL only for a company-less exchange; this company-scoped surface never
    # lists one (migration 0035), and the field stays honest about the column.
    company_id: UUID | None
    direction: str
    channel: str
    mailbox_message_id: str | None
    thread_id: str | None
    sender_address: str | None
    recipient_addresses: str | None
    confirmation_request_id: UUID | None
    language: str | None
    parse_status: str | None
    parse_routing: str | None
    received_at: datetime
    created_at: datetime
    raw_document_id: UUID


class ReplyParseConfirmationResponse(BaseModel):
    """One parsed answer as the read carries it: which fact, which answer.

    There is deliberately nothing else — no ``evidence_span``, no ``uncertain``
    flag, none of the client's words. The checklist join a reviewer reads is
    ``fact_id`` → ``answer``; the supporting quote stays in the customer's own
    database, on the parse row itself.
    """

    fact_id: str
    answer: str


class ReplyParseResponse(BaseModel):
    """The newest parse of one reply — the read ``…/parse`` answers with.

    Metadata only, the M4/M5a custody rule: status, language, routing, the
    confirmations' ``fact_id`` → ``answer`` join and the proposed change-event
    ids. A withdrawal parse's proposed-event list is empty (it moved an
    earlier event rather than proposing one); the review queue is where the
    events live and die. Never an evidence span, never the reply body: both
    stay in the customer's own database, and this shape has no field that
    could carry them.
    """

    id: UUID
    communication_id: UUID
    confirmation_request_id: UUID | None
    status: str
    language: str | None
    routing: str | None
    confirmations: list[ReplyParseConfirmationResponse]
    event_ids: list[UUID]
    created_at: datetime


def _communication_response(row: Communication, parse: ReplyParse | None) -> CommunicationResponse:
    return CommunicationResponse(
        id=row.id,
        company_id=row.company_id,
        direction=row.direction,
        channel=row.channel,
        mailbox_message_id=row.mailbox_message_id,
        thread_id=row.thread_id,
        sender_address=row.sender_address,
        recipient_addresses=row.recipient_addresses,
        confirmation_request_id=row.confirmation_request_id,
        language=row.language,
        parse_status=None if parse is None else parse.status,
        parse_routing=None if parse is None else parse.routing,
        received_at=row.received_at,
        created_at=row.created_at,
        raw_document_id=row.raw_document_id,
    )


def _parse_response(row: ReplyParse) -> ReplyParseResponse:
    # This surface reads the reply leg's parses (fetched by communication);
    # the split leg's parse belongs to the work-item surface, not here.
    assert row.communication_id is not None
    return ReplyParseResponse(
        id=row.id,
        communication_id=row.communication_id,
        confirmation_request_id=row.confirmation_request_id,
        status=row.status,
        language=row.language,
        routing=row.routing,
        confirmations=[
            ReplyParseConfirmationResponse(fact_id=item["fact_id"], answer=item["answer"])
            for item in row.confirmations
        ],
        event_ids=[UUID(value) for value in row.event_ids],
        created_at=row.created_at,
    )


@router.get("/{company_id}/communications")
async def list_company_communications(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CommunicationResponse]:
    """The company's communications, newest receipt first (metadata only)."""
    try:
        rows = await communications.list_company_communications(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    parses = await reply_parsing.latest_parses(
        session, tenant_id=actor.tenant_id, company_id=company_id
    )
    return [_communication_response(row, parses.get(row.id)) for row in rows]


@router.get("/{company_id}/communications/{communication_id}/parse")
async def get_communication_parse(
    company_id: UUID,
    communication_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReplyParseResponse:
    """The newest parse of one of the company's replies (metadata only).

    What the reply parser decided about this message: the grammar's status,
    the detected language, the routing band, each checklist fact's answer and
    the change events it proposed. 404 ``communication not found`` when the id
    is not one of the company's (one answer for cross-company and cross-tenant
    ids); 404 ``not_parsed`` when no cycle has parsed the reply yet — the
    trigger is ``POST /api/v1/mail/parse-replies``. Re-parses (a reply
    answering a reconfirmation) append rows, and this read returns the newest.
    """
    try:
        row = await reply_parsing.latest_parse(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            communication_id=communication_id,
        )
    except reply_parsing.CommunicationNotFoundError:
        raise HTTPException(status_code=404, detail="communication not found") from None
    except reply_parsing.ParseNotFoundError:
        raise HTTPException(status_code=404, detail="not_parsed") from None
    return _parse_response(row)
