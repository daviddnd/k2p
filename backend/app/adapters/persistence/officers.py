"""Officer appointments and shareholdings — the §2.4 master records
(migration ``0025``).

The two effective-dated records the officer and member boxes of both Wave-1
forms are sourced from (contract §2.4, appendix A):

- ``officer_appointments`` links a tenant-level ``Person`` to a company with
  the office's role, the registry's capacity boxes, the alternate's name and
  the address **as filed** — the §3.4 purpose-split puts that address on the
  appointment rather than on the person, because natural persons file a
  correspondence address and body corporates a registered office, and the
  filing carries the one its subject was appointed under.
- ``shareholdings`` carries one member position: the class, the shares held,
  the joint holder, the amounts paid and unpaid, and the holder's address as
  filed on Schedule 1/2.

**The window is the service window, not the bookkeeping pair.** ``valid_from``
is the day the officer holds office from and ``valid_to`` the cessation's
effective date — the first day *out* of office (in office as of ``as_of`` ⟺
``valid_from <= as_of < valid_to``; ``NULL`` is open-ended). The
effective-dated fact tables use the same column names for their supersession
chain, which is a different thing: those are maintained by
``company_master.record_fact`` and closed when a newer fact supersedes them,
while these two windows are the regulatory interval itself, written by staff
as the appointment and the cessation they record. The domains'
``app.domain.officers.in_office`` is the one reader of it.

Tenant pinning follows the F1 pattern: every row carries one ``tenant_id`` and
the composite foreign keys force the company, the person and the creating user
into that same tenant, so a cross-tenant row is uninsertable at the database
level.

``capacity`` and the two address columns are JSON, like the payload column of
``change_events``: the shape is ``app.domain.officers``' (the registry's three
box values; the §2.5 address object), deliberately not a database CHECK —
a CHECK would be a second, weaker statement of a rule that grows with the
registry. The CHECKs this module does carry are the facts a row can state
about itself: the role vocabulary, the window's order, and the non-negative
figures the member boxes print.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

# The vocabularies' database backstops (app.domain.change_events owns the role
# values; app.domain.officers the capacity ones — a JSON list is the domain's).
_ROLE_CHECK = "role IN ('company_secretary', 'director', 'reserve_director')"
_WINDOW_CHECK = "valid_to IS NULL OR valid_to > valid_from"


class OfficerAppointment(Base):
    """One person's office in one company, over one window (contract §2.4).

    ``alternate_to`` is the ``代替`` name and exists only beside an
    ``alternate_director`` capacity (the domain's rule, §2.4). ``created_by``
    is null when the system wrote the row and an account when staff did.
    """

    __tablename__ = "officer_appointments"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint(_ROLE_CHECK, name="officer_appointments_role_check"),
        CheckConstraint(_WINDOW_CHECK, name="officer_appointments_window_check"),
        Index("ix_officer_appointments_company_id", "company_id"),
        Index("ix_officer_appointments_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    company_id: Mapped[UUID] = mapped_column()
    person_id: Mapped[UUID] = mapped_column()
    role: Mapped[str] = mapped_column(String(30))
    # The registry's capacity values; empty when the office prints none.
    capacity: Mapped[list[str]] = mapped_column(JSON, server_default=text("'[]'::json"))
    alternate_to: Mapped[str | None] = mapped_column(String(300))
    # The §2.5 Address object as filed: natural persons correspondence, body
    # corporates registered office — enforced in the domain, not here.
    correspondence_address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    registered_office_address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The service window (see the module docstring).
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[UUID | None] = mapped_column()
    # clock_timestamp() (not now()): per-statement wall clock, so two appends
    # in one transaction still order by created_at (migration 0004).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Shareholding(Base):
    """One member position, effective-dated (contract §2.4).

    ``percentage_held`` is the Schedule 2 listed-company ≥5% disclosure and is
    nullable: most holdings state shares and amounts, not a percentage.
    ``jointly_held`` names the joint holder as the form's box asks for it.
    """

    __tablename__ = "shareholdings"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint("shares_held >= 0", name="shareholdings_shares_held_check"),
        CheckConstraint(
            "percentage_held >= 0 AND percentage_held <= 100",
            name="shareholdings_percentage_held_check",
        ),
        CheckConstraint("amount_paid >= 0", name="shareholdings_amount_paid_check"),
        CheckConstraint("amount_unpaid >= 0", name="shareholdings_amount_unpaid_check"),
        CheckConstraint(_WINDOW_CHECK, name="shareholdings_window_check"),
        Index("ix_shareholdings_company_id", "company_id"),
        Index("ix_shareholdings_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    company_id: Mapped[UUID] = mapped_column()
    person_id: Mapped[UUID] = mapped_column()
    class_name: Mapped[str] = mapped_column(String(100))
    shares_held: Mapped[int] = mapped_column(Integer)
    jointly_held: Mapped[str | None] = mapped_column(String(300))
    remarks: Mapped[str | None] = mapped_column(String(500))
    percentage_held: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    allotment_date: Mapped[date | None] = mapped_column(Date)
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(19, 2))
    amount_unpaid: Mapped[Decimal | None] = mapped_column(Numeric(19, 2))
    # The §2.5 Address object, as filed on Schedule 1/2.
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
