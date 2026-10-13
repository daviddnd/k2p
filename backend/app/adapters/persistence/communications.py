"""Client-communication persistence (ADR-0005; data-model §2.9, migrations
``0015``, ``0023`` and ``0027``).

One row per exchange with a client. The ingestion slice writes the inbound
email half: the row a message received on the dedicated mailbox becomes,
carrying the provider's ids, the sender the attribution rule matched on, the
provider's received timestamp (``received_at`` — the ingestion cursor is
derived from it), and the stored raw message. The sending slice writes the
**outbound** half when a confirmation request's message is accepted: the same
shape, the mailbox it went out from in ``sender_address``, the client
addresses it went to in ``recipient_addresses``, and the request it answers in
``confirmation_request_id``.

``language`` and the fact checklist of §2.9 are not columns yet: the reply
parser is the slice that fills them, and an always-null column is a promise
the code does not keep. ``kind`` *is* a column (migration ``0027``): §2.9's
kind vocabulary — ``confirmation_request | reminder | reply |
reconfirmation | clarification`` — backfilled from ``direction`` (every stored
outbound row is a ``confirmation_request``, every inbound one a ``reply``)
and NOT NULL under the CHECK, widened by migration ``0033`` with the
attribution exchange's ``attribution_ask`` / ``attribution_confirmation``
(ADR-0019 §1). It says what the exchange is in the request's thread, read
directly by the reminder sweep and the dashboard's
pending-confirmations widget instead of being re-derived by counting rows
(§5.1.12). §2.5's quarantine is not a column here either: a message that
cannot be attributed has no company to file it under, so it is recorded in
its own table (``quarantined_messages``, migration 0016) and points at the
``Communication`` it became only once one exists.

The unique ``(tenant_id, mailbox_message_id)`` index is the idempotency
guarantee: a message id can be recorded once per tenant, whatever a cycle's
cursor did. Outbound rows carry it under the same rule — the provider's id on
the Gmail leg, and the ``Message-ID`` the deployment stamped on the SMTP leg,
which is the identity a message has when no provider answers with one. An
inbound id and an outbound one cannot collide in practice (one is a provider
id, the other an RFC 5322 header value), and the index is what would catch it
if they ever did.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class Communication(Base):
    """One immutable client communication (data-model §2.9).

    One ``tenant_id`` per row: the composite foreign keys force the company
    and the stored message into that same tenant (F1 pattern). Rows are
    append-only in practice — nothing in this slice updates one — and the raw
    message they point at is WORM.
    """

    __tablename__ = "communications"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        # The outbound row points at the confirmation request it is the letter
        # of (migration 0023); the composite key 0018 added exists for it.
        ForeignKeyConstraint(
            ["confirmation_request_id", "tenant_id"],
            ["confirmation_requests.id", "confirmation_requests.tenant_id"],
        ),
        # Composite-FK target for quarantined_messages and mail_outbox (F1
        # pattern; migration 0016 adds it, under the name Postgres gives this
        # constraint).
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="communications_direction_check"
        ),
        CheckConstraint("channel IN ('email')", name="communications_channel_check"),
        CheckConstraint(
            "language IS NULL OR language IN ('en', 'zh-hk', 'mixed')",
            name="communications_language_check",
        ),
        CheckConstraint(
            "kind IN ('confirmation_request', 'reminder', 'reply', 'reconfirmation', "
            "'clarification', 'attribution_ask', 'attribution_confirmation')",
            name="communications_kind_check",
        ),
        # One provider message id per tenant, inbound or outbound: the
        # ingestion cycle's idempotency guarantee, enforced where it cannot
        # be raced (see the module docstring).
        Index(
            "uq_communications_message_id_per_tenant",
            "tenant_id",
            "mailbox_message_id",
            unique=True,
            postgresql_where=text("mailbox_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the company-less exchanges the clarification loop's delivered
    # asks record (migration 0035 — the company-bound rows keep their F1
    # tenant pinning; a NULL is simply not checked).
    company_id: Mapped[UUID | None] = mapped_column(index=True)
    # Vocabularies: app.domain.communications (Direction / Channel /
    # CommunicationKind); the DB CHECKs are the backstop.
    direction: Mapped[str] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(20))
    # The exchange's role in its thread: §2.9's kinds, made a column by
    # migration ``0027`` (contract §5.1.12) and backfilled from ``direction``
    # — an outbound row is a confirmation_request, an inbound one a reply.
    # Widened to 32 by migration ``0033``: the attribution exchange's
    # ``attribution_confirmation`` is 24 characters, and this is the width the
    # revision's vocabulary columns carry.
    kind: Mapped[str] = mapped_column(String(32))
    # The provider's ids. §2.9 names both; the message id is unique per tenant
    # and the thread is how the reply parser will join a message to the
    # confirmation request it answers. For an outbound row the message id is
    # the provider's own (Gmail) or the ``Message-ID`` the deployment stamped
    # (SMTP), and the thread is the provider's, when it reports one.
    # 320, the outbox row's own width: the stamped header is sized by the
    # mailbox's domain, and it is written here *after* the provider accepted
    # the message (migration ``0023``).
    mailbox_message_id: Mapped[str | None] = mapped_column(String(320))
    thread_id: Mapped[str | None] = mapped_column(String(128))
    # The bare address the attribution rule matched on for an inbound row, and
    # the mailbox the message went out from for an outbound one.
    sender_address: Mapped[str | None] = mapped_column(String(320))
    # Who an outbound message went to, comma-joined in the request's own order
    # (null for inbound rows, whose counterparty is ``sender_address``).
    # Unbounded on purpose: it is a list of addresses, each of which the
    # request row already bounds.
    recipient_addresses: Mapped[str | None] = mapped_column(String)
    # The request whose stored letter this row is the exchange of (null for
    # inbound rows, and for any outbound mail a later slice sends that is not a
    # confirmation request).
    confirmation_request_id: Mapped[UUID | None] = mapped_column()
    language: Mapped[str | None] = mapped_column(String(10))
    # The provider's own timestamp, not ours: for an inbound row the receipt
    # moment (the ingestion cursor is a mailbox position, and this is the value
    # the mailbox orders by); for an outbound one the moment the provider
    # accepted the message.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
