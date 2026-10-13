"""service renewals: the continuation and payment record (scope §2.15;
data-model §2.9, contract §5.1.9)

``service_renewals`` holds one row per company per anniversary year: whether
the client is continuing the engagement for that period and whether the fee has
been received. The founder's two rules live in the shape of the table:

1. **One status column, not two booleans.** ``status`` is
   ``pending | confirmed | declined | payment_received``, and
   ``payment_received`` is reachable only from ``confirmed`` — so "paid but not
   confirmed" is not a row this schema can hold. A pair of booleans could, and
   the ordering the founder asked for ("before anything is done, the client
   confirms they are continuing; the filled documents do not reach them until
   the payment has arrived") would then be a property of the code rather than
   of the data.

2. **A CHECK that the status and its timestamps agree** (the ``0017``/``0018``
   precedent): a pending row carries neither stamp, a decided period carries
   ``client_decided_at``, and only a paid one carries ``payment_received_at``.
   A status can therefore never be read against a stamp from another state.

The unique index on ``(tenant_id, company_id, anniversary_year)`` is the
"which row is this period's?" guarantee. The design for this slice said
*partial*; with all three columns NOT NULL and a lifecycle that never returns
to ``pending`` (``declined`` and ``payment_received`` are terminal), a partial
predicate would admit exactly the rows a full one does, so this is a plain
unique index and the difference is stated here rather than implied.

Tenant pinning follows the F1 pattern: the company, the deciding account and
the paying account are pinned to the row's tenant by composite foreign keys, so
a cross-tenant row is uninsertable.

No backfill: a deployment that has never recorded a renewal has no rows, and
there is nothing to derive one from — a period the client has not answered is
absent, which reads as "nothing confirmed" through the same fail-safe default
the gate applies to a ``pending`` row.

Revision ID: 0020_service_renewals
Revises: 0019_tenant_milestones
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_service_renewals"
down_revision: str | None = "0019_tenant_milestones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_renewals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("anniversary_year", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("client_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("payment_by", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # The company and the two staff accounts, in this row's own tenant (F1).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["decided_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(["payment_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'declined', 'payment_received')",
            name="service_renewals_status_check",
        ),
        sa.CheckConstraint(
            "status = 'pending' AND client_decided_at IS NULL AND payment_received_at IS NULL"
            " OR status = 'confirmed' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NULL"
            " OR status = 'declined' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NULL"
            " OR status = 'payment_received' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NOT NULL",
            name="service_renewals_decided_at_check",
        ),
    )
    op.create_index("ix_service_renewals_tenant_id", "service_renewals", ["tenant_id"])
    op.create_index("ix_service_renewals_company_id", "service_renewals", ["company_id"])
    op.create_index(
        "uq_service_renewals_period",
        "service_renewals",
        ["tenant_id", "company_id", "anniversary_year"],
        unique=True,
    )


def downgrade() -> None:
    # The rows go with the table. Nothing else references them — the gate reads
    # them at request time and stores no verdict — and the audit chain keeps
    # every ``service_renewal_recorded`` entry, so what was decided, by whom,
    # and when survives the downgrade. What is lost is the current state of
    # each period, which is what a schema without the table cannot express.
    op.drop_index("uq_service_renewals_period", table_name="service_renewals")
    op.drop_index("ix_service_renewals_company_id", table_name="service_renewals")
    op.drop_index("ix_service_renewals_tenant_id", table_name="service_renewals")
    op.drop_table("service_renewals")
