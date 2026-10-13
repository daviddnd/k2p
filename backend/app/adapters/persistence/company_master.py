"""Effective-dated company master facts (ADR-0007; data-model §2.3-2.5).

One table per fact group, all append-only and carrying the data-model §1
effective-dating fields: ``effective_date``, ``valid_from``/``valid_to``, and
provenance (``source_type``/``source_id``/``created_by``/``created_at``).
As-of resolution is the domain rule in ``app.domain.effective_dating`` — it
depends only on ``effective_date`` and ``created_at``; ``valid_from`` /
``valid_to`` are maintained by the application layer's supersession chain and
are not read at resolution time.

Tenant pinning follows the F1 pattern: every fact row carries one
``tenant_id``, and the composite foreign keys force the company (and the
creating user, when set) into that same tenant, so cross-tenant rows are
uninsertable at the database level.

``source_id`` has no foreign key yet: its targets (``import_batches``,
``change_events``) land with the CSV-import and change-event slices.
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

_SOURCE_TYPE_CHECK = "source_type IN ('import_batch', 'change_event', 'manual')"


def _fact_table_args(table_name: str, *extra: object) -> tuple[object, ...]:
    """Constraints every effective-dated fact table carries.

    One ``tenant_id`` per row: the composite FKs force the company and the
    creating user into that same tenant, so cross-tenant rows are
    uninsertable at the database level (F1 hardening pattern).
    """
    return (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["created_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint(_SOURCE_TYPE_CHECK, name=f"{table_name}_source_type_check"),
        Index(f"ix_{table_name}_company_effective", "company_id", "effective_date"),
        *extra,
    )


class _EffectiveDatedFact(Base):
    """Shared columns for every effective-dated company fact (data-model §1)."""

    __abstract__ = True

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[UUID] = mapped_column()
    effective_date: Mapped[date] = mapped_column(Date)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source_type: Mapped[str] = mapped_column(String(20))
    # No FK: import_batches / change_events land in later slices.
    source_id: Mapped[UUID | None] = mapped_column()
    # Null = created by the system (e.g. AI extraction), not a staff user.
    created_by: Mapped[UUID | None] = mapped_column()
    # clock_timestamp() (not now()): per-statement wall clock, so two appends
    # in one transaction still order by created_at (migration 0004).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class CompanyNameRecord(_EffectiveDatedFact):
    """Registered company name(s) and business name (§2.3)."""

    __tablename__ = "company_name_records"
    __table_args__ = _fact_table_args(
        __tablename__,
        CheckConstraint(
            "name_en IS NOT NULL OR name_zh IS NOT NULL",
            name="company_name_records_some_name_check",
        ),
    )

    # Either or both may be registered (EN-only, zh-only, or bilingual).
    name_en: Mapped[str | None] = mapped_column(String(300))
    name_zh: Mapped[str | None] = mapped_column(String(300))
    business_name: Mapped[str | None] = mapped_column(String(300))


class RegisteredOfficeRecord(_EffectiveDatedFact):
    """Registered office address (§2.3), Address value-object columns (§2.5)."""

    __tablename__ = "registered_office_records"
    __table_args__ = _fact_table_args(__tablename__)

    flat: Mapped[str] = mapped_column(String(200))
    building: Mapped[str] = mapped_column(String(200))
    street: Mapped[str] = mapped_column(String(200))
    district: Mapped[str] = mapped_column(String(100))
    # Omitted for HK-only addresses (Region is pre-printed on the form, §2.5).
    country: Mapped[str | None] = mapped_column(String(100))


class CompanyStatusRecord(_EffectiveDatedFact):
    """Company status; dormant excludes NAR1 deadline generation (§2.4)."""

    __tablename__ = "company_status_records"
    __table_args__ = _fact_table_args(
        __tablename__,
        CheckConstraint(
            "status IN ('active', 'dormant', 'striking_off', 'dissolved')",
            name="company_status_records_status_check",
        ),
    )

    status: Mapped[str] = mapped_column(String(20))


class BusinessNatureRecord(_EffectiveDatedFact):
    __tablename__ = "business_nature_records"
    __table_args__ = _fact_table_args(__tablename__)

    business_nature_code: Mapped[str] = mapped_column(String(20))
    business_nature_description: Mapped[str] = mapped_column(String(300))


class CompanyContactFact(_EffectiveDatedFact):
    """Company contact details; +852 is pre-printed on the form (§2.3)."""

    __tablename__ = "company_contact_facts"
    __table_args__ = _fact_table_args(
        __tablename__,
        CheckConstraint(
            "email IS NOT NULL OR contact_phone_hk IS NOT NULL",
            name="company_contact_facts_some_channel_check",
        ),
    )

    email: Mapped[str | None] = mapped_column(String(320))
    contact_phone_hk: Mapped[str | None] = mapped_column(String(20))


class CompanyFilingFact(_EffectiveDatedFact):
    """Filing-level company figures (§2.3)."""

    __tablename__ = "company_filing_facts"
    __table_args__ = _fact_table_args(__tablename__)

    mortgages_and_charges_total: Mapped[Decimal | None] = mapped_column(Numeric(19, 2))
    # Only for companies without share capital (§2.3).
    member_count: Mapped[int | None] = mapped_column(Integer)


class RecordsKeptRecord(_EffectiveDatedFact):
    """Where company records are kept (§2.3): list of {description, address}."""

    __tablename__ = "records_kept_records"
    __table_args__ = _fact_table_args(__tablename__)

    records_kept: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class ShareCapitalClassRecord(_EffectiveDatedFact):
    """Share capital by class (§2.3): list of {class_name, currency,
    total_shares, total_amount, total_paid_up}.

    ``share_capital.total.*`` is derived (sum over classes) at generation
    time, never stored (§1 derived-values rule). More than 4 classes on one
    NAR1 is the manual-escalation boundary (fill-spike OQ5).
    """

    __tablename__ = "share_capital_class_records"
    __table_args__ = _fact_table_args(__tablename__)

    classes: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
