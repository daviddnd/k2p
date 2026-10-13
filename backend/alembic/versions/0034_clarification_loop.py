"""the clarification loop: company-less cases and asks (workstream ②, ADR-0019 §1)

Workstream ②'s two automatic ask classes — the which-company ask (A-class) and
the first-contact ask (B-class) — open their ``attribution_cases`` rows with no
company and queue their outbox asks the same way, one revision over ``0033``
(the ``0028``/``0029`` single-revision precedent):

1. **``attribution_cases.company_id`` drops NOT NULL** (ruling D2). The
   composite FK into ``companies`` stays: Postgres' MATCH SIMPLE does not
   check a NULL, so non-null companies keep exactly the F1 tenant pinning
   they have today, and a company-less row is simply not checked. The case
   takes its company at resolution/closure — the row grows nothing new.

2. **``mail_outbox.company_id`` drops NOT NULL** (ruling D2): the ask a
   company-less case rides has no company either. Same MATCH SIMPLE
   reasoning; the composite FK and its pinning are unchanged for the rows
   that carry a company.

3. **The status CHECK widens** with ``awaiting_clarification`` (ruling D1) —
   A-class's open state, named beside ①'s two awaits. The move is one value,
   same CHECK name.

4. **The ``ask_kind`` CHECK widens** with ``clarification`` (ruling D1) —
   Letter A's kind. Both columns keep the family's padded 32 width.

5. **The stamp-pairing CHECK widens with the new status** — a move the
   brief's list does not name but the schema forces: ``awaiting_clarification``
   is an open state like the two ① awaits, carrying neither ``closed_at``
   nor ``dismissed_at``, and the pairing CHECK enumerates the open statuses
   verbatim — without this move the new status would insert nowhere.

6. **The thread index re-leads** (ruling D2): ``ix_attribution_cases_thread``
   drops ``(tenant_id, company_id, thread_id, status)`` and recreates as
   ``(tenant_id, status, thread_id)``, so the company-less anchor reads the
   suppression and processing queries will make can use it.

No backfill anywhere: the dropped NOT NULLs only admit a shape no stored row
has, and every stored row's vocabulary stays inside the widened CHECKs.

The downgrade restores 0033's shape — narrower CHECKs, both NOT NULLs, the
old index lead — and **can fail on data this revision legitimately holds**,
the ``0027``/``0029``/``0033`` precedent, stated plainly: a company-less
case or outbox row (NULL ``company_id``) cannot satisfy the restored NOT
NULL, and a row carrying ``awaiting_clarification`` / ``clarification`` has
no value the restored CHECKs admit — Postgres refuses rather than rewriting
or dropping the rows, which is the honest answer, and the downgrade body's
own comment enumerates the same. The remedy is the one 0033 prescribes: let
the company-less cases resolve, close or dismiss (they take a company then,
or leave the table's vocabulary) and deliver or cancel the pending
company-less ask rows before narrowing; the data the wider schema added
cannot be silently flattened into the narrower one. The index re-lead is
lossless in both directions.

Built HELD for founder sign-off before apply (the M6/M5b/M7/0030/0031/0032/
0033 pattern): the revision is committed with the code, applied to the
review databases by the slice's owner after sign-off, never by this change
itself.

Revision ID: 0034_clarification_loop
Revises: 0033_attribution_cases
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_clarification_loop"
down_revision: str | None = "0033_attribution_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1/2. The company-less row's two legs: both tables drop the NOT NULL,
    # the composite FKs stay and simply do not check a NULL (MATCH SIMPLE).
    op.alter_column(
        "attribution_cases",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "mail_outbox",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )

    # 3/4/5. The widened vocabularies, same CHECK names: the new status, the
    # new ask kind, and the stamp-pairing CHECK's open-status list the new
    # status joins (an open state carries neither closure stamp).
    op.drop_constraint("attribution_cases_status_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_status_check",
        "attribution_cases",
        "status IN ('awaiting_registered_channel', 'awaiting_confirmation', "
        "'awaiting_clarification', 'resolved', 'closed', 'dismissed')",
    )
    op.drop_constraint("attribution_cases_ask_kind_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_ask_kind_check",
        "attribution_cases",
        "ask_kind IS NULL OR ask_kind IN ('registered_channel', 'confirmation', 'clarification')",
    )
    op.drop_constraint("attribution_cases_closed_at_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_closed_at_check",
        "attribution_cases",
        "status IN ('awaiting_registered_channel', 'awaiting_confirmation', "
        "'awaiting_clarification', 'resolved')"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " OR status = 'closed' AND closed_at IS NOT NULL AND dismissed_at IS NULL"
        " OR status = 'dismissed' AND dismissed_at IS NOT NULL AND closed_at IS NULL",
    )

    # 6. The thread anchor's read re-leads company-less: status before the
    # anchor, no company leg — the suppression and processing queries filter
    # by tenant, openness, and the anchor alone.
    op.drop_index("ix_attribution_cases_thread", table_name="attribution_cases")
    op.create_index(
        "ix_attribution_cases_thread",
        "attribution_cases",
        ["tenant_id", "status", "thread_id"],
    )


def downgrade() -> None:
    # The narrowing can fail on data this revision legitimately holds — the
    # 0027/0029/0033 precedent, stated in the docstring: a company-less case
    # or outbox row (NULL company_id) cannot satisfy the restored NOT NULL,
    # and a row carrying the new status or ask kind has no value the restored
    # CHECKs admit. Postgres refuses rather than rewriting or dropping the
    # rows; let the company-less cases resolve, close or dismiss and the
    # pending asks deliver or cancel before narrowing.
    op.drop_index("ix_attribution_cases_thread", table_name="attribution_cases")
    op.create_index(
        "ix_attribution_cases_thread",
        "attribution_cases",
        ["tenant_id", "company_id", "thread_id", "status"],
    )
    op.drop_constraint("attribution_cases_closed_at_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_closed_at_check",
        "attribution_cases",
        "status IN ('awaiting_registered_channel', 'awaiting_confirmation', 'resolved')"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " OR status = 'closed' AND closed_at IS NOT NULL AND dismissed_at IS NULL"
        " OR status = 'dismissed' AND dismissed_at IS NOT NULL AND closed_at IS NULL",
    )
    op.drop_constraint("attribution_cases_ask_kind_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_ask_kind_check",
        "attribution_cases",
        "ask_kind IS NULL OR ask_kind IN ('registered_channel', 'confirmation')",
    )
    op.drop_constraint("attribution_cases_status_check", "attribution_cases", type_="check")
    op.create_check_constraint(
        "attribution_cases_status_check",
        "attribution_cases",
        "status IN ('awaiting_registered_channel', 'awaiting_confirmation', 'resolved', "
        "'closed', 'dismissed')",
    )
    op.alter_column(
        "mail_outbox",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "attribution_cases",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )
