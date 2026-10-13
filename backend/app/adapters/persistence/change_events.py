"""Change-event persistence (scope §2.7-§2.8; data-model §2.6; migration
``0024``).

One event is a set of **append-only version rows** that share an ``id`` and
differ by ``version_no``: the current version is the highest ``version_no``,
a revision inserts the next one (``version_no + 1``, same ``id``), and a
status change updates the current row in place. ``(id, version_no)`` is
therefore the primary key — the ``id`` is the event's identity across its
revisions, which is what makes ``/change-events/{id}/...`` mean the same
event before and after an edit.

What that means for the keys:

- the company, the creator, the reviewer and the approver are each pinned to
  the row's own tenant by the F1 composite foreign keys, so a row cannot
  describe another tenant's company or attribute an act to another tenant's
  user;
- the ``(id, version_no, tenant_id)`` unique key is the composite target a
  future child table would point at to name **one version row** — the
  ``(id, tenant_id)`` key of the other tables cannot exist here, because the
  same ``id`` repeats once per version;
- ``related_event_id`` is a plain column, not a foreign key: the reference is
  to an *event* (its identity, which repeats across version rows), so no
  unique key can anchor it at the database. The service resolves it through
  the company-scoped seam and refuses an unknown or other-company target (the
  ``import_batches.document_id`` precedent — a plain link where no composite
  key exists, with the tenant pinned through the company).

Nothing is hard-deleted (§2.8): a delete is the ``deleted`` status on the
current version, and a rejection (which also closes the event) is the same
status written by its own action. ``payload`` is JSONB and typed per
``type`` — the grammar lives in ``app.domain.change_events``, deliberately
not in a database CHECK: the shapes grow with the registry, and the domain
rule is the one a caller can read.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

# The vocabularies' database backstops (app.domain.change_events owns the
# values; the CHECKs are the copy that cannot be bypassed by a caller).
_TYPE_CHECK = (
    "type IN ('director_appointment', 'director_cessation', 'secretary_appointment', "
    "'secretary_cessation', 'registered_office_change', 'company_name_change', 'other')"
)
_STATUS_CHECK = (
    "status IN ('pending', 'client_reconfirming', 'under_review', 'reviewed', "
    "'confirmed', 'applied_to_draft', 'submitted', 'withdrawn', 'deleted')"
)
_SOURCE_CHECK = "source_type IN ('communication', 'document', 'manual', 'split_work_item')"
_RELATION_CHECK = "relation IS NULL OR relation IN ('corrects', 'withdraws', 'supersedes')"


class ChangeEvent(Base):
    """One version of one change event (data-model §2.6).

    ``effective_date_text`` is the verbatim source text the date was parsed
    from (the reply-parse schema); it is null for an event staff typed in.
    ``informed_date`` is when the TCSP learned of the change — null when
    nobody recorded it, and never after ``effective_date`` (the domain rule).
    ``rationale`` carries the reason a rejection or a staff-recorded
    withdrawal was made; the same words are in the audit chain, which is
    where a later edit of the row cannot erase them.
    """

    __tablename__ = "change_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "version_no"),
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["reviewed_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["approved_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # Composite target for a child table that has to name one version row
        # (see the module docstring).
        UniqueConstraint("id", "version_no", "tenant_id"),
        CheckConstraint(_TYPE_CHECK, name="change_events_type_check"),
        CheckConstraint(_STATUS_CHECK, name="change_events_status_check"),
        CheckConstraint(_SOURCE_CHECK, name="change_events_source_type_check"),
        CheckConstraint(_RELATION_CHECK, name="change_events_relation_check"),
        # A reference and its relation go together: one cannot be read without
        # the other, so a half-stated correction is uninsertable.
        CheckConstraint(
            "(relation IS NULL) = (related_event_id IS NULL)",
            name="change_events_relation_pair_check",
        ),
        # A review/approval stamp and its account go together, the same rule
        # the renewal record follows for its own stamps.
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="change_events_reviewed_check"
        ),
        CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)", name="change_events_approved_check"
        ),
        Index("ix_change_events_tenant_id", "tenant_id"),
        Index("ix_change_events_company_id", "company_id"),
        Index("ix_change_events_related_event_id", "related_event_id"),
    )

    # The event's identity; repeats once per version row.
    id: Mapped[UUID] = mapped_column(default=uuid4)
    version_no: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    company_id: Mapped[UUID] = mapped_column()
    # Vocabularies: app.domain.change_events.
    type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    effective_date: Mapped[date] = mapped_column(Date)
    effective_date_text: Mapped[str | None] = mapped_column(String(500))
    informed_date: Mapped[date | None] = mapped_column(Date)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_type: Mapped[str] = mapped_column(String(20))
    # What the event came from: a Communication or a Document, depending on
    # source_type. Left unresolved here by construction — the two live in
    # different tables and only the parser's slices can name which one they
    # wrote.
    source_id: Mapped[UUID | None] = mapped_column()
    related_event_id: Mapped[UUID | None] = mapped_column()
    relation: Mapped[str | None] = mapped_column(String(20))
    reviewed_by: Mapped[UUID | None] = mapped_column()
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(String(500))
    # NULL = the system (the reply parser's extractions, the draft linkage).
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
