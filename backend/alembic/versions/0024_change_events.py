"""change events: append-only version rows (§2.6, §4.1)

The `ChangeEvent` entity (contract §2.6; scope §2.7-§2.8) as its persistence
model requires it: one table whose rows are the event's **versions**.

`(id, version_no)` is the primary key, deliberately. The `id` is the event's
identity — `GET/POST /change-events/{id}/...` name the same event before and
after an edit — and each revision appends a row with the same `id` and the
next `version_no`; a status change updates the current row in place. The
current version is therefore the highest `version_no`, and no partial unique
index is needed to say so: the one executor serializes writers per event by
locking the event's existing rows, so two revisions cannot both read the same
highest version and both claim the next number (see
`app.application.change_events`).

The tenancy keys are the F1 pattern with one honest difference: the composite
`(company_id, tenant_id)`, `(created_by, tenant_id)`, `(reviewed_by,
tenant_id)` and `(approved_by, tenant_id)` foreign keys pin every cross-tenant
reference at the database, but the `(id, tenant_id)` unique key the other
tables carry cannot exist here, because `id` repeats once per version. The
composite target provided instead is `(id, version_no, tenant_id)` — the key a
future child would use to name one version row. `related_event_id` is a plain
column rather than a foreign key: the reference is to an event's identity,
which no unique key anchors in this table, so the service resolves it through
the company-scoped read and refuses an unknown or other-company target (the
`import_batches.document_id` precedent).

`payload` is JSONB because the shape is typed per event `type` by
`app.domain.change_events`, not by the schema: the draft step will compare the
payload against the ND2A PI sheet and the master data, and the grammar grows
with the registry — a CHECK here would be a second, weaker statement of it.
The CHECKs the migration *does* carry are the closed vocabularies (`type`,
`status`, `source_type`, `relation`), the relation/reference pairing, and the
review/approval stamp-and-account pairings — each a fact the table can state
about itself.

**No backfill, and nothing to back-fill**: the table is new, and no earlier
revision wrote a change event.

Revision ID: 0024_change_events
Revises: 0023_mail_outbox
Create Date: 2026-09-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024_change_events"
down_revision: str | None = "0023_mail_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("effective_date_text", sa.String(length=500), nullable=True),
        sa.Column("informed_date", sa.Date(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("related_event_id", sa.Uuid(), nullable=True),
        sa.Column("relation", sa.String(length=20), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        # The event's identity is (id, version_no): a revision is a new row
        # with the same id and the next version number.
        sa.PrimaryKeyConstraint("id", "version_no"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(["reviewed_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(["approved_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # The composite target a child table would name one version row by.
        sa.UniqueConstraint("id", "version_no", "tenant_id"),
        sa.CheckConstraint(
            "type IN ('director_appointment', 'director_cessation', 'secretary_appointment', "
            "'secretary_cessation', 'registered_office_change', 'company_name_change', 'other')",
            name="change_events_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'client_reconfirming', 'under_review', 'reviewed', "
            "'confirmed', 'applied_to_draft', 'submitted', 'withdrawn', 'deleted')",
            name="change_events_status_check",
        ),
        sa.CheckConstraint(
            "source_type IN ('communication', 'document', 'manual')",
            name="change_events_source_type_check",
        ),
        sa.CheckConstraint(
            "relation IS NULL OR relation IN ('corrects', 'withdraws', 'supersedes')",
            name="change_events_relation_check",
        ),
        sa.CheckConstraint(
            "(relation IS NULL) = (related_event_id IS NULL)",
            name="change_events_relation_pair_check",
        ),
        sa.CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="change_events_reviewed_check"
        ),
        sa.CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)", name="change_events_approved_check"
        ),
    )
    op.create_index("ix_change_events_tenant_id", "change_events", ["tenant_id"])
    op.create_index("ix_change_events_company_id", "change_events", ["company_id"])
    op.create_index("ix_change_events_related_event_id", "change_events", ["related_event_id"])


def downgrade() -> None:
    # The table is dropped whole, and deliberately without ceremony: a change
    # event has no other home in a schema that predates it, and the rows it
    # held are the record of what staff entered. The audit chain keeps what
    # each write *was* (`change_event_recorded` /
    # `change_event_transition_recorded` entries carry the event id, the
    # statuses and the actor), so the downgrade loses the payload and the
    # version lineage rather than the fact that they existed — which is
    # exactly what a schema without this table means (the migration-0017
    # precedent for a slice's own table).
    op.drop_index("ix_change_events_related_event_id", table_name="change_events")
    op.drop_index("ix_change_events_company_id", table_name="change_events")
    op.drop_index("ix_change_events_tenant_id", table_name="change_events")
    op.drop_table("change_events")
