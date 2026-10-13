"""tenant configuration: per-tenant naming/storage templates

TenantConfig (data-model §2.9; ADR-0013 item 1; scope §2.15). One row per
tenant carrying ``naming_templates`` — the token-based document/generated
storage-key templates validated per contract §5.3. No backfill: a tenant
without a row reads the shipped §5.3 defaults (fail-safe default), so the
migration only creates the table. Later TenantConfig fields (auto-send,
reminder cadence, calendar sync) land with their own phases.

Revision ID: 0006_tenant_config
Revises: 0005_external_contacts
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_tenant_config"
down_revision: str | None = "0005_external_contacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_config",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # {"document": "...", "generated": "..."} — validated per contract §5.3
        # by the single domain rule before write.
        sa.Column("naming_templates", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )


def downgrade() -> None:
    op.drop_table("tenant_config")
