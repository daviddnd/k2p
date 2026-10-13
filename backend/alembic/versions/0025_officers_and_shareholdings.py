"""officers and shareholdings: the §2.4 master records, and the officer-facing
person columns

Phase 5's master record slice (contract §2.4; ADR-0007/0009). Three changes,
one slice:

- **``persons`` gains the officer-facing identity columns.** The
  external-contact subset landed in Phase 1 (migration ``0005``); this is the
  rest of §2.4's ``Person`` — the person type, the natural person's English
  name parts, the name history (``previous_names``/``aliases`` as lists of
  ``{name_zh, name_en}``), the body-corporate ``br_number``, the public-page
  identification values and the TCSP licence. The **§1 protected class is
  deliberately absent**: ``hkid_full``, ``hkid_check_digit``,
  ``passport_full`` and ``usual_residential_address`` get no columns. §1
  stores that class encrypted with per-tenant keys, no field-level encryption
  exists yet (§8 scope, Phase 7, and the CE1 refusal of 2026-09-25 ruled that
  no table may become the first place its plaintext lives), so it stays
  encrypted-and-unbuilt until the encryption slice lands. Nothing here is
  nullable-by-omission: every new column is either a name part or a value a
  form's public pages print.
  ``persons_some_name_check`` is **relaxed** — the English name parts are
  names too, so a surname-first record is storable — and existing rows
  satisfy both forms: the new check is a strict superset of the old one.
  ``person_type`` is NOT NULL with a server default of ``'natural_person'``,
  and **that default is a backfill guess, stated as one**: nothing recorded a
  person's type before this column existed, and every person on file until
  now was entered as an external contact — which the Phase 1 subset only
  admitted for natural persons. The domain (``app.domain.officers``) owns the
  rule per type from here on; the column's CHECK is the database's copy of
  the vocabulary.
- **``officer_appointments``** is contract §2.4's ``OfficerAppointmentRecord``:
  a person's office in a company over a window, with the registry's capacity
  boxes, the alternate's name, and the address **as filed** (§2.4/§3.4's
  purpose-split — natural persons carry ``correspondence_address``, body
  corporates ``registered_office_address``; the domain enforces which one).
  ``valid_from``/``valid_to`` are the **service window**, not the fact
  tables' supersession pair: in office as of ``as_of`` ⟺
  ``valid_from <= as_of < valid_to``, ``NULL`` open-ended, and ``valid_to`` is
  the cessation's effective date — the first day *out* of office. The CHECK
  states the order the window must have (``valid_to > valid_from``), which is
  what makes the half-open interval non-empty.
- **``shareholdings``** is contract §2.4's ``ShareholdingRecord`` (as §3.3
  adopted it): the member's class, shares held, joint holder, percentage
  (Schedule 2 listed-company disclosure only), allotment date, amounts paid
  and unpaid, and the holder's address as filed on Schedule 1/2. It is
  effective-dated on the same service-window semantics.

Both new tables carry the F1 tenant-pinning pattern: one ``tenant_id`` per row
with composite foreign keys forcing the company, the person and the creating
user into that same tenant, so a cross-tenant row is uninsertable at the
database level. ``(company_id, tenant_id)`` → ``companies``,
``(person_id, tenant_id)`` → ``persons``, ``(created_by, tenant_id)`` →
``users``; ``created_at`` is ``clock_timestamp()`` per migration ``0004``'s
rule. ``capacity``, the address columns and the name-history lists are JSON —
the shape is the domain's (``app.domain.change_events``' promoted grammar and
``app.domain.officers``), deliberately not a CHECK, the ``change_events``
payload precedent: a CHECK would be a second, weaker statement of a rule that
grows with the registry.

Revision ID: 0025_officers_and_shareholdings
Revises: 0024_change_events
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_officers_and_shareholdings"
down_revision: str | None = "0024_change_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RELAXED_NAME_CHECK = (
    "name_en IS NOT NULL OR name_zh IS NOT NULL "
    "OR surname_en IS NOT NULL OR other_names_en IS NOT NULL"
)
_WINDOW_CHECK = "valid_to IS NULL OR valid_to > valid_from"


def upgrade() -> None:
    op.add_column(
        "persons",
        # The backfill guess the docstring states: everyone on record was a
        # natural person under the Phase 1 subset.
        sa.Column(
            "person_type",
            sa.String(length=20),
            server_default="natural_person",
            nullable=False,
        ),
    )
    op.add_column("persons", sa.Column("surname_en", sa.String(length=300), nullable=True))
    op.add_column("persons", sa.Column("other_names_en", sa.String(length=300), nullable=True))
    op.add_column("persons", sa.Column("previous_names", sa.JSON(), nullable=True))
    op.add_column("persons", sa.Column("aliases", sa.JSON(), nullable=True))
    op.add_column("persons", sa.Column("br_number", sa.String(length=40), nullable=True))
    op.add_column("persons", sa.Column("hkid_partial", sa.String(length=40), nullable=True))
    op.add_column("persons", sa.Column("passport_partial", sa.String(length=40), nullable=True))
    op.add_column(
        "persons",
        sa.Column("passport_issuing_country_region", sa.String(length=100), nullable=True),
    )
    op.add_column("persons", sa.Column("tcsp_licence", sa.JSON(), nullable=True))
    op.drop_constraint("persons_some_name_check", "persons", type_="check")
    op.create_check_constraint(
        "persons_some_name_check", "persons", _RELAXED_NAME_CHECK
    )
    op.create_check_constraint(
        "persons_person_type_check",
        "persons",
        "person_type IN ('natural_person', 'body_corporate')",
    )

    op.create_table(
        "officer_appointments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        # The registry's capacity values; the domain validates the list.
        sa.Column("capacity", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("alternate_to", sa.String(length=300), nullable=True),
        sa.Column("correspondence_address", sa.JSON(), nullable=True),
        sa.Column("registered_office_address", sa.JSON(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force the company, the
        # person and the creating user into that same tenant (F1 pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint(
            "role IN ('company_secretary', 'director', 'reserve_director')",
            name="officer_appointments_role_check",
        ),
        sa.CheckConstraint(_WINDOW_CHECK, name="officer_appointments_window_check"),
    )
    op.create_index(
        "ix_officer_appointments_company_id", "officer_appointments", ["company_id"]
    )
    op.create_index("ix_officer_appointments_tenant_id", "officer_appointments", ["tenant_id"])

    op.create_table(
        "shareholdings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("class_name", sa.String(length=100), nullable=False),
        sa.Column("shares_held", sa.Integer(), nullable=False),
        sa.Column("jointly_held", sa.String(length=300), nullable=True),
        sa.Column("remarks", sa.String(length=500), nullable=True),
        sa.Column("percentage_held", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("allotment_date", sa.Date(), nullable=True),
        sa.Column("amount_paid", sa.Numeric(precision=19, scale=2), nullable=True),
        sa.Column("amount_unpaid", sa.Numeric(precision=19, scale=2), nullable=True),
        sa.Column("address", sa.JSON(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.CheckConstraint("shares_held >= 0", name="shareholdings_shares_held_check"),
        sa.CheckConstraint(
            "percentage_held >= 0 AND percentage_held <= 100",
            name="shareholdings_percentage_held_check",
        ),
        sa.CheckConstraint("amount_paid >= 0", name="shareholdings_amount_paid_check"),
        sa.CheckConstraint("amount_unpaid >= 0", name="shareholdings_amount_unpaid_check"),
        sa.CheckConstraint(_WINDOW_CHECK, name="shareholdings_window_check"),
    )
    op.create_index("ix_shareholdings_company_id", "shareholdings", ["company_id"])
    op.create_index("ix_shareholdings_tenant_id", "shareholdings", ["tenant_id"])


def downgrade() -> None:
    # The two new tables are dropped whole, and deliberately without ceremony:
    # they have no other home in a schema that predates them, and the master
    # data they hold is what the office/form prints, not a record of an act.
    # The audit chain keeps what each write *was* (the officer/shareholding
    # entries carry the ids, the role or class and the before/after values), so
    # the downgrade loses the particulars rather than the fact that they
    # existed — the migration-0024 precedent for a slice's own tables.
    op.drop_index("ix_shareholdings_tenant_id", table_name="shareholdings")
    op.drop_index("ix_shareholdings_company_id", table_name="shareholdings")
    op.drop_table("shareholdings")
    op.drop_index("ix_officer_appointments_tenant_id", table_name="officer_appointments")
    op.drop_index("ix_officer_appointments_company_id", table_name="officer_appointments")
    op.drop_table("officer_appointments")

    op.drop_constraint("persons_person_type_check", "persons", type_="check")
    op.drop_constraint("persons_some_name_check", "persons", type_="check")
    # The old check is restored before the English name parts are dropped:
    # a restored form must hold for the rows that remain, and any row whose
    # only names are the English parts is refused by it — which is exactly what
    # a schema without those columns means.
    op.create_check_constraint(
        "persons_some_name_check", "persons", "name_en IS NOT NULL OR name_zh IS NOT NULL"
    )
    op.drop_column("persons", "tcsp_licence")
    op.drop_column("persons", "passport_issuing_country_region")
    op.drop_column("persons", "passport_partial")
    op.drop_column("persons", "hkid_partial")
    op.drop_column("persons", "br_number")
    op.drop_column("persons", "aliases")
    op.drop_column("persons", "previous_names")
    op.drop_column("persons", "other_names_en")
    op.drop_column("persons", "surname_en")
    op.drop_column("persons", "person_type")
