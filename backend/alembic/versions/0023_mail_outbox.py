"""the send outbox, the outbound communication's columns, and the send modes

Three changes from the sending slice (contract §5.1.8's M2 note, §5.1.11):

1. **``mail_outbox``** — ADR-0011 §1's outbox: one row per intent to send a
   confirmation request's stored message, written in the same transaction as
   the release that requests it and delivered by a worker afterwards. The
   partial unique index ``(tenant_id, confirmation_request_id) WHERE pending``
   is the release's idempotency, so two overlapping sends cannot both enqueue.

2. **``communications``** gains the two columns an outbound row needs —
   ``confirmation_request_id`` (the composite foreign key 0018's unique key
   exists for) and ``recipient_addresses`` — so one row per *exchange* stays
   true for both directions, and its ``mailbox_message_id`` widens from 128 to
   the outbox row's own 320. On the SMTP leg that column holds the RFC 5322
   ``Message-ID`` the deployment stamped, which the mailbox's own domain sizes,
   and the sender writes it **after** the provider accepted the message — so a
   width that cannot hold it is a 500 and a duplicate on the retry, not a
   refusal. **No backfill**: every existing row is an inbound
   one, and both columns are null for it, which is exactly what "inbound" means
   here.

3. **``confirmation_requests.status``** widens from ``draft | superseded`` to
   the send lifecycle ``draft | queued | sent | superseded``, and the "one live
   request per company and form" index now covers ``draft`` **and** ``queued``:
   a released-but-undelivered request is still the live one, so a regeneration
   supersedes it instead of letting two letters race. **No backfill**: existing
   rows are drafts and superseded requests, both of which the new vocabulary
   and index already cover — widening an index predicate and a CHECK is the
   one migration here that changes nothing about existing data.

The downgrade deletes nothing (the migration-0017 precedent): the two new
statuses map onto the vocabulary that predates them — ``queued`` back to
``draft``, ``sent`` to ``superseded`` — and the chain, the ``Communication``
rows and the stored messages keep the facts that the narrower vocabulary cannot
express.

``tenant_config.send_modes`` is added NOT NULL with the shipped defaults as the
column's own default, the milestone block's pattern (migration ``0019``): the
rows that predate the column are filled from it, and the read path has no NULL
branch. The defaults are a literal in this file, not an import of
``app.domain.send_modes``: a migration must be reproducible from its own text.

Revision ID: 0023_mail_outbox
Revises: 0022_milestone_default_chain
Create Date: 2026-09-25

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_mail_outbox"
down_revision: str | None = "0022_milestone_default_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The shipped send modes as of this migration (founder-ruled 2026-09-24):
# client mail goes out automatically for efficiency, and anything bound for a
# government filing destination waits for a staff member — which is the only
# value that destination admits (``app.domain.send_modes``).
DEFAULT_SEND_MODES = {"client": "automatic", "government": "review_required"}


def upgrade() -> None:
    op.create_table(
        "mail_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("confirmation_request_id", sa.Uuid(), nullable=False),
        sa.Column("destination", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("message_id", sa.String(length=320), nullable=True),
        sa.Column("provider_message_id", sa.String(length=320), nullable=True),
        sa.Column("communication_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # The company, the request, the exchange it became, and the releasing
        # account, each pinned to this row's own tenant (F1).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_request_id", "tenant_id"],
            ["confirmation_requests.id", "confirmation_requests.tenant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint(
            "destination IN ('client', 'government')", name="mail_outbox_destination_check"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'cancelled')",
            name="mail_outbox_status_check",
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR (sent_at IS NOT NULL AND last_error IS NULL "
            "AND blocked_reason IS NULL)",
            name="mail_outbox_sent_check",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL", name="mail_outbox_failed_check"
        ),
    )
    op.create_index("ix_mail_outbox_tenant_id", "mail_outbox", ["tenant_id"])
    op.create_index("ix_mail_outbox_company_id", "mail_outbox", ["company_id"])
    op.create_index(
        "ix_mail_outbox_confirmation_request_id", "mail_outbox", ["confirmation_request_id"]
    )
    op.create_index("ix_mail_outbox_status_created", "mail_outbox", ["status", "created_at"])
    op.create_index(
        "uq_mail_outbox_pending_per_request",
        "mail_outbox",
        ["tenant_id", "confirmation_request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # The stamped ``Message-ID`` the SMTP leg records here is sized by the
    # mailbox's own domain, and the sender writes it after the provider
    # accepted the message: the column holds what the outbox row's own
    # ``message_id`` (320) holds, or a long domain's letter cannot be recorded.
    op.alter_column(
        "communications",
        "mailbox_message_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=320),
        existing_nullable=True,
    )
    op.add_column("communications", sa.Column("recipient_addresses", sa.String(), nullable=True))
    op.add_column("communications", sa.Column("confirmation_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_communications_confirmation_request",
        "communications",
        "confirmation_requests",
        ["confirmation_request_id", "tenant_id"],
        ["id", "tenant_id"],
    )

    op.drop_index("uq_confirmation_requests_open_draft", table_name="confirmation_requests")
    op.drop_constraint(
        "confirmation_requests_status_check", "confirmation_requests", type_="check"
    )
    op.create_check_constraint(
        "confirmation_requests_status_check",
        "confirmation_requests",
        "status IN ('draft', 'queued', 'sent', 'superseded')",
    )
    op.create_index(
        "uq_confirmation_requests_open_draft",
        "confirmation_requests",
        ["tenant_id", "company_id", "form_type"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft', 'queued')"),
    )

    op.add_column(
        "tenant_config",
        sa.Column(
            "send_modes",
            sa.JSON(),
            nullable=False,
            # The backfill: existing rows are filled from the new column's
            # default, and the default stays for any later row that omits it.
            server_default=sa.text(f"'{json.dumps(DEFAULT_SEND_MODES)}'::json"),
        ),
    )


def downgrade() -> None:
    # Nothing is deleted, and nothing is silently invented. The old vocabulary
    # cannot say ``queued`` or ``sent``, so a released-but-undelivered request
    # comes back as ``draft`` (the old schema's only open state, which is what
    # the release left it) and a sent one as ``superseded`` (the old schema's
    # only closed state). Neither is the whole truth, and the whole truth
    # survives the downgrade elsewhere: the ``Communication`` row the message
    # became, its stored ``.eml``, and the audit chain's
    # ``confirmation_request_released`` / ``mail_message_sent`` entries all
    # stay, so what was sent, to whom and when is still readable. What a
    # schema without the outbox loses is the **queue**: a release that had not
    # been delivered has nowhere to live, which is exactly what downgrading a
    # schema that could not express it means (the migration-0017 precedent).
    op.drop_column("tenant_config", "send_modes")

    op.drop_index("uq_confirmation_requests_open_draft", table_name="confirmation_requests")
    op.drop_constraint(
        "confirmation_requests_status_check", "confirmation_requests", type_="check"
    )
    op.execute("UPDATE confirmation_requests SET status = 'draft' WHERE status = 'queued'")
    op.execute("UPDATE confirmation_requests SET status = 'superseded' WHERE status = 'sent'")
    op.create_check_constraint(
        "confirmation_requests_status_check",
        "confirmation_requests",
        "status IN ('draft', 'superseded')",
    )
    op.create_index(
        "uq_confirmation_requests_open_draft",
        "confirmation_requests",
        ["tenant_id", "company_id", "form_type"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )

    op.drop_constraint(
        "fk_communications_confirmation_request", "communications", type_="foreignkey"
    )
    op.drop_column("communications", "confirmation_request_id")
    op.drop_column("communications", "recipient_addresses")
    # Symmetric with the widening above, and it can fail: an id longer than
    # 128 characters recorded by this revision does not fit the width that
    # precedes it. Postgres refuses the narrowing rather than truncating, which
    # is the honest answer — the row is the only copy of that identity.
    op.alter_column(
        "communications",
        "mailbox_message_id",
        existing_type=sa.String(length=320),
        type_=sa.String(length=128),
        existing_nullable=True,
    )

    op.drop_index("uq_mail_outbox_pending_per_request", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_status_created", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_confirmation_request_id", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_company_id", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_tenant_id", table_name="mail_outbox")
    op.drop_table("mail_outbox")
