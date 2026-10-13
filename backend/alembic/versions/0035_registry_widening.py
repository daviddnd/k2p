"""the registry widening: company-less documents and exchanges (the 0035 slice)

Workstream ②'s clarification asks compose company-less letters, and until this
revision the records registry refused them: both tables that must hold one —
the stored letter (`documents`) and its delivered exchange (`communications`) —
carried NOT NULL `company_id` columns, and the single registration seam
resolves the company for the storage key's CR/BRN tokens. The ② slice
therefore stopped at the seam (``letter_not_registrable``, recorded in its
plan's decision log); this revision removes the wall, two moves over ``0034``
(the ``0034`` precedent — one revision, both legs):

1. **``documents.company_id`` drops NOT NULL.** The letter a company-less case's
   ask stores is no company's document; the composite foreign key into
   ``companies`` stays, and Postgres' MATCH SIMPLE simply does not check a
   NULL, so company-bound rows keep exactly the F1 tenant pinning they have
   always had.

2. **``communications.company_id`` drops NOT NULL**, the same rule: the send
   cycle's outbound row for a delivered company-less ask names no company
   either, and every company-bound exchange is unchanged.

No other move: both tables' CHECK vocabularies, the partial unique indexes and
the composite targets are untouched — this revision widens exactly the two
columns the wall stood on, and the tenant-scoped key shape the company-less
letter registers under is the documents seam's own rule (the ``_correspondence``
fallback segment), not schema.

No backfill anywhere: the dropped NOT NULLs only admit a shape no stored row
has, and every stored row's company stays inside the widened foreign keys.

The downgrade restores 0034's shape — both NOT NULLs back — and **can fail on
data this revision legitimately holds**, the ``0027``/``0029``/``0033``
precedent, stated plainly: a stored company-less document or a delivered
company-less exchange cannot satisfy the restored NOT NULL, and Postgres
refuses rather than rewriting or dropping the rows. The remedy is the one the
precedent prescribes: let the company-less cases settle and their letters
deliver (or delete the rows deliberately) before narrowing; the data the wider
schema admitted cannot be silently flattened into the narrower one.

Built HELD for founder sign-off before apply (the M6/M5b/M7/0030/0031/0032/
0033/0034 pattern): the revision is committed with the code, applied to the
review databases by the slice's owner after sign-off, never by this change
itself. Reviewed in the same sitting as Letters A/B's copy approval (the
founder's 2026-10-09 bundling decision), on ``feat/clarification-loop-2026-10-08``.

Revision ID: 0035_registry_widening
Revises: 0034_clarification_loop
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_registry_widening"
down_revision: str | None = "0034_clarification_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1/2. The wall's two legs: a company-less letter and its delivered
    # exchange each name no company. The composite foreign keys stay; a NULL
    # is simply not checked (MATCH SIMPLE), and company-bound rows keep their
    # F1 tenant pinning untouched.
    op.alter_column(
        "documents",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "communications",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    # The narrowing refuses live company-less rows — a stored company-less
    # document or a delivered company-less exchange cannot satisfy the
    # restored NOT NULL — and Postgres refuses rather than rewriting or
    # dropping them; let the cases settle and the letters deliver (or delete
    # the rows deliberately) before narrowing.
    op.alter_column(
        "communications",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "documents",
        "company_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
        nullable=False,
    )
