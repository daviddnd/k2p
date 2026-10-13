"""Prior-filing ingestion persistence (scope §2.1; contract §2.7, §2.9; migration 0010).

Three tables, one extraction run:

- ``import_batches`` — the run itself: which stored document was read, for
  which company, in which of the §2.1 input shapes, and which form registry
  produced the proposals (the ``documents`` row does not carry the form
  code, and the review queue has to name what it is reviewing).
- ``import_batch_records`` — one proposed field: a master-data ``target``,
  the extracted ``value``, the reader's ``confidence``, and the staff
  decision (``pending`` until someone confirms or rejects it). Only
  ``confirmed`` records become master data.
- ``extraction_assessments`` — the §2.7 diagnostic record: the per-field
  scores and signal log behind the routing. Diagnostics live apart from
  business data so they can be recomputed without touching history.

Tenant pinning follows the F1 pattern: composite foreign keys force the
company, the batch, and the deciding user into the row's tenant, so a
cross-tenant row is uninsertable at the database level. The document link
is a plain foreign key — ``documents`` carries no ``(id, tenant_id)``
unique key (migration 0009), and the batch already pins its tenant through
the company; the application resolves the document through the
company-scoped read seam.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class ImportBatch(Base):
    """One extraction run over one stored prior filing (contract §2.9).

    ``shape`` is the §2.1 input shape the reader found (``needs_ocr`` is a
    recorded outcome, not a failure). ``form`` names the registry entry the
    proposals came from, so a batch is readable without re-reading the
    document.
    """

    __tablename__ = "import_batches"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # Composite-FK target for import_batch_records (F1 pattern).
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint("form IN ('nar1', 'nd2a')", name="import_batches_form_check"),
        CheckConstraint(
            "shape IN ('acroform', 'text_layer', 'needs_ocr')",
            name="import_batches_shape_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[UUID] = mapped_column(index=True)
    # Plain FK: documents has no (id, tenant_id) unique key; the tenant is
    # pinned through the company composite FK above.
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    form: Mapped[str] = mapped_column(String(10))
    shape: Mapped[str] = mapped_column(String(20))
    # NULL = system actor (no extraction trigger runs as the system yet).
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class ImportBatchRecord(Base):
    """One proposed master-data field, pending staff review (scope §2.1).

    ``target`` is a contract §1 master-data path (the closed vocabulary
    lives in ``app.domain.prior_filing.TARGETS``, not in a CHECK — the
    vocabulary grows with the registry). ``effective_date`` carries the
    date the filing stated (its made-up date); when the filing states none
    it is NULL and the reviewer supplies one at confirmation (ADR-0007),
    which is then the date the record carries — the audit entry keeps both.
    """

    __tablename__ = "import_batch_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "tenant_id"], ["import_batches.id", "import_batches.tenant_id"]
        ),
        ForeignKeyConstraint(["decided_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="import_batch_records_status_check",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="import_batch_records_confidence_check",
        ),
        # A decision date and a decided status go together.
        CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name="import_batch_records_decided_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    target: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    effective_date: Mapped[date | None] = mapped_column(Date)
    decided_by: Mapped[UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExtractionAssessment(Base):
    """The §2.7 diagnostic record behind one extraction or resolver run.

    ``subject`` is the record the assessment is about — a document (the
    extraction path), a ``Communication``, or a quarantine row (the reply
    resolver's); ``subject_type`` names the table ``subject_id`` points into.
    Assessments accumulate per subject: a re-run adds another, and the import
    batch view shows the newest for its document. ``field_scores`` maps
    target → score and ``signal_log`` explains each score or absence; both are
    recomputable diagnostics, never business data. ``raw_model_resolution``
    and ``enforced_resolution`` belong to the multi-company resolver's model
    fallback alone (slice M5b): what the model answered and what the
    deterministic rule then enforced, in the resolver domain's three-case
    vocabulary (``resolved | ambiguous | no_match``). They carry no CHECK —
    the vocabulary is the resolver's own, and the deterministic rule stays
    the one enforcer; runs the rule decided without a model leave both NULL.
    """

    __tablename__ = "extraction_assessments"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('change_event', 'communication', 'document', 'quarantine', "
            "'split_work_item')",
            name="extraction_assessments_subject_type_check",
        ),
        CheckConstraint(
            "component IN ('reply_parser', 'document_extractor', 'multi_company_resolver')",
            name="extraction_assessments_component_check",
        ),
        CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 1)",
            name="extraction_assessments_overall_score_check",
        ),
        CheckConstraint(
            "routing IS NULL OR routing IN ('auto_accept', 'staff_review', 'client_clarification')",
            name="extraction_assessments_routing_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[UUID] = mapped_column(index=True)
    component: Mapped[str] = mapped_column(String(30))
    # The reader that produced the scores: a pinned model id for the LLM
    # paths, the extractor implementation for the deterministic ones.
    model: Mapped[str] = mapped_column(String(100))
    field_scores: Mapped[dict[str, Any]] = mapped_column(JSON)
    signal_log: Mapped[dict[str, Any]] = mapped_column(JSON)
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    routing: Mapped[str | None] = mapped_column(String(30))
    # The resolver model fallback's pair (slice M5b, migration 0029): the
    # model's answer and the rule's enforcement, in the resolver domain's
    # three-case vocabulary — nullable, no CHECK (see the class docstring).
    raw_model_resolution: Mapped[str | None] = mapped_column(String(20))
    enforced_resolution: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
