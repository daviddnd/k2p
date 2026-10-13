"""Draft persistence (data-model §2.8; migration ``0026``).

One draft is a set of **version rows** that share an ``id`` and differ by
``version_no`` — the CE1 shape ``change_events`` carries, for the same reason:
each generation appends the next version, so nothing is overwritten and the
history is the earlier rows. The current version is the highest
``version_no``; ``(id, version_no)`` is the primary key, and the composite
target a later child table names is ``(id, version_no, tenant_id)`` (the
``(id, tenant_id)`` key the single-row tables carry cannot exist here).

Two columns beyond §2.8's field list, both required by what the contract asks
of the phase and documented in the migration and the contract's Phase 5 note:

- ``applied_event_ids`` (JSONB, default ``[]``) is the §4.1 "draft linkage
  recorded" — the change events this version applied, stored as ids (JSON has
  no UUID). It is what makes "invalidates affected drafts" a lookup rather
  than a scan across every draft's snapshot, and it is the one place the
  linkage lives: the ``changes`` block of ``resolved_state`` is the human
  readable view of the same fact.
- ``form_version`` (JSONB) is ADR-0010's pin: the registry's
  ``specification_no`` and template SHA-256 at generation, so a draft always
  says which revision of the form it was built against.

``resolved_state``, ``field_provenance``, ``qc_flags`` and ``filing_metadata``
are JSONB documents whose shapes are ``app.domain.drafts``' — deliberately not
CHECKs, the ``change_events.payload`` precedent: the shapes grow with the
registry and the domain rule is the one a reader can read. ``qc_flags`` and
``applied_event_ids`` are NOT NULL with an empty-list server default, so no
row can carry ``null`` where the contract says list. The CHECKs the table does
carry are the closed vocabularies (``form_type``, ``status``,
``signature_status``) and the stamp-and-account pairings, each a fact the row
can state about itself.

Tenancy follows the F1 pattern: one ``tenant_id`` per row with composite
foreign keys forcing the company, the creator, the statement confirmer, the
reviewer and the approver into that same tenant, so a cross-tenant row is
uninsertable at the database. ``created_at`` is ``clock_timestamp()`` per
migration ``0004``'s rule.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

# The vocabularies' database backstops (app.domain.drafts owns the values; the
# CHECKs are the copy a caller cannot bypass).
_FORM_TYPE_CHECK = "form_type IN ('nar1', 'nd2a')"
_STATUS_CHECK = (
    "status IN ('generated', 'under_review', 'sent_for_signature', "
    "'signature_received', 'reviewed', 'approved', 'exported', 'rejected', "
    "'invalidated')"
)
_SIGNATURE_CHECK = "signature_status IN ('not_sent', 'sent_for_signature', 'signature_received')"


class Draft(Base):
    """One version of one draft (data-model §2.8).

    ``private_company_statement`` is NAR1 item 16's legal declaration,
    pre-filled draft-only (§3.3): the statement is stored true for a private
    company's NAR1 and null elsewhere, and the ``statement_confirmed_by/at``
    pair records the human confirmation export will require. ``rationale`` and
    ``comments`` are the review/approval notes (§2.11); ``invalidated_at`` and
    ``invalidation_reason`` are written when an applied change event is
    withdrawn, deleted or reverted (§4.1).
    """

    __tablename__ = "drafts"
    __table_args__ = (
        PrimaryKeyConstraint("id", "version_no"),
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(
            ["statement_confirmed_by", "tenant_id"], ["users.id", "users.tenant_id"]
        ),
        ForeignKeyConstraint(["reviewed_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["approved_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # Composite target for a child table that has to name one version row
        # (see the module docstring).
        UniqueConstraint("id", "version_no", "tenant_id"),
        CheckConstraint(_FORM_TYPE_CHECK, name="drafts_form_type_check"),
        CheckConstraint(_STATUS_CHECK, name="drafts_status_check"),
        CheckConstraint(_SIGNATURE_CHECK, name="drafts_signature_status_check"),
        # A stamp and its account go together, the renewal/change-event rule.
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="drafts_reviewed_check"
        ),
        CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)", name="drafts_approved_check"
        ),
        CheckConstraint(
            "(statement_confirmed_by IS NULL) = (statement_confirmed_at IS NULL)",
            name="drafts_statement_check",
        ),
        Index("ix_drafts_tenant_id", "tenant_id"),
        Index("ix_drafts_company_form", "company_id", "form_type"),
    )

    # The draft lineage's identity; repeats once per version row.
    id: Mapped[UUID] = mapped_column(default=uuid4)
    version_no: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    company_id: Mapped[UUID] = mapped_column()
    # Vocabularies: app.domain.drafts.
    form_type: Mapped[str] = mapped_column(String(10))
    target_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(25))
    resolved_state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    field_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    qc_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    filing_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The §4.1 draft linkage: the applied events' ids (JSON has no UUID).
    applied_event_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # ADR-0010's pin: the registry revision this draft was built against.
    form_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    private_company_statement: Mapped[bool | None] = mapped_column(Boolean)
    statement_confirmed_by: Mapped[UUID | None] = mapped_column()
    statement_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signature_status: Mapped[str] = mapped_column(String(30), server_default="not_sent")
    reviewed_by: Mapped[UUID | None] = mapped_column()
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(Text)
    comments: Mapped[str | None] = mapped_column(Text)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(300))
    # NULL = the system; draft generation is the system's own act.
    created_by: Mapped[UUID | None] = mapped_column()
    # clock_timestamp() (not now()): per-statement wall clock, so two appends
    # in one transaction still order by created_at (migration 0004).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
