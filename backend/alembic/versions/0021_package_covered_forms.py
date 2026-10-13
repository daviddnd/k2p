"""service packages: the covered work (scope §2.15; data-model §2.9,
contract §5.1.9)

``service_packages.covered_forms`` is the list of registry form codes a package
covers — the *work* dimension, beside ``included_services``'s *automation*
dimension. The client-release gate reads it: a document may go to a client only
if its form is in the assigned package's list (plus the coverage and payment
legs), because paying for the coming year's annual return does not authorise a
director or address change (founder direction, 2026-09-24).

**The backfill is a guess, and the column comment says so.** Existing packages
predate the column and nobody has said what work they cover, so every one of
them is given the codes the registry ships *today* — the least surprising
answer and the one that keeps a running deployment working. It is still a
guess: a TCSP that sells an annual-return-only retainer has just been told it
also covers ND2A work, and the honest remedy is to narrow the list through
``PATCH /service-packages/{id}`` before relying on the gate. The alternative
(no backfill, NOT NULL refused) would leave existing rows unreadable; leaving
the column nullable would make "covers nothing declared" a silent state, which
is exactly the permissive default the declared list exists to avoid.

The column is added nullable, backfilled, then made NOT NULL, in that order:
one statement per step so the guess is visible in the migration rather than
hidden in a server default, and the server default is removed at the end so a
row inserted later must declare its own list.

Revision ID: 0021_package_covered_forms
Revises: 0020_service_renewals
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_package_covered_forms"
down_revision: str | None = "0020_service_renewals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The codes ``backend/forms/`` ships today (each manifest's ``form_code``).
# Spelled out rather than read from the registry: a migration is a dated
# script, and what it backfilled has to stay what it backfilled, whatever the
# registry ships later.
SHIPPED_FORM_CODES = '["NAR1", "ND2A"]'


def upgrade() -> None:
    op.add_column("service_packages", sa.Column("covered_forms", sa.JSON(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE service_packages SET covered_forms = CAST(:forms AS json) "
            "WHERE covered_forms IS NULL"
        ).bindparams(forms=SHIPPED_FORM_CODES)
    )
    op.alter_column("service_packages", "covered_forms", nullable=False)


def downgrade() -> None:
    # The list goes with the column. Nothing else stores a package's covered
    # work: the release gate reads it at request time and stores no verdict,
    # and the audit chain keeps every ``service_package_created`` /
    # ``service_package_updated`` entry that carried it, so what a package was
    # declared to cover survives the downgrade. What is lost is the current
    # answer, which a schema without the column cannot express.
    op.drop_column("service_packages", "covered_forms")
