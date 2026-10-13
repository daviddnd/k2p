"""control-plane lease cache: one row per deployment (ADR-0016; contract §2.2)

The execution plane's memory of the entitlement lease the vendor service
issued: the frozen payload fields it last read, the instant it read them, and
the refresh bookkeeping (last attempt, last failure reason) that keeps a
control-plane outage from making every read wait on a timeout
(``app.domain.entitlement.refresh_due``).

Only non-secret state is stored here. The activation token — the credential
that asks for a refresh — lives in a 0600 file
(``app.adapters.entitlement.activation_store``), never in the database, whose
backups would leak it (ADR-0014 custody rules applied per ADR-0016 point 3).

The row is keyed by the control plane's tenant id. It is deliberately **not**
a foreign key to ``tenants``: that table holds this deployment's own tenants,
a different id space, and the unauthenticated activation callback that first
creates this row has no local tenant to resolve.

``tier`` is nullable because the row is created when activation completes,
which can happen before — or without — a first successful lease fetch; the
check constraint keeps the "a payload arrives with a fetch instant" invariant
the read path relies on.

Revision ID: 0011_lease_cache
Revises: 0010_import_batches
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_lease_cache"
down_revision: str | None = "0010_import_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lease_cache",
        sa.Column("control_plane_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(length=30), nullable=True),
        sa.Column("license_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seat_cap", sa.Integer(), nullable=True),
        sa.Column("company_cap", sa.Integer(), nullable=True),
        sa.Column("entitlement_version", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint("control_plane_tenant_id"),
        sa.CheckConstraint(
            "tier IS NULL OR tier IN ('free', 'standard', 'enterprise_offline')",
            name="lease_cache_tier_check",
        ),
        sa.CheckConstraint(
            "(tier IS NULL) = (fetched_at IS NULL)",
            name="lease_cache_payload_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("lease_cache")
