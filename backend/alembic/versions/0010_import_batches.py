"""prior-filing ingestion: import batches, proposed records, assessments

The execution-plane half of §2.1's data ingestion (scope §2.1, §2.15 gate):
one document upload is extracted into one ``import_batches`` run, whose
``import_batch_records`` are proposals awaiting staff review — only
``confirmed`` records become authoritative master data, written through the
existing single write path (``company_master.record_fact`` /
``correct_statics``). ``extraction_assessments`` carries the §2.7
diagnostic split: per-field scores, the fired signals, and the routing
band, kept apart from business history so a re-parse can recompute them.

``import_batches`` also records which input shape the reader found
(``needs_ocr`` is a recorded outcome — scans are read by the E2 OCR slice,
and this slice refuses to guess at them) and which form registry produced
the proposals, since the ``documents`` row does not carry the form code.

Tenant pinning follows the F1 pattern: composite FKs pin the company, the
batch, and the deciding user to the row's tenant, so a cross-tenant row is
uninsertable. The ``document_id`` link is a plain FK — ``documents`` has no
``(id, tenant_id)`` unique key (migration 0009) — and the application
resolves the document through the company-scoped read seam instead.

``target`` carries no CHECK: the writable target vocabulary lives in
``app.domain.prior_filing`` and grows as extraction covers more of the
registry (the same posture as ``audit_log.action``); ``status``,
``routing``, ``component`` and ``subject_type`` are closed vocabularies and
are checked.

Revision ID: 0010_import_batches
Revises: 0009_documents
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_import_batches"
down_revision: str | None = "0009_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        # The registry entry the proposals came from; the documents row does
        # not carry the form code.
        sa.Column("form", sa.String(length=10), nullable=False),
        # The §2.1 input shape the reader found (needs_ocr is an outcome).
        sa.Column("shape", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        # One tenant_id per row: composite FKs pin the company and the
        # trigger user into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.UniqueConstraint("id", "tenant_id"),
        sa.CheckConstraint("form IN ('nar1', 'nd2a')", name="import_batches_form_check"),
        sa.CheckConstraint(
            "shape IN ('acroform', 'text_layer', 'needs_ocr')",
            name="import_batches_shape_check",
        ),
    )
    op.create_index("ix_import_batches_tenant_id", "import_batches", ["tenant_id"])
    op.create_index("ix_import_batches_company_id", "import_batches", ["company_id"])

    op.create_table(
        "import_batch_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # A contract §1 master-data path; the vocabulary lives in
        # app.domain.prior_filing (no CHECK — it grows with the registry).
        sa.Column("target", sa.String(length=120), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        # The date the filing stated, or the reviewer's at confirmation
        # (ADR-0007); NULL on a pending record means the filing stated none.
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["batch_id", "tenant_id"], ["import_batches.id", "import_batches.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["decided_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="import_batch_records_status_check",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="import_batch_records_confidence_check",
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name="import_batch_records_decided_check",
        ),
    )
    op.create_index("ix_import_batch_records_batch_id", "import_batch_records", ["batch_id"])
    op.create_index("ix_import_batch_records_tenant_id", "import_batch_records", ["tenant_id"])

    op.create_table(
        "extraction_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("field_scores", sa.JSON(), nullable=False),
        sa.Column("signal_log", sa.JSON(), nullable=False),
        # NULL when nothing was assessed (a shape with no readable field).
        sa.Column("overall_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("routing", sa.String(length=30), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.CheckConstraint(
            "subject_type IN ('change_event', 'communication', 'document')",
            name="extraction_assessments_subject_type_check",
        ),
        sa.CheckConstraint(
            "component IN ('reply_parser', 'document_extractor', 'multi_company_resolver')",
            name="extraction_assessments_component_check",
        ),
        sa.CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 1)",
            name="extraction_assessments_overall_score_check",
        ),
        sa.CheckConstraint(
            "routing IS NULL OR routing IN "
            "('auto_accept', 'staff_review', 'client_clarification')",
            name="extraction_assessments_routing_check",
        ),
    )
    op.create_index(
        "ix_extraction_assessments_tenant_id", "extraction_assessments", ["tenant_id"]
    )
    op.create_index(
        "ix_extraction_assessments_subject_id", "extraction_assessments", ["subject_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_assessments_subject_id", table_name="extraction_assessments")
    op.drop_index("ix_extraction_assessments_tenant_id", table_name="extraction_assessments")
    op.drop_table("extraction_assessments")
    op.drop_index("ix_import_batch_records_tenant_id", table_name="import_batch_records")
    op.drop_index("ix_import_batch_records_batch_id", table_name="import_batch_records")
    op.drop_table("import_batch_records")
    op.drop_index("ix_import_batches_company_id", table_name="import_batches")
    op.drop_index("ix_import_batches_tenant_id", table_name="import_batches")
    op.drop_table("import_batches")
