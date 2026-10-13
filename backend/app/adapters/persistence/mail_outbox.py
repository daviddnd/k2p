"""The send outbox (ADR-0011 §1; contract §5.1.11/§5.1.12, migrations
``0023``/``0027``/``0033``).

One row per **intent to send**. ADR-0011 is why it exists: the intent is
written in the same transaction as the state change that requests it — the
release — and a worker delivers it afterwards, so no transaction ever holds a
network call and a crash cannot lose the decision to send. The row is the
deployment's own queue, not client data: it carries ids, a status and a
bounded reason, and **never** a subject, a body, an attachment name or a
credential.

Most rows are a confirmation request's letter, and then ``confirmation_request_id``
names the request. The attribution ask (ADR-0019 §1, migration ``0033``) is the
first **request-less** row: its ``confirmation_request_id`` is NULL and
``attribution_case_id`` names the case it serves — the two anchors are mutually
exclusive in practice, each with its own partial pending index below.

**One pending row per anchor and kind.** The partial unique indexes are what
make the releases idempotent at the database level: two overlapping sends of
one request cannot both enqueue, a second attempt after a *failed* one is a
new row rather than a reopened one — the old row stays as the record of what
was tried — and the indexes' ``kind`` legs let a request and its reminder both
pend (§5.1.12) and a case's ask pend beside anything else.

**``sent_at`` and the provider's id are written only on success.** Gmail answers
with a message id and a thread; SMTP answers with nothing at all, so
``provider_message_id`` is null for a message that went out over SMTP (the
``Message-ID`` the deployment stamped is on the outbox row's
``message_id``) — and the ``Communication`` row is the record either way.

**A refusal does not close the row.** ``blocked_reason`` carries the release
gate's own vocabulary while the row waits: staff fix what refused it, and the
next cycle sends the message. Only a provider refusal that will not change
(``status = 'failed'`` with ``last_error``) or a regeneration that superseded
the request (``cancelled``) ends it.

**``not_before`` is the one scheduling column, and ``kind`` says what the row
sends** (§5.1.12, migrations ``0027``, ``0029`` and ``0033``). ``not_before``
NULL is send-on-release — the behaviour every request row has; the reminder
sweep enqueues with ``not_before`` set to the cadence instant, and the
worker's selection reads the column. ``kind`` is ``confirmation_request |
reminder | reconfirmation | attribution_ask``: a request row delivers the
request's own stored letter (``letter_document_id`` stays null, as since
``0023``); a reminder row names the stored ``.eml`` it will deliver; a
reconfirmation row (the structured reconfirmer's letter, slice M5b) names its
own stored letter like the reminder does, and answers the same request's
thread; an attribution-ask row (migration ``0033``) names its own stored
letter the same way and carries no request at all — the case's column is its
anchor.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class MailOutbox(Base):
    """One send intent — a confirmation request's, or an attribution ask's.

    Vocabularies: ``status`` is ``app.domain.mail_send.OutboxStatus`` and
    ``destination`` is ``app.domain.send_modes.SendDestination``; ``kind`` is
    ``confirmation_request | reminder | reconfirmation | attribution_ask``
    (contract §5.1.12, migrations ``0027``/``0029``/``0033``); the DB CHECKs
    are the backstop, as everywhere else in this service.

    The row's anchor is one of two: ``confirmation_request_id`` (the request
    whose letter the row is, as since ``0023``) or ``attribution_case_id``
    (the request-less attribution ask, migration ``0033``). Either may be
    NULL; both are pinned to this row's tenant by composite foreign keys,
    and so is ``company_id`` — which is itself nullable since ``0034``, when
    the case the ask serves is company-less (the clarification loop).

    ``blocked_reason`` and ``last_error`` are separate on purpose: the first is
    the **release gate's** answer while the row waits (staff-fixable, retried
    every cycle), the second is the last **delivery** attempt's outcome. One
    column would leave a transient delivery failure looking like a gate
    refusal, and the audit rule reads the first one (one entry per distinct
    gate refusal, never one per retry).
    """

    __tablename__ = "mail_outbox"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        # The composite key migration 0018 added to ``confirmation_requests``
        # exists for exactly this: the outbound row points back at the request
        # whose letter it is, and the FK pins it to this row's tenant.
        ForeignKeyConstraint(
            ["confirmation_request_id", "tenant_id"],
            ["confirmation_requests.id", "confirmation_requests.tenant_id"],
        ),
        # The request-less ask's anchor (migration 0033): the case the ask
        # serves, pinned to this row's tenant the same way.
        ForeignKeyConstraint(
            ["attribution_case_id", "tenant_id"],
            ["attribution_cases.id", "attribution_cases.tenant_id"],
        ),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # The exchange this row became, pinned to the same tenant (F1). NULL
        # while the row is pending, so the constraint is simply not checked
        # then (Postgres' MATCH SIMPLE).
        ForeignKeyConstraint(
            ["communication_id", "tenant_id"], ["communications.id", "communications.tenant_id"]
        ),
        CheckConstraint(
            "destination IN ('client', 'government')", name="mail_outbox_destination_check"
        ),
        CheckConstraint(
            "kind IN ('confirmation_request', 'reminder', 'reconfirmation', 'attribution_ask')",
            name="mail_outbox_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'cancelled')",
            name="mail_outbox_status_check",
        ),
        # A sent row carries the moment; a failed one carries the reason that
        # ended it; and a sent row carries neither a reason nor a refusal —
        # the three states are not allowed to blur in the table itself.
        CheckConstraint(
            "status <> 'sent' OR (sent_at IS NOT NULL AND last_error IS NULL "
            "AND blocked_reason IS NULL)",
            name="mail_outbox_sent_check",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL", name="mail_outbox_failed_check"
        ),
        # One pending send per request and kind: the release's idempotency —
        # and the sweep's, a request and its reminder may both pend — where it
        # cannot be raced (the partial unique index precedent of
        # ``confirmation_requests`` and ``mail_ingestion_state``'s cousins).
        Index(
            "uq_mail_outbox_pending_per_request",
            "tenant_id",
            "confirmation_request_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # One pending ask per case and kind (migration 0033): the same
        # idempotency for the request-less rows. Request-bearing rows are
        # outside the predicate, so the two indexes never overlap; a NULL
        # anchor is excluded, so rows without either anchor collide with
        # neither.
        Index(
            "uq_mail_outbox_pending_per_ask",
            "tenant_id",
            "attribution_case_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'pending' AND attribution_case_id IS NOT NULL"),
        ),
        Index("ix_mail_outbox_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the company-less ask's row (the clarification loop's A/B
    # classes, migration 0034); the composite FK above is not checked for a
    # NULL (MATCH SIMPLE).
    company_id: Mapped[UUID | None] = mapped_column(index=True)
    # The request whose letter the row is — NULL for the request-less
    # attribution ask (migration 0033), which names its case below instead.
    confirmation_request_id: Mapped[UUID | None] = mapped_column(index=True)
    # The attribution case the request-less ask serves (migration 0033); NULL
    # for every request-bearing row.
    attribution_case_id: Mapped[UUID | None] = mapped_column()
    # Vocabularies: app.domain.send_modes.SendDestination,
    # app.domain.mail_send.OutboxStatus.
    destination: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16))
    # How many delivery attempts this row has seen. Recorded, never a limit:
    # the cap is the cycle's, and a row that keeps failing transiently keeps
    # waiting rather than being abandoned.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # The release gate's own reason while the row waits (null once nothing
    # refuses it), and the last delivery outcome's bounded code.
    blocked_reason: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(String(200))
    # The RFC 5322 ``Message-ID`` of the stored message: the idempotency
    # anchor the reconciliation looks for, and the identity a row has even
    # when its transport reports none.
    message_id: Mapped[str | None] = mapped_column(String(320))
    # The provider's own id, when it has one (Gmail does; SMTP does not).
    provider_message_id: Mapped[str | None] = mapped_column(String(320))
    # The outbound ``Communication`` row, written when the message was
    # accepted. Null while the row is pending.
    communication_id: Mapped[UUID | None] = mapped_column()
    # NULL = the system (the automatic release at generation).
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The cadence instant a reminder may leave at (contract §5.1.12): NULL is
    # send-on-release, the behaviour every request row has. Stamped by the
    # reminder sweep; the worker's selection reads it.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the row sends: ``confirmation_request`` (the default — the letter
    # is derived from the request and ``letter_document_id`` stays null, as
    # since migration 0023), ``reminder``, ``reconfirmation`` (migration
    # ``0029`` — the reconfirmer's letter; like a reminder, the row names its
    # own stored letter below), or ``attribution_ask`` (migration ``0033`` —
    # the request-less ask, which names its own stored letter the same way and
    # carries its case in ``attribution_case_id``). The server default is the
    # M2 behaviour for writers that omit the column, not a stored promise.
    kind: Mapped[str] = mapped_column(String(20), server_default=text("'confirmation_request'"))
    # The stored ``.eml`` this row will deliver. A reminder or ask row names
    # its own letter; a request row leaves it null (the letter is the
    # request's stored draft, derived as since migration 0023). Plain FK:
    # documents has no (id, tenant_id) unique key (the ``raw_document_id``
    # precedent).
    letter_document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"))
