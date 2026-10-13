"""Officer and shareholding orchestration: the only write path for
``officer_appointments`` and ``shareholdings`` (contract §2.4; migration
``0025``).

Both records are ordinary staff master-data maintenance, so every route is
grant-gated per company with **no permission flag** (the master-data rule,
founder decision 2026-08-24) and ungated by the §5.1 automation gate: keeping
the officer register straight is not an automation any tier gates.

- ``create_appointment`` / ``update_appointment`` / ``delete_appointment`` and
  the shareholding trio are the one write path for their tables. Each loads
  its company through ``company_master.get_company``, validates the resolved
  record through ``app.domain.officers`` (the one home of the rules), refuses
  a window that overlaps another appointment of the same person in the same
  role, and emits its audit entry on the same session — so the change and its
  entry commit or roll back together (ADR-0009).
- **Persons have one writer.** A create body names an existing ``person_id``
  or carries a new person's fields; either way the person goes through
  ``app.application.contacts`` (``get_person`` / ``create_person``), never a
  second writer here. An unknown or another tenant's ``person_id`` is
  ``PersonNotFoundError`` — the same 404 as an unknown id, deliberately.
- ``list_appointments`` / ``list_shareholdings`` are the reads F2's draft
  construction consumes: every row, newest ``valid_from`` first, each with its
  person snapshot, and ``as_of`` filtering to the rows in office that day —
  the half-open service window ``app.domain.officers.in_office`` states.

**The overlap rule, and why it is not a database constraint.** §2.4's record
is effective-dated, so the same person may hold the same office twice over
disjoint windows (a reappointment) and hold several offices at once (a
directorship and the company secretary). What the product refuses is two
*concurrent* appointments of the same person in the same role at the same
company: that is a double-entry of one office, and every box it feeds would
print twice. The refusal names the condition (``officer_appointment_overlap``,
409) and the caller fixes it by correcting the window it meant. The write path
owns the rule because the table is corrected in place and its windows are
open-ended; the domain's ``overlaps`` is the predicate it asks.

**Person identity corrections stay on the person's own route**:
``PATCH /persons/{id}`` (Administrator-only) writes the officer-facing person
fields, and an appointment's PATCH never touches them — one record, one write
path.

Services flush but never commit — the API session dependency commits at the
request edge. Reads write nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import Person
from app.adapters.persistence.officers import OfficerAppointment, Shareholding
from app.application import audit, company_master, contacts
from app.domain.audit import AuditAction
from app.domain.change_events import OfficerRole
from app.domain.officers import (
    PersonType,
    appointment_violations,
    decimal_value,
    in_office,
    overlaps,
    shareholding_violations,
)
from app.domain.rbac import ActorContext, can_access_company
from app.domain.reply_parsing import joined_english_name


class AppointmentNotFoundError(Exception):
    """No officer appointment with this id in the actor's tenant (cross-tenant → unknown)."""


class ShareholdingNotFoundError(Exception):
    """No shareholding with this id in the actor's tenant (cross-tenant → unknown)."""


class OfficerForbiddenError(Exception):
    """The actor has no per-company access grant for the record's company."""


class OfficerOverlapError(Exception):
    """Another appointment of this person, in this role, covers the same days."""


class OfficerInvalidError(Exception):
    """The record leaves ``app.domain.officers``' rules; ``reasons`` names them.

    The reasons are the domain's own — ``alternate_to_required``,
    ``address_required``, ``capacity_role_mismatch``,
    ``valid_to_not_after_valid_from``, ``shareholding_invalid: <field>`` and
    the rest — and the API answers them verbatim (contract §5.1).
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class AppointmentWithPerson:
    """An appointment with its person snapshot — the read F2 consumes."""

    appointment: OfficerAppointment
    person: Person


@dataclass(frozen=True)
class ShareholdingWithPerson:
    """A shareholding with its person snapshot — the read F2 consumes."""

    shareholding: Shareholding
    person: Person


# The fields an appointment's PATCH may state (its office, its boxes, its
# addresses and its window). The person's identity is not among them: it is
# the person record's, written on ``PATCH /persons/{id}``.
APPOINTMENT_FIELDS: tuple[str, ...] = (
    "role",
    "capacity",
    "alternate_to",
    "valid_from",
    "valid_to",
    "correspondence_address",
    "registered_office_address",
)

# The fields a shareholding's PATCH may state.
SHAREHOLDING_FIELDS: tuple[str, ...] = (
    "class_name",
    "shares_held",
    "jointly_held",
    "remarks",
    "percentage_held",
    "allotment_date",
    "amount_paid",
    "amount_unpaid",
    "address",
    "valid_from",
    "valid_to",
)

# The fields a record cannot be left without: an appointment with no office or
# no start, a holding with no class or no shares, is not a record.
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"role", "valid_from", "class_name", "shares_held"}
)


async def _get_appointment(
    session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID
) -> OfficerAppointment:
    """The appointment, or ``AppointmentNotFoundError`` (unknown and cross-tenant alike)."""
    row = await session.scalar(
        select(OfficerAppointment).where(
            OfficerAppointment.id == appointment_id,
            OfficerAppointment.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise AppointmentNotFoundError
    return row


async def _get_shareholding(
    session: AsyncSession, *, tenant_id: UUID, shareholding_id: UUID
) -> Shareholding:
    """The shareholding, or ``ShareholdingNotFoundError`` (unknown and cross-tenant alike)."""
    row = await session.scalar(
        select(Shareholding).where(
            Shareholding.id == shareholding_id,
            Shareholding.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise ShareholdingNotFoundError
    return row


def require_company_access(context: ActorContext, company_id: UUID) -> None:
    """The grant-only gate for the id-scoped routes (no ``company_id`` in path).

    The same single decision ``app.api.deps.require_company_access`` applies to
    company-scoped routes; these routes learn their company from the row, so
    the check happens here instead — one mechanism, one place it is consulted.
    """
    if not can_access_company(context, company_id):
        raise OfficerForbiddenError


async def _person_for(session: AsyncSession, *, tenant_id: UUID, person_id: UUID) -> Person:
    """The person through the contacts seam (404 for unknown or cross-tenant)."""
    row = await contacts.get_person(session, tenant_id=tenant_id, person_id=person_id)
    return row.person


async def _create_person(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    person: Mapping[str, Any],
) -> Person:
    """A new person through the contacts write path (never a second writer)."""
    return await contacts.create_person(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        name_en=person.get("name_en"),
        name_zh=person.get("name_zh"),
        email=person.get("email"),
        person_type=person.get("person_type"),
        surname_en=person.get("surname_en"),
        other_names_en=person.get("other_names_en"),
        previous_names=person.get("previous_names"),
        aliases=person.get("aliases"),
        br_number=person.get("br_number"),
        hkid_partial=person.get("hkid_partial"),
        passport_partial=person.get("passport_partial"),
        passport_issuing_country_region=person.get("passport_issuing_country_region"),
        tcsp_licence=person.get("tcsp_licence"),
    )


async def _resolve_person(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    person_id: UUID | None,
    person: Mapping[str, Any] | None,
) -> Person:
    """The person a create names — an existing one, or a newly created one.

    Exactly one of the two shapes may be present (the route's schema says the
    same); neither is a request with nobody to appoint, and both is a request
    that cannot decide who. The existing-id branch goes through the contacts
    read seam, so an unknown or cross-tenant id is the same 404.
    """
    _require_one_person_shape(person_id=person_id, person=person)
    if person_id is not None:
        return await _person_for(session, tenant_id=tenant_id, person_id=person_id)
    assert person is not None  # the guard above
    return await _create_person(
        session, tenant_id=tenant_id, actor_user_id=actor_user_id, person=person
    )


def _require_one_person_shape(
    *, person_id: UUID | None, person: Mapping[str, Any] | None
) -> None:
    """Exactly one of the two shapes, or the request cannot name its person."""
    if person_id is None and person is None:
        raise OfficerInvalidError(["person_required"])
    if person_id is not None and person is not None:
        raise OfficerInvalidError(["person_not_applicable"])


async def _person_type_for(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    person_id: UUID | None,
    person: Mapping[str, Any] | None,
) -> str:
    """The person type a create's address rule is judged against, without writing.

    An existing person is read (the same 404 seam as ``_resolve_person``); an
    inline one is described by the body's own ``person_type``, because nothing
    is stored until the record the person is attached to has been validated —
    a refused appointment leaves no person behind.
    """
    _require_one_person_shape(person_id=person_id, person=person)
    if person_id is not None:
        row = await _person_for(session, tenant_id=tenant_id, person_id=person_id)
        return row.person_type
    assert person is not None  # the guard above
    return str(person.get("person_type") or PersonType.NATURAL_PERSON.value)


async def _people_by_id(
    session: AsyncSession, *, tenant_id: UUID, person_ids: set[UUID]
) -> dict[UUID, Person]:
    """The persons these records point at, keyed by id (empty → none)."""
    if not person_ids:
        return {}
    rows = await session.scalars(
        select(Person).where(Person.tenant_id == tenant_id, Person.id.in_(person_ids))
    )
    return {person.id: person for person in rows}


async def _require_no_overlap(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    person_id: UUID,
    role: OfficerRole,
    valid_from: date,
    valid_to: date | None,
    exclude_id: UUID | None = None,
) -> None:
    """Refuse a window that shares a day with another appointment of this role.

    The candidates are the same company, person and role — a handful of rows
    over a career — and the predicate is the domain's, so the service never
    restates the half-open rule in SQL.
    """
    statement = select(OfficerAppointment).where(
        OfficerAppointment.tenant_id == tenant_id,
        OfficerAppointment.company_id == company_id,
        OfficerAppointment.person_id == person_id,
        OfficerAppointment.role == role.value,
    )
    if exclude_id is not None:
        statement = statement.where(OfficerAppointment.id != exclude_id)
    rows = (await session.scalars(statement)).all()
    for row in rows:
        if overlaps(row.valid_from, row.valid_to, valid_from, valid_to):
            raise OfficerOverlapError


def _appointment_values(row: OfficerAppointment) -> dict[str, object]:
    """Every field an appointment carries, as the audit entry's snapshot."""
    return {
        "role": row.role,
        "capacity": list(row.capacity or []),
        "alternate_to": row.alternate_to,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "correspondence_address": row.correspondence_address,
        "registered_office_address": row.registered_office_address,
    }


def _appointment_column(field: str, value: Any) -> Any:
    """A stated appointment value as the column stores it (the request's shape → the row's).

    An explicit ``null`` clears the boxes (``[]``, the column's own default)
    and the addresses; the office and the start cannot be cleared (refused
    before this is reached).
    """
    if field == "role":
        return OfficerRole(str(value)).value
    if field == "capacity":
        return [] if value is None else [str(item) for item in value]
    if field in ("correspondence_address", "registered_office_address"):
        return None if value is None else dict(value)
    return value


def _appointment_reasons(row: OfficerAppointment, *, person_type: str) -> list[str]:
    """The domain's reasons for an appointment row, as it stands."""
    return appointment_violations(
        person_type=PersonType(person_type),
        role=OfficerRole(row.role),
        capacity=row.capacity,
        alternate_to=row.alternate_to,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        correspondence_address=row.correspondence_address,
        registered_office_address=row.registered_office_address,
    )


def _shareholding_values(row: Shareholding) -> dict[str, object]:
    """Every field a shareholding carries, as the audit entry's snapshot."""
    return {
        "class_name": row.class_name,
        "shares_held": row.shares_held,
        "jointly_held": row.jointly_held,
        "remarks": row.remarks,
        "percentage_held": row.percentage_held,
        "allotment_date": row.allotment_date,
        "amount_paid": row.amount_paid,
        "amount_unpaid": row.amount_unpaid,
        "address": row.address,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
    }


def _shareholding_column(field: str, value: Any) -> Any:
    """A stated shareholding value as the column stores it.

    The three numeric columns are parsed here (the domain has already judged
    the caller's raw value); an explicit ``null`` clears any nullable field.
    """
    if field in ("percentage_held", "amount_paid", "amount_unpaid"):
        return decimal_value(value)
    if field == "address":
        return None if value is None else dict(value)
    return value


def _shareholding_reasons(
    *,
    class_name: object,
    shares_held: object,
    valid_from: date,
    jointly_held: object = None,
    remarks: object = None,
    percentage_held: object = None,
    amount_paid: object = None,
    amount_unpaid: object = None,
    valid_to: date | None = None,
) -> list[str]:
    """The domain's reasons for the values a shareholding would carry.

    The amounts are the caller's raw values (strings on the wire), not the
    parsed decimals: a value that is not a decimal at all must be refused as
    its field's reason rather than silently stored as NULL. The window is the
    proposed one — the row's own values for fields the request did not state.
    """
    return shareholding_violations(
        class_name=class_name,
        shares_held=shares_held,
        valid_from=valid_from,
        jointly_held=jointly_held,
        remarks=remarks,
        percentage_held=percentage_held,
        amount_paid=amount_paid,
        amount_unpaid=amount_unpaid,
        valid_to=valid_to,
    )


# --- Officer appointments ---


async def create_appointment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    role: OfficerRole,
    valid_from: date,
    capacity: Sequence[str] | None = None,
    person_id: UUID | None = None,
    person: Mapping[str, Any] | None = None,
    alternate_to: str | None = None,
    valid_to: date | None = None,
    correspondence_address: Mapping[str, Any] | None = None,
    registered_office_address: Mapping[str, Any] | None = None,
) -> AppointmentWithPerson:
    """Record one officer's office, creating the person when the body carries one.

    The record is validated **before** anything is written: a refused
    appointment leaves no person behind. ``CompanyNotFoundError`` for an
    unknown or cross-tenant company, ``PersonNotFoundError`` for an unknown or
    cross-tenant ``person_id``, ``PersonInvalidError``/``OfficerInvalidError``
    with the domain's named reasons for a record the rules refuse, and
    ``OfficerOverlapError`` when the window covers another appointment of the
    same person in the same role.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    person_type = await _person_type_for(
        session, tenant_id=tenant_id, person_id=person_id, person=person
    )
    reasons = appointment_violations(
        person_type=PersonType(person_type),
        role=role,
        capacity=capacity,
        alternate_to=alternate_to,
        valid_from=valid_from,
        valid_to=valid_to,
        correspondence_address=correspondence_address,
        registered_office_address=registered_office_address,
    )
    if reasons:
        raise OfficerInvalidError(reasons)
    officer = await _resolve_person(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        person_id=person_id,
        person=person,
    )
    await _require_no_overlap(
        session,
        tenant_id=tenant_id,
        company_id=company.id,
        person_id=officer.id,
        role=role,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    row = OfficerAppointment(
        tenant_id=tenant_id,
        company_id=company.id,
        person_id=officer.id,
        role=role.value,
        capacity=[] if capacity is None else [str(value) for value in capacity],
        alternate_to=alternate_to,
        valid_from=valid_from,
        valid_to=valid_to,
        correspondence_address=(
            None if correspondence_address is None else dict(correspondence_address)
        ),
        registered_office_address=(
            None if registered_office_address is None else dict(registered_office_address)
        ),
        created_by=actor_user_id,
    )
    session.add(row)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.OFFICER_APPOINTMENT_CREATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="officer_appointment",
        entity_id=row.id,
        details={
            "company_id": company.id,
            "person_id": officer.id,
            "role": row.role,
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
        },
    )
    return AppointmentWithPerson(appointment=row, person=officer)


async def update_appointment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    appointment_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> AppointmentWithPerson:
    """Correct one appointment's fields in place (partial; explicit null clears).

    The person's identity is not among the changes: an appointment's fields
    are its office, its boxes, its addresses and its window, and a person
    correction is the person route's. ``AppointmentNotFoundError`` for an
    unknown or cross-tenant id, ``OfficerForbiddenError`` without the
    company's grant, and the domain's named reasons (or
    ``OfficerOverlapError``) for a change the rules refuse. The audit entry
    carries the before and after values of exactly the fields the request
    stated.
    """
    row = await _get_appointment(session, tenant_id=tenant_id, appointment_id=appointment_id)
    require_company_access(context, row.company_id)
    officer = await _person_for(session, tenant_id=tenant_id, person_id=row.person_id)
    resolved: dict[str, Any] = {}
    for field in APPOINTMENT_FIELDS:
        if field not in changes:
            continue
        value = changes[field]
        if value is None and field in _REQUIRED_FIELDS:
            raise OfficerInvalidError([f"{field}: cannot be cleared"])
        resolved[field] = value
    if not resolved:
        raise OfficerInvalidError(["edit: nothing to change"])
    # The record is judged as it *would* stand, before anything is written:
    # a refused correction leaves the row exactly as it was.
    proposed = {
        field: resolved.get(field, getattr(row, field)) for field in APPOINTMENT_FIELDS
    }
    role = OfficerRole(proposed["role"])
    reasons = appointment_violations(
        person_type=PersonType(officer.person_type),
        role=role,
        capacity=proposed["capacity"],
        alternate_to=proposed["alternate_to"],
        valid_from=proposed["valid_from"],
        valid_to=proposed["valid_to"],
        correspondence_address=proposed["correspondence_address"],
        registered_office_address=proposed["registered_office_address"],
    )
    if reasons:
        raise OfficerInvalidError(reasons)
    await _require_no_overlap(
        session,
        tenant_id=tenant_id,
        company_id=row.company_id,
        person_id=row.person_id,
        role=role,
        valid_from=proposed["valid_from"],
        valid_to=proposed["valid_to"],
        exclude_id=row.id,
    )
    before = {field: getattr(row, field) for field in resolved}
    for field, value in resolved.items():
        setattr(row, field, _appointment_column(field, value))
    await session.flush()
    # ``updated_at`` is the database's own (``onupdate=func.now()``), so the
    # UPDATE expires it and the read below is what loads it again — the
    # response carries the moment the database wrote.
    await session.refresh(row, attribute_names=["updated_at"])
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.OFFICER_APPOINTMENT_UPDATED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="officer_appointment",
        entity_id=row.id,
        details={
            "company_id": row.company_id,
            "person_id": row.person_id,
            "role": row.role,
            "before": before,
            "after": {field: getattr(row, field) for field in resolved},
        },
    )
    return AppointmentWithPerson(appointment=row, person=officer)


async def delete_appointment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    appointment_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """Remove one appointment; the audit entry carries the values it held.

    Officers and members are master data corrected in place, not a workflow
    record: a row entered by mistake is deleted rather than soft-ended, and
    the chain keeps what it said. A cessation is a correction of the window
    (``valid_to``), which is a PATCH — the change event carries the event
    itself, the appointment its effect on the register.
    """
    row = await _get_appointment(session, tenant_id=tenant_id, appointment_id=appointment_id)
    require_company_access(context, row.company_id)
    details = {
        "company_id": row.company_id,
        "person_id": row.person_id,
        "role": row.role,
        "before": _appointment_values(row),
        "after": None,
    }
    await session.delete(row)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.OFFICER_APPOINTMENT_DELETED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="officer_appointment",
        entity_id=row.id,
        details=details,
    )


async def list_appointments(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, as_of: date | None = None
) -> list[AppointmentWithPerson]:
    """The company's appointments, newest ``valid_from`` first, with persons.

    ``as_of`` filters to the appointments in office that day (the half-open
    window); without it every row is returned, the ceased ones included —
    which is what a correction screen and the draft's provenance need.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = list(
        (
            await session.scalars(
                select(OfficerAppointment)
                .where(
                    OfficerAppointment.tenant_id == tenant_id,
                    OfficerAppointment.company_id == company.id,
                )
                .order_by(
                    OfficerAppointment.valid_from.desc(),
                    OfficerAppointment.created_at.desc(),
                    OfficerAppointment.id,
                )
            )
        ).all()
    )
    if as_of is not None:
        rows = [row for row in rows if in_office(row.valid_from, row.valid_to, as_of)]
    people = await _people_by_id(
        session, tenant_id=tenant_id, person_ids={row.person_id for row in rows}
    )
    return [AppointmentWithPerson(appointment=row, person=people[row.person_id]) for row in rows]


# --- Shareholdings ---


async def create_shareholding(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    class_name: str,
    shares_held: int,
    valid_from: date,
    person_id: UUID | None = None,
    person: Mapping[str, Any] | None = None,
    jointly_held: str | None = None,
    remarks: str | None = None,
    percentage_held: object = None,
    allotment_date: date | None = None,
    amount_paid: object = None,
    amount_unpaid: object = None,
    address: Mapping[str, Any] | None = None,
    valid_to: date | None = None,
) -> ShareholdingWithPerson:
    """Record one member's holding, creating the person when the body carries one.

    As with an appointment, the record is validated before anything is
    written: a refused holding leaves no person behind. Money and the
    percentage cross as strings (the house ``Numeric`` convention) and are
    stored as ``Decimal``; a value that is not a decimal number is the
    domain's ``shareholding_invalid: <field>`` rather than a silently stored
    NULL. The same ``CompanyNotFoundError`` / ``PersonNotFoundError`` /
    ``OfficerInvalidError`` refusals as an appointment's create.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    reasons = _shareholding_reasons(
        class_name=class_name,
        shares_held=shares_held,
        valid_from=valid_from,
        jointly_held=jointly_held,
        remarks=remarks,
        percentage_held=percentage_held,
        amount_paid=amount_paid,
        amount_unpaid=amount_unpaid,
        valid_to=valid_to,
    )
    if reasons:
        raise OfficerInvalidError(reasons)
    holder = await _resolve_person(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        person_id=person_id,
        person=person,
    )
    row = Shareholding(
        tenant_id=tenant_id,
        company_id=company.id,
        person_id=holder.id,
        class_name=class_name,
        shares_held=shares_held,
        jointly_held=jointly_held,
        remarks=remarks,
        percentage_held=decimal_value(percentage_held),
        allotment_date=allotment_date,
        amount_paid=decimal_value(amount_paid),
        amount_unpaid=decimal_value(amount_unpaid),
        address=None if address is None else dict(address),
        valid_from=valid_from,
        valid_to=valid_to,
        created_by=actor_user_id,
    )
    session.add(row)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SHAREHOLDING_CREATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="shareholding",
        entity_id=row.id,
        details={
            "company_id": company.id,
            "person_id": holder.id,
            "class_name": row.class_name,
            "shares_held": row.shares_held,
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
        },
    )
    return ShareholdingWithPerson(shareholding=row, person=holder)


async def update_shareholding(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    shareholding_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> ShareholdingWithPerson:
    """Correct one shareholding in place (partial; explicit null clears).

    A money field present with ``null`` clears it; the surface fields
    (``class_name``, ``shares_held``, ``valid_from``) cannot be cleared. The
    refusals are the create path's, and the audit entry carries the before and
    after values of exactly the fields the request stated.
    """
    row = await _get_shareholding(session, tenant_id=tenant_id, shareholding_id=shareholding_id)
    require_company_access(context, row.company_id)
    holder = await _person_for(session, tenant_id=tenant_id, person_id=row.person_id)
    resolved: dict[str, Any] = {}
    for field in SHAREHOLDING_FIELDS:
        if field not in changes:
            continue
        value = changes[field]
        if value is None and field in _REQUIRED_FIELDS:
            raise OfficerInvalidError([f"{field}: cannot be cleared"])
        resolved[field] = value
    if not resolved:
        raise OfficerInvalidError(["edit: nothing to change"])
    # The record is judged as it *would* stand, before anything is written —
    # and the amounts are judged on the caller's raw values, so a value that is
    # not a decimal number is that field's named reason rather than a NULL.
    reasons = _shareholding_reasons(
        class_name=resolved.get("class_name", row.class_name),
        shares_held=resolved.get("shares_held", row.shares_held),
        valid_from=resolved.get("valid_from", row.valid_from),
        jointly_held=resolved.get("jointly_held", row.jointly_held),
        remarks=resolved.get("remarks", row.remarks),
        percentage_held=resolved.get("percentage_held", row.percentage_held),
        amount_paid=resolved.get("amount_paid", row.amount_paid),
        amount_unpaid=resolved.get("amount_unpaid", row.amount_unpaid),
        valid_to=resolved.get("valid_to", row.valid_to),
    )
    if reasons:
        raise OfficerInvalidError(reasons)
    before = {field: getattr(row, field) for field in resolved}
    for field, value in resolved.items():
        setattr(row, field, _shareholding_column(field, value))
    await session.flush()
    # ``updated_at`` is the database's own (``onupdate=func.now()``): the
    # UPDATE expires it, and this reloads the moment the database wrote.
    await session.refresh(row, attribute_names=["updated_at"])
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SHAREHOLDING_UPDATED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="shareholding",
        entity_id=row.id,
        details={
            "company_id": row.company_id,
            "person_id": row.person_id,
            "class_name": row.class_name,
            "before": before,
            "after": {field: getattr(row, field) for field in resolved},
        },
    )
    return ShareholdingWithPerson(shareholding=row, person=holder)


async def delete_shareholding(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    shareholding_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """Remove one shareholding; the audit entry carries the values it held."""
    row = await _get_shareholding(session, tenant_id=tenant_id, shareholding_id=shareholding_id)
    require_company_access(context, row.company_id)
    details = {
        "company_id": row.company_id,
        "person_id": row.person_id,
        "class_name": row.class_name,
        "before": _shareholding_values(row),
        "after": None,
    }
    await session.delete(row)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SHAREHOLDING_DELETED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="shareholding",
        entity_id=row.id,
        details=details,
    )


async def list_shareholdings(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, as_of: date | None = None
) -> list[ShareholdingWithPerson]:
    """The company's shareholdings, newest ``valid_from`` first, with persons.

    The same shape as ``list_appointments``: every row with its person
    snapshot, ``as_of`` narrowing to the holdings effective that day.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = list(
        (
            await session.scalars(
                select(Shareholding)
                .where(
                    Shareholding.tenant_id == tenant_id,
                    Shareholding.company_id == company.id,
                )
                .order_by(
                    Shareholding.valid_from.desc(),
                    Shareholding.created_at.desc(),
                    Shareholding.id,
                )
            )
        ).all()
    )
    if as_of is not None:
        rows = [row for row in rows if in_office(row.valid_from, row.valid_to, as_of)]
    people = await _people_by_id(
        session, tenant_id=tenant_id, person_ids={row.person_id for row in rows}
    )
    return [
        ShareholdingWithPerson(shareholding=row, person=people[row.person_id]) for row in rows
    ]


def person_name_forms(person: Person) -> list[str]:
    """Every script form of one person's name, for the typed lexicon
    (ruling R-T4b's one home — moved from the reply parser when the split
    became the second caller, workstream ③).

    The English name, the split ``surname_en``/``other_names_en`` pair joined
    by ``joined_english_name``, and the Chinese name as its own value — so a
    message naming either script form is tokenized even though only one form
    is the record's display name, and so the register R-D compares holds
    exactly the forms the lexicon replaces. Forms are de-duplicated, never
    empty.
    """
    names: list[str] = []
    candidates = [
        person.name_en,
        joined_english_name(
            surname_en=person.surname_en, other_names_en=person.other_names_en
        ),
        person.name_zh,
    ]
    for candidate in candidates:
        if candidate and candidate not in names:
            names.append(candidate)
    return names
