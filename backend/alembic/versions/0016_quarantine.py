"""quarantine: the messages that could not be attributed (scope §2.5;
data-model §2.9, contract §5.1.7)

Scope §2.5: "Replies from unregistered addresses are quarantined for staff
review." ``quarantined_messages`` is that record: one row per message the
ingestion cycle could not attribute to exactly one company, holding the
provider's message and thread ids, the sender address, the provider's receipt
time, why it was refused, and — once staff work it out — when and into which
communication it was resolved.

The table carries **no ``company_id``** (the message has not been attributed,
which is the whole point) and no subject, body, or attachment column (the
mail stays in the mailbox, ADR-0005; the row is metadata staff can act on).

``reason`` follows the sender, not the resolver's case: an address registered
for no company is ``unregistered_sender``, and one registered for several is
``multiple_companies`` — including a message of theirs that names none of
them, because that sender *is* registered.

The unique index on ``(tenant_id, mailbox_message_id)`` is the same
idempotency guarantee 0015 gives ``communications``: one quarantine row per
message, ever, however often a cycle's overlap re-reads it. It is a plain
unique index — this table's message id is ``NOT NULL``, so there is no
nullable case to exclude (0015's is partial because ``communications``
records outbound rows under an id that may be absent).

``extraction_assessments.subject_type`` gains ``'quarantine'`` here. §2.7's
vocabulary names the table ``subject_id`` points into, and a message that
could not be attributed has no ``Communication`` row to be assessed against —
only its quarantine record — so the type has to exist for that id to resolve
anywhere. The constraint is amended in place because this migration is
unreleased; the downgrade restores the three-value vocabulary.

``communications`` gains its ``(id, tenant_id)`` unique key in this migration
too: it is the composite parent key the F1 pattern needs for
``resolved_communication_id`` to be a real composite foreign key, so a
quarantine row can only ever point at a communication in its own tenant (the
precedent is ``import_batches``, migration 0010).

Tenant pinning follows the F1 pattern: the composite foreign keys pin the row
to its tenant, so cross-tenant rows are uninsertable.

No backfill: a deployment that has never refused a message has no rows.

Revision ID: 0016_quarantine
Revises: 0015_mail_communications
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_quarantine"
down_revision: str | None = "0015_mail_communications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The composite parent key the foreign key below needs (the neighbour
    # tables carry theirs from their own CREATE TABLE; ``communications`` is
    # already there, so this migration adds it — under the name Postgres
    # gives an unnamed ``UNIQUE (id, tenant_id)``, which is what the model
    # declares).
    op.create_unique_constraint(
        "communications_id_tenant_id_key", "communications", ["id", "tenant_id"]
    )
    op.create_table(
        "quarantined_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_message_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_communication_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # The communication this message became, in this row's own tenant
        # (F1): the parent key is created just below.
        sa.ForeignKeyConstraint(
            ["resolved_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        sa.CheckConstraint(
            "reason IN ('unregistered_sender', 'multiple_companies')",
            name="quarantined_messages_reason_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="quarantined_messages_status_check",
        ),
    )
    op.create_index("ix_quarantined_messages_tenant_id", "quarantined_messages", ["tenant_id"])
    op.create_index(
        "uq_quarantined_messages_message_id_per_tenant",
        "quarantined_messages",
        ["tenant_id", "mailbox_message_id"],
        unique=True,
    )
    # §2.7's subject_type names the table ``subject_id`` points into, and a
    # quarantined message's assessment points at its quarantine row (see the
    # module docstring).
    op.drop_constraint(
        "extraction_assessments_subject_type_check", "extraction_assessments", type_="check"
    )
    op.create_check_constraint(
        "extraction_assessments_subject_type_check",
        "extraction_assessments",
        "subject_type IN ('change_event', 'communication', 'document', 'quarantine')",
    )


def downgrade() -> None:
    # The assessments about quarantined messages go with the table: they are
    # recomputable diagnostics (data-model §2.7) whose subject this downgrade
    # removes, and the restored vocabulary below could not describe them.
    op.execute("DELETE FROM extraction_assessments WHERE subject_type = 'quarantine'")
    op.drop_constraint(
        "extraction_assessments_subject_type_check", "extraction_assessments", type_="check"
    )
    op.create_check_constraint(
        "extraction_assessments_subject_type_check",
        "extraction_assessments",
        "subject_type IN ('change_event', 'communication', 'document')",
    )
    op.drop_index(
        "uq_quarantined_messages_message_id_per_tenant", table_name="quarantined_messages"
    )
    op.drop_index("ix_quarantined_messages_tenant_id", table_name="quarantined_messages")
    op.drop_table("quarantined_messages")
    op.drop_constraint("communications_id_tenant_id_key", "communications", type_="unique")
