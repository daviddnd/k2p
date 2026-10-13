"""Officer and shareholding endpoints (contract §5.1 Officers/Shareholdings;
data-model §2.4).

The §5.1 rows, exactly:

- ``POST /companies/{id}/officers`` — record one officer's office, naming an
  existing person (``person_id``) **or** carrying a new person's fields
  (``person``), exactly one of the two. Grant-gated per company with no
  permission flag: officer and member records are ordinary master-data
  maintenance (founder decision 2026-08-24), and the §5.1 automation gate does
  not apply to a manual write in any tier (ADR-0012).
- ``GET /companies/{id}/officers?as_of=YYYY-MM-DD`` — the company's
  appointments, newest ``valid_from`` first, each with its person snapshot;
  ``as_of`` narrows to the rows in office that day (the half-open window, §2).
  The read the draft construction consumes; ``GET /companies/{id}/master-data``
  is deliberately not reshaped to carry officers.
- ``PATCH /officers/{id}`` — partial update (``model_fields_set``; an explicit
  ``null`` clears where clearing is legal). Appointment fields only: person
  identity corrections are ``PATCH /persons/{id}``'s, which is
  Administrator-only.
- ``DELETE /officers/{id}`` — 204; audited, the entry carrying the values the
  row held.
- ``POST/GET /companies/{id}/shareholdings``, ``PATCH/DELETE
  /shareholdings/{id}`` — the same four shapes over the member records, with
  money and the percentage crossing as strings (the house ``Numeric(19,2)``
  convention) in both directions.

The id-scoped routes have no company in the path, so they learn it from the
row and ask the same single grant decision in the application layer
(``app.application.officers.require_company_access``), exactly as the
change-event routes do. Refusals: 401 unauthenticated; 403 without the
company's grant; 404 ``company not found`` / ``person not found`` /
``appointment not found`` / ``shareholding not found`` (an unknown id and
another tenant's are one answer, deliberately); 409
``officer_appointment_overlap``; 422 with the domain's named reasons verbatim
(``person_name_required``, ``alternate_to_required``, ``address_required``,
``address_not_applicable``, ``capacity_role_mismatch``,
``valid_to_not_after_valid_from``, ``shareholding_invalid: shares_held``,
``tcsp_licence_invalid``, …).
"""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import Person
from app.adapters.persistence.officers import OfficerAppointment, Shareholding
from app.adapters.persistence.session import get_session
from app.api.deps import (
    ActorRef,
    get_actor_context,
    get_current_actor,
    require_company_access,
)
from app.application import contacts, officers
from app.application.company_master import CompanyNotFoundError
from app.application.officers import AppointmentWithPerson, ShareholdingWithPerson
from app.domain.change_events import (
    MAX_CAPACITIES,
    MAX_EMAIL_CHARS,
    MAX_ID_CHARS,
    MAX_NAME_HISTORY_ENTRIES,
    MAX_TEXT_CHARS,
    OfficerCapacity,
    OfficerRole,
)
from app.domain.officers import (
    MAX_CLASS_NAME_CHARS,
    MAX_COUNTRY_REGION_CHARS,
    MAX_JOINTLY_HELD_CHARS,
    MAX_REMARKS_CHARS,
    PersonType,
)
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["officers"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"
# The API's own bound on a money string crossing as text; the value itself is
# the domain's to judge.
MAX_AMOUNT_CHARS = 20


# --- Request payloads ---


class NamePair(BaseModel):
    """One canonical name pair (§3.4): at least one of ``name_zh``/``name_en``.

    The "at least one" rule is the domain's (``name_history_invalid``); this
    shape only says what an entry is.
    """

    model_config = ConfigDict(extra="forbid")

    name_zh: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    name_en: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)


class TcspLicence(BaseModel):
    """Form note 20's either/or: a licence number, or not-required with a reason.

    Both branches are stated and neither is invented; the domain refuses a
    body that says neither or both (``tcsp_licence_invalid``).
    """

    model_config = ConfigDict(extra="forbid")

    licence_no: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    not_required: bool = False
    not_required_reason: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)


class PersonWrite(BaseModel):
    """A new person's officer-facing fields (contract §2.4 identity groups).

    The §1 protected class has no field here — it is stored encrypted and
    unbuilt until §8's field-level encryption (the CE1 posture), so the public
    identification values are what an officer record carries.

    ``extra="forbid"``: the shapes are two (an existing ``person_id``, or this
    object) and a misspelled field must be a refusal rather than a person
    created without it.
    """

    model_config = ConfigDict(extra="forbid")

    person_type: PersonType = PersonType.NATURAL_PERSON
    name_zh: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    name_en: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    surname_en: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    other_names_en: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    previous_names: list[NamePair] = Field(
        default_factory=list, max_length=MAX_NAME_HISTORY_ENTRIES
    )
    aliases: list[NamePair] = Field(default_factory=list, max_length=MAX_NAME_HISTORY_ENTRIES)
    email: str | None = Field(default=None, max_length=MAX_EMAIL_CHARS, pattern=_EMAIL_PATTERN)
    br_number: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    hkid_partial: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    passport_partial: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    passport_issuing_country_region: str | None = Field(
        default=None, max_length=MAX_COUNTRY_REGION_CHARS
    )
    tcsp_licence: TcspLicence | None = None


_ADDRESS_DESCRIPTION = (
    "The §2.5 address object as filed: flat, building, street and district "
    "(required), country (optional; omitted for HK-only addresses). The "
    "shape is validated by the domain, so a refusal names the rule "
    "(`address_invalid`, `address_required`, `address_not_applicable`) "
    "rather than listing the object's keys."
)


class OfficerAppointmentCreate(BaseModel):
    """One new appointment: who, which office, from when, and as filed.

    ``person_id`` XOR ``person`` — a person already on the tenant's register,
    or the fields of one to create through the contacts write path (one human
    or firm may serve several companies, which is why the id branch exists).

    ``capacity`` is a list of strings, not the enum: which values are the
    registry's three boxes is the domain's rule, so an out-of-vocabulary tick
    is refused as ``capacity_invalid`` (the payload-grammar precedent, where
    the nested shapes belong to ``app.domain.change_events``).
    """

    model_config = ConfigDict(extra="forbid")

    person_id: UUID | None = None
    person: PersonWrite | None = None
    role: OfficerRole
    capacity: list[str] = Field(default_factory=list, max_length=MAX_CAPACITIES)
    alternate_to: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    valid_from: date
    valid_to: date | None = None
    correspondence_address: dict[str, Any] | None = Field(
        default=None, description=_ADDRESS_DESCRIPTION
    )
    registered_office_address: dict[str, Any] | None = Field(
        default=None, description=_ADDRESS_DESCRIPTION
    )

    @model_validator(mode="after")
    def _exactly_one_person(self) -> "OfficerAppointmentCreate":
        if (self.person_id is None) == (self.person is None):
            raise ValueError("exactly one of person_id and person is required")
        return self


class OfficerAppointmentUpdate(BaseModel):
    """Partial update of one appointment: omitted fields are unchanged, a field
    present with value ``null`` clears it where clearing is legal.

    The person's identity is deliberately absent — that is
    ``PATCH /persons/{id}``'s, Administrator-only.
    """

    model_config = ConfigDict(extra="forbid")

    role: OfficerRole | None = None
    capacity: list[str] | None = Field(default=None, max_length=MAX_CAPACITIES)
    alternate_to: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    valid_from: date | None = None
    valid_to: date | None = None
    correspondence_address: dict[str, Any] | None = Field(
        default=None, description=_ADDRESS_DESCRIPTION
    )
    registered_office_address: dict[str, Any] | None = Field(
        default=None, description=_ADDRESS_DESCRIPTION
    )

    @model_validator(mode="after")
    def _some_field_required(self) -> "OfficerAppointmentUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class ShareholdingCreate(BaseModel):
    """One new member position; money crosses as strings (the house convention).

    ``shares_held`` and the amounts are not range-checked here: the domain
    refuses a negative or a non-decimal value with its own field-named reason,
    which is the answer a caller needs, rather than a generic body error.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: UUID | None = None
    person: PersonWrite | None = None
    class_name: str = Field(max_length=MAX_CLASS_NAME_CHARS)
    shares_held: int
    jointly_held: str | None = Field(default=None, max_length=MAX_JOINTLY_HELD_CHARS)
    remarks: str | None = Field(default=None, max_length=MAX_REMARKS_CHARS)
    percentage_held: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    allotment_date: date | None = None
    amount_paid: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    amount_unpaid: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    address: dict[str, Any] | None = Field(default=None, description=_ADDRESS_DESCRIPTION)
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def _exactly_one_person(self) -> "ShareholdingCreate":
        if (self.person_id is None) == (self.person is None):
            raise ValueError("exactly one of person_id and person is required")
        return self


class ShareholdingUpdate(BaseModel):
    """Partial update of one shareholding; the create body's rules apply."""

    model_config = ConfigDict(extra="forbid")

    class_name: str | None = Field(default=None, max_length=MAX_CLASS_NAME_CHARS)
    shares_held: int | None = None
    jointly_held: str | None = Field(default=None, max_length=MAX_JOINTLY_HELD_CHARS)
    remarks: str | None = Field(default=None, max_length=MAX_REMARKS_CHARS)
    percentage_held: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    allotment_date: date | None = None
    amount_paid: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    amount_unpaid: str | None = Field(default=None, max_length=MAX_AMOUNT_CHARS)
    address: dict[str, Any] | None = Field(default=None, description=_ADDRESS_DESCRIPTION)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _some_field_required(self) -> "ShareholdingUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


# --- Responses ---


class PersonSnapshotResponse(BaseModel):
    """The person an appointment or a holding points at (§2.4 identity groups)."""

    id: UUID
    person_type: PersonType
    name_zh: str | None
    name_en: str | None
    surname_en: str | None
    other_names_en: str | None
    previous_names: list[dict[str, Any]]
    aliases: list[dict[str, Any]]
    email: str | None
    br_number: str | None
    hkid_partial: str | None
    passport_partial: str | None
    passport_issuing_country_region: str | None
    tcsp_licence: dict[str, Any] | None


class OfficerAppointmentResponse(BaseModel):
    """One appointment as filed, with its person snapshot.

    ``valid_from``/``valid_to`` are the service window: in office as of
    ``as_of`` ⟺ ``valid_from <= as_of < valid_to``, ``valid_to`` being the
    cessation's effective date (the first day out of office) and ``null``
    open-ended.
    """

    id: UUID
    company_id: UUID
    person_id: UUID
    role: OfficerRole
    capacity: list[OfficerCapacity]
    alternate_to: str | None
    valid_from: date
    valid_to: date | None
    correspondence_address: dict[str, Any] | None
    registered_office_address: dict[str, Any] | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    person: PersonSnapshotResponse


class ShareholdingResponse(BaseModel):
    """One member position, with its person snapshot.

    ``percentage_held``, ``amount_paid`` and ``amount_unpaid`` are strings —
    ``Numeric`` values cross as strings in both directions (the house
    convention), so a cent never rounds through a float.
    """

    id: UUID
    company_id: UUID
    person_id: UUID
    class_name: str
    shares_held: int
    jointly_held: str | None
    remarks: str | None
    percentage_held: str | None
    allotment_date: date | None
    amount_paid: str | None
    amount_unpaid: str | None
    address: dict[str, Any] | None
    valid_from: date
    valid_to: date | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    person: PersonSnapshotResponse


def _address(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """The address as the §2.5 object stores it: an absent country is omitted.

    An HK-only address carries four keys, never ``country: null`` — the
    contract's rule ("omitted for HK-only addresses, never invented as an
    empty string"), and what the domain's validator expects. Nothing else is
    touched: the domain is what decides whether the object is an address.
    """
    if value is None:
        return None
    address = dict(value)
    if address.get("country") is None:
        address.pop("country", None)
    return address


def _person_response(person: Person) -> PersonSnapshotResponse:
    return PersonSnapshotResponse(
        id=person.id,
        person_type=PersonType(person.person_type),
        name_zh=person.name_zh,
        name_en=person.name_en,
        surname_en=person.surname_en,
        other_names_en=person.other_names_en,
        previous_names=list(person.previous_names or []),
        aliases=list(person.aliases or []),
        email=person.email,
        br_number=person.br_number,
        hkid_partial=person.hkid_partial,
        passport_partial=person.passport_partial,
        passport_issuing_country_region=person.passport_issuing_country_region,
        tcsp_licence=person.tcsp_licence,
    )


def _appointment_response(row: AppointmentWithPerson) -> OfficerAppointmentResponse:
    appointment: OfficerAppointment = row.appointment
    return OfficerAppointmentResponse(
        id=appointment.id,
        company_id=appointment.company_id,
        person_id=appointment.person_id,
        role=OfficerRole(appointment.role),
        capacity=[OfficerCapacity(value) for value in appointment.capacity or []],
        alternate_to=appointment.alternate_to,
        valid_from=appointment.valid_from,
        valid_to=appointment.valid_to,
        correspondence_address=appointment.correspondence_address,
        registered_office_address=appointment.registered_office_address,
        created_by=appointment.created_by,
        created_at=appointment.created_at,
        updated_at=appointment.updated_at,
        person=_person_response(row.person),
    )


def _shareholding_response(row: ShareholdingWithPerson) -> ShareholdingResponse:
    shareholding: Shareholding = row.shareholding
    return ShareholdingResponse(
        id=shareholding.id,
        company_id=shareholding.company_id,
        person_id=shareholding.person_id,
        class_name=shareholding.class_name,
        shares_held=shareholding.shares_held,
        jointly_held=shareholding.jointly_held,
        remarks=shareholding.remarks,
        percentage_held=_amount(shareholding.percentage_held),
        allotment_date=shareholding.allotment_date,
        amount_paid=_amount(shareholding.amount_paid),
        amount_unpaid=_amount(shareholding.amount_unpaid),
        address=shareholding.address,
        valid_from=shareholding.valid_from,
        valid_to=shareholding.valid_to,
        created_by=shareholding.created_by,
        created_at=shareholding.created_at,
        updated_at=shareholding.updated_at,
        person=_person_response(row.person),
    )


def _amount(value: object) -> str | None:
    """A ``Numeric`` column as the string the API carries it as (``None`` → null)."""
    return None if value is None else str(value)


def _invalid(exc: officers.OfficerInvalidError | contacts.PersonInvalidError) -> HTTPException:
    """The typed refusal for a record that leaves the rules (contract §5.1).

    Every reason travels in one answer, field paths and rules only — never
    client data — the way the change-event grammar's violations do.
    """
    return HTTPException(status_code=422, detail="; ".join(exc.reasons))


def _person_fields(payload: PersonWrite | None) -> dict[str, Any] | None:
    """The new person's fields as the write path takes them (plain JSON)."""
    return None if payload is None else payload.model_dump()


def _appointment_changes(payload: OfficerAppointmentUpdate) -> dict[str, Any]:
    """The appointment fields this request actually stated (absent keeps, null clears)."""
    changes: dict[str, Any] = {}
    for field in officers.APPOINTMENT_FIELDS:
        if field not in payload.model_fields_set:
            continue
        value = getattr(payload, field)
        if field in ("correspondence_address", "registered_office_address"):
            changes[field] = _address(value)
        else:
            changes[field] = value
    return changes


def _shareholding_changes(payload: ShareholdingUpdate) -> dict[str, Any]:
    """The shareholding fields this request actually stated (absent keeps, null clears)."""
    changes: dict[str, Any] = {}
    for field in officers.SHAREHOLDING_FIELDS:
        if field not in payload.model_fields_set:
            continue
        value = getattr(payload, field)
        changes[field] = _address(value) if field == "address" else value
    return changes


# --- Officer appointment routes ---


@router.post("/companies/{company_id}/officers", status_code=201)
async def create_officer_appointment(
    company_id: UUID,
    payload: OfficerAppointmentCreate,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OfficerAppointmentResponse:
    """Record one officer's office (an existing person, or a new one).

    404 for an unknown or cross-tenant company, or for a ``person_id`` that is
    not on this tenant's register; 422 with the domain's named reasons for a
    record the rules refuse; 409 ``officer_appointment_overlap`` when the
    window covers another appointment of the same person in the same role.
    """
    try:
        row = await officers.create_appointment(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            role=payload.role,
            capacity=payload.capacity,
            valid_from=payload.valid_from,
            person_id=payload.person_id,
            person=_person_fields(payload.person),
            alternate_to=payload.alternate_to,
            valid_to=payload.valid_to,
            correspondence_address=_address(payload.correspondence_address),
            registered_office_address=_address(payload.registered_office_address),
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    except (officers.OfficerInvalidError, contacts.PersonInvalidError) as exc:
        raise _invalid(exc) from None
    except officers.OfficerOverlapError:
        raise HTTPException(status_code=409, detail="officer_appointment_overlap") from None
    return _appointment_response(row)


@router.get("/companies/{company_id}/officers")
async def list_officer_appointments(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[date | None, Query()] = None,
) -> list[OfficerAppointmentResponse]:
    """The company's appointments, newest first, each with its person.

    ``as_of`` narrows to the appointments in office that day; without it every
    row is returned, the ceased ones included. A read: no writes, no audit
    entries.
    """
    try:
        rows = await officers.list_appointments(
            session, tenant_id=actor.tenant_id, company_id=company_id, as_of=as_of
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_appointment_response(row) for row in rows]


@router.patch("/officers/{appointment_id}")
async def update_officer_appointment(
    appointment_id: UUID,
    payload: OfficerAppointmentUpdate,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OfficerAppointmentResponse:
    """Correct one appointment in place (partial; explicit null clears).

    404 for an unknown or cross-tenant id, 403 without the company's grant,
    422 with the domain's named reasons (or a field that cannot be cleared),
    409 ``officer_appointment_overlap`` when the new window covers another
    appointment of the same person in the same role.
    """
    try:
        row = await officers.update_appointment(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            appointment_id=appointment_id,
            actor_user_id=actor.user_id,
            changes=_appointment_changes(payload),
        )
    except officers.AppointmentNotFoundError:
        raise HTTPException(status_code=404, detail="appointment not found") from None
    except officers.OfficerForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    except officers.OfficerInvalidError as exc:
        raise _invalid(exc) from None
    except officers.OfficerOverlapError:
        raise HTTPException(status_code=409, detail="officer_appointment_overlap") from None
    return _appointment_response(row)


@router.delete("/officers/{appointment_id}", status_code=204)
async def delete_officer_appointment(
    appointment_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove one appointment; the audit entry carries the values it held.

    A cessation is a window correction (PATCH ``valid_to``); this route is for
    a row entered by mistake, and nothing about it is recoverable from the
    tables — the chain is where what it said survives.
    """
    try:
        await officers.delete_appointment(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            appointment_id=appointment_id,
            actor_user_id=actor.user_id,
        )
    except officers.AppointmentNotFoundError:
        raise HTTPException(status_code=404, detail="appointment not found") from None
    except officers.OfficerForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None


# --- Shareholding routes ---


@router.post("/companies/{company_id}/shareholdings", status_code=201)
async def create_shareholding(
    company_id: UUID,
    payload: ShareholdingCreate,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ShareholdingResponse:
    """Record one member's holding (an existing person, or a new one).

    The refusals are the appointment create's; a value the member boxes cannot
    carry answers ``shareholding_invalid: <field>`` naming the box to fix.
    """
    try:
        row = await officers.create_shareholding(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            class_name=payload.class_name,
            shares_held=payload.shares_held,
            valid_from=payload.valid_from,
            person_id=payload.person_id,
            person=_person_fields(payload.person),
            jointly_held=payload.jointly_held,
            remarks=payload.remarks,
            percentage_held=payload.percentage_held,
            allotment_date=payload.allotment_date,
            amount_paid=payload.amount_paid,
            amount_unpaid=payload.amount_unpaid,
            address=_address(payload.address),
            valid_to=payload.valid_to,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except contacts.PersonNotFoundError:
        raise HTTPException(status_code=404, detail="person not found") from None
    except (officers.OfficerInvalidError, contacts.PersonInvalidError) as exc:
        raise _invalid(exc) from None
    return _shareholding_response(row)


@router.get("/companies/{company_id}/shareholdings")
async def list_shareholdings(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[date | None, Query()] = None,
) -> list[ShareholdingResponse]:
    """The company's shareholdings, newest first, each with its person.

    ``as_of`` narrows to the holdings effective that day; without it every row
    is returned. A read: no writes, no audit entries.
    """
    try:
        rows = await officers.list_shareholdings(
            session, tenant_id=actor.tenant_id, company_id=company_id, as_of=as_of
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_shareholding_response(row) for row in rows]


@router.patch("/shareholdings/{shareholding_id}")
async def update_shareholding(
    shareholding_id: UUID,
    payload: ShareholdingUpdate,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ShareholdingResponse:
    """Correct one shareholding in place (partial; explicit null clears)."""
    try:
        row = await officers.update_shareholding(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            shareholding_id=shareholding_id,
            actor_user_id=actor.user_id,
            changes=_shareholding_changes(payload),
        )
    except officers.ShareholdingNotFoundError:
        raise HTTPException(status_code=404, detail="shareholding not found") from None
    except officers.OfficerForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
    except officers.OfficerInvalidError as exc:
        raise _invalid(exc) from None
    return _shareholding_response(row)


@router.delete("/shareholdings/{shareholding_id}", status_code=204)
async def delete_shareholding(
    shareholding_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(get_actor_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Remove one shareholding; the audit entry carries the values it held."""
    try:
        await officers.delete_shareholding(
            session,
            tenant_id=actor.tenant_id,
            context=context,
            shareholding_id=shareholding_id,
            actor_user_id=actor.user_id,
        )
    except officers.ShareholdingNotFoundError:
        raise HTTPException(status_code=404, detail="shareholding not found") from None
    except officers.OfficerForbiddenError:
        raise HTTPException(status_code=403, detail="forbidden") from None
