"""company master: Company statics + effective-dated fact tables

Adds the static (non-time-bound) profile attributes to ``companies``
(data-model §2.3) and creates the eight append-only effective-dated fact
tables (ADR-0007; data-model §2.3-2.5). Every fact row carries one
``tenant_id`` with composite FKs pinning company and creator to that tenant
(the F1 hardening pattern), so cross-tenant rows are uninsertable.

The NOT NULL statics carry no server default: the companies anchor table has
no production rows yet (single-tenant dev deployments only).

Revision ID: 0003_company_master
Revises: 0002_auth_credentials_sessions
Create Date: 2026-08-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_company_master"
down_revision: str | None = "0002_auth_credentials_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TYPE_CHECK = "source_type IN ('import_batch', 'change_event', 'manual')"

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


def _fact_columns() -> list[sa.Column]:
    """The data-model §1 effective-dating + provenance + tenant-pinning columns."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        # No FK: import_batches / change_events land in later slices.
        sa.Column("source_id", sa.Uuid(), nullable=True),
        # Null = created by the system (e.g. AI extraction), not a staff user.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _create_fact_table(
    name: str, payload_columns: list[sa.Column], *extra_constraints: sa.Constraint
) -> None:
    op.create_table(
        name,
        *_fact_columns(),
        *payload_columns,
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force the company and the
        # creating user into that same tenant (F1 hardening pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint(_SOURCE_TYPE_CHECK, name=f"{name}_source_type_check"),
        *extra_constraints,
    )
    op.create_index(f"ix_{name}_tenant_id", name, ["tenant_id"])
    op.create_index(f"ix_{name}_company_effective", name, ["company_id", "effective_date"])


def upgrade() -> None:
    op.add_column("companies", sa.Column("cr_number", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("br_number", sa.String(length=20), nullable=False))
    op.add_column(
        "companies", sa.Column("date_of_incorporation", sa.Date(), nullable=False)
    )
    op.add_column("companies", sa.Column("anniversary_date", sa.Date(), nullable=False))
    op.add_column("companies", sa.Column("type", sa.String(length=30), nullable=False))
    op.add_column(
        "companies",
        sa.Column("ai_disabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "companies_type_check",
        "companies",
        "type IN ('private', 'public', 'company_limited_by_guarantee')",
    )
    # One row per real company in a tenant's portfolio; the BRN is the
    # CSV-import match anchor (founder-approved 2026-08-20).
    op.create_unique_constraint(
        "uq_companies_tenant_br_number", "companies", ["tenant_id", "br_number"]
    )

    _create_fact_table(
        "company_name_records",
        [
            # Either or both may be registered (EN-only, zh-only, or bilingual).
            sa.Column("name_en", sa.String(length=300), nullable=True),
            sa.Column("name_zh", sa.String(length=300), nullable=True),
            sa.Column("business_name", sa.String(length=300), nullable=True),
        ],
        sa.CheckConstraint(
            "name_en IS NOT NULL OR name_zh IS NOT NULL",
            name="company_name_records_some_name_check",
        ),
    )
    _create_fact_table(
        "registered_office_records",
        [
            # Address value-object columns (data-model §2.5).
            sa.Column("flat", sa.String(length=200), nullable=False),
            sa.Column("building", sa.String(length=200), nullable=False),
            sa.Column("street", sa.String(length=200), nullable=False),
            sa.Column("district", sa.String(length=100), nullable=False),
            # Omitted for HK-only addresses (Region pre-printed on the form).
            sa.Column("country", sa.String(length=100), nullable=True),
        ],
    )
    _create_fact_table(
        "company_status_records",
        [sa.Column("status", sa.String(length=20), nullable=False)],
        sa.CheckConstraint(
            "status IN ('active', 'dormant', 'striking_off', 'dissolved')",
            name="company_status_records_status_check",
        ),
    )
    _create_fact_table(
        "business_nature_records",
        [
            sa.Column("business_nature_code", sa.String(length=20), nullable=False),
            sa.Column("business_nature_description", sa.String(length=300), nullable=False),
        ],
    )
    _create_fact_table(
        "company_contact_facts",
        [
            sa.Column("email", sa.String(length=320), nullable=True),
            # HK subscriber number; +852 is pre-printed on the form.
            sa.Column("contact_phone_hk", sa.String(length=20), nullable=True),
        ],
        sa.CheckConstraint(
            "email IS NOT NULL OR contact_phone_hk IS NOT NULL",
            name="company_contact_facts_some_channel_check",
        ),
    )
    _create_fact_table(
        "company_filing_facts",
        [
            sa.Column("mortgages_and_charges_total", sa.Numeric(19, 2), nullable=True),
            # Only for companies without share capital (data-model §2.3).
            sa.Column("member_count", sa.Integer(), nullable=True),
        ],
    )
    _create_fact_table(
        "records_kept_records",
        # List of {description, address} (data-model §2.3).
        [sa.Column("records_kept", sa.JSON(), nullable=False)],
    )
    _create_fact_table(
        "share_capital_class_records",
        # List of {class_name, currency, total_shares, total_amount,
        # total_paid_up}; share_capital.total.* is derived, never stored.
        [sa.Column("classes", sa.JSON(), nullable=False)],
    )


def downgrade() -> None:
    for name in reversed(FACT_TABLES):
        op.drop_index(f"ix_{name}_company_effective", table_name=name)
        op.drop_index(f"ix_{name}_tenant_id", table_name=name)
        op.drop_table(name)
    op.drop_constraint("uq_companies_tenant_br_number", "companies", type_="unique")
    op.drop_constraint("companies_type_check", "companies", type_="check")
    op.drop_column("companies", "ai_disabled")
    op.drop_column("companies", "type")
    op.drop_column("companies", "anniversary_date")
    op.drop_column("companies", "date_of_incorporation")
    op.drop_column("companies", "br_number")
    op.drop_column("companies", "cr_number")
