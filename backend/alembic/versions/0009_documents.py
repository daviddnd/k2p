"""records registry: documents table (ADR-0009)

The metadata half of the records registry (blobs live in MinIO). One row
per stored object: the company it belongs to, the §2.9 ``kind``, the
ADR-0009 ``origin`` and ``retention_class`` (tagged from day one so
lifecycle purging can land later without reclassifying a legacy corpus),
the SHA-256 computed at ingestion, the storage key rendered from the
tenant's naming template at write time (ADR-0013 — template changes never
rewrite stored keys, preserving WORM), and the version chain
(``supersedes_document_id`` — new versions are new objects; supersession
is metadata only).

``scan_status`` implements the option (a) interim malware-scan posture
(founder-approved 2026-08-25): uploads land ``pending`` — validated by
type and size (scope §8) but not yet malware-scanned; ClamAV enforcement
lands with the Phase 3 email-ingestion slice. See the ADR-0009 interim
amendment: ``pending`` files may be served/processed only in
internal/development deployments.

Tenant pinning follows the F1 pattern: composite FKs pin the company and
the uploader to the row's tenant, so cross-tenant rows are uninsertable.
Rows are mutable (scan status, supersession metadata) — only the blobs are
WORM; the append-only guarantee for *events* lives in ``audit_log``.

Revision ID: 0009_documents
Revises: 0008_audit_log
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_documents"
down_revision: str | None = "0008_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("retention_class", sa.String(length=30), nullable=False),
        # SHA-256 hex digest computed at ingestion (ADR-0009: hash-at-
        # ingestion gives dispute-grade evidence; re-verified by the
        # ADR-0011 integrity job).
        sa.Column("sha256", sa.String(length=64), nullable=False),
        # Rendered from the tenant's naming template at write time; never
        # rewritten (WORM — ADR-0009/0013).
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("scan_status", sa.String(length=20), server_default="pending", nullable=False),
        # Version chain: new versions are new objects; supersession happens
        # in metadata only (ADR-0009).
        sa.Column("supersedes_document_id", sa.Uuid(), nullable=True),
        # NULL = system (generated documents in later phases).
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force the company and the
        # uploader into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["uploaded_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(["supersedes_document_id"], ["documents.id"]),
        sa.UniqueConstraint("storage_key"),
        sa.CheckConstraint(
            "kind IN ('prior_filing', 'client_reply', 'signed_draft', 'generated_draft', "
            "'export_package', 'client_document')",
            name="documents_kind_check",
        ),
        sa.CheckConstraint(
            "origin IN ('upload', 'email_attachment', 'generated')",
            name="documents_origin_check",
        ),
        sa.CheckConstraint(
            "retention_class IN ('statutory_filing', 'client_communication', "
            "'cdd_id_document', 'generated_draft', 'export_package')",
            name="documents_retention_class_check",
        ),
        sa.CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected')",
            name="documents_scan_status_check",
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_company_id", "documents", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_company_id", table_name="documents")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
