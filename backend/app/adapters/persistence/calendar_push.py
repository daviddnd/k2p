"""Calendar push entries (scope §2.4 v1.22; ADR-0013's 2026-10-03 ruling R3).

One row per obligation the push has written to the tenant's provider
calendar: the obligation's UID (the feed projection's UID — obligation- and
period-keyed, so a moved due date keeps its row and updates in place), the
provider's event id, and the content hash of the entry as last pushed
(``app.domain.calendar_push.content_hash``). The sweep compares the current
projection's hashes against these rows: a new or changed entry is pushed, an
unchanged one is not, and a row whose obligation has disappeared is deleted
from the provider once and forgotten.

This is operational state, deliberately *not* custody: the credential lives
in the 0600 file store (``app.adapters.calendar.credential_store``), never
here — a database backup must not carry the provider credential.

Tenancy follows the feed credential's shape: rows are pinned to their
tenant, and one row per (tenant, uid) — the unique index a sweep's
upsert relies on.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class CalendarPushEntry(Base):
    """One pushed obligation: its UID, the provider's event id, its hash."""

    __tablename__ = "calendar_push_entries"
    __table_args__ = (
        # A sweep reconciles by (tenant, uid), and two rows for one
        # obligation would push twice.
        Index("ux_calendar_push_entries_tenant_uid", "tenant_id", "uid", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # The feed projection's UID — ``{kind}-{company_id}-{period}@{domain}``.
    uid: Mapped[str] = mapped_column(String(255))
    # The provider's own event id, as the upsert returned it.
    provider_event_id: Mapped[str] = mapped_column(String(255))
    # The content hash last pushed (``app.domain.calendar_push.content_hash``).
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
