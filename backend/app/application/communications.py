"""Client-communication records (data-model §2.9; ADR-0005 ingress).

- ``record_inbound_message`` is the write path for a message the deployment
  received: the row the ingestion cycle creates for one accepted message,
  pointing at the raw message the records registry has already stored.
- ``record_outbound_message`` is the write path for a message it sent: the row
  the sender creates when the provider accepts a letter — a confirmation
  request's (§5.1.11), a reminder's (§5.1.12: the sweep stored the letter
  and the outbox row first; the sender records the exchange on acceptance,
  ``kind=reminder``), a reconfirmation's (M5b), or an attribution ask's
  (ADR-0019 §1, ``kind=attribution_ask`` — the request-less shape).
- ``list_company_communications`` is the company-scoped read: what this
  deployment has exchanged with a company, newest first, metadata only.

Both writes are deliberately silent (no audit entry of their own): the cycle
that calls the first writes one ``mail_message_ingested`` entry covering the
message, its documents, and their scan outcomes, and the sender that calls the
second writes one ``mail_message_sent`` entry covering the exchange, the
request and the outbox row. A second entry here would say the same thing twice.

Services flush but never commit — the API session dependency commits at the
request edge.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.communications import Communication
from app.application import company_master
from app.domain.communications import Channel, CommunicationKind, Direction


async def record_inbound_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    mailbox_message_id: str,
    thread_id: str | None,
    sender_address: str | None,
    received_at: datetime,
    raw_document_id: UUID,
    confirmation_request_id: UUID | None = None,
    kind: CommunicationKind = CommunicationKind.REPLY,
) -> Communication:
    """Register one received message as a ``Communication`` row.

    ``language`` stays null: this slice holds the message, the parser slice
    reads it. The company is the one the attribution rule resolved — an
    unattributable message is never recorded, so the row's company is always
    a decision someone can point at. ``confirmation_request_id`` is the
    deterministic reply join (§5.1.12): the request the message answers, when
    the header/thread match decided one — ``None`` when nothing matched or the
    match was ambiguous, because a guessed join would end the wrong chain.

    ``kind`` is the exchange's role in the thread, and it defaults to the
    ordinary answer: ``attribution_confirmation`` (ADR-0019 §1, ruling R5) is
    a registered channel's confirming reply, which authorises the cases it
    names rather than being a client reply like any other — the cycle that
    stores it names it honestly, and the reply parser's listing leaves it out
    for exactly that reason.
    """
    row = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction=Direction.INBOUND.value,
        channel=Channel.EMAIL.value,
        kind=kind.value,
        mailbox_message_id=mailbox_message_id,
        thread_id=thread_id,
        sender_address=sender_address,
        received_at=received_at,
        raw_document_id=raw_document_id,
        confirmation_request_id=confirmation_request_id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_company_communications(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[Communication]:
    """The company's communications, newest receipt first.

    Metadata only — the message text lives in the stored raw document, which
    this read never loads and never returns.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = await session.scalars(
        select(Communication)
        .where(Communication.company_id == company.id, Communication.tenant_id == tenant_id)
        .order_by(Communication.received_at.desc(), Communication.id)
    )
    return list(rows.all())


async def record_outbound_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID | None,
    confirmation_request_id: UUID | None,
    mailbox_message_id: str | None,
    thread_id: str | None,
    sender_address: str,
    recipient_addresses: str,
    received_at: datetime,
    raw_document_id: UUID,
    kind: CommunicationKind = CommunicationKind.CONFIRMATION_REQUEST,
) -> Communication:
    """Register one sent message as an outbound ``Communication`` row.

    Written by the sender in the same transaction as the request's ``sent``
    state and the outbox row's outcome, so the exchange, the record that it
    happened and the queue's answer are one fact (ADR-0009's one-transaction
    rule). ``received_at`` carries the moment the provider accepted the
    message — the column is the provider's own timestamp for both directions,
    and for an outbound row that is the acceptance moment.

    ``kind`` is the exchange's role: ``confirmation_request`` for the request's
    own letter, ``reminder`` for a reminder row's (§5.1.12) — the reminder
    sweep counts a request's immutable ``reminder`` rows to learn where the
    ladder stands, so the sender names a reminder honestly rather than letting
    it count as a second request. ``attribution_ask`` (ADR-0019 §1) is the
    request-less kind: ``confirmation_request_id`` is ``None`` for it — the
    ask serves a case, not a request — and the row records the notice the
    deployment sent. The column has been nullable since migration ``0033``
    for exactly this shape.

    ``mailbox_message_id`` is the provider's own id when it reports one (Gmail),
    and the RFC 5322 ``Message-ID`` the deployment stamped when it does not
    (SMTP) — the SMTP leg's only identity is the header it wrote, and the unique
    per-tenant index is what refuses a second record of it. Two Gmail deliveries
    of one letter carry two provider ids, so that leg's guard is the reconcile
    before the send, not this index (§5.1.11) — and the delivery-time row lock
    (``app.application.outbox.lock_job``) is what keeps two deliveries from
    reaching the provider. The index's refusal is answered, not raised: a raced
    duplicate insert re-reads the winner's row and returns it — one letter has
    one record — and only an insert whose refusal the index cannot explain (a
    null ``mailbox_message_id``, or no winner to re-read) re-raises.

    Nothing here is audited: the sender writes one ``mail_message_sent`` entry
    covering the message, the request and the row. A second entry would say the
    same thing twice (the ingestion path's rule, mirrored).
    """
    row = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction=Direction.OUTBOUND.value,
        channel=Channel.EMAIL.value,
        kind=kind.value,
        mailbox_message_id=mailbox_message_id,
        thread_id=thread_id,
        sender_address=sender_address,
        recipient_addresses=recipient_addresses,
        confirmation_request_id=confirmation_request_id,
        received_at=received_at,
        raw_document_id=raw_document_id,
    )
    try:
        # The savepoint keeps the refused insert from poisoning the caller's
        # transaction: the loser of the race has its own record to write.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        if mailbox_message_id is None:
            raise
        existing = await session.scalar(
            select(Communication).where(
                Communication.tenant_id == tenant_id,
                Communication.mailbox_message_id == mailbox_message_id,
            )
        )
        if existing is None:
            raise  # the index is not what refused this insert
        return existing
    return row
