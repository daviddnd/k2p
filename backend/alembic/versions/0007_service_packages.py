"""service packages: service_packages + service_assignments

Service packages with per-service automation gating (ADR-0013 item 4;
data-model §2.9, §5.1). ``service_packages`` is the tenant-level package
definition (name, ``included_services[]`` from the §2.2 capability-flag
vocabulary, tracking-only fee). ``service_assignments`` links a company to
a package with a coverage period; a partial unique index enforces at most
one ``active`` assignment per company, so the gating decision never has to
arbitrate between competing packages.

Tenant pinning follows the F1 pattern: every row carries one ``tenant_id``
with composite FKs pinning package and company to that tenant, so
cross-tenant rows are uninsertable.

Revision ID: 0007_service_packages
Revises: 0006_tenant_config
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_service_packages"
down_revision: str | None = "0006_tenant_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # List of §2.2 capability-flag values the package covers; an empty
        # list is a valid manual-only package.
        sa.Column("included_services", sa.JSON(), nullable=False),
        # Tracking only — no invoicing or billing in v1.0 (ADR-0013).
        sa.Column("fee", sa.Numeric(19, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # Composite-FK target so service_assignments can pin package and
        # tenant together.
        sa.UniqueConstraint("id", "tenant_id"),
        # One name per tenant — packages are picked by name.
        sa.UniqueConstraint("tenant_id", "name"),
    )
    op.create_index("ix_service_packages_tenant_id", "service_packages", ["tenant_id"])

    op.create_table(
        "service_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force both the company
        # and the package into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(
            ["package_id", "tenant_id"], ["service_packages.id", "service_packages.tenant_id"]
        ),
        sa.CheckConstraint(
            "status IN ('active', 'ended')",
            name="service_assignments_status_check",
        ),
        sa.CheckConstraint(
            "coverage_start <= coverage_end",
            name="service_assignments_coverage_order_check",
        ),
    )
    op.create_index("ix_service_assignments_tenant_id", "service_assignments", ["tenant_id"])
    op.create_index("ix_service_assignments_company_id", "service_assignments", ["company_id"])
    # At most one active assignment per company — the gating decision never
    # arbitrates between competing packages. Lapsed/ended rows coexist.
    op.create_index(
        "uq_service_assignments_one_active_per_company",
        "service_assignments",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_service_assignments_one_active_per_company", table_name="service_assignments")
    op.drop_index("ix_service_assignments_company_id", table_name="service_assignments")
    op.drop_index("ix_service_assignments_tenant_id", table_name="service_assignments")
    op.drop_table("service_assignments")
    op.drop_index("ix_service_packages_tenant_id", table_name="service_packages")
    op.drop_table("service_packages")
