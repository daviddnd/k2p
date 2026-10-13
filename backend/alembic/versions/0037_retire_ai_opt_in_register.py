"""retire the company AI opt-in register (the consent-model transcription, ADR-0019 §2)

The founder's 2026-10-08 ruling (ADR-0019 §2, option 甲 — 撤門留閥) retires the
per-company Q1.8 opt-in gate: a client company no longer chooses whether the
TCSP may use cloud tooling — the TCSP's subscription, the operator-layer
pre-use risk acknowledgment, and the TCSP's own client notices are the basis
for cloud use. The register that recorded the delivered notice (or obtained
consent) per company is therefore retired, and with it:

1. **The three columns** — ``companies.ai_notice_delivered_at`` (timestamptz),
   ``companies.ai_notice_basis`` (varchar), ``companies.ai_notice_recorded_by``
   (uuid).
2. **Both CHECKs** — ``companies_ai_notice_basis_check`` (the closed
   ``notice | consent`` vocabulary) and ``companies_ai_notice_pair_check``
   (the basis/timestamp pairing).
3. **The tenant-pinning foreign key** — ``fk_companies_ai_notice_recorded_by``
   (the F1 composite into ``users``).

No backfill: where rows exist they are dropped with the columns — the
register's history, where any exists, is the hash-chained audit chain
(``company_ai_opt_in_recorded`` entries), which the retirement of the write
path leaves untouched as chain history; the audit report's action mapping
loses the row with the enum member. The gateway's pre-flight no longer
consults the register (``decide_call`` leg 5 and ``ai_not_opted_in`` retired
with it — slice ``feat/consent-transcription-2026-10-10``); the
``ai_disabled`` objection valve, the Q1.3 deployment gate, the operator
acknowledgment and the per-tier configuration legs are unchanged.

Schema change — human-in-the-loop per AGENTS.md: built HELD, the founder's
sign-off rides the merge (the ``0033``-``0036`` precedent). The downgrade
restores the register exactly as ``0028_company_ai_opt_in`` built it, so a
reversal is a downgrade away until sign-off; it refuses re-upgrade over rows
a reversed deployment wrote — stated, as its predecessor stated its own
sharp edge.

Revision ID: 0037_retire_ai_opt_in_register
Revises: 0036_multi_company_split
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_retire_ai_opt_in_register"
down_revision: str | None = "0036_multi_company_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_companies_ai_notice_recorded_by", "companies", type_="foreignkey")
    op.drop_constraint("companies_ai_notice_pair_check", "companies", type_="check")
    op.drop_constraint("companies_ai_notice_basis_check", "companies", type_="check")
    op.drop_column("companies", "ai_notice_recorded_by")
    op.drop_column("companies", "ai_notice_basis")
    op.drop_column("companies", "ai_notice_delivered_at")


def downgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("ai_notice_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("companies", sa.Column("ai_notice_basis", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("ai_notice_recorded_by", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        "companies_ai_notice_basis_check",
        "companies",
        "ai_notice_basis IN ('notice', 'consent')",
    )
    op.create_check_constraint(
        "companies_ai_notice_pair_check",
        "companies",
        "(ai_notice_delivered_at IS NULL) = (ai_notice_basis IS NULL)",
    )
    op.create_foreign_key(
        "fk_companies_ai_notice_recorded_by",
        "companies",
        "users",
        ["ai_notice_recorded_by", "tenant_id"],
        ["id", "tenant_id"],
    )
