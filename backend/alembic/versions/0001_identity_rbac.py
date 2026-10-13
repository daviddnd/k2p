"""identity and RBAC schema (scope §2.14, data-model §2.9)

Creates tenants, users (role + per-account permission flags), the companies
anchor table, and per-company access grants for General accounts.

Revision ID: 0001_identity_rbac
Revises:
Create Date: 2026-08-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_identity_rbac"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("review_events", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("approve_events", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("review_drafts", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("approve_drafts", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('administrator', 'general')", name="users_role_check"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "email"),
        # Composite-FK target so company_access_grants can pin user and
        # tenant together.
        sa.UniqueConstraint("id", "tenant_id"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Composite-FK target so company_access_grants can pin company and
        # tenant together.
        sa.UniqueConstraint("id", "tenant_id"),
    )
    op.create_index("ix_companies_tenant_id", "companies", ["tenant_id"])
    op.create_table(
        "company_access_grants",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One tenant_id per row: the composite FKs force the granted user and
        # company into that same tenant, so a cross-tenant grant is
        # uninsertable at the database level.
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.PrimaryKeyConstraint("user_id", "company_id"),
    )
    op.create_index("ix_company_access_grants_tenant_id", "company_access_grants", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_company_access_grants_tenant_id", table_name="company_access_grants")
    op.drop_table("company_access_grants")
    op.drop_index("ix_companies_tenant_id", table_name="companies")
    op.drop_table("companies")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
