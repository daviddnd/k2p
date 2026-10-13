"""inbound mail: communications and the mailbox ingestion cursor (ADR-0005;
data-model §2.9, contract §5.1.7)

``communications`` records one exchange with a client. This migration creates
the half the ingestion slice writes — inbound email: the provider's message
and thread ids, the sender address the attribution rule matched on, the
provider's receipt time (the cursor is derived from it), and the stored raw
message (``raw_document_id``). ``language`` is present and null: the reply
parser fills it. The §2.9 fact checklist and the §2.5 quarantine flag are not
columns yet — they land with the slice that has something to put in them.

The partial unique index on ``(tenant_id, mailbox_message_id)`` is what makes
ingestion idempotent at the database level: a provider message id is recorded
once per tenant however often a cycle's cursor looks at it.

``mail_ingestion_state`` is the cursor itself: one row per tenant, the
provider ``internalDate`` of the newest message examined. It holds no client
data — the messages are in ``communications`` and the records registry — so
it is written without audit entries.

Tenant pinning follows the F1 pattern: composite FKs pin the company to the
row's tenant, so cross-tenant rows are uninsertable.

No backfill: a deployment that has never ingested a message has no rows.

Revision ID: 0015_mail_communications
Revises: 0014_pack_cache
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_mail_communications"
down_revision: str | None = "0014_pack_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "communications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("mailbox_message_id", sa.String(length=128), nullable=True),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        # A plain FK: documents has no (id, tenant_id) unique key, which the
        # company composite FK above already forces this row into.
        sa.ForeignKeyConstraint(["raw_document_id"], ["documents.id"]),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="communications_direction_check"
        ),
        sa.CheckConstraint("channel IN ('email')", name="communications_channel_check"),
        sa.CheckConstraint(
            "language IS NULL OR language IN ('en', 'zh-hk', 'mixed')",
            name="communications_language_check",
        ),
    )
    op.create_index("ix_communications_tenant_id", "communications", ["tenant_id"])
    op.create_index("ix_communications_company_id", "communications", ["company_id"])
    op.create_index(
        "uq_communications_message_id_per_tenant",
        "communications",
        ["tenant_id", "mailbox_message_id"],
        unique=True,
        postgresql_where=sa.text("mailbox_message_id IS NOT NULL"),
    )

    op.create_table(
        "mail_ingestion_state",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("cursor_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )


def downgrade() -> None:
    op.drop_table("mail_ingestion_state")
    op.drop_index("uq_communications_message_id_per_tenant", table_name="communications")
    op.drop_index("ix_communications_company_id", table_name="communications")
    op.drop_index("ix_communications_tenant_id", table_name="communications")
    op.drop_table("communications")
