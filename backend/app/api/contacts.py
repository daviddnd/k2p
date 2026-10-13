"""External-contact endpoints (data-model §5.1 Persons/contacts; §2.4).

Implements the client contact-channel configuration piece of DoD §9 item 1
(Flow 1 step 3). Every route is Administrator-only via ``require_admin``
(design decision 2026-08-24): Flow 1 step 3 is an administrative function,
and persons are tenant-level so per-company grant-gating does not apply;
fail-closed is the default.

Mutations route through ``app.application.contacts`` — the only write path
for persons, channels, and associations. ``POST /companies/{id}/contacts``
is idempotent: re-adding an existing association returns 200 with the
existing association row. Every mutation emits its audit event in the same
transaction (ADR-0009).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import ContactChannel, Person
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.api.officers import NamePair, TcspLicence
from app.application import contacts
from app.application.company_master import CompanyNotFoundError
from app.domain.contacts import (
    ChannelStatus,
    ChannelType,
    InvalidChannelIdentifierError,
)
from app.domain.officers import PersonType
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["contacts"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"


# --- Request payloads ---


class CreatePersonRequest(BaseModel):
    """A new external contact (``is_external_contact`` is set by the service).

    Mirrors the DB check: at least one name is required.
    """

    name_en: str | None = Field(default=None, min_length=1, max_length=300)
    name_zh: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320, pattern=_EMAIL_PATTERN)

    @model_validator(mode="after")
    def _some_name_required(self) -> "CreatePersonRequest":
        if self.name_en is None and self.name_zh is None:
            raise ValueError("name_en or name_zh is required")
        return self


class UpdatePersonRequest(BaseModel):
    """Partial update: omitted fields are unchanged; a field present with
    value ``null`` clears it. At least one field is required.

    The officer-facing identity group (migration ``0025``, contract §2.4) is
    here because this is a person's one correction route — an appointment's
    PATCH never touches person identity. The rules those fields must satisfy
    are the domain's (``app.domain.officers``), and their named reasons are
    what a refusal answers (``person_name_required``, ``name_history_invalid``,
    ``tcsp_licence_invalid``, …). The §1 protected class is absent by design:
    it is stored encrypted and unbuilt until §8's field-level encryption.
    """

    name_en: str | None = Field(default=None, min_length=1, max_length=300)
    name_zh: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320, pattern=_EMAIL_PATTERN)
    person_type: PersonType | None = None
    surname_en: str | None = Field(default=None, min_length=1, max_length=300)
    other_names_en: str | None = Field(default=None, min_length=1, max_length=300)
    previous_names: list[NamePair] | None = None
    aliases: list[NamePair] | None = None
    br_number: str | None = Field(default=None, min_length=1, max_length=40)
    hkid_partial: str | None = Field(default=None, min_length=1, max_length=40)
    passport_partial: str | None = Field(default=None, min_length=1, max_length=40)
    passport_issuing_country_region: str | None = Field(
        default=None, min_length=1, max_length=100
    )
    tcsp_licence: TcspLicence | None = None

    @model_validator(mode="after")
    def _some_field_required(self) -> "UpdatePersonRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class AddChannelRequest(BaseModel):
    channel_type: ChannelType
    identifier: str = Field(min_length=1, max_length=320)


class UpdateChannelRequest(BaseModel):
    """Update identifier and/or status; at least one field is required."""

    identifier: str | None = Field(default=None, min_length=1, max_length=320)
    status: ChannelStatus | None = None

    @model_validator(mode="after")
    def _some_field_required(self) -> "UpdateChannelRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class AssociateContactRequest(BaseModel):
    person_id: UUID


# --- Responses ---


class ContactChannelResponse(BaseModel):
    id: UUID
    person_id: UUID
    channel_type: str
    identifier: str
    status: str
    verified_at: datetime | None


class PersonResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name_en: str | None
    name_zh: str | None
    email: str | None
    is_external_contact: bool
    channels: list[ContactChannelResponse]


class CompanyContactAssociationResponse(BaseModel):
    company_id: UUID
    person_id: UUID
    created_at: datetime


def _channel_response(channel: ContactChannel) -> ContactChannelResponse:
    return ContactChannelResponse(
        id=channel.id,
        person_id=channel.person_id,
        channel_type=channel.channel_type,
        identifier=channel.identifier,
        status=channel.status,
        verified_at=channel.verified_at,
    )


def _person_response(
    person: Person, channels: list[ContactChannel] | None = None
) -> PersonResponse:
    return PersonResponse(
        id=person.id,
        tenant_id=person.tenant_id,
        name_en=person.name_en,
        name_zh=person.name_zh,
        email=person.email,
        is_external_contact=person.is_external_contact,
        channels=[_channel_response(channel) for channel in channels or []],
    )


def _invalid_identifier() -> HTTPException:
    return HTTPException(status_code=422, detail="identifier is not valid for the channel type")


# --- Person routes ---


@router.post("/persons", status_code=201)
async def create_person(
    payload: CreatePersonRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PersonResponse:
    """Create an external contact in the actor's tenant.

    The body carries no ``tenant_id`` — the person lands in the actor's own
    tenant, so cross-tenant creation is impossible by construction. 422 with
    the domain's named reasons for a person the rules refuse
    (``person_name_required`` for a name that is only whitespace, …) — the
    same vocabulary and the same mapping the PATCH route uses.
    """
    try:
        person = await contacts.create_person(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            name_en=payload.name_en,
            name_zh=payload.name_zh,
            email=payload.email,
        )
    except contacts.PersonInvalidError as exc:
        raise HTTPException(status_code=422, detail="; ".join(exc.reasons)) from None
    return _person_response(person)


@router.get("/persons")
async def list_persons(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PersonResponse]:
    """Every person in the actor's tenant, with their channels."""
    rows = await contacts.list_persons(session, tenant_id=actor.tenant_id)
    return [_person_response(row.person, row.channels) for row in rows]


@router.get("/persons/{person_id}")
async def get_person(
    person_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PersonResponse:
    """One person with channels; 404 for unknown or cross-tenant ids."""
    try:
        row = await contacts.get_person(session, tenant_id=actor.tenant_id, person_id=person_id)
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    return _person_response(row.person, row.channels)


@router.patch("/persons/{person_id}")
async def update_person(
    person_id: UUID,
    payload: UpdatePersonRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PersonResponse:
    """Partial update of a person's fields; returns the person with its channels.

    422 answers the domain's named reasons verbatim for the officer-facing
    fields (``person_name_required`` when the resolved record would have no
    name of its type, ``name_history_invalid``, ``tcsp_licence_invalid``, …),
    which is the same vocabulary the officer routes answer.
    """
    changes: dict[str, Any] = payload.model_dump(include=payload.model_fields_set)
    try:
        person = await contacts.update_person(
            session,
            tenant_id=actor.tenant_id,
            person_id=person_id,
            actor_user_id=actor.user_id,
            changes=changes,
        )
        row = await contacts.get_person(session, tenant_id=actor.tenant_id, person_id=person.id)
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    except contacts.PersonInvalidError as exc:
        raise HTTPException(status_code=422, detail="; ".join(exc.reasons)) from None
    return _person_response(row.person, row.channels)


# --- Channel routes ---


@router.post("/persons/{person_id}/contact-channels", status_code=201)
async def add_contact_channel(
    person_id: UUID,
    payload: AddChannelRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ContactChannelResponse:
    """Add a channel to a person; 409 on a duplicate (channel_type, identifier)."""
    try:
        channel = await contacts.add_contact_channel(
            session,
            tenant_id=actor.tenant_id,
            person_id=person_id,
            actor_user_id=actor.user_id,
            channel_type=payload.channel_type,
            identifier=payload.identifier,
        )
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    except InvalidChannelIdentifierError:
        raise _invalid_identifier() from None
    except contacts.ChannelIdentifierInUseError:
        raise HTTPException(
            status_code=409, detail="channel identifier already in use in this tenant"
        ) from None
    return _channel_response(channel)


@router.patch("/persons/{person_id}/contact-channels/{channel_id}")
async def update_contact_channel(
    person_id: UUID,
    channel_id: UUID,
    payload: UpdateChannelRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ContactChannelResponse:
    """Update a channel's identifier and/or status."""
    try:
        channel = await contacts.update_contact_channel(
            session,
            tenant_id=actor.tenant_id,
            person_id=person_id,
            channel_id=channel_id,
            actor_user_id=actor.user_id,
            identifier=payload.identifier,
            status=payload.status,
        )
    except contacts.ChannelNotFoundError:
        raise HTTPException(status_code=404, detail="channel not found") from None
    except InvalidChannelIdentifierError:
        raise _invalid_identifier() from None
    except contacts.ChannelIdentifierInUseError:
        raise HTTPException(
            status_code=409, detail="channel identifier already in use in this tenant"
        ) from None
    return _channel_response(channel)


# --- Company association routes ---


@router.get("/companies/{company_id}/contacts")
async def list_company_contacts(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PersonResponse]:
    """The company's external contacts (person + channels)."""
    try:
        rows = await contacts.list_company_contacts(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_person_response(row.person, row.channels) for row in rows]


@router.post("/companies/{company_id}/contacts")
async def associate_contact(
    company_id: UUID,
    payload: AssociateContactRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyContactAssociationResponse:
    """Associate an external contact with the company (always 200).

    Idempotent: re-adding an existing association returns the existing
    association row unchanged. 404 when the company or the person is unknown
    or cross-tenant.
    """
    try:
        association = await contacts.associate_contact(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            person_id=payload.person_id,
            actor_user_id=actor.user_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    return CompanyContactAssociationResponse(
        company_id=association.company_id,
        person_id=association.person_id,
        created_at=association.created_at,
    )


@router.delete("/companies/{company_id}/contacts/{person_id}", status_code=204)
async def dissociate_contact(
    company_id: UUID,
    person_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove the association; 404 if the company is unknown/cross-tenant or
    the association is absent."""
    try:
        await contacts.dissociate_contact(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            person_id=person_id,
            actor_user_id=actor.user_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except contacts.AssociationNotFoundError:
        raise HTTPException(status_code=404, detail="association not found") from None
