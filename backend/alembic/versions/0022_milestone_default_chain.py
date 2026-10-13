"""the shipped milestone defaults repaired into the chain (data-model §2.9,
contract §5.3)

``tenant_config.milestones`` shipped with 60/30/20/21. Every offset is counted
*back* from the anniversary, so a larger number is an **earlier** date — and 21
for the signature posting put it a day *before* the client's reply-by (20),
i.e. the product would have posted documents for a signature before the client
had confirmed the data they are signing. The ordering rule now enforces the
chain the work follows (``continuation > data confirmation > client reply >
signature posting``), so that block no longer validates — and a stored block
that does not validate is ``409 tenant_config_invalid`` on every read that
derives from it (the panel and the confirmation trigger; the configuration read
returns that block's shipped defaults and names it — 2026-09-24). Left alone,
every deployment whose tenant never opened the panel would answer 409s.

Two things are repaired, and only these two:

1. **The column's server default** — what a row written without the block gets
   (the pre-``0019`` write shape, and the path the read model's "no NULL
   branch" note rests on). It moves to the new shipped block.
2. **The rows whose block is *exactly* ``0019``'s shipped default** — the rows
   nobody has configured: the ones that migration backfilled, and any row an
   Administrator saved with those numbers verbatim (the README's own recipe
   showed them). A row a tenant has configured differently is left alone,
   because that is their effective configuration and ``0019`` said a later
   change to the shipped defaults does not rewrite it; if such a row no longer
   validates, the remedy is the ``PUT`` its refusal names.

The repaired rows' new block is the same numbers a tenant with no row reads,
so nothing about a deployment's behaviour changes except that its panel becomes
readable again. The audit chain gets no entry: this is the vendor shipping a
corrected default, not an actor changing a tenant's data — the same posture
``0019``'s backfill and ``0021``'s covered-forms guess took.

Revision ID: 0022_milestone_default_chain
Revises: 0021_package_covered_forms
Create Date: 2026-09-24

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_milestone_default_chain"
down_revision: str | None = "0021_package_covered_forms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ``0019``'s shipped defaults, spelled out as that migration wrote them — the
# exact block a backfilled row carries. A migration is a dated script: neither
# value may be read back from ``app.domain.milestones``, whose constants will
# move again.
OLD_DEFAULT_MILESTONES = {
    "continuation_request_days_before": 60,
    "data_confirmation_days_before": 30,
    "client_reply_days_before": 20,
    "signature_post_days_before": 21,
    "minimum_reply_window_days": 7,
    "reminder_cadence_days": [3, 7, 10],
    "anchor": "anniversary",
}

# The corrected default: the same block with the signature posting (14) inside
# the client's reply-by (20), so a tenant that never opens the panel posts
# documents only after the client was asked to reply.
NEW_DEFAULT_MILESTONES = {
    **OLD_DEFAULT_MILESTONES,
    "signature_post_days_before": 14,
}


def upgrade() -> None:
    op.alter_column(
        "tenant_config",
        "milestones",
        server_default=sa.text(f"'{json.dumps(NEW_DEFAULT_MILESTONES)}'::json"),
    )
    # ``json`` carries no equality operator in PostgreSQL, so the comparison
    # casts to ``jsonb`` (which does) while the stored value keeps its type.
    op.execute(
        sa.text(
            "UPDATE tenant_config SET milestones = CAST(:new AS json) "
            "WHERE milestones::jsonb = CAST(:old AS jsonb)"
        ).bindparams(
            new=json.dumps(NEW_DEFAULT_MILESTONES),
            old=json.dumps(OLD_DEFAULT_MILESTONES),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE tenant_config SET milestones = CAST(:old AS json) "
            "WHERE milestones::jsonb = CAST(:new AS jsonb)"
        ).bindparams(
            new=json.dumps(NEW_DEFAULT_MILESTONES),
            old=json.dumps(OLD_DEFAULT_MILESTONES),
        )
    )
    op.alter_column(
        "tenant_config",
        "milestones",
        server_default=sa.text(f"'{json.dumps(OLD_DEFAULT_MILESTONES)}'::json"),
    )
