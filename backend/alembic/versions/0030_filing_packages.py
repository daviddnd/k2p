"""filing_packages: the FilingPackage — one row per export (slice P6-4)

Phase 6's export record (contract §2.8, §2.12, §4.2; scope §2.11). One row
per export of one draft version: the mode (the three-value §2.12
vocabulary), the WORM ``export_package`` document the primary artifact was
stored as, the ``exported_by/at`` pair, and the **envelope** — the derived
counts used at assembly (sheet counts per slug, each sheet copy's
page-number pair) plus the companion documents' ids (the e-Registry helper,
the paper checklist — ``None`` where the mode carries none), the shape
``app.domain.export_package`` owns (JSONB, deliberately not a CHECK, the
drafts' documents precedent).

The draft linkage names one **version row**: ``(draft_id, draft_version_no)``
references ``drafts``' primary key, and the tenant is pinned through the
``drafts`` ``(id, version_no, tenant_id)`` unique target — the child-table
shape migration ``0026`` reserved for exactly this table, so a cross-tenant
package is uninsertable at the database. The exporter joins ``users`` the
F1 way; the artifact joins ``documents`` the same way, which needs the
``(id, tenant_id)`` unique constraint this revision also adds to
``documents`` — every other table the single-row pattern references already
carries one (the F1 hardening pattern), and ``documents`` having been built
before any child needed it is the only reason it did not.

``mode`` is a three-value CHECK — the backstop a caller cannot bypass;
``exported_by``/``exported_at`` are NOT NULL because an export is a staff
action recorded with its actor, never the system's. ``created_at`` is
``clock_timestamp()`` per migration ``0004``'s rule; the index is the
lineage lookup a draft's export history makes.

The rows are append-only in practice: §4.2's guard and the executor make a
second export of one version unreachable (``exported`` admits nothing), so
no uniqueness beyond the primary key is needed — the lifecycle, not a
constraint, is the idempotence story (Review Focus 4).

No backfill: the table is new, and no earlier revision wrote an export.

Built HELD for founder sign-off before apply (the M6 pattern): the revision
is committed with the code, applied to the review databases by the slice's
owner after sign-off, never by this change itself.

Revision ID: 0030_filing_packages
Revises: 0029_reply_parsing
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030_filing_packages"
down_revision: str | None = "0029_reply_parsing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODE_CHECK = "mode IN ('e_registry_prep', 'paper', 'api_ready')"


def upgrade() -> None:
    # The composite target documents needs for F1 pinning from a child table
    # (id alone is the primary key, so the pair is trivially unique).
    op.create_unique_constraint("uq_documents_id_tenant", "documents", ["id", "tenant_id"])

    op.create_table(
        "filing_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("draft_version_no", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("exported_by", sa.Uuid(), nullable=False),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # F1 tenant pinning: the exporter and the stored artifact join this
        # row's tenant; the draft linkage names one version row, pinned
        # through the drafts (id, version_no, tenant_id) unique target.
        sa.ForeignKeyConstraint(["exported_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(
            ["draft_id", "draft_version_no", "tenant_id"],
            ["drafts.id", "drafts.version_no", "drafts.tenant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        sa.CheckConstraint(_MODE_CHECK, name="filing_packages_mode_check"),
    )
    op.create_index(
        "ix_filing_packages_tenant_draft", "filing_packages", ["tenant_id", "draft_id"]
    )


def downgrade() -> None:
    # The table drops whole (the export's business record — the audit chain
    # keeps what each export *was*, the migration-0029 precedent for a
    # slice's own table), and the documents constraint with it.
    op.drop_index("ix_filing_packages_tenant_draft", table_name="filing_packages")
    op.drop_table("filing_packages")
    op.drop_constraint("uq_documents_id_tenant", "documents", type_="unique")
