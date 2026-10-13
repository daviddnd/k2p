"""quarantine: a row staff can dismiss (scope §2.5; data-model §2.9,
contract §5.1.7)

``quarantined_messages.status`` gains ``dismissed``, and the table gains
``dismissed_at``. Until now the only way out of ``pending`` was a resolution
— the message attributed and ingested — which a message the mailbox no longer
has can never reach: the provider answers ``404``-class for it, so the retry
fails for ever and the row stays open. A dismissal is staff saying "no further
action", which is a decision the record has to be able to hold.

``resolved_at`` stays what it is, and the two terminal states are kept
distinct on purpose: ``resolved`` points at the ``Communication`` the message
became, ``dismissed`` does not. The CHECK added here makes the pair
consistent — a pending row carries neither stamp, a resolved one only
``resolved_at``, a dismissed one only ``dismissed_at`` — so a status can never
be read against a stamp belonging to the other state. It is honest for every
row this schema has ever held: ``app.application.quarantine._resolve`` is the
only writer of ``resolved``, and it sets both fields together.

``extraction_assessments`` is untouched: a dismissal writes no assessment
(there is no decision to record — the §2.7 record belongs to the resolver's
runs), and the assessment of a quarantined message keeps pointing at the row
that still exists.

The downgrade restores the two-value vocabulary, and a dismissed row cannot be
described by it: those rows come back as ``pending``, which is the only open
state the earlier schema has. Nothing is deleted — the row, its message id and
its reason survive — and the dismissal itself stays in the audit chain as
``mail_quarantine_dismissed``; the timestamp is lost with the column, which is
what downgrading a schema that could not express the state means.

Revision ID: 0017_quarantine_dismissal
Revises: 0016_quarantine
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_quarantine_dismissal"
down_revision: str | None = "0016_quarantine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "quarantined_messages", sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint(
        "quarantined_messages_status_check", "quarantined_messages", type_="check"
    )
    op.create_check_constraint(
        "quarantined_messages_status_check",
        "quarantined_messages",
        "status IN ('pending', 'resolved', 'dismissed')",
    )
    op.create_check_constraint(
        "quarantined_messages_closed_at_check",
        "quarantined_messages",
        "status = 'pending' AND resolved_at IS NULL AND dismissed_at IS NULL"
        " OR status = 'resolved' AND resolved_at IS NOT NULL AND dismissed_at IS NULL"
        " OR status = 'dismissed' AND dismissed_at IS NOT NULL AND resolved_at IS NULL",
    )


def downgrade() -> None:
    # The exactly-one-stamp rule goes first: the re-opening below sets
    # ``pending`` on rows that still carry ``dismissed_at``, which the rule
    # would refuse.
    op.drop_constraint(
        "quarantined_messages_closed_at_check", "quarantined_messages", type_="check"
    )
    op.execute("UPDATE quarantined_messages SET status = 'pending' WHERE status = 'dismissed'")
    op.drop_column("quarantined_messages", "dismissed_at")
    op.drop_constraint(
        "quarantined_messages_status_check", "quarantined_messages", type_="check"
    )
    op.create_check_constraint(
        "quarantined_messages_status_check",
        "quarantined_messages",
        "status IN ('pending', 'resolved')",
    )
