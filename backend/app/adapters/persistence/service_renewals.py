"""Service-renewal persistence (scope §2.15; data-model §2.9, migration
``0020``).

One row per company per anniversary year: whether the client is continuing the
engagement for that period, and whether the fee has been received. The row is
the whole record — a **single** ``status`` column over
``pending | confirmed | declined | payment_received`` rather than two booleans,
because that is what makes payment before confirmation unrepresentable: the
only path to ``payment_received`` runs through ``confirmed``
(``app.domain.service_renewals``), and the CHECK below refuses a row whose
status and timestamps tell different stories.

``status`` is ``app.domain.service_renewals.RenewalStatus``; the DB CHECKs are
the backstop. The unique index on ``(tenant_id, company_id, anniversary_year)``
is the "which row is this period's?" guarantee — one row per period, ever, so a
concurrent ``POST`` can never leave a company with two competing answers about
the same year (the create path re-reads and returns the winner).

Tenant pinning follows the F1 pattern: the composite foreign keys pin the
company, the deciding staff account and the paying one to the row's tenant, so
a cross-tenant row is uninsertable. ``decided_by`` and ``payment_by`` are
nullable only because the NULL actor is this codebase's system-actor
convention; every route that writes one supplies the authenticated account, and
the audit entry names it as well.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class ServiceRenewal(Base):
    """One period's continuation record (scope §2.15; contract §5.1.9).

    ``anniversary_year`` is the period's own year — the year the anniversary
    (the company's made-up date) falls in, which is what the milestone panel
    counts its offsets back from and what the confirmation request's
    ``target_date.year`` names. The two are the same year by construction, so a
    gate can resolve a period from either side.

    ``note`` is staff's own line about the decision or the payment ("client
    called on the 12th", "refused — moving to another provider"): free text,
    written when the request carries one and left alone when it does not, and
    each version is in the audit chain. A decline **requires** one at the API
    (a refusal that stops a period needs a reason), which is why the column is
    where a reader looks for it.
    """

    __tablename__ = "service_renewals"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["decided_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["payment_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'declined', 'payment_received')",
            name="service_renewals_status_check",
        ),
        # The status and its timestamps agree: a pending row carries neither
        # stamp, a decided period carries the day the client answered, and only
        # a paid one carries the payment day. The single-status rule (payment
        # implies confirmation) is enforced by the guard; this is the database
        # refusing a row the guard could not have produced.
        CheckConstraint(
            "status = 'pending' AND client_decided_at IS NULL AND payment_received_at IS NULL"
            " OR status = 'confirmed' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NULL"
            " OR status = 'declined' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NULL"
            " OR status = 'payment_received' AND client_decided_at IS NOT NULL"
            " AND payment_received_at IS NOT NULL",
            name="service_renewals_decided_at_check",
        ),
        Index(
            "uq_service_renewals_period",
            "tenant_id",
            "company_id",
            "anniversary_year",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[UUID] = mapped_column(index=True)
    # The anniversary (made-up) date's calendar year — the NAR1 period this row
    # answers for, and the year panel milestones count back from.
    anniversary_year: Mapped[int] = mapped_column(Integer)
    # app.domain.service_renewals.RenewalStatus; DB CHECK is the backstop.
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    # When the client answered, and when the fee was recorded — our clock (the
    # staff record), never a provider timestamp.
    client_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL = system actor (the convention every table here follows).
    decided_by: Mapped[UUID | None] = mapped_column()
    payment_by: Mapped[UUID | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
