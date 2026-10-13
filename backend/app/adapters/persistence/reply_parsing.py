"""Reply-parsing persistence (slice M5b; data-model §2.7; migration ``0029``).

``reply_parses`` is the parse result's **business** home: one row per parse
run, carrying what the client actually answered (``confirmations[]`` — the
§2.9 checklist join) and what the parser proposed (``event_ids``). The
**diagnostic** home stays where §2.7 puts it — ``extraction_assessments``
(``app.adapters.persistence.prior_filing``): the per-field scores, signal
log and routing behind the run. The founder's D3 ruling keeps the two
apart: business data is read by staff and clients, diagnostics are
recomputable and may be dropped without touching history, and a column that
serves both would serve neither. A parse run writes both rows on one
session — the parse's home and its assessment — but they never share a
column.

**Custody.** The ``confirmations[].evidence_span`` strings this table holds
are verbatim quotes of the client's own mail; the reply body itself lives
on the WORM ``documents`` row the ``Communication`` points at. Neither ever
leaves its home: no audit entry, log line, or API response carries a span
or a body — those surfaces carry coordinates only (ids and ``fact_id``
values, counts, status and routing), the M4/M5a custody rule this table
joins.

Re-parses (a reply answering a reconfirmation) append a row; reads take the
newest per communication. ``event_ids`` is a JSONB list of the proposed
change events' ids in their string form — a reference the change-event
write path created, not an embedded copy, so the review queue remains the
one owner of event rows. ``confirmation_request_id`` is the §5.1.12 join
the deterministic thread/header matcher already sets on the inbound
``Communication``; NULL for the unsolicited and off-topic mail a request
never produced.

Tenant pinning follows the F1 pattern: the composite foreign keys force
the company, the communication and the request into the row's tenant, so a
cross-tenant row is uninsertable at the database level.
"""

from datetime import datetime
from typing import Any
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

# The vocabularies' database backstops — the domain owns the values:
# status is ``app.domain.reply_parsing.ReplyStatus`` (the schema's six),
# language is ``app.domain.reply_parsing.ReplyLanguage`` (the same
# vocabulary ``communications.language`` carries, migration ``0015``), and
# routing is ``app.domain.confidence``'s band rule.
_STATUS_CHECK = (
    "status IN ('confirmed_no_change', 'changes_reported', 'correction', "
    "'withdrawal', 'unclear', 'off_topic')"
)
_LANGUAGE_CHECK = "language IS NULL OR language IN ('en', 'zh-hk', 'mixed')"
_ROUTING_CHECK = (
    "routing IS NULL OR routing IN ('auto_accept', 'staff_review', 'client_clarification')"
)


class ReplyParse(Base):
    """One parse run over one stored subject (data-model §2.7).

    The subject is exactly one of two legs (migration ``0036``, the
    exactly-one CHECK below): an inbound reply — ``communication_id`` set —
    or a split work item's per-company slice (workstream ③) —
    ``split_work_item_id`` set, ``communication_id`` NULL. Either way the
    parse is its company's record: ``company_id`` stays NOT NULL, so the
    §5.1 gate's per-company leg and the RBAC reads are unchanged.

    ``status`` is what the reply says in the grammar's six values; ``language``
    is the detected reply language the parser fills on the ``Communication``
    as well; ``routing`` is the confidence band the scorer landed on, or NULL
    when nothing was scored (an empty off-topic parse). ``confirmations`` is
    the parsed checklist join — ``fact_id``, ``answer``, ``evidence_span``,
    ``uncertain`` items; ``event_ids`` names the proposed change events.
    Both JSON lists are empty, never NULL: a parse that confirmed nothing
    and proposed nothing still parsed.
    """

    __tablename__ = "reply_parses"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(
            ["communication_id", "tenant_id"], ["communications.id", "communications.tenant_id"]
        ),
        # NULL for mail no request produced; when set, the request is pinned
        # to this row's tenant (F1), the ``mail_outbox`` nullable-leg precedent.
        ForeignKeyConstraint(
            ["confirmation_request_id", "tenant_id"],
            ["confirmation_requests.id", "confirmation_requests.tenant_id"],
        ),
        # The split's work-item leg (migration 0036): the slice this run
        # parsed, pinned to this row's tenant — a parse reads a stored reply
        # or a split slice, exactly one (the CHECK below).
        ForeignKeyConstraint(
            ["split_work_item_id", "tenant_id"],
            ["split_work_items.id", "split_work_items.tenant_id"],
        ),
        CheckConstraint(_STATUS_CHECK, name="reply_parses_status_check"),
        CheckConstraint(_LANGUAGE_CHECK, name="reply_parses_language_check"),
        CheckConstraint(_ROUTING_CHECK, name="reply_parses_routing_check"),
        # Exactly one subject leg: a stored reply, or a split work item —
        # never both, never neither.
        CheckConstraint(
            "(communication_id IS NULL) <> (split_work_item_id IS NULL)",
            name="reply_parses_subject_check",
        ),
        Index("ix_reply_parses_company_id", "company_id"),
        Index("ix_reply_parses_communication_id", "communication_id"),
        Index("ix_reply_parses_split_work_item_id", "split_work_item_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[UUID] = mapped_column()
    # The reply this run parsed — NULL on the split leg, where the subject
    # is the work item below (the exactly-one CHECK pairs them; the column is
    # nullable since migration 0036, which the split leg's shape forces).
    communication_id: Mapped[UUID | None] = mapped_column()
    # The split work item this run parsed (workstream ③); NULL for every
    # reply parse.
    split_work_item_id: Mapped[UUID | None] = mapped_column()
    # The request it answers (the §5.1.12 join, already set on the inbound
    # Communication by the deterministic thread/header matcher).
    confirmation_request_id: Mapped[UUID | None] = mapped_column()
    # Vocabularies: app.domain.reply_parsing (ReplyStatus / ReplyLanguage)
    # and app.domain.confidence's bands; the DB CHECKs are the backstop.
    status: Mapped[str] = mapped_column(String(30))
    language: Mapped[str | None] = mapped_column(String(10))
    routing: Mapped[str | None] = mapped_column(String(30))
    # The business payload (see the module docstring): the parsed
    # confirmations[] and the proposed events' ids. Both carry verbatim
    # client text in the evidence_spans — the custody rule applies.
    confirmations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    event_ids: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
