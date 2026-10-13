"""command inbox: envelopes received and outcomes produced locally
(ADR-0012 open action 3; contract §5.2)

One row per command envelope the worker received, holding the envelope
exactly as it arrived and — once the command has been executed — the result
envelope produced from it. ADR-0012 requires outcomes and audit entries to be
written locally regardless of connectivity, so this table is what a
deployment that is offline keeps: an executed command whose report could not
leave the building, waiting for the next successful cycle.

Both JSON columns are closed documents (``app.domain.commands``): opaque
references, counts, durations, versions, timestamps, and command names. The
primary key is the envelope's ``command_id`` — the transport's idempotency
key — so a re-delivered envelope cannot produce a second row, a second
execution, or a second audit entry.

``status`` mirrors the result document and drives the worker's two queries:
null means "received, not executed yet", non-null with ``reported_at`` null
means "executed, not reported yet".

Revision ID: 0012_command_inbox
Revises: 0011_lease_cache
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_command_inbox"
down_revision: str | None = "0011_lease_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_inbox",
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("command_id"),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('succeeded', 'failed', 'rejected')",
            name="command_inbox_status_check",
        ),
        sa.CheckConstraint(
            "(status IS NULL) = (result IS NULL)",
            name="command_inbox_result_check",
        ),
        sa.CheckConstraint(
            "(result IS NULL) = (completed_at IS NULL)",
            name="command_inbox_completion_check",
        ),
        sa.CheckConstraint(
            "reported_at IS NULL OR result IS NOT NULL",
            name="command_inbox_report_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("command_inbox")
