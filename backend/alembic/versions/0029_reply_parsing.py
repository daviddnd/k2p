"""reply parsing: the reply_parses business home, the resolver's two
assessment columns, and the reconfirmation outbox kind (slice M5b)

Three schema changes the reply parser and the structured reconfirmer stand
on, one revision (the ``0028`` single-revision precedent):

1. **``reply_parses``** (new table) — the parse result's *business* home:
   one row per parse run over one inbound reply, carrying the parsed
   ``confirmations[]`` (the §2.9 checklist join) and the proposed change
   events' ``event_ids``. The per-field scores and signal log stay in the
   §2.7 *diagnostic* home, ``extraction_assessments`` — the founder's D3
   ruling keeps business and diagnostic columns apart. The closed
   vocabularies become CHECKs: ``status`` is the reply-parse grammar's six
   values, ``language`` the ``communications.language`` vocabulary
   (``en | zh-hk | mixed``, nullable — an undetected reply is a fact, not a
   refusal), and ``routing`` the §2.7 band vocabulary (nullable — an empty
   off-topic parse has nothing to score). Tenant pinning is the F1 pattern:
   the company, the communication and the confirmation request each join
   the row's own ``tenant_id`` in a composite foreign key, so a
   cross-tenant parse is uninsertable. ``confirmation_request_id`` is NULL
   for mail no request produced; the composite FK is simply not checked on
   a NULL leg (Postgres' MATCH SIMPLE), the ``mail_outbox`` precedent.
   Re-parses append; reads take the newest per communication.

2. **``extraction_assessments.raw_model_resolution`` /
   ``enforced_resolution``** (nullable varchar) — the §2.7 field list names
   the pair ("resolver only") but no column exists: the resolver's model
   fallback (M5b) records what the model answered and what the rule then
   enforced. Deliberately **no CHECK**: the three-case vocabulary
   (``resolved | ambiguous | no_match``) is the resolver domain's own, and
   the schema does not restate it — the deterministic rule stays the one
   enforcer.

3. **``mail_outbox.kind`` CHECK widens** with ``reconfirmation`` — the
   structured reconfirmer's letter rides the same outbox as the request and
   the reminder, referencing the request it answers; §5.1.12's partial
   pending index is already per ``(tenant_id, confirmation_request_id,
   kind)``, so a reconfirmation pends beside the request's own row without
   collision. No other outbox shape changes.

No backfill: the table is new, the two columns are nullable, and every
stored outbox row's ``kind`` ('confirmation_request') remains in the
widened vocabulary.

The downgrade round-trips: ``reply_parses`` drops whole (the parse's
business record — the audit chain keeps what each run *was*, the
migration-0017 precedent for a slice's own table), the two columns drop,
and the CHECK narrows back. That narrowing **can fail**, the ``0027``
index-narrowing precedent: rows this revision legitimately holds —
reconfirmation sends — have no shape the ``0028`` CHECK admits, and
Postgres refuses rather than rewriting the rows. The remedy is to deliver
or cancel the pending reconfirmation rows first; the data the wider schema
added cannot be silently flattened into the narrower one.

Built HELD for founder sign-off before apply (the M6 pattern): the
revision is committed with the code, applied to the review databases by
the slice's owner after sign-off, never by this change itself.

Revision ID: 0029_reply_parsing
Revises: 0028_company_ai_opt_in
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029_reply_parsing"
down_revision: str | None = "0028_company_ai_opt_in"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reply_parses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("communication_id", sa.Uuid(), nullable=False),
        sa.Column("confirmation_request_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("routing", sa.String(length=30), nullable=True),
        sa.Column("confirmations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # F1 tenant pinning: the company, the reply it answered's exchange
        # row, and the request the reply answers all join this row's tenant.
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_request_id", "tenant_id"],
            ["confirmation_requests.id", "confirmation_requests.tenant_id"],
        ),
        sa.CheckConstraint(
            "status IN ('confirmed_no_change', 'changes_reported', 'correction', "
            "'withdrawal', 'unclear', 'off_topic')",
            name="reply_parses_status_check",
        ),
        sa.CheckConstraint(
            "language IS NULL OR language IN ('en', 'zh-hk', 'mixed')",
            name="reply_parses_language_check",
        ),
        sa.CheckConstraint(
            "routing IS NULL OR routing IN ('auto_accept', 'staff_review', "
            "'client_clarification')",
            name="reply_parses_routing_check",
        ),
    )
    op.create_index("ix_reply_parses_tenant_id", "reply_parses", ["tenant_id"])
    op.create_index("ix_reply_parses_company_id", "reply_parses", ["company_id"])
    op.create_index("ix_reply_parses_communication_id", "reply_parses", ["communication_id"])

    # The resolver's model-fallback pair: what the model answered, what the
    # deterministic rule then enforced. Nullable, no CHECK — the three-case
    # vocabulary is the resolver domain's own (see the docstring).
    op.add_column(
        "extraction_assessments",
        sa.Column("raw_model_resolution", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "extraction_assessments",
        sa.Column("enforced_resolution", sa.String(length=20), nullable=True),
    )

    # The reconfirmer's kind joins the request's and the reminder's on the
    # same outbox — same name, one more value, nothing else moves.
    op.drop_constraint("mail_outbox_kind_check", "mail_outbox", type_="check")
    op.create_check_constraint(
        "mail_outbox_kind_check",
        "mail_outbox",
        "kind IN ('confirmation_request', 'reminder', 'reconfirmation')",
    )


def downgrade() -> None:
    # The narrowing can fail on data this revision legitimately holds —
    # pending 'reconfirmation' rows have no shape the 0028 CHECK admits, and
    # Postgres refuses rather than rewriting them (the migration-0027
    # precedent, stated in the docstring).
    op.drop_constraint("mail_outbox_kind_check", "mail_outbox", type_="check")
    op.create_check_constraint(
        "mail_outbox_kind_check",
        "mail_outbox",
        "kind IN ('confirmation_request', 'reminder')",
    )
    op.drop_column("extraction_assessments", "enforced_resolution")
    op.drop_column("extraction_assessments", "raw_model_resolution")
    op.drop_index("ix_reply_parses_communication_id", table_name="reply_parses")
    op.drop_index("ix_reply_parses_company_id", table_name="reply_parses")
    op.drop_index("ix_reply_parses_tenant_id", table_name="reply_parses")
    op.drop_table("reply_parses")
