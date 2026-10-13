"""calendar push entries: the push's idempotency state (slice M8)

Scope §2.4 v1.22's push half (ADR-0013 item 3, 2026-10-03 ruling R3): one
row per obligation the push has written to the tenant's provider calendar,
mapping the feed projection's UID to the provider event id and the content
hash last pushed. The sweep (``app.application.calendar_sync.sweep_pushes``)
pushes only what is new or changed, deletes a disappeared obligation's
provider event once, and forgets the row — so a re-sweep with an unchanged
projection writes nothing. The credential itself never lives here: it is
ADR-0014 custody, in the 0600 per-tenant file store.

Three columns and tenancy, nothing else:

- **``calendar_push_entries.uid``** (String(255), NOT NULL) — the projection's
  obligation UID; with ``tenant_id`` it is the reconciliation key, unique
  under ``ux_calendar_push_entries_tenant_uid``.
- **``calendar_push_entries.provider_event_id``** (String(255), NOT NULL) —
  the provider's own event id, as the upsert returned it; the delete leg
  addresses the provider event by it.
- **``calendar_push_entries.content_hash``** (String(64), NOT NULL) — the
  hex SHA-256 of the entry's canonical fields
  (``app.domain.calendar_push.content_hash``), the idempotency comparison.

Tenancy mirrors the feed credential table (``0013``): rows are pinned to
their tenant by a plain foreign key — the single-tenant deployment keeps one
tenant's rows, and the shape does not have to change when it stops being
single-tenant.

Built HELD for founder sign-off before apply (the M6/M5b/M7/0030/0031
pattern): the revision is committed with the code, applied to the review
databases by the slice's owner after sign-off, never by this change itself.

Revision ID: 0032_calendar_push
Revises: 0031_users_mfa
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_calendar_push"
down_revision: str | None = "0031_users_mfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_push_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("uid", sa.String(length=255), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_push_entries_tenant_id",
        "calendar_push_entries",
        ["tenant_id"],
    )
    op.create_index(
        "ux_calendar_push_entries_tenant_uid",
        "calendar_push_entries",
        ["tenant_id", "uid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_calendar_push_entries_tenant_uid", table_name="calendar_push_entries"
    )
    op.drop_index("ix_calendar_push_entries_tenant_id", table_name="calendar_push_entries")
    op.drop_table("calendar_push_entries")
