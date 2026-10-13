"""Quarantined-message persistence (scope §2.5; data-model §2.9, migrations
``0016`` and ``0017``).

One row per message the ingestion cycle could not attribute to exactly one
company (``app.application.mail_ingest``): the provider's ids, the sender the
attribution rule read, the provider's receipt time, why it was refused, and
what became of it once staff worked it out.

Two omissions are the point of the table. There is deliberately **no
``company_id``**: the message has not been attributed, which is why it is
here rather than in ``communications``. And there is deliberately no subject,
no body, and no attachment column: the mail stays in the mailbox (ADR-0005),
and this row is the metadata a staff member can act on — never a copy of what
the client wrote.

``status`` and the closing fields are what the review surface moves, and the
row has two ways to close. ``resolved`` points at the ``Communication`` the
message became and records ``resolved_at`` (that link is a composite foreign
key into ``communications``, F1 pattern, which is why that table gains its
``(id, tenant_id)`` unique key in migration ``0016``). ``dismissed`` records
``dismissed_at`` and nothing else: staff decided no further action was needed,
and a message the mailbox no longer has could not be resolved however long
anyone waited. A CHECK keeps the deadlines honest — a pending row carries
neither timestamp and a terminal row exactly the one its status names — so a
status can never be read against a stamp from the other state.

The unique index on ``(tenant_id, mailbox_message_id)`` is the idempotency
guarantee, the same discipline as ``communications``: one quarantine row per
message, ever, however often a cycle's overlap re-reads the message.
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class QuarantinedMessage(Base):
    """One message awaiting staff attribution (scope §2.5).

    ``reason`` and ``status`` are the vocabularies of
    ``app.domain.quarantine``; the DB CHECKs are the backstop.
    """

    __tablename__ = "quarantined_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["resolved_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        CheckConstraint(
            "reason IN ('unregistered_sender', 'multiple_companies')",
            name="quarantined_messages_reason_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="quarantined_messages_status_check",
        ),
        # A terminal row carries the stamp of the state it is in, and a
        # pending one carries neither: the status and the timestamps cannot
        # tell two different stories about the same row.
        CheckConstraint(
            "status = 'pending' AND resolved_at IS NULL AND dismissed_at IS NULL"
            " OR status = 'resolved' AND resolved_at IS NOT NULL"
            " AND dismissed_at IS NULL"
            " OR status = 'dismissed' AND dismissed_at IS NOT NULL"
            " AND resolved_at IS NULL",
            name="quarantined_messages_closed_at_check",
        ),
        # One quarantine row per provider message id per tenant, ever. A
        # plain unique index, not ``communications``' partial one: this
        # table's message id is NOT NULL, so there is nothing to exclude.
        Index(
            "uq_quarantined_messages_message_id_per_tenant",
            "tenant_id",
            "mailbox_message_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # The provider's message id: what the row is keyed by, and what a retry
    # re-examines (the mailbox, never a copy of the message).
    mailbox_message_id: Mapped[str] = mapped_column(String(128))
    thread_id: Mapped[str | None] = mapped_column(String(128))
    # The bare address, as ``communications`` stores it (never the display
    # name, which is whatever the sender typed).
    sender_address: Mapped[str] = mapped_column(String(320))
    # The provider's own receipt time, not ours — the same value the
    # ``Communication`` row and the ingestion cursor are built from.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When staff closed the row as needing no further action. Our clock, like
    # ``resolved_at`` — not the provider's receipt time (``received_at``),
    # which never changes.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_communication_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
