"""pack cache: the licensed packs this deployment has downloaded and can
still refresh (ADR-0012; contract §2.2 ``PackCache``)

One row per (pack_type, version). A new version is added beside the old one
rather than replacing it: ADR-0012 promises that a customer who lapses keeps
what they hold, so nothing local is deleted when a newer pack arrives.

``frozen``/``frozen_at`` are the §4.3 downgrade switch, set together by a
check constraint. Frozen content stays readable — existing drafts still
render and manual work is never gated — and only refreshing stops.

The payloads are licensed vendor content (form mappings, deadline rules, fee
schedules, prompt sets), never client data, and no audit entry ever carries
them: the audit records pack type, version, and digest.

No backfill: a deployment that has never refreshed has no rows.

Revision ID: 0014_pack_cache
Revises: 0013_calendar_feed_credentials
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_pack_cache"
down_revision: str | None = "0013_calendar_feed_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pack_cache",
        sa.Column("pack_type", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("pack_type", "version"),
        sa.CheckConstraint(
            "pack_type IN ('form_mapping', 'deadline_rules', 'fee_schedule', 'prompt_pack')",
            name="pack_cache_type_check",
        ),
        sa.CheckConstraint("version > 0", name="pack_cache_version_check"),
        sa.CheckConstraint(
            "(frozen AND frozen_at IS NOT NULL) OR (NOT frozen AND frozen_at IS NULL)",
            name="pack_cache_frozen_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("pack_cache")
