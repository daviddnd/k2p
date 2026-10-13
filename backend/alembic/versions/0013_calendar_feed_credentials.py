"""calendar feed credentials: the per-tenant capability URL (scope §2.4;
ADR-0013 item 3)

One row per subscription credential a TCSP has created for the `.ics` feed.
The token itself is never stored — only its SHA-256 digest
(``app.domain.calendar_sync.hash_feed_token``): the row would otherwise be a
live credential for a URL whose body names the TCSP's client companies,
sitting in every database backup.

At most one active credential per tenant (partial unique index on
``revoked_at IS NULL``), so rotation is explicit: revoke, then create.
Revocation is a timestamp, not a delete — the row is the record that the
credential existed, and the audit chain holds who created and revoked it.

No backfill: a tenant with no row has simply never created a subscription.

Revision ID: 0013_calendar_feed_credentials
Revises: 0012_command_inbox
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_calendar_feed_credentials"
down_revision: str | None = "0012_command_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_feed_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # SHA-256 hex digest of the token — never the token (module note).
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        # NULL = system actor; composite FK pins the actor to the tenant (F1).
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"], ["users.id", "users.tenant_id"]
        ),
    )
    op.create_index(
        "ux_calendar_feed_credentials_token_hash",
        "calendar_feed_credentials",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ux_calendar_feed_credentials_active_tenant",
        "calendar_feed_credentials",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # The model declares tenant_id indexed (the house convention for every
    # tenant-scoped table) and the admin listing reads across revoked rows
    # too, which the partial unique index above does not cover.
    op.create_index(
        "ix_calendar_feed_credentials_tenant_id",
        "calendar_feed_credentials",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_feed_credentials_tenant_id", table_name="calendar_feed_credentials"
    )
    op.drop_index(
        "ux_calendar_feed_credentials_active_tenant", table_name="calendar_feed_credentials"
    )
    op.drop_index("ux_calendar_feed_credentials_token_hash", table_name="calendar_feed_credentials")
    op.drop_table("calendar_feed_credentials")
