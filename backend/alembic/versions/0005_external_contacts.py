"""external contacts: persons + contact_channels + company_external_contacts

Client contact-channel configuration (DoD §9 item 1; data-model §2.4).
``persons`` is tenant-level and implements only the external-contact subset
of ``Person`` (no officer/identification columns yet). ``contact_channels``
carries one row per channel with a per-tenant unique
``(channel_type, identifier)`` — Phase 3 reply attribution depends on it.
``company_external_contacts`` is the company↔person association.

Tenant pinning follows the F1 pattern: every row carries one ``tenant_id``
with composite FKs pinning person and company to that tenant, so
cross-tenant rows are uninsertable.

Revision ID: 0005_external_contacts
Revises: 0004_fact_created_at_clock_ts
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_external_contacts"
down_revision: str | None = "0004_fact_created_at_clock_ts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "persons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # At least one name is required (mirrors the company-name convention).
        sa.Column("name_en", sa.String(length=300), nullable=True),
        sa.Column("name_zh", sa.String(length=300), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("is_external_contact", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # Composite-FK target so contact_channels and company_external_contacts
        # can pin person and tenant together.
        sa.UniqueConstraint("id", "tenant_id"),
        sa.CheckConstraint(
            "name_en IS NOT NULL OR name_zh IS NOT NULL",
            name="persons_some_name_check",
        ),
    )
    op.create_index("ix_persons_tenant_id", "persons", ["tenant_id"])

    op.create_table(
        "contact_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_type", sa.String(length=20), nullable=False),
        sa.Column("identifier", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FK forces the person into that
        # same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        # One channel identifier maps to exactly one person per tenant —
        # Phase 3 reply attribution depends on it.
        sa.UniqueConstraint("tenant_id", "channel_type", "identifier"),
        sa.CheckConstraint(
            "channel_type IN ('email', 'phone', 'whatsapp', 'wechat')",
            name="contact_channels_channel_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'unverified', 'bounced')",
            name="contact_channels_status_check",
        ),
    )
    op.create_index("ix_contact_channels_tenant_id", "contact_channels", ["tenant_id"])

    op.create_table(
        "company_external_contacts",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("company_id", "person_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force both the company and
        # the person into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
    )
    op.create_index(
        "ix_company_external_contacts_tenant_id", "company_external_contacts", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_company_external_contacts_tenant_id", table_name="company_external_contacts")
    op.drop_table("company_external_contacts")
    op.drop_index("ix_contact_channels_tenant_id", table_name="contact_channels")
    op.drop_table("contact_channels")
    op.drop_index("ix_persons_tenant_id", table_name="persons")
    op.drop_table("persons")
