"""users MFA: the TOTP enrollment columns and the session's MFA flag (phase 7)

Scope §8 makes MFA mandatory for Administrators; this revision carries the
schema the E-slice's application layer (E3) enforces. Four columns, nothing
else touched:

- **``users.totp_secret``** (Text, nullable) — the D1 AES-GCM envelope the
  encryption seam (``application/encryption.py``) seals under
  ``KeyPurpose.totp_secret``; **plaintext never lives in this column** (a
  database leak must not leak live TOTP secrets). ``NULL`` reads "not
  enrolled", the fail-safe default.
- **``users.totp_enrolled_at``** (timestamptz, nullable) — when enrollment
  completed; ``NULL`` until then, paired with ``totp_secret`` by the write
  path, not by a CHECK (enrollment is one application transaction, the
  ``0020`` status/stamp precedent — a half-recorded enrollment cannot be
  produced by an interrupt).
- **``users.totp_last_step``** (BigInteger, nullable) — the
  replay-containment watermark: ``app.domain.mfa.verify_totp`` returns the
  step a code matched, the caller persists it, and a code at or below the
  watermark is refused even inside the window.
- **``auth_sessions.mfa_verified``** (Boolean, NOT NULL, server default
  ``true``) — ``false`` marks a session issued by password alone for an
  enrolled Administrator, refused by the request edge until the TOTP
  challenge passes. The ``true`` default on the existing rows is a
  **backfill guess, stated as one** (the ``0025`` person_type precedent):
  sessions issued before MFA existed predate this threat model, and
  enrollment forces re-verification, so every pre-existing session
  legitimately holds ``true``. The column is added nullable and tightened
  in two steps so the default backfills before the NOT NULL lands.

No other table, constraint, or index changes: the replay rule lives in the
domain, the challenge in the application layer, and this revision is
deliberately only the columns both need (E3 depends on these exact names).

Built HELD for founder sign-off before apply (the M6 pattern): the revision
is committed with the code, applied to the review databases by the slice's
owner after sign-off, never by this change itself.

Revision ID: 0031_users_mfa
Revises: 0030_filing_packages
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_users_mfa"
down_revision: str | None = "0030_filing_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        # The D1 envelope, sealed under KeyPurpose.totp_secret — plaintext
        # never in this column; NULL = not enrolled.
        sa.Column("totp_secret", sa.Text(), nullable=True),
    )
    op.add_column(
        "users", sa.Column("totp_enrolled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))

    # Added nullable with the default, then tightened: server default true
    # is the backfill guess the docstring states (sessions predating MFA
    # stay valid); every existing row reads true before the NOT NULL lands.
    op.add_column(
        "auth_sessions",
        sa.Column("mfa_verified", sa.Boolean(), server_default=sa.true(), nullable=True),
    )
    op.alter_column(
        "auth_sessions",
        "mfa_verified",
        existing_type=sa.Boolean(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("auth_sessions", "mfa_verified")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_enrolled_at")
    op.drop_column("users", "totp_secret")
