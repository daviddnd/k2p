"""tenant milestones and the confirmation request's statutory due date

Two columns of the founder's 2026-09-24 ruling: the per-tenant milestone
panel, and the statutory NAR1 due date a generated confirmation request is
refused against and now records.

1. ``tenant_config.milestones`` — the milestone block next to
   ``naming_templates``, validated by ``app.domain.milestones`` at every write.
   **Backfilled with the shipped defaults**: added NOT NULL with those defaults
   as the column's own default, so every existing row carries them and a row
   written without the block (or by anything that predates this migration)
   reads the shipped defaults rather than NULL. The read path therefore has no
   NULL branch, and the write paths always supply a validated block.

   The defaults are a literal in this file, not an import of
   ``app.domain.milestones``: a migration must be reproducible from its own
   text, and a later change to the shipped defaults must not rewrite what this
   one backfilled — a stored block is the tenant's effective configuration, and
   it is only ever changed by that tenant's own PUT.

2. ``confirmation_requests.statutory_due`` — nullable, **no backfill**. Rows
   written before the milestone panel was generated under the old rule (the
   reply-by date *was* the statutory date), and the column's absence on those
   rows is the truth about them: inventing a date for a historical request
   would claim a guard that was never run. New rows always carry it — the
   generator refuses when the reply-by would fall after the statutory date —
   so the guarantee is durable on the record itself.

Slices C and D of the same change (the ``service_renewals`` record and the
release gate) add their own migrations; this one carries only what the
milestone panel and the reply-by anchor need.

Revision ID: 0019_tenant_milestones
Revises: 0018_confirmation_requests
Create Date: 2026-09-24

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_tenant_milestones"
down_revision: str | None = "0018_confirmation_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The shipped defaults as of this migration (founder-ruled 2026-09-24):
# continuation 60, data confirmation 30, client reply 20, signature posting 21,
# minimum reply window 7, reminder cadence 3/7/10, anchor the anniversary.
DEFAULT_MILESTONES = {
    "continuation_request_days_before": 60,
    "data_confirmation_days_before": 30,
    "client_reply_days_before": 20,
    "signature_post_days_before": 21,
    "minimum_reply_window_days": 7,
    "reminder_cadence_days": [3, 7, 10],
    "anchor": "anniversary",
}


def upgrade() -> None:
    op.add_column(
        "tenant_config",
        sa.Column(
            "milestones",
            sa.JSON(),
            nullable=False,
            # The backfill: existing rows are filled from the new column's
            # default, and the default stays for any later row that omits it.
            server_default=sa.text(f"'{json.dumps(DEFAULT_MILESTONES)}'::json"),
        ),
    )
    op.add_column("confirmation_requests", sa.Column("statutory_due", sa.Date(), nullable=True))


def downgrade() -> None:
    # The statutory due date and the backfilled block go; the request rows and
    # their stored messages stay (ADR-0009 — the registry keeps the artifacts,
    # and the audit chain keeps what was drafted under either rule).
    op.drop_column("confirmation_requests", "statutory_due")
    op.drop_column("tenant_config", "milestones")
