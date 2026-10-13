"""login credentials and staff sessions

Adds ``users.password_hash`` (nullable: an identity record without
credentials simply cannot log in — fail closed) and the ``auth_sessions``
table holding digests of issued session tokens.

Revision ID: 0002_auth_credentials_sessions
Revises: 0001_identity_rbac
Create Date: 2026-08-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_auth_credentials_sessions"
down_revision: str | None = "0001_identity_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # Only the SHA-256 digest of the token is stored; the raw token is
        # returned to the client once and never persisted.
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One tenant_id per row: the composite FK pins the session's user and
        # tenant together, so a cross-tenant session row is uninsertable
        # (same hardening pattern as company_access_grants in 0001).
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_auth_sessions_tenant_id", "auth_sessions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_tenant_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_column("users", "password_hash")
