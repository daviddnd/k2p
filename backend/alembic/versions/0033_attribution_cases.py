"""attribution cases: the registration/confirmation flow's row (ADR-0019 §1)

ADR-0019 §1's thread evidence and instruction-authority rules stand on one new
row and one widened outbox, one revision (the ``0028``/``0029`` single-
revision precedent):

1. **``attribution_cases``** (new table) — one row per original message a
   threaded sender that is not a registered channel of the chain's company
   produced (rulings R3/R6/R7). The row carries the two thread anchors the
   match reads (``thread_id``, ``original_rfc_message_id``), the sender, the
   provider message id (**unique per tenant**: one case per original message,
   ever — the ``quarantined_messages`` discipline), the ask side
   (``ask_kind``, ``ask_sent_at`` — NULL until an ask leaves, which is also
   the suppressed state), the status, and what the case became (the
   resolving and confirming exchanges, R5/R6). ``status`` and ``ask_kind``
   are ``app.domain.attribution``'s closed vocabularies verbatim, made
   CHECKs; the closure stamps pair with their statuses the ``0016``/``0017``
   way. The ``(id, tenant_id)`` unique key is the composite FK target the
   request-less outbox row below needs, and both ``communication`` columns
   are composite FKs into ``communications`` (F1 tenant pinning).

2. **``mail_outbox``** gains the request-less row shape and the ask's kind
   (rulings R4; contract §5.1.12): ``confirmation_request_id`` **drops NOT
   NULL** (the ask belongs to a case, not a request — every stored row is a
   request's letter, so nothing needs a backfill), ``attribution_case_id``
   is added as a nullable composite FK to ``attribution_cases``, the kind
   CHECK widens with ``attribution_ask``, and a second partial unique index
   — ``(tenant_id, attribution_case_id, kind) WHERE status = 'pending' AND
   attribution_case_id IS NOT NULL`` — carries the ask's enqueue idempotency.
   The existing ``uq_mail_outbox_pending_per_request`` is untouched: its
   predicate excludes the new rows (their request leg is NULL, and Postgres
   treats NULLs as distinct in a unique index), so the two indexes cannot
   collide.

3. **``communications.kind`` widens** (``String(20)`` → ``String(32)``) and
   its CHECK gains ``attribution_ask`` and ``attribution_confirmation`` —
   the ask the deployment sends, and the registered channel's confirming
   reply (R5), recorded as an ordinary exchange in the case's thread. The
   width move is forced by the vocabulary: §2.9's longest kind is
   ``confirmation_request`` (20), and ``attribution_confirmation`` is 24 — a
   column that cannot hold the kind its CHECK admits is a 500 on the
   confirming reply, not a refusal. 32 is this revision's vocabulary width
   (the new table's ``ask_kind``/``status`` columns carry it too).

No backfill anywhere: the table is new, the column drops a NOT NULL, and every
stored row's ``kind`` remains in the widened vocabularies.

The downgrade removes the new table's column, index and table and restores
the narrower CHECKs. Three of its moves **can fail**, the ``0027``/``0029``
precedent: a stored ``attribution_ask`` / ``attribution_confirmation`` exchange
has no shape the restored ``communications_kind_check`` admits (and cannot fit
the restored width), a pending ``attribution_ask`` row has none the restored
``mail_outbox_kind_check`` admits, and a request-less row cannot satisfy the
restored ``confirmation_request_id`` NOT NULL — Postgres refuses rather than
rewriting or dropping the rows, which is the honest answer, and the downgrade
body's own comment enumerates the same three. The remedy is to deliver or
cancel the pending ask rows
and let the exchanges stand (they are history) before narrowing; the data the
wider schema added cannot be silently flattened into the narrower one.

Built HELD for founder sign-off before apply (the M6/M5b/M7/0030/0031/0032
pattern): the revision is committed with the code, applied to the review
databases by the slice's owner after sign-off, never by this change itself.

Revision ID: 0033_attribution_cases
Revises: 0032_calendar_push
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_attribution_cases"
down_revision: str | None = "0032_calendar_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attribution_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=True),
        sa.Column("original_rfc_message_id", sa.String(length=320), nullable=True),
        sa.Column("mailbox_message_id", sa.String(length=320), nullable=False),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("ask_kind", sa.String(length=32), nullable=True),
        sa.Column("ask_sent_at", sa.DateTime(timezone=True), nullable=True),
        # 32, not the brief's 24: the longest status value
        # (``awaiting_registered_channel``) is 27 characters, and this column
        # is the family's padded width (``ask_kind`` above, quarantine's
        # ``reason``) so the schema can store its own vocabulary.
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolved_communication_id", sa.Uuid(), nullable=True),
        sa.Column("confirmation_communication_id", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # The composite key the request-less outbox row's FK points at (the
        # quarantine/communications F1 pattern).
        sa.UniqueConstraint("id", "tenant_id"),
        # F1 tenant pinning: the company, the resolving exchange and the
        # confirming exchange each join this row's own tenant.
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["resolved_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_registered_channel', 'awaiting_confirmation', 'resolved', "
            "'closed', 'dismissed')",
            name="attribution_cases_status_check",
        ),
        sa.CheckConstraint(
            "ask_kind IS NULL OR ask_kind IN ('registered_channel', 'confirmation')",
            name="attribution_cases_ask_kind_check",
        ),
        # The status and its stamps agree (the 0016/0017 rule): the awaits and
        # the resolved case carry neither closure stamp, and each closure
        # carries exactly the one its status names.
        sa.CheckConstraint(
            "status IN ('awaiting_registered_channel', 'awaiting_confirmation', 'resolved')"
            " AND closed_at IS NULL AND dismissed_at IS NULL"
            " OR status = 'closed' AND closed_at IS NOT NULL AND dismissed_at IS NULL"
            " OR status = 'dismissed' AND dismissed_at IS NOT NULL AND closed_at IS NULL",
            name="attribution_cases_closed_at_check",
        ),
    )
    op.create_index(
        "uq_attribution_cases_message_id_per_tenant",
        "attribution_cases",
        ["tenant_id", "mailbox_message_id"],
        unique=True,
    )
    op.create_index(
        "ix_attribution_cases_thread",
        "attribution_cases",
        ["tenant_id", "company_id", "thread_id", "status"],
    )

    # 2. The outbox's request-less ask: the request link becomes optional, the
    # case becomes the second anchor, the kind joins the vocabulary, and the
    # new partial pending index carries the ask's idempotency.
    op.alter_column(
        "mail_outbox",
        "confirmation_request_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.add_column("mail_outbox", sa.Column("attribution_case_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_mail_outbox_attribution_case",
        "mail_outbox",
        "attribution_cases",
        ["attribution_case_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.drop_constraint("mail_outbox_kind_check", "mail_outbox", type_="check")
    op.create_check_constraint(
        "mail_outbox_kind_check",
        "mail_outbox",
        "kind IN ('confirmation_request', 'reminder', 'reconfirmation', 'attribution_ask')",
    )
    op.create_index(
        "uq_mail_outbox_pending_per_ask",
        "mail_outbox",
        ["tenant_id", "attribution_case_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND attribution_case_id IS NOT NULL"),
    )

    # 3. The attribution exchange's two kinds join §2.9's five — the column
    # widens to hold them (see the docstring), same CHECK name, two more
    # values, nothing else moves.
    op.alter_column(
        "communications",
        "kind",
        existing_type=sa.String(length=20),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_constraint("communications_kind_check", "communications", type_="check")
    op.create_check_constraint(
        "communications_kind_check",
        "communications",
        "kind IN ('confirmation_request', 'reminder', 'reply', 'reconfirmation', "
        "'clarification', 'attribution_ask', 'attribution_confirmation')",
    )


def downgrade() -> None:
    # The narrowing can fail on data this revision legitimately holds — the
    # 0027/0029 precedent, stated in the docstring: stored attribution kinds
    # have no shape the five-value CHECK admits (and cannot fit the restored
    # width), a pending ask row has none the three-value kind CHECK admits,
    # and a request-less row cannot satisfy the restored NOT NULL. Postgres
    # refuses rather than rewriting or dropping the rows.
    op.drop_constraint("communications_kind_check", "communications", type_="check")
    op.create_check_constraint(
        "communications_kind_check",
        "communications",
        "kind IN ('confirmation_request', 'reminder', 'reply', 'reconfirmation', 'clarification')",
    )
    op.alter_column(
        "communications",
        "kind",
        existing_type=sa.String(length=32),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.drop_index("uq_mail_outbox_pending_per_ask", table_name="mail_outbox")
    op.drop_constraint("mail_outbox_kind_check", "mail_outbox", type_="check")
    op.create_check_constraint(
        "mail_outbox_kind_check",
        "mail_outbox",
        "kind IN ('confirmation_request', 'reminder', 'reconfirmation')",
    )
    op.drop_constraint("fk_mail_outbox_attribution_case", "mail_outbox", type_="foreignkey")
    op.drop_column("mail_outbox", "attribution_case_id")
    op.alter_column(
        "mail_outbox",
        "confirmation_request_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )

    op.drop_index("ix_attribution_cases_thread", table_name="attribution_cases")
    op.drop_index("uq_attribution_cases_message_id_per_tenant", table_name="attribution_cases")
    op.drop_table("attribution_cases")
