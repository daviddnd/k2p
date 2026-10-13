"""External-contact orchestration: the only write path for persons, contact
channels, and company↔person associations (data-model §2.4).

- ``create_person`` / ``update_person`` manage tenant-level person rows —
  external contacts and officers alike, because §2.4's ``Person`` is one
  entity: the contact subset, the officer-facing identity group (the person
  type, the English name parts, the name history, the body-corporate BRN, the
  public-page identification values and the TCSP licence, migration ``0025``)
  and the relaxed name rule all live here. ``update_person`` is a partial
  overwrite (persons are not effective-dated — no regulatory as-of
  semantics); the API layer passes only explicitly-provided fields via
  ``model_fields_set`` (same pattern as ``company_master.correct_statics``),
  so a key present with value ``None`` clears a nullable field. The fields'
  own rules are ``app.domain.officers``' (one home): both write paths validate
  the *resolved* person and raise ``PersonInvalidError`` with the domain's
  named reasons, so no caller can store a record the domain would refuse.
- ``add_contact_channel`` / ``update_contact_channel`` manage channels. The
  identifier is validated by the single domain rule
  (``app.domain.contacts.validate_identifier``) and pre-checked against the
  ``(tenant_id, channel_type, identifier)`` unique constraint, which remains
  the database backstop.
- ``associate_contact`` / ``dissociate_contact`` / ``list_company_contacts``
  manage the company↔person association. ``associate_contact`` is
  idempotent: re-adding an existing association returns the existing row.
- ``companies_for_channel_identifier`` is the read the inbound-mail
  attribution rule asks: which companies does this address belong to?
  ``is_channel_for_company`` is its membership form — whether an address is a
  registered channel of one company — which is the question ADR-0019 §1's
  decision asks of the chain's company (the sender's own status, and each
  To/Cc address). ``stored_channel_forms`` is the send-fidelity read beside
  them: the form the tenant registered for each of the addresses an ask
  carries, over the same membership the two above agree on. The three share
  the one read, so they cannot disagree about what a registered channel is.

Cross-tenant ids are indistinguishable from unknown (404 both ways) via the
shared ``_get_person`` seam and the ``company_master.get_company`` seam.

Channel-status semantics (chosen here, deliberately simple): setting the
status to ``active`` records ``verified_at = now`` (the moment the channel
was last made active); setting ``unverified`` or ``bounced`` clears it. A
freshly-added channel defaults to ``active`` with ``verified_at = NULL`` —
creation records no verification event.

Services flush but never commit — the API session dependency commits at the
request edge. Every write path emits its audit event on the same session via
``app.application.audit.record_event`` (ADR-0009).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import (
    CompanyExternalContact,
    ContactChannel,
    Person,
)
from app.application import audit, company_master
from app.domain.audit import AuditAction
from app.domain.contacts import (
    ChannelStatus,
    ChannelType,
    validate_identifier,
)
from app.domain.officers import PersonType, person_violations


class PersonNotFoundError(Exception):
    """No person with this id exists in the tenant.

    Cross-tenant ids are indistinguishable from unknown ids: existence is
    never leaked across tenant boundaries.
    """


class ChannelNotFoundError(Exception):
    """No channel with this id exists on the person (cross-tenant → unknown)."""


class ChannelIdentifierInUseError(Exception):
    """The (channel_type, identifier) pair is already taken in the tenant."""


class PersonInvalidError(Exception):
    """The person leaves the domain's officer-facing rules; ``reasons`` names them.

    The named reasons are ``app.domain.officers.person_violations``' — the
    same vocabulary the API answers verbatim (``person_name_required``,
    ``name_history_invalid``, ``tcsp_licence_invalid``, …).
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class AssociationNotFoundError(Exception):
    """No association exists between the company and the person."""


# The person fields the write path carries: the contact subset plus the
# officer-facing identity group (§2.4). One tuple, so ``create_person`` and
# ``update_person`` cannot drift apart about what a person is.
PERSON_FIELDS: tuple[str, ...] = (
    "name_en",
    "name_zh",
    "email",
    "person_type",
    "surname_en",
    "other_names_en",
    "previous_names",
    "aliases",
    "br_number",
    "hkid_partial",
    "passport_partial",
    "passport_issuing_country_region",
    "tcsp_licence",
)


@dataclass(frozen=True)
class PersonWithChannels:
    """A person plus its contact channels (the API read shape)."""

    person: Person
    channels: list[ContactChannel]


async def _get_person(session: AsyncSession, *, tenant_id: UUID, person_id: UUID) -> Person:
    """The person, or ``PersonNotFoundError`` when it is not in the tenant."""
    person = await session.scalar(
        select(Person).where(Person.id == person_id, Person.tenant_id == tenant_id)
    )
    if person is None:
        raise PersonNotFoundError
    return person


async def _get_channel(
    session: AsyncSession, *, tenant_id: UUID, person_id: UUID, channel_id: UUID
) -> ContactChannel:
    """The channel on this person in this tenant, or ``ChannelNotFoundError``."""
    channel = await session.scalar(
        select(ContactChannel).where(
            ContactChannel.id == channel_id,
            ContactChannel.person_id == person_id,
            ContactChannel.tenant_id == tenant_id,
        )
    )
    if channel is None:
        raise ChannelNotFoundError
    return channel


async def _get_channels(
    session: AsyncSession, *, tenant_id: UUID, person_ids: list[UUID]
) -> list[ContactChannel]:
    """Every channel of the given persons in the tenant (empty list → none)."""
    if not person_ids:
        return []
    rows = await session.scalars(
        select(ContactChannel).where(
            ContactChannel.tenant_id == tenant_id,
            ContactChannel.person_id.in_(person_ids),
        )
    )
    return list(rows.all())


def _with_channels(person: Person, channels: list[ContactChannel]) -> PersonWithChannels:
    return PersonWithChannels(
        person=person,
        channels=[channel for channel in channels if channel.person_id == person.id],
    )


async def _check_identifier_available(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    channel_type: str,
    identifier: str,
    exclude_channel_id: UUID | None = None,
) -> None:
    """Raise ``ChannelIdentifierInUseError`` when (type, identifier) is taken."""
    statement = select(ContactChannel.id).where(
        ContactChannel.tenant_id == tenant_id,
        ContactChannel.channel_type == channel_type,
        ContactChannel.identifier == identifier,
    )
    if exclude_channel_id is not None:
        statement = statement.where(ContactChannel.id != exclude_channel_id)
    if (await session.scalar(statement)) is not None:
        raise ChannelIdentifierInUseError


# --- Persons ---


async def create_person(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    name_en: str | None = None,
    name_zh: str | None = None,
    email: str | None = None,
    person_type: str | None = None,
    surname_en: str | None = None,
    other_names_en: str | None = None,
    previous_names: list[dict[str, Any]] | None = None,
    aliases: list[dict[str, Any]] | None = None,
    br_number: str | None = None,
    hkid_partial: str | None = None,
    passport_partial: str | None = None,
    passport_issuing_country_region: str | None = None,
    tcsp_licence: Mapping[str, Any] | None = None,
) -> Person:
    """Insert a person row in the actor's tenant.

    ``is_external_contact`` is always set true: the same tenant-level row
    serves as a contact and as the officer an appointment points at (§2.4).
    The officer-facing fields default to a natural person with the columns
    unset, which is what the database's own defaults say; the resolved record
    is validated by ``app.domain.officers`` (the one home of the rules) and a
    refusal raises ``PersonInvalidError`` carrying its named reasons. The
    name-presence rule is part of that domain rule and the
    ``persons_some_name_check`` constraint is the backstop.
    """
    person = Person(
        tenant_id=tenant_id,
        name_en=name_en,
        name_zh=name_zh,
        email=email,
        person_type=person_type or PersonType.NATURAL_PERSON.value,
        surname_en=surname_en,
        other_names_en=other_names_en,
        previous_names=previous_names,
        aliases=aliases,
        br_number=br_number,
        hkid_partial=hkid_partial,
        passport_partial=passport_partial,
        passport_issuing_country_region=passport_issuing_country_region,
        tcsp_licence=None if tcsp_licence is None else dict(tcsp_licence),
        is_external_contact=True,
    )
    _require_valid_person(person)
    session.add(person)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.PERSON_CREATED,
        actor_user_id=actor_user_id,
        entity_type="person",
        entity_id=person.id,
        details={"name_en": person.name_en, "name_zh": person.name_zh, "email": person.email},
    )
    return person


async def update_person(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    person_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> Person:
    """Partially overwrite a person's fields in place.

    Supported ``changes`` keys are exactly ``PERSON_FIELDS`` — the contact
    subset and the officer-facing identity group; a key present with value
    ``None`` clears the field. The resolved record is then validated by
    ``app.domain.officers`` (one home), whose named reasons travel in
    ``PersonInvalidError`` — clearing every name is the domain's own
    ``person_name_required``, with the relaxed DB check as the backstop.
    Cross-tenant ids are indistinguishable from unknown via the shared
    ``_get_person`` seam.
    """
    person = await _get_person(session, tenant_id=tenant_id, person_id=person_id)
    before = {field: getattr(person, field) for field in changes}
    for field in PERSON_FIELDS:
        if field in changes:
            value = changes[field]
            setattr(person, field, dict(value) if isinstance(value, Mapping) else value)
    _require_valid_person(person)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.PERSON_UPDATED,
        actor_user_id=actor_user_id,
        entity_type="person",
        entity_id=person.id,
        details={"before": before, "after": dict(changes)},
    )
    return person


def _require_valid_person(person: Person) -> None:
    """Refuse a person the officer-facing domain rules refuse.

    Called before the row is added or flushed in both write paths, so the
    named reasons are the answer rather than a database constraint error.
    """
    reasons = person_violations(
        person_type=PersonType(person.person_type),
        name_zh=person.name_zh,
        name_en=person.name_en,
        surname_en=person.surname_en,
        other_names_en=person.other_names_en,
        previous_names=person.previous_names,
        aliases=person.aliases,
        br_number=person.br_number,
        hkid_partial=person.hkid_partial,
        passport_partial=person.passport_partial,
        passport_issuing_country_region=person.passport_issuing_country_region,
        tcsp_licence=person.tcsp_licence,
    )
    if reasons:
        raise PersonInvalidError(reasons)


async def get_person(
    session: AsyncSession, *, tenant_id: UUID, person_id: UUID
) -> PersonWithChannels:
    """One person with its channels; 404 for unknown or cross-tenant ids."""
    person = await _get_person(session, tenant_id=tenant_id, person_id=person_id)
    channels = await _get_channels(session, tenant_id=tenant_id, person_ids=[person.id])
    return _with_channels(person, channels)


async def list_persons(session: AsyncSession, *, tenant_id: UUID) -> list[PersonWithChannels]:
    """Every person in the tenant with its channels, ordered for a stable listing.

    Unpaginated in this slice: a tenant's external contacts are bounded by
    its portfolio size; revisit when a larger deployment needs it.
    """
    persons = list(
        (
            await session.scalars(
                select(Person).where(Person.tenant_id == tenant_id).order_by(Person.created_at)
            )
        ).all()
    )
    channels = await _get_channels(
        session, tenant_id=tenant_id, person_ids=[person.id for person in persons]
    )
    return [_with_channels(person, channels) for person in persons]


# --- Contact channels ---


async def add_contact_channel(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    person_id: UUID,
    actor_user_id: UUID | None,
    channel_type: ChannelType,
    identifier: str,
) -> ContactChannel:
    """Add one channel to a person; status defaults to ``active``.

    The identifier is validated by the single domain rule and stored
    trimmed. A duplicate ``(channel_type, identifier)`` in the tenant raises
    ``ChannelIdentifierInUseError``; the unique constraint is the backstop.
    ``verified_at`` stays ``NULL`` at creation — no verification event has
    occurred (see the module docstring for the status semantics).
    """
    person = await _get_person(session, tenant_id=tenant_id, person_id=person_id)
    trimmed = validate_identifier(channel_type, identifier)
    await _check_identifier_available(
        session, tenant_id=tenant_id, channel_type=channel_type.value, identifier=trimmed
    )
    channel = ContactChannel(
        person_id=person.id,
        tenant_id=tenant_id,
        channel_type=channel_type.value,
        identifier=trimmed,
        status=ChannelStatus.ACTIVE.value,
    )
    session.add(channel)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CONTACT_CHANNEL_ADDED,
        actor_user_id=actor_user_id,
        entity_type="contact_channel",
        entity_id=channel.id,
        details={
            "person_id": person.id,
            "channel_type": channel.channel_type,
            "identifier": channel.identifier,
        },
    )
    return channel


async def update_contact_channel(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    person_id: UUID,
    channel_id: UUID,
    actor_user_id: UUID | None,
    identifier: str | None = None,
    status: ChannelStatus | None = None,
) -> ContactChannel:
    """Update a channel's identifier and/or status (``None`` = unchanged).

    A changed identifier is validated and re-checked against the per-tenant
    unique pair. Status semantics: ``active`` records ``verified_at = now``;
    ``unverified``/``bounced`` clear it (see the module docstring).
    """
    channel = await _get_channel(
        session, tenant_id=tenant_id, person_id=person_id, channel_id=channel_id
    )
    before: dict[str, Any] = {}
    if identifier is not None:
        trimmed = validate_identifier(ChannelType(channel.channel_type), identifier)
        if trimmed != channel.identifier:
            await _check_identifier_available(
                session,
                tenant_id=tenant_id,
                channel_type=channel.channel_type,
                identifier=trimmed,
                exclude_channel_id=channel.id,
            )
            before["identifier"] = channel.identifier
            channel.identifier = trimmed
    if status is not None and status.value != channel.status:
        before["status"] = channel.status
        channel.status = status.value
        channel.verified_at = datetime.now(UTC) if status is ChannelStatus.ACTIVE else None
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CONTACT_CHANNEL_UPDATED,
        actor_user_id=actor_user_id,
        entity_type="contact_channel",
        entity_id=channel.id,
        details={
            "person_id": person_id,
            "before": before,
            "after": {"identifier": channel.identifier, "status": channel.status},
        },
    )
    return channel


# --- Company associations ---


async def associate_contact(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    person_id: UUID,
    actor_user_id: UUID | None,
) -> CompanyExternalContact:
    """Associate an external contact with a company; idempotent.

    Re-adding an existing association returns the existing row (the API maps
    this to 200). Both the company and the person must belong to the tenant —
    unknown and cross-tenant ids are indistinguishable via the shared seams,
    and the composite foreign keys make a cross-tenant row uninsertable
    regardless.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    person = await _get_person(session, tenant_id=tenant_id, person_id=person_id)
    existing = await session.scalar(
        select(CompanyExternalContact).where(
            CompanyExternalContact.company_id == company.id,
            CompanyExternalContact.person_id == person.id,
        )
    )
    if existing is not None:
        return existing
    association = CompanyExternalContact(
        company_id=company.id, person_id=person.id, tenant_id=tenant_id
    )
    session.add(association)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANY_CONTACT_LINKED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="company_external_contact",
        details={"person_id": person.id},
    )
    return association


async def dissociate_contact(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    person_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """Remove the association; ``AssociationNotFoundError`` when absent."""
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    association = await session.scalar(
        select(CompanyExternalContact).where(
            CompanyExternalContact.company_id == company.id,
            CompanyExternalContact.person_id == person_id,
        )
    )
    if association is None:
        raise AssociationNotFoundError
    await session.delete(association)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.COMPANY_CONTACT_UNLINKED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="company_external_contact",
        details={"person_id": person_id},
    )


async def list_company_contacts(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[PersonWithChannels]:
    """The company's external contacts (persons + channels)."""
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    person_ids = list(
        (
            await session.scalars(
                select(CompanyExternalContact.person_id).where(
                    CompanyExternalContact.company_id == company.id
                )
            )
        ).all()
    )
    persons = (
        list(
            (
                await session.scalars(
                    select(Person)
                    .where(Person.tenant_id == tenant_id, Person.id.in_(person_ids))
                    .order_by(Person.created_at)
                )
            ).all()
        )
        if person_ids
        else []
    )
    channels = await _get_channels(session, tenant_id=tenant_id, person_ids=person_ids)
    return [_with_channels(person, channels) for person in persons]


async def companies_for_channel_identifier(
    session: AsyncSession, *, tenant_id: UUID, channel_type: ChannelType, identifier: str
) -> list[UUID]:
    """The companies one channel identifier is registered against.

    The attribution read behind inbound mail (Phase 3): a message's sender is
    matched to the tenant's contact channels — trimmed and case-folded, since
    the stored identifier keeps whatever staff typed — and the companies come
    from that person's company associations. Zero companies means the sender
    is unregistered (§2.5's quarantine case) and several means the message
    does not name its own company (the multi-company resolver's case); both
    are the caller's decision to make, not this read's.
    """
    person_ids = select(ContactChannel.person_id).where(
        ContactChannel.tenant_id == tenant_id,
        ContactChannel.channel_type == channel_type.value,
        func.lower(func.trim(ContactChannel.identifier)) == identifier.strip().casefold(),
    )
    rows = await session.scalars(
        select(CompanyExternalContact.company_id)
        .where(
            CompanyExternalContact.tenant_id == tenant_id,
            CompanyExternalContact.person_id.in_(person_ids),
        )
        .distinct()
        .order_by(CompanyExternalContact.company_id)
    )
    return list(rows.all())


async def is_channel_for_company(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, identifier: str
) -> bool:
    """Whether one address is a registered channel of one company.

    The membership question ADR-0019 §1 asks about the chain's company —
    whether the message's sender, or an address in its To/Cc, is one of that
    company's registered channels — expressed as membership in the read
    above, so the two can never disagree about what a registered channel is.
    An address registered for a *different* company answers ``False``: the
    chain's company is the only one whose registration counts (ruling R1).
    Email is the channel type the mail rules read; the vocabulary closes the
    set for now (``app.domain.contacts.ChannelType``).
    """
    companies = await companies_for_channel_identifier(
        session,
        tenant_id=tenant_id,
        channel_type=ChannelType.EMAIL,
        identifier=identifier,
    )
    return company_id in companies


async def stored_channel_forms(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    identifiers: Sequence[str],
) -> dict[str, str]:
    """The stored form of each registered email channel among ``identifiers``.

    The send-fidelity read behind the attribution asks (ADR-0019 §1): an ask's
    recipients arrive in their comparison form (trimmed, case-folded — the
    form the decision compares in), while the address a client reads should
    be the one the tenant registered. One entry per *matched* identifier,
    keyed by its comparison form, the stored identifier as the value; an
    identifier no company channel matches is simply absent, and the caller
    falls back to the comparison form it already holds. The read is scoped
    exactly like ``companies_for_channel_identifier`` — the company's own
    associated persons' channels — so the address the ask uses is one the
    membership read agrees is a channel.
    """
    wanted = {item.strip().casefold() for item in identifiers if item.strip()}
    if not wanted:
        return {}
    person_ids = select(CompanyExternalContact.person_id).where(
        CompanyExternalContact.tenant_id == tenant_id,
        CompanyExternalContact.company_id == company_id,
    )
    rows = await session.scalars(
        select(ContactChannel.identifier).where(
            ContactChannel.tenant_id == tenant_id,
            ContactChannel.channel_type == ChannelType.EMAIL.value,
            ContactChannel.person_id.in_(person_ids),
        )
    )
    forms: dict[str, str] = {}
    for stored in rows.all():
        key = stored.strip().casefold()
        if key in wanted:
            forms.setdefault(key, stored)
    return forms
