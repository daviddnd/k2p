"""fact created_at default → clock_timestamp(): same-transaction correction tie-break

``resolve_as_of`` breaks same-day-correction ties on ``created_at`` — the
record the system learned about later supersedes. The 0003 default
``now()`` is the PostgreSQL *transaction* timestamp, so two fact appends in
one transaction received identical ``created_at`` values, producing the
full ``(effective_date, created_at)`` tie the resolution rule cannot break.
``clock_timestamp()`` is the per-statement wall clock, so corrections order
correctly even within a single transaction (e.g. future batched CSV-import
or extraction writes). Existing rows keep their stored values; only the
default changes.

Revision ID: 0004_fact_created_at_clock_ts
Revises: 0003_company_master
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_fact_created_at_clock_ts"
down_revision: str | None = "0003_company_master"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FACT_TABLES = (
    "company_name_records",
    "registered_office_records",
    "company_status_records",
    "business_nature_records",
    "company_contact_facts",
    "company_filing_facts",
    "records_kept_records",
    "share_capital_class_records",
)


def upgrade() -> None:
    for table in FACT_TABLES:
        op.alter_column(table, "created_at", server_default=sa.func.clock_timestamp())


def downgrade() -> None:
    for table in FACT_TABLES:
        op.alter_column(table, "created_at", server_default=sa.func.now())
