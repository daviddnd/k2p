"""Confirmation-request persistence (scope §2.5; data-model §2.9, migrations
``0018`` and ``0023``).

One row per confirmation request the generator drafted: the form and the date
it prepares for, the reply-by deadline the client is given, the status of the
request's lifecycle, the stored ``message/rfc822`` document that is the letter
itself, the checklist the client is asked about, and who it goes to.

**Why its own table, and not a ``communications`` row.** That table's own
docstring is "one row per exchange with a client", and a draft is not one: it
has no provider timestamp (``received_at`` is NOT NULL there), no status, no
form, and no recipients. Writing a draft there would put four or five columns
on the core correspondence table to describe a message that has not happened.
The outbound ``Communication`` row is written by the sending slice when the
message actually goes out, and it links back here (migration ``0023`` adds the
outbound half's columns).

**The status vocabulary is the send lifecycle** (``app.domain.confirmation``):
``draft`` (written, not released), ``queued`` (released; an outbox row is
pending), ``sent`` (the provider accepted the message), ``superseded`` (a
regeneration closed it). The send path's two states arrived with migration
``0023``; the sending slice writes them **only** through the one executor in
``app.application.outbox``.

``checklist`` and ``recipients`` are JSON because nothing queries an
individual item yet: the reply parser reads the whole checklist of the one
request it is answering, and staff read the recipients of one row. Promote
them to tables when a read needs per-item querying, not before.

``form_type`` is ``app.domain.prior_filing.FormCode``; the DB CHECKs are the
backstop. The partial unique index on ``(tenant_id, company_id, form_type)``
for the **live** statuses (``draft`` and ``queued``) is the "which request is
live?" guarantee — one working request per company and form, whatever a caller
does, so a regeneration supersedes a released-but-undelivered letter rather
than letting two race.

Tenant pinning follows the F1 pattern (the composite foreign keys pin the
company and the creator to the row's tenant). ``document_id`` is a plain
foreign key, like ``import_batches``': ``documents`` carries no
``(id, tenant_id)`` unique key, and the row's tenant is already pinned by the
company composite FK while the service resolves the document through the
company-scoped read.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class ConfirmationRequest(Base):
    """One drafted confirmation request for a company and a form.

    ``checklist`` is the items array (``app.domain.confirmation.ChecklistItem``
    serialized: ``fact_id``, ``question``, ``expected_value``,
    ``source_record_id``) and ``recipients`` is
    ``[{"person_id": ..., "address": ...}]`` — the company's registered client
    contacts the draft is addressed to.

    **No ``sent_at`` and no ``communication_id``**, though migration 0018's
    docstring anticipated both. The link and the moment live on the row that
    records the exchange — the outbound ``Communication``, which points back
    here through ``confirmation_request_id`` and carries the moment the
    provider accepted the message as its ``received_at`` and the outbox row's
    ``sent_at``. A second copy here would be a second thing to keep in step,
    and one fact gets one home.
    """

    __tablename__ = "confirmation_requests"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # Composite parent key: the sending slice's outbound Communication row
        # and outbox row point back at the request they answered.
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint("form_type IN ('nar1')", name="confirmation_requests_form_type_check"),
        CheckConstraint(
            "status IN ('draft', 'queued', 'sent', 'superseded')",
            name="confirmation_requests_status_check",
        ),
        # The live statuses are the two a request can be worked on from: a
        # draft waiting for staff, and a released one whose message has not
        # left yet. A sent or superseded request is history and does not block
        # the next version.
        Index(
            "uq_confirmation_requests_open_draft",
            "tenant_id",
            "company_id",
            "form_type",
            unique=True,
            postgresql_where=text("status IN ('draft', 'queued')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[UUID] = mapped_column(index=True)
    # Vocabularies: app.domain.prior_filing.FormCode /
    # app.domain.confirmation.RequestStatus.
    form_type: Mapped[str] = mapped_column(String(20))
    # The made-up date the request prepares for, and the deadline the client
    # is asked to reply by (the milestone panel's offset from the anniversary,
    # floored at the minimum reply window, never a guess).
    target_date: Mapped[date] = mapped_column(Date)
    reply_by: Mapped[date] = mapped_column(Date)
    # CR's own deadline for the period — anniversary + 42 days — computed as the
    # guard the reply-by was checked against, and stored so the guarantee is
    # durable on the record. NULL on rows written before migration 0019: those
    # were generated under the old rule, where the reply-by *was* the statutory
    # date, and inventing the guard for them would claim a check never run.
    statutory_due: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    # The draft lineage's version: 1 for a company's first request for a form,
    # and the next integer for each regeneration.
    version_no: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    checklist: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    recipients: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # NULL = system (the sending slice's timed triggers).
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
