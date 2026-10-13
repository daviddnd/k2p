"""External contacts: tenant-level persons, their contact channels, and the
company↔person association (data-model §2.4, scope §9 item 1).

External contacts are client-side directors/staff the TCSP communicates
with; they are never login users (scope §2.14). ``Person`` is the tenant-level
person every role shares: the external-contact subset landed first (Phase 1),
and the officer-facing identity columns — the person type, the English name
parts, the name history, the body-corporate BRN, the public-page
identification values and the TCSP licence — arrived with the officer
master-record slice (migration ``0025``). The §1 protected class
(``hkid_full``/``hkid_check_digit``/``passport_full``/``usual_residential_address``)
deliberately has **no column**: it is stored encrypted with per-tenant keys
(§8, Phase 7) and unbuilt until then.

Tenant pinning follows the F1 pattern: every row carries one ``tenant_id``,
and the composite foreign keys force the person (and the company, for
association rows) into that same tenant, so cross-tenant rows are
uninsertable at the database level.

One channel identifier maps to exactly one person per tenant
(``UniqueConstraint(tenant_id, channel_type, identifier)``) — Phase 3 reply
attribution depends on it.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

_CHANNEL_TYPE_CHECK = "channel_type IN ('email', 'phone', 'whatsapp', 'wechat')"
_CHANNEL_STATUS_CHECK = "status IN ('active', 'unverified', 'bounced')"
_PERSON_TYPE_CHECK = "person_type IN ('natural_person', 'body_corporate')"
# The relaxed name check (migration 0025): the English name parts are names
# too, which is what a body corporate or a surname-first record carries.
_SOME_NAME_CHECK = (
    "name_en IS NOT NULL OR name_zh IS NOT NULL "
    "OR surname_en IS NOT NULL OR other_names_en IS NOT NULL"
)


class Person(Base):
    """A tenant-level person (data-model §2.4): external contact and officer.

    ``is_external_contact`` defaults to true and the write path always sets
    it; the same row is what an officer appointment points at, because one
    human or firm may serve several companies in several roles. Not
    effective-dated: names/email are corrected in place (no regulatory as-of
    semantics); the officer-facing identity fields are written by the same two
    seams (``contacts.create_person`` / ``contacts.update_person``), never a
    second writer.
    """

    __tablename__ = "persons"
    __table_args__ = (
        # Composite-FK target so contact_channels, company_external_contacts,
        # officer_appointments and shareholdings can pin person and tenant
        # together.
        UniqueConstraint("id", "tenant_id"),
        # At least one name is required (mirrors the company-name convention).
        CheckConstraint(_SOME_NAME_CHECK, name="persons_some_name_check"),
        CheckConstraint(_PERSON_TYPE_CHECK, name="persons_person_type_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # A backfill guess for the rows that predate the column (migration 0025):
    # every person on record until then was a natural person.
    person_type: Mapped[str] = mapped_column(String(20), server_default="natural_person")
    name_en: Mapped[str | None] = mapped_column(String(300))
    name_zh: Mapped[str | None] = mapped_column(String(300))
    # The natural person's English name parts (contract §2.4 identity group).
    surname_en: Mapped[str | None] = mapped_column(String(300))
    other_names_en: Mapped[str | None] = mapped_column(String(300))
    # Name history: lists of {name_zh, name_en} pairs (§2.4; the domain
    # validates, deliberately no CHECK on JSON).
    previous_names: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    aliases: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    email: Mapped[str | None] = mapped_column(String(320))
    # The body-corporate identity: the officer's own BRN, plain text — it is
    # identification, not a match key, so no digit normalization.
    br_number: Mapped[str | None] = mapped_column(String(40))
    # The public-page identification values (contract §2.4); the §1 protected
    # class has no column (module docstring).
    hkid_partial: Mapped[str | None] = mapped_column(String(40))
    passport_partial: Mapped[str | None] = mapped_column(String(40))
    passport_issuing_country_region: Mapped[str | None] = mapped_column(String(100))
    # {licence_no} or {not_required: true, not_required_reason} (form note 20).
    tcsp_licence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_external_contact: Mapped[bool] = mapped_column(server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactChannel(Base):
    """One contact channel for a person (data-model §2.4).

    One ``tenant_id`` per row: the composite foreign key forces the person
    into that same tenant (F1 pattern). ``verified_at`` records when the
    channel last became ``active``; it is null for a freshly-added channel
    and cleared when the status leaves ``active``.
    """

    __tablename__ = "contact_channels"
    __table_args__ = (
        ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
        # One channel identifier maps to exactly one person per tenant —
        # Phase 3 reply attribution depends on it.
        UniqueConstraint("tenant_id", "channel_type", "identifier"),
        CheckConstraint(_CHANNEL_TYPE_CHECK, name="contact_channels_channel_type_check"),
        CheckConstraint(_CHANNEL_STATUS_CHECK, name="contact_channels_status_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    person_id: Mapped[UUID] = mapped_column()
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(20))
    identifier: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyExternalContact(Base):
    """Association: one external contact serving one company (data-model §2.4).

    One ``tenant_id`` per row: the composite foreign keys force both the
    company and the person into that same tenant, so a cross-tenant
    association row is uninsertable at the database level (F1 pattern).
    """

    __tablename__ = "company_external_contacts"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["person_id", "tenant_id"], ["persons.id", "persons.tenant_id"]),
    )

    company_id: Mapped[UUID] = mapped_column(primary_key=True)
    person_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
