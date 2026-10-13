"""audit log: append-only, hash-chained audit_log table (ADR-0009)

The single audit log (scope §2.13, §8): every mutating operation writes an
entry here in the same database transaction as the change it describes.

Design decisions (recorded in the Phase 1 plan decision log):

- **Hash chain per tenant.** ``prev_hash`` links to the tenant's previous
  entry (NULL = genesis); ``entry_hash`` is the SHA-256 of the entry's
  canonical serialization including the link (the computation lives in
  ``app.domain.audit``). ``seq`` is a global identity column giving a
  monotonic insertion order for chain-walk verification.
- **Append-only by trigger.** ``UPDATE`` and ``DELETE`` are rejected by
  database triggers — the mechanism ADR-0009 names — so no application or
  operator session can rewrite history through this schema.
- **``action`` carries no CHECK constraint.** The vocabulary grows with
  every slice and the append-only table must never be rewritten; the
  closed vocabulary lives in ``app.domain.audit.AuditAction`` and the
  application layer only writes enum members.
- **``occurred_at`` is application-set** (no server default): the value is
  an input to the entry hash, so it must be known before insert.
- **``llm_call`` is reserved** for the ADR-0009 LLM-call metadata
  (provider, model, timestamp, token usage, prompt summary); it stays NULL
  until the Phase 3 AI gateway lands.
- Tenant pinning follows the F1 pattern: composite FKs pin the actor and
  the company scope to the row's tenant, so a cross-tenant entry is
  uninsertable.

Revision ID: 0008_audit_log
Revises: 0007_service_packages
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_audit_log"
down_revision: str | None = "0007_service_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # Application-set: the value is hashed, so it must be known before
        # insert (no server default).
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        # NULL = system actor (e.g. the bootstrap path before any user).
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        # NULL = tenant-scoped event with no company scope.
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        # Before/after summary (ADR-0009), stored exactly as hashed.
        sa.Column("details", sa.JSON(), nullable=False),
        # Reserved for ADR-0009 LLM-call metadata; NULL until Phase 3.
        sa.Column("llm_call", sa.JSON(), nullable=True),
        # NULL = the tenant's genesis entry.
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force both the actor and
        # the company scope into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(["actor_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.UniqueConstraint("seq"),
        sa.UniqueConstraint("entry_hash"),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_company_id", "audit_log", ["company_id"])

    # Append-only enforcement (ADR-0009): reject UPDATE and DELETE.
    op.execute(
        """
        CREATE FUNCTION audit_log_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION audit_log_immutable")
    op.drop_index("ix_audit_log_company_id", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")
