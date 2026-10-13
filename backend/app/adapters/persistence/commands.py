"""Command-inbox persistence (ADR-0012 open action 3; migration 0012, §5.2).

Every envelope the worker has received, and every outcome it has produced,
whether or not the control plane got to hear about it. That is the whole
point of the table: ADR-0012 requires the outcome and the audit entry to be
written **locally, regardless of connectivity**, so an executed command is
recorded on the deployment even if the report never leaves the building.

Two JSON columns, each holding one envelope exactly as it crossed:
``envelope`` as received, ``result`` as produced. Both are closed documents
(``app.domain.commands``), so the columns carry opaque ids, counts,
durations, versions, timestamps, and command names — the vocabulary guard in
the integration suite checks them as stored. ``status`` mirrors
``result.status`` because the worker's two queries — "not executed yet" and
"not reported yet" — are exactly the rows where it is null and non-null.

``command_id`` is the primary key: it is the transport's idempotency key, so
a re-delivered envelope cannot produce a second row, second execution, or
second audit entry.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

_STATUS_CHECK = "status IS NULL OR status IN ('succeeded', 'failed', 'rejected')"


class CommandInbox(Base):
    """One received command envelope and, once produced, its outcome."""

    __tablename__ = "command_inbox"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="command_inbox_status_check"),
        # An outcome and its instant arrive together, and the status mirrors
        # the result document: the worker's pending queries rely on all three
        # moving together.
        CheckConstraint(
            "(status IS NULL) = (result IS NULL)",
            name="command_inbox_result_check",
        ),
        CheckConstraint(
            "(result IS NULL) = (completed_at IS NULL)",
            name="command_inbox_completion_check",
        ),
        CheckConstraint(
            "reported_at IS NULL OR result IS NOT NULL", name="command_inbox_report_check"
        ),
    )

    command_id: Mapped[UUID] = mapped_column(primary_key=True)
    # The envelope as received — never re-written, so a refusal can always be
    # read back exactly as it arrived.
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # NULL = received, not yet executed; the mirror of ``result.status``.
    status: Mapped[str | None] = mapped_column(String(20))
    # The result envelope, exactly as it is (or was) reported.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only when the control plane accepted the report: this is what makes
    # reporting exactly-once from the deployment's side.
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
