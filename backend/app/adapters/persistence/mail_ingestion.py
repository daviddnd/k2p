"""The mailbox ingestion cursor (migration ``0015``).

One row per tenant, holding how far the ingestion cycle has read the
dedicated mailbox. The cycle advances it over the messages it actually
examined — whatever the outcome — which is what makes a burst larger than one
cycle's bound safe: nothing above the cursor is skipped, and nothing below it
is examined twice.

The row is not a record of anything the client sent: the messages themselves
are in ``communications`` and the records registry. It is a position, which is
why it is written without an audit entry — the audit chain records changes to
the tenant's compliance data, and moving a read position is not one.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class MailIngestionState(Base):
    """The tenant's mailbox read position."""

    __tablename__ = "mail_ingestion_state"

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    # The provider's ``internalDate`` of the newest message examined. NULL is
    # a tenant that has never ingested: the first cycle reads the newest
    # messages the mailbox holds, bounded by the per-cycle limit.
    cursor_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
