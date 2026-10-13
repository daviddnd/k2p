"""confirmation requests: the drafts the generator writes (scope §2.5, §7;
data-model §2.9, contract §5.1.8)

``confirmation_requests`` is where a draft confirmation request lives: the
form and the date it prepares for, the reply-by deadline the client is given,
the draft lineage's version, the stored ``message/rfc822`` document that is
the letter, the checklist the client is asked about, and the recipients.

**Why a new table rather than a ``communications`` row.** ``communications``
is "one row per exchange with a client" and carries no form, no status, no
recipients, and a NOT NULL ``received_at`` — a draft has no provider
timestamp, because it has not been sent. The outbound ``Communication`` row
belongs to the **sending** slice, when the message actually goes out; until
then this row is the record of what the product drafted (contract §2.9's
note and §5.1.8).

**Deliberately absent: ``communication_id``, ``sent_at``, and a ``sent``
status.** They arrive with the sending slice, when a ``Communication`` row
exists for them to describe. An always-null column is a promise the code does
not keep. *(Written 2026-09-25 by the sending slice, migration ``0023``: the
``sent`` status and the wider ``queued`` state arrived there, and the two
columns did **not** — the link back to this row and the moment the provider
accepted the message live on the outbound ``Communication`` row, one fact one
home. This note stays as the record of the design as it stood.)*

The partial unique index on ``(tenant_id, company_id, form_type)`` for open
drafts is the "which draft is live?" guarantee — one ``draft`` per company and
form. A regeneration supersedes the open one (``status = 'superseded'``,
keeping its row and its document) and writes the next version, so the old
draft is never deleted and never ambiguous.

Tenant pinning follows the F1 pattern: the composite foreign keys pin the
company and the creator to the row's tenant, so a cross-tenant row is
uninsertable. ``document_id`` is a plain foreign key — ``documents`` has no
``(id, tenant_id)`` unique key, and the same call the ``import_batches``
precedent makes: the row's tenant is pinned by the company composite FK, and
the service resolves the document through the company-scoped read.

No backfill: a deployment that has never drafted a request has no rows, and
there is nothing to derive one from.

Revision ID: 0018_confirmation_requests
Revises: 0017_quarantine_dismissal
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_confirmation_requests"
down_revision: str | None = "0017_quarantine_dismissal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confirmation_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("form_type", sa.String(length=20), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("reply_by", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("checklist", sa.JSON(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        # The company and the creator, in this row's own tenant (F1).
        sa.ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        sa.ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        sa.UniqueConstraint("id", "tenant_id"),
        sa.CheckConstraint("form_type IN ('nar1')", name="confirmation_requests_form_type_check"),
        sa.CheckConstraint(
            "status IN ('draft', 'superseded')", name="confirmation_requests_status_check"
        ),
    )
    op.create_index("ix_confirmation_requests_tenant_id", "confirmation_requests", ["tenant_id"])
    op.create_index("ix_confirmation_requests_company_id", "confirmation_requests", ["company_id"])
    op.create_index(
        "uq_confirmation_requests_open_draft",
        "confirmation_requests",
        ["tenant_id", "company_id", "form_type"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )


def downgrade() -> None:
    # Only the request rows go. The stored ``.eml`` messages stay in the
    # records registry — they are registered documents with their own
    # ``document_registered`` audit entries, and a schema downgrade does not
    # rewrite the registry (ADR-0009) — and the audit chain keeps the
    # ``confirmation_request_drafted`` entries that say what was drafted. What
    # is lost with the table is the recipe: the checklist the client was asked
    # about and the recipients. A downgrade drops the artifact, not the
    # history.
    op.drop_index("uq_confirmation_requests_open_draft", table_name="confirmation_requests")
    op.drop_index("ix_confirmation_requests_company_id", table_name="confirmation_requests")
    op.drop_index("ix_confirmation_requests_tenant_id", table_name="confirmation_requests")
    op.drop_table("confirmation_requests")
