"""company AI opt-in register: the Q1.8 notice/consent columns (slice M6)

The Q1.8 register lives on the ``companies`` row the gateway already reads for
``ai_disabled`` — one-row atomic check, no join (decision 2026-09-30). Three
nullable columns record the delivered notice (or obtained consent):

1. **``companies.ai_notice_delivered_at``** (nullable timestamptz) — when the
   notice was delivered. ``NULL`` is the default and reads "not opted in",
   which is the fail-safe: absent affirmative evidence, no client data
   leaves for an inference provider.
2. **``companies.ai_notice_basis``** (nullable varchar, CHECK
   ``IN ('notice', 'consent')`` when not null) — the legal basis: the Q1.6
   notice, or consent where replies may surface third-party personal data or
   the engagement letter is silent on outsourcing.
3. **``companies.ai_notice_recorded_by``** (nullable uuid) — the staff
   account that recorded the entry, pinned to the company's tenant by a
   composite foreign key (the F1 pattern), so a cross-tenant register entry
   is uninsertable at the database level.

A CHECK pairs the basis with the timestamp — both null or both set, the
``0020`` status/stamp precedent: a half-recorded notice (a basis with no
delivery instant, or a delivery instant with no basis) is not a state the
schema can hold, so the register can never be read against a stamp from
another meaning. ``ai_notice_recorded_by`` stays independently nullable: the
audit chain pins the actor, and the column exists for the register's own
readability.

The register is deliberately **overwrite, not append** — the single opt-in
write path re-notices by overwriting, the ``correct_statics`` precedent, and
the hash-chained audit chain (``company_ai_opt_in_recorded``, carrying the
basis, the per-company evidence reference, and the deployment Q1.3 gate value
with its evidence string) is the history a re-notice never erases.

No backfill: a deployment that has never recorded a notice has only ``NULL``
columns, which reads as "not opted in" — exactly the default ``decide_call``
applies to the register.

Revision ID: 0028_company_ai_opt_in
Revises: 0027_reminder_scheduling
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_company_ai_opt_in"
down_revision: str | None = "0027_reminder_scheduling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    # F1 tenant pinning: the recording account is in this row's tenant, so a
    # cross-tenant register entry is uninsertable.
    op.create_foreign_key(
        "fk_companies_ai_notice_recorded_by",
        "companies",
        "users",
        ["ai_notice_recorded_by", "tenant_id"],
        ["id", "tenant_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_companies_ai_notice_recorded_by", "companies", type_="foreignkey")
    op.drop_constraint("companies_ai_notice_pair_check", "companies", type_="check")
    op.drop_constraint("companies_ai_notice_basis_check", "companies", type_="check")
    op.drop_column("companies", "ai_notice_recorded_by")
    op.drop_column("companies", "ai_notice_basis")
    op.drop_column("companies", "ai_notice_delivered_at")
