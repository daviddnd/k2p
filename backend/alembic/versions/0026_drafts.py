"""drafts: the §2.8 entity as version rows, with the ADR-0010 form pin

Phase 5's core table (contract §2.8, §4.2; scope §2.9). One draft is a set of
**version rows** sharing an ``id``: each generation appends the next
``version_no``, so the history is the earlier rows and the current version is
the highest number (the ``change_events`` CE1 shape, migration ``0024``). The
``id`` is therefore the draft lineage's identity — a fresh id for the first
version and the same one for every regeneration — and ``(id, version_no)`` is
the primary key. The composite target a later child table names is
``(id, version_no, tenant_id)``: the ``(id, tenant_id)`` unique key the
single-row tables carry cannot exist where ``id`` repeats once per version.

**Two columns beyond §2.8's field list**, both recorded here and in the
contract's Phase 5 note because a reader of the migration should meet the
reason, not just the column:

- ``applied_event_ids`` (JSONB, default ``[]``) is §4.1's "draft linkage
  recorded": the ids of the change events this version applied — the
  superset of ``resolved_state.changes``, which is the human-readable view of
  the same fact. It is what makes "invalidates affected drafts" a lookup on a
  JSONB containment rather than a scan of every draft's snapshot, and an event
  may be linked by several versions until ``submit`` exists (Phase 6 files the
  event; until then a previously applied event keeps applying to later drafts,
  scope §2.9 step 1). Ids are stored as strings — JSON has no UUID.
- ``form_version`` (JSONB) is ADR-0010's requirement that every draft records
  the form version it was created with: ``{"specification_no": ...,
  "template_sha256": ...}`` read from ``load_form_registry`` at generation, so
  a draft printed later can always say which revision it was built against even
  after the registry is refreshed.

The four documents §2.8 names as JSON are ``resolved_state`` (the snapshot),
``field_provenance`` (per registry item: source and changed-vs-prior-filing),
``qc_flags`` (the prior-filing cross-check's findings) and ``filing_metadata``
(the return's dates, the delivery channel, presentor/signature/consent). All
four are NOT NULL; ``qc_flags`` and ``applied_event_ids`` carry an empty-list
server default so "no flags" is stored as a list and never as null. Their
shapes are the domain's (``app.domain.drafts``), deliberately not CHECKs — the
``change_events.payload`` precedent: a CHECK would be a second, weaker
statement of a rule that grows with the registry.

``private_company_statement`` is NAR1 item 16's declaration, pre-filled
draft-only (§3; ``null`` for ND2A), with the human confirmation it will need
before export in the ``statement_confirmed_by/at`` pair (both null or both set,
the stamp pairing every other validated stamp carries). ``signature_status``
starts ``not_sent`` — the signature actions are refused by name until the
filler and Phase 6 land. ``rationale``/``comments`` are TEXT (a review note is
prose, not a column-sized identifier) and ``invalidation_reason`` is
VARCHAR(300), the reason a §4.1 withdrawal/deletion left in the row.

Tenancy is the F1 pattern: ``(company_id, tenant_id)``, ``(created_by,
tenant_id)``, ``(statement_confirmed_by, tenant_id)``, ``(reviewed_by,
tenant_id)`` and ``(approved_by, tenant_id)`` all reference the composite keys
of ``companies``/``users``, so a cross-tenant row is uninsertable at the
database. ``created_at`` is ``clock_timestamp()`` per migration ``0004``'s
rule. Indexes: ``(tenant_id)`` and ``(company_id, form_type)`` — the lineage
lookup generation makes, and the company read's own path.

**No backfill, and nothing to back-fill**: the table is new, and no earlier
revision wrote a draft.

Revision ID: 0026_drafts
Revises: 0025_officers_and_shareholdings
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_drafts"
down_revision: str | None = "0025_officers_and_shareholdings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORM_TYPE_CHECK = "form_type IN ('nar1', 'nd2a')"
_STATUS_CHECK = (
    "status IN ('generated', 'under_review', 'sent_for_signature', "
    "'signature_received', 'reviewed', 'approved', 'exported', 'rejected', "
    "'invalidated')"
)
_SIGNATURE_CHECK = "signature_status IN ('not_sent', 'sent_for_signature', 'signature_received')"


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("form_type", sa.String(length=10), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=25), nullable=False),
        sa.Column("resolved_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("field_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "qc_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("filing_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "applied_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("form_version", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("private_company_statement", sa.Boolean(), nullable=True),
        sa.Column("statement_confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("statement_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "signature_status", sa.String(length=30), server_default="not_sent", nullable=False
        ),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=300), nullable=True),
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
        sa.PrimaryKeyConstraint("id", "version_no"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        # One tenant_id per row: the composite FKs force the company, the
        # creator and every stamped account into that same tenant (F1 pattern).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(
            ["statement_confirmed_by", "tenant_id"], ["users.id", "users.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["reviewed_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.ForeignKeyConstraint(["approved_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.UniqueConstraint("id", "version_no", "tenant_id"),
        sa.CheckConstraint(_FORM_TYPE_CHECK, name="drafts_form_type_check"),
        sa.CheckConstraint(_STATUS_CHECK, name="drafts_status_check"),
        sa.CheckConstraint(_SIGNATURE_CHECK, name="drafts_signature_status_check"),
        sa.CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="drafts_reviewed_check"
        ),
        sa.CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL)", name="drafts_approved_check"
        ),
        sa.CheckConstraint(
            "(statement_confirmed_by IS NULL) = (statement_confirmed_at IS NULL)",
            name="drafts_statement_check",
        ),
    )
    op.create_index("ix_drafts_tenant_id", "drafts", ["tenant_id"])
    op.create_index("ix_drafts_company_form", "drafts", ["company_id", "form_type"])


def downgrade() -> None:
    # The table is dropped whole, and deliberately without ceremony: it has no
    # other home in a schema that predates it, and the audit chain keeps what
    # each generation *was* (``draft_generated`` carries the company, the form,
    # the version and the applied event ids; the transition and invalidation
    # entries carry their own coordinates), so the downgrade loses the
    # snapshots rather than the fact that the drafts existed — the
    # migration-0024/0025 precedent for a slice's own tables.
    op.drop_index("ix_drafts_company_form", table_name="drafts")
    op.drop_index("ix_drafts_tenant_id", table_name="drafts")
    op.drop_table("drafts")
