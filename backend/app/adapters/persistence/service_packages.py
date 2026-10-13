"""Service packages and company service assignments (data-model §2.9;
ADR-0013 item 4).

``ServicePackage`` is the tenant-level definition (name, per-service
``included_services`` from the §2.2 capability-flag vocabulary, the
``covered_forms`` of work the package pays for, tracking-only fee — no
invoicing in v1.0). ``ServiceAssignment`` links a company to a package with a
coverage period; at most one ``active`` assignment per company (partial unique
index), so the §5.1 gating decision never arbitrates between competing
packages.

The two lists are different axes and both are load-bearing: ``included_services``
governs which automations may run (``app.domain.service_packages.AutomationType``)
and ``covered_forms`` governs what work the client has paid for
(``app.domain.service_packages.CoveredForm``, the form codes ``backend/forms/``
ships). The second is what the client-release gate reads — paying for the
coming year's annual return does not authorise a director or address change —
and it is NOT NULL with no server default, so a package cannot be created
without declaring it.

Tenant pinning follows the F1 pattern: every row carries one ``tenant_id``,
and the composite foreign keys force the package (and the company, for
assignment rows) into that same tenant, so cross-tenant rows are
uninsertable at the database level.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class ServicePackage(Base):
    """A tenant-level service package (data-model §2.9).

    ``included_services`` is a JSON list of §2.2 capability-flag values — which
    automations the package covers; an empty list is a valid manual-only
    package. ``covered_forms`` is a JSON list of the registry's form codes —
    which work the package covers, and what the client-release gate reads; it
    is NOT NULL and refuses to be empty (``app.domain.service_packages``).
    ``fee`` is tracking only (ADR-0013). Not effective-dated: packages are
    commercial records corrected in place (no regulatory as-of semantics).
    """

    __tablename__ = "service_packages"
    __table_args__ = (
        # Composite-FK target so service_assignments can pin package and
        # tenant together.
        UniqueConstraint("id", "tenant_id"),
        # One name per tenant — packages are picked by name.
        UniqueConstraint("tenant_id", "name"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    included_services: Mapped[list[str]] = mapped_column(JSON)
    covered_forms: Mapped[list[str]] = mapped_column(JSON)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(19, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ServiceAssignment(Base):
    """Assignment of one company to one package with a coverage period.

    One ``tenant_id`` per row: the composite foreign keys force both the
    company and the package into that same tenant (F1 pattern). The partial
    unique index allows at most one ``active`` assignment per company;
    ``ended`` rows are kept as the commercial history.
    """

    __tablename__ = "service_assignments"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(
            ["package_id", "tenant_id"], ["service_packages.id", "service_packages.tenant_id"]
        ),
        CheckConstraint(
            "status IN ('active', 'ended')", name="service_assignments_status_check"
        ),
        CheckConstraint(
            "coverage_start <= coverage_end", name="service_assignments_coverage_order_check"
        ),
        Index(
            "uq_service_assignments_one_active_per_company",
            "company_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(index=True)
    package_id: Mapped[UUID] = mapped_column()
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    coverage_start: Mapped[date] = mapped_column(Date)
    coverage_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
