"""reminder scheduling: the outbox's ``not_before``/``kind``/letter, and the
Communication's ``kind`` (contract §5.1.12, slice R)

The reminder sweep enqueues a reminder the same way the release enqueues a
request's send — one outbox row, written in the same transaction as the
decision — but a reminder may not leave until its cadence instant, so the
row gains what the release never needed (§5.1.12's "the outbox column"):

1. **``mail_outbox.not_before``** (nullable timestamptz): ``NULL`` is exactly
   today's send-on-release behaviour, unchanged — every existing row keeps
   it. The sweep stamps the cadence instant; the worker's selection gains the
   ``not_before IS NULL OR not_before <= now`` leg.
2. **``mail_outbox.kind``** (``confirmation_request | reminder``, NOT NULL,
   server default ``confirmation_request``): what the row sends. The default
   is the backfill — every stored row is a request's send — and it stays, so
   a writer that omits the column gets the M2 behaviour rather than a
   refusal.
3. **``mail_outbox.letter_document_id``** (nullable FK to ``documents``): a
   reminder row names the stored ``.eml`` letter it will deliver; a request
   row leaves it ``NULL`` and keeps deriving the letter from the request,
   exactly as since migration ``0023``.
4. The partial unique pending index widens from ``(tenant_id,
   confirmation_request_id)`` to include ``kind``: release idempotency is per
   request **and** kind (§5.1.12), so a request and its reminder may both
   pend.

**``communications.kind``** is §2.9's five-kind vocabulary —
``confirmation_request | reminder | reply | reconfirmation | clarification`` —
made a column. The backfill is deterministic per §5.1.12: every stored
outbound row is a ``confirmation_request`` (the only outbound mail v1.0
writes), every stored inbound row is a ``reply``. The column is added nullable,
backfilled in two statements, then made NOT NULL under the CHECK — one
statement per step so the backfill is visible in the script rather than hidden
in a server default. There is deliberately no server default here: what an
exchange *is* is a fact the writer records, and the two existing writers set
it explicitly.

The downgrade deletes the new columns, CHECKs and the widened index, and
restores the narrower pending index — nothing else. The M2 rows' facts
survive in the ``Communication`` rows and the stored letters, as in migration
``0023``'s downgrade. The narrower index's restoration **can fail**, the
``0023`` mailbox-message-id narrowing precedent: rows this revision
legitimately holds — a request and its reminder both pending for one request —
have no shape the ``0026`` index admits, and Postgres refuses the index
rather than drop a row, which is the honest answer. The remedy is to deliver
or cancel the pending reminder rows first; the data the wider schema added
cannot be silently flattened into the narrower one.

Revision ID: 0027_reminder_scheduling
Revises: 0026_drafts
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_reminder_scheduling"
down_revision: str | None = "0026_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1-3: the scheduling column, the row kind, and the reminder letter
    # reference. ``not_before`` is nullable with no default: NULL is the
    # send-on-release behaviour every stored row already has.
    op.add_column("mail_outbox", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    # The server default is the backfill (every stored row is a request's
    # send) and stays for writers that omit the column — §5.1.12 pins the M2
    # behaviour as the default, not a NOT NULL refusal.
    op.add_column(
        "mail_outbox",
        sa.Column(
            "kind",
            sa.String(length=20),
            server_default=sa.text("'confirmation_request'"),
            nullable=False,
        ),
    )
    # Plain FK: documents has no (id, tenant_id) unique key, the
    # communications.raw_document_id precedent.
    op.add_column("mail_outbox", sa.Column("letter_document_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_mail_outbox_letter_document",
        "mail_outbox",
        "documents",
        ["letter_document_id"],
        ["id"],
    )
    op.create_check_constraint(
        "mail_outbox_kind_check", "mail_outbox", "kind IN ('confirmation_request', 'reminder')"
    )
    # 4: the widened partial unique index — same name, one more column, so a
    # request and its reminder may both pend (idempotency per request *and*
    # kind).
    op.drop_index("uq_mail_outbox_pending_per_request", table_name="mail_outbox")
    op.create_index(
        "uq_mail_outbox_pending_per_request",
        "mail_outbox",
        ["tenant_id", "confirmation_request_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # The Communication's role in its thread, added nullable, backfilled
    # deterministically (§5.1.12: outbound = confirmation_request, inbound =
    # reply), then NOT NULL under the five-kind CHECK.
    op.add_column("communications", sa.Column("kind", sa.String(length=20), nullable=True))
    op.execute(
        "UPDATE communications SET kind = 'confirmation_request' WHERE direction = 'outbound'"
    )
    op.execute("UPDATE communications SET kind = 'reply' WHERE direction = 'inbound'")
    op.alter_column("communications", "kind", nullable=False)
    op.create_check_constraint(
        "communications_kind_check",
        "communications",
        "kind IN ('confirmation_request', 'reminder', 'reply', 'reconfirmation', 'clarification')",
    )


def downgrade() -> None:
    op.drop_constraint("communications_kind_check", "communications", type_="check")
    op.drop_column("communications", "kind")

    # The narrow index's restoration can fail on data this revision
    # legitimately holds: a request and its reminder both pending for one
    # request cannot be expressed by a (tenant_id, confirmation_request_id)
    # unique index, and Postgres refuses rather than dropping a row (the
    # migration-0023 narrowing precedent, stated in the docstring above).
    op.drop_index("uq_mail_outbox_pending_per_request", table_name="mail_outbox")
    op.create_index(
        "uq_mail_outbox_pending_per_request",
        "mail_outbox",
        ["tenant_id", "confirmation_request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_constraint("mail_outbox_kind_check", "mail_outbox", type_="check")
    op.drop_constraint("fk_mail_outbox_letter_document", "mail_outbox", type_="foreignkey")
    op.drop_column("mail_outbox", "letter_document_id")
    op.drop_column("mail_outbox", "kind")
    op.drop_column("mail_outbox", "not_before")
