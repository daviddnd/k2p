"""the multi-company split: the parent and work-item records (workstream ③, ADR-0019 §3)

The launch-gated capability's data model, one revision over ``0035`` (the
0034/0035 conventions verbatim — composite foreign keys, tenant pinning, a
unique natural key, CHECK-carried vocabularies):

1. **``split_parents``** (new table) — one row per inbound provider message
   deterministically attributed to **several** of the sender's own companies
   (E1's decisive resolver match, E2's several-answer handoff). The unique
   natural key is ``(tenant_id, mailbox_message_id)`` — one parent per
   message, ever, the quarantine/case discipline; the row carries the sender,
   the subject, the receipt time, and the WORM raw document the parent's
   content lives on (a composite foreign key, F1 tenant pinning). **No
   ``company_id``**: the parent is no company's record — the involved set
   lives on the items, not the parent (ruling D6), and no ``Communication``
   invariant is touched. The status is the split's own closed vocabulary:
   ``split_pending`` (created, division owed), ``split_done`` (work items
   exist), ``staff_only`` (the pre-flight or a violation parked it), and
   ``failed``.

2. **``split_work_items``** (new table) — one row per (parent, company):
   ``company_id`` NOT NULL under the composite foreign key into ``companies``
   (the item IS its company's record), the per-company restored slice stored
   as its own ``documents`` row (composite FK, WORM), and the item's own
   status (``awaiting_parse | parsed | failed``). One item per (parent,
   company) is the unique constraint's own word — a second row for the same
   pair is a refusal, not an overwrite.

3. **``reply_parses`` gains the work-item leg** — a nullable
   ``split_work_item_id`` (composite FK into ``split_work_items``) beside the
   existing ``communication_id``, with an exactly-one CHECK between them: a
   parse runs over a stored reply **or** a split slice, never both, never
   neither. The move the brief's list does not name but the schema forces:
   ``communication_id`` drops NOT NULL — the exactly-one rule is
   unsatisfiable otherwise, the 0034 stamp-pairing precedent. The per-company
   parse is company-bound either way (``company_id`` stays NOT NULL), so the
   §5.1 gate's per-company leg and the RBAC reads are untouched.

4. **``extraction_assessments.subject_type`` widens** with
   ``split_work_item`` (migration 0036, this revision): the parse's
   diagnostic row hangs off the work item when no ``Communication`` exists —
   the vocabulary names the table ``subject_id`` points into, exactly as
   ``quarantine`` did for the resolver's refusals.

5. **``change_events.source_type`` widens** with ``split_work_item``: a
   work-item parse's proposals land through the unchanged change-event write
   path, and the source column says where they came from — the same pattern
   the parser's two source values were admitted with (the domain enum's own
   note: the values are in the vocabulary because the column's CHECK admits
   them). The withdraw move stays reply-shaped: a slice's withdrawal
   decision does not move events in this slice (the application records it
   unlanded, never guessed).

No backfill anywhere: both tables are new, the new column is nullable, and
every stored parse row carries its communication — the widened CHECK admits
them all.

The downgrade drops the two tables, removes the work-item leg, restores
``communication_id``'s NOT NULL, narrows the assessment vocabulary and the
event-source vocabulary — and
**can fail on data this revision legitimately holds**, the 0027/0029/0033
precedent, stated plainly: a work-item parse row cannot satisfy the restored
NOT NULL (and its ``split_work_item_id`` reference has no shape at all once
the column and table leave), an assessment naming ``split_work_item`` has no
value the narrowed CHECK admits, and the split tables' own rows are **dropped
with the tables** — Postgres refuses the NOT NULL restore rather than
rewriting the rows, and the tables' removal is deletion, not flattening. The
remedy is the precedent's own: let the items parse (or fail) and the parents
settle, then downgrade; the data the wider schema admitted cannot be silently
flattened into the narrower one.

Built HELD for founder sign-off before apply (the M6/M5b/M7/0030-0035
pattern): the revision is committed with the code, applied to the review
databases by the slice's owner after sign-off, never by this change itself.
Reviewed with workstream ③'s merge review on
``feat/multi-company-split-2026-10-09``; the control-plane mirror (the
task-type CHECK and the prompt binding) is CP migration ``0009``, a separate
HELD revision in the same review.

Revision ID: 0036_multi_company_split
Revises: 0035_registry_widening
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_multi_company_split"
down_revision: str | None = "0035_registry_widening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. The parent: one row per multi-company message, no company of its
    # own — the involved set lives on the items (ruling D6).
    op.create_table(
        "split_parents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_message_id", sa.String(length=320), nullable=False),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
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
        # F1 tenant pinning: the WORM raw the parent's content lives on.
        sa.ForeignKeyConstraint(
            ["raw_document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        # The composite target the work items' parent leg points at.
        sa.UniqueConstraint("id", "tenant_id"),
        sa.CheckConstraint(
            "status IN ('split_pending', 'split_done', 'staff_only', 'failed')",
            name="split_parents_status_check",
        ),
    )
    op.create_index(
        "uq_split_parents_message_id_per_tenant",
        "split_parents",
        ["tenant_id", "mailbox_message_id"],
        unique=True,
    )
    op.create_index("ix_split_parents_tenant_id", "split_parents", ["tenant_id"])

    # 2. The work item: one (parent, company) slice, the item's company and
    # slice document tenant-pinned; one item per pair, ever.
    op.create_table(
        "split_work_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("slice_document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["parent_id", "tenant_id"], ["split_parents.id", "split_parents.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["slice_document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        # The composite target the reply-parse work-item leg points at (the
        # F1 pattern), and one slice per (parent, company): the second row
        # for the same pair is a refusal, not an overwrite.
        sa.UniqueConstraint("id", "tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "parent_id", "company_id", name="uq_split_work_items_parent_company"
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_parse', 'parsed', 'failed')",
            name="split_work_items_status_check",
        ),
    )
    op.create_index("ix_split_work_items_tenant_id", "split_work_items", ["tenant_id"])

    # 3. The parse's work-item leg: the split_work_item_id column beside the
    # communication leg, the communication NOT NULL dropped (the exactly-one
    # rule forces it — a work-item parse has no communication), and the XOR
    # CHECK between them.
    op.add_column(
        "reply_parses", sa.Column("split_work_item_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_reply_parses_split_work_item",
        "reply_parses",
        "split_work_items",
        ["split_work_item_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_check_constraint(
        "reply_parses_subject_check",
        "reply_parses",
        "(communication_id IS NULL) <> (split_work_item_id IS NULL)",
    )
    op.alter_column(
        "reply_parses",
        "communication_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.create_index(
        "ix_reply_parses_split_work_item_id", "reply_parses", ["split_work_item_id"]
    )

    # 4. The assessment vocabulary names the table subject_id points into —
    # the parse's diagnostic row hangs off the work item when no
    # Communication exists (the quarantine precedent, 0016).
    op.drop_constraint(
        "extraction_assessments_subject_type_check", "extraction_assessments", type_="check"
    )
    op.create_check_constraint(
        "extraction_assessments_subject_type_check",
        "extraction_assessments",
        "subject_type IN ('change_event', 'communication', 'document', 'quarantine',"
        " 'split_work_item')",
    )

    # 5. The event source vocabulary: a work-item parse's proposals name
    # their source, the parser-values pattern.
    op.drop_constraint("change_events_source_type_check", "change_events", type_="check")
    op.create_check_constraint(
        "change_events_source_type_check",
        "change_events",
        "source_type IN ('communication', 'document', 'manual', 'split_work_item')",
    )


def downgrade() -> None:
    # The narrowing refuses what only the wider schema admits — the 0027/
    # 0029/0033 precedent, stated in the docstring: a work-item parse cannot
    # satisfy the restored NOT NULL, a split_work_item assessment has no
    # value the narrowed CHECK admits, and the split tables' rows are dropped
    # with the tables. Let the items parse and the parents settle (or delete
    # the rows deliberately) before narrowing.
    op.drop_constraint(
        "extraction_assessments_subject_type_check", "extraction_assessments", type_="check"
    )
    op.create_check_constraint(
        "extraction_assessments_subject_type_check",
        "extraction_assessments",
        "subject_type IN ('change_event', 'communication', 'document', 'quarantine')",
    )
    op.drop_constraint("change_events_source_type_check", "change_events", type_="check")
    op.create_check_constraint(
        "change_events_source_type_check",
        "change_events",
        "source_type IN ('communication', 'document', 'manual')",
    )
    op.alter_column(
        "reply_parses",
        "communication_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )
    op.drop_index("ix_reply_parses_split_work_item_id", table_name="reply_parses")
    op.drop_constraint("reply_parses_subject_check", "reply_parses", type_="check")
    op.drop_constraint("fk_reply_parses_split_work_item", "reply_parses", type_="foreignkey")
    op.drop_column("reply_parses", "split_work_item_id")

    op.drop_index("ix_split_work_items_tenant_id", table_name="split_work_items")
    op.drop_table("split_work_items")
    op.drop_index("uq_split_parents_message_id_per_tenant", table_name="split_parents")
    op.drop_index("ix_split_parents_tenant_id", table_name="split_parents")
    op.drop_table("split_parents")
