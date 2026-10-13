"""Calendar feed credentials (scope §2.4; ADR-0013 item 3).

One row per subscription credential a TCSP has created: the opaque token a
calendar provider fetches the `.ics` feed with, stored **only** as its
SHA-256 digest (`app.domain.calendar_sync.hash_feed_token`). The plaintext
exists in exactly one place — the response to the call that created it —
because the row is otherwise a database backup away from being a live
credential for a URL whose body names client companies.

At most one active credential per tenant (partial unique index on
``revoked_at IS NULL``): rotation is explicit — revoke, then create — the
same rule service assignments follow, and it keeps "which URL is live?"
answerable without a list.

Tenant pinning follows the F1 pattern: ``created_by`` is pinned to the
tenant by a composite foreign key, so a credential cannot record an actor
from another tenant.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class CalendarFeedCredential(Base):
    """One feed credential: its digest, who created it, and its revocation."""

    __tablename__ = "calendar_feed_credentials"
    __table_args__ = (
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # Lookup is by digest, and two credentials may never share one.
        Index("ux_calendar_feed_credentials_token_hash", "token_hash", unique=True),
        Index(
            "ux_calendar_feed_credentials_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # SHA-256 of the token, hex. Never the token itself (see the module note).
    token_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # NULL = active. Set once, by the revoke path; the row stays as the
    # record that the credential existed.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
