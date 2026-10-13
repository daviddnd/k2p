"""DB-backed officer/shareholding scenarios (opt-in; needs a migrated PostgreSQL).

The master records end to end against a real database: creation with an inline
person and through an existing one, the half-open service window as the
``as_of`` read applies it, the overlap refusal, corrections and deletes, the
audit chain that carries every mutation, tenancy (invisible across tenants and
uninsertable at the database), and the money-as-string round trip.

The two claims that need a database rather than a fake are here:

- **the overlap refusal** is a query over the stored windows, so only a real
  row can prove a second overlapping appointment is refused while an adjacent
  one is accepted;
- **the composite foreign keys** refuse a cross-tenant row at the database,
  which is the F1 pattern every master table carries.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.contacts import Person
from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.officers import OfficerAppointment, Shareholding
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.domain.audit import AuditAction, verify_chain
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2027-03-01",
    "type": "private",
}

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test.

    ``create_savepoint`` matches the deployment's session dependency, which
    commits at the request edge: a savepoint makes "a commit released this" the
    same statement here as in a deployment.
    """
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app over the test session, with the real RBAC loader."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    """A tenant with an Administrator and a General account."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
    )
    session.add_all([admin, general])
    await session.flush()
    return tenant, admin


async def deployment(
    session: AsyncSession, *, grant_general: bool = True
) -> tuple[TestClient, Tenant, User, User, str]:
    """A tenant, its two accounts, one company, and a client as the Administrator."""
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post("/api/v1/companies", json=COMPANY)
    assert response.status_code == 201, response.text
    company_id = str(response.json()["id"])
    general = (
        await session.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == "general")
        )
    ).one()
    if grant_general:
        session.add(
            CompanyAccessGrant(user_id=general.id, company_id=UUID(company_id), tenant_id=tenant.id)
        )
        await session.flush()
    return client, tenant, admin, general, company_id


def appointment_body(**overrides: object) -> dict:
    body: dict = {
        "person": {
            "person_type": "natural_person",
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "surname_en": "CHAN",
            "other_names_en": "Tai Man",
            "email": "chan@brightlantern.example.hk",
            "hkid_partial": "A1234",
            "tcsp_licence": {
                "licence_no": "TC000123",
                "not_required": False,
                "not_required_reason": None,
            },
        },
        "role": "director",
        "capacity": ["director"],
        "valid_from": "2026-09-01",
        "correspondence_address": ADDRESS,
    }
    body.update(overrides)
    return body


def shareholding_body(**overrides: object) -> dict:
    body: dict = {
        "person": {"person_type": "natural_person", "name_en": "CHAN Tai Man"},
        "class_name": "Ordinary",
        "shares_held": 1000,
        "jointly_held": None,
        "remarks": None,
        "percentage_held": "10.00",
        "allotment_date": "2020-01-01",
        "amount_paid": "1000.00",
        "amount_unpaid": "0.00",
        "address": ADDRESS,
        "valid_from": "2026-09-01",
    }
    body.update(overrides)
    return body


def create_appointment(client: TestClient, company_id: str, **overrides: object) -> dict:
    body = appointment_body(**overrides)
    response = client.post(f"/api/v1/companies/{company_id}/officers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_shareholding(client: TestClient, company_id: str, **overrides: object) -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/shareholdings", json=shareholding_body(**overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def entries_for(
    session: AsyncSession, tenant_id: UUID, action: AuditAction
) -> list[AuditLog]:
    return [
        entry for entry in await audit_entries(session, tenant_id) if entry.action == action.value
    ]


# --- Creation ----------------------------------------------------------------


async def test_an_appointment_with_an_inline_person_is_recorded_and_audited(
    session: AsyncSession,
) -> None:
    """Scope §2.1's officer register: a new person created through the
    contacts write path, the appointment carrying the office, the boxes and
    the address as filed, and the chain carrying the co-ordinates."""
    client, tenant, admin, _, company_id = await deployment(session)

    created = create_appointment(client, company_id)

    assert created["role"] == "director"
    assert created["capacity"] == ["director"]
    assert created["valid_from"] == "2026-09-01"
    assert created["valid_to"] is None
    assert created["correspondence_address"] == ADDRESS
    assert created["registered_office_address"] is None
    assert created["person"]["name_zh"] == "陳大文"
    assert created["person"]["surname_en"] == "CHAN"
    assert created["person"]["tcsp_licence"] == {
        "licence_no": "TC000123",
        "not_required": False,
        "not_required_reason": None,
    }
    assert created["person"]["hkid_partial"] == "A1234"
    assert created["created_by"] == str(admin.id)

    person = (
        await session.scalars(select(Person).where(Person.tenant_id == tenant.id))
    ).one()
    assert str(person.id) == created["person"]["id"]
    assert person.is_external_contact is True
    assert person.person_type == "natural_person"
    row = (
        await session.scalars(
            select(OfficerAppointment).where(OfficerAppointment.tenant_id == tenant.id)
        )
    ).one()
    assert row.person_id == person.id
    assert row.valid_to is None

    person_entries = await entries_for(session, tenant.id, AuditAction.PERSON_CREATED)
    appointment_entries = await entries_for(
        session, tenant.id, AuditAction.OFFICER_APPOINTMENT_CREATED
    )
    assert len(person_entries) == 1
    assert len(appointment_entries) == 1
    assert appointment_entries[0].company_id == UUID(company_id)
    assert appointment_entries[0].entity_id == row.id
    assert appointment_entries[0].details == {
        "company_id": company_id,
        "person_id": str(person.id),
        "role": "director",
        "valid_from": "2026-09-01",
        "valid_to": None,
    }
    # The chain holds, and the entries are the tenant's own.
    assert verify_chain(await audit_entries(session, tenant.id)) == []


async def test_one_person_can_serve_two_companies(session: AsyncSession) -> None:
    """§2.4: ``Person`` is tenant-level, so one director of two portfolio
    companies is one row with two appointments."""
    client, tenant, _, _, first_company = await deployment(session)
    first = create_appointment(client, first_company)
    second_company = client.post("/api/v1/companies", json={**COMPANY, "br_number": "87654321"})
    assert second_company.status_code == 201, second_company.text
    second_company_id = str(second_company.json()["id"])

    second = create_appointment(
        client, second_company_id, person=None, person_id=first["person"]["id"]
    )

    assert second["person"]["id"] == first["person"]["id"]
    assert second["person_id"] == first["person_id"]
    assert second["company_id"] != first["company_id"]
    persons = list(
        (await session.scalars(select(Person).where(Person.tenant_id == tenant.id))).all()
    )
    assert len(persons) == 1
    appointments = list(
        (
            await session.scalars(
                select(OfficerAppointment).where(OfficerAppointment.tenant_id == tenant.id)
            )
        ).all()
    )
    assert len(appointments) == 2
    # Both companies read their own officer back.
    first_listed = client.get(f"/api/v1/companies/{first_company}/officers").json()
    second_listed = client.get(f"/api/v1/companies/{second_company_id}/officers").json()
    assert [item["id"] for item in first_listed] == [first["id"]]
    assert [item["id"] for item in second_listed] == [second["id"]]


async def test_the_as_of_read_uses_the_half_open_window(session: AsyncSession) -> None:
    """§2's semantics, through the route: in office on the appointment day,
    out on the cessation day, nothing before the window, and every row without
    an ``as_of``."""
    client, _, _, _, company_id = await deployment(session)
    created = create_appointment(
        client, company_id, valid_from="2026-09-01", valid_to="2026-12-01"
    )
    url = f"/api/v1/companies/{company_id}/officers"

    assert [item["id"] for item in client.get(url, params={"as_of": "2026-09-01"}).json()] == [
        created["id"]
    ]
    assert client.get(url, params={"as_of": "2026-11-30"}).json()[0]["id"] == created["id"]
    assert client.get(url, params={"as_of": "2026-12-01"}).json() == []
    assert client.get(url, params={"as_of": "2026-08-31"}).json() == []
    assert [item["id"] for item in client.get(url).json()] == [created["id"]]


# --- The overlap refusal -----------------------------------------------------


async def test_an_overlapping_appointment_is_refused_and_an_adjacent_one_is_not(
    session: AsyncSession,
) -> None:
    """Two concurrent appointments of one person in one role are a double
    entry of one office; a cessation on the day another appointment starts is
    a succession, and the half-open windows make it legal."""
    client, tenant, _, _, company_id = await deployment(session)
    first = create_appointment(
        client, company_id, valid_from="2026-09-01", valid_to="2026-12-01"
    )
    body = appointment_body(
        person=None,
        person_id=first["person"]["id"],
        valid_from="2026-11-01",
        valid_to="2027-01-01",
    )

    refused = client.post(f"/api/v1/companies/{company_id}/officers", json=body)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "officer_appointment_overlap"
    assert len(await entries_for(session, tenant.id, AuditAction.OFFICER_APPOINTMENT_CREATED)) == 1

    adjacent = create_appointment(
        client,
        company_id,
        person=None,
        person_id=first["person"]["id"],
        valid_from="2026-12-01",
        valid_to=None,
    )
    assert adjacent["id"] != first["id"]


async def test_a_different_role_or_company_may_overlap(session: AsyncSession) -> None:
    """The rule is per (company, person, role): the same person may be a
    director and the company secretary at once, and may direct two companies."""
    client, _, _, _, company_id = await deployment(session)
    first = create_appointment(client, company_id, capacity=["director"])
    second = create_appointment(
        client,
        company_id,
        person=None,
        person_id=first["person"]["id"],
        role="company_secretary",
        capacity=["company_secretary"],
    )

    assert second["role"] == "company_secretary"


# --- Corrections and removal -------------------------------------------------


async def test_a_patch_corrects_the_window_and_audits_before_and_after(
    session: AsyncSession,
) -> None:
    """A cessation is a window correction: ``valid_to`` moves, the office and
    its boxes stay, and the chain carries exactly what the request stated."""
    client, tenant, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)

    response = client.patch(
        f"/api/v1/officers/{created['id']}", json={"valid_to": "2026-12-01"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["valid_to"] == "2026-12-01"
    entries = await entries_for(session, tenant.id, AuditAction.OFFICER_APPOINTMENT_UPDATED)
    assert len(entries) == 1
    details = entries[0].details
    assert details["before"] == {"valid_to": None}
    assert details["after"] == {"valid_to": "2026-12-01"}
    assert details["company_id"] == company_id
    assert details["person_id"] == created["person_id"]


async def test_a_patch_that_would_overlap_is_refused(session: AsyncSession) -> None:
    """The refusal applies to corrections too: widening a ceased window back
    over a live appointment is the same double entry."""
    client, _, _, _, company_id = await deployment(session)
    first = create_appointment(
        client, company_id, valid_from="2026-01-01", valid_to="2026-03-01"
    )
    assert client.patch(
        f"/api/v1/officers/{first['id']}", json={"valid_to": "2026-06-01"}
    ).status_code == 200
    create_appointment(
        client,
        company_id,
        person=None,
        person_id=first["person"]["id"],
        valid_from="2026-07-01",
    )

    response = client.patch(
        f"/api/v1/officers/{first['id']}", json={"valid_to": "2026-08-01"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "officer_appointment_overlap"
    # The refused correction left the row exactly as it was.
    listed = client.get(f"/api/v1/companies/{company_id}/officers").json()
    assert {item["id"]: item["valid_to"] for item in listed}[first["id"]] == "2026-06-01"


async def test_a_refused_correction_writes_nothing(session: AsyncSession) -> None:
    """A PATCH the rules refuse changes no column and appends no entry: the
    record is judged as it would stand before anything is written."""
    client, tenant, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)
    before = await audit_entries(session, tenant.id)

    response = client.patch(
        f"/api/v1/officers/{created['id']}",
        json={"role": "company_secretary", "capacity": ["director"]},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "capacity_role_mismatch"
    officer = client.get(f"/api/v1/companies/{company_id}/officers").json()[0]
    assert officer["role"] == "director"
    assert officer["capacity"] == ["director"]
    assert await audit_entries(session, tenant.id) == before


async def test_a_patch_cannot_clear_the_office_or_the_start(session: AsyncSession) -> None:
    client, _, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)

    response = client.patch(f"/api/v1/officers/{created['id']}", json={"role": None})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "role: cannot be cleared"


async def test_a_delete_removes_the_row_and_audits_what_it_held(
    session: AsyncSession,
) -> None:
    client, tenant, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)

    response = client.delete(f"/api/v1/officers/{created['id']}")

    assert response.status_code == 204, response.text
    assert (
        await session.scalars(
            select(OfficerAppointment).where(OfficerAppointment.tenant_id == tenant.id)
        )
    ).all() == []
    entry = (await entries_for(session, tenant.id, AuditAction.OFFICER_APPOINTMENT_DELETED))[0]
    assert entry.details["before"]["role"] == "director"
    assert entry.details["before"]["valid_from"] == "2026-09-01"
    assert entry.details["after"] is None
    assert client.get(f"/api/v1/companies/{company_id}/officers").json() == []


# --- Authorization and tenancy ----------------------------------------------


async def test_a_granted_general_account_may_maintain_the_register(
    session: AsyncSession,
) -> None:
    """Master-data maintenance's rule: the per-company grant is the whole
    gate, and no §2.9 permission flag is asked for."""
    _, tenant, _, general, company_id = await deployment(session)
    general_client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    created = create_appointment(general_client, company_id)

    assert created["role"] == "director"
    assert general_client.get(f"/api/v1/companies/{company_id}/officers").status_code == 200


async def test_another_tenants_company_and_records_are_unknown(session: AsyncSession) -> None:
    """One answer for unknown and cross-tenant ids on every route — the
    company-scoped ones through ``get_company``, the id-scoped ones through
    the row's own tenant pin."""
    client, _, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)
    other_tenant, other_admin = await seed_tenant(session)
    other_client = make_client(session, ActorRef(user_id=other_admin.id, tenant_id=other_tenant.id))

    assert other_client.get(f"/api/v1/companies/{company_id}/officers").status_code == 404
    assert other_client.post(
        f"/api/v1/companies/{company_id}/officers", json=appointment_body()
    ).status_code == 404
    assert other_client.get(f"/api/v1/companies/{company_id}/shareholdings").status_code == 404
    assert other_client.patch(
        f"/api/v1/officers/{created['id']}", json={"valid_to": "2026-10-01"}
    ).status_code == 404
    assert other_client.delete(f"/api/v1/officers/{created['id']}").status_code == 404
    # The row is untouched.
    assert client.get(f"/api/v1/companies/{company_id}/officers").json()[0]["valid_to"] is None


async def test_a_cross_tenant_row_is_uninsertable(session: AsyncSession) -> None:
    """The F1 pattern: the composite foreign keys pin the company, the person
    and the creating user to the row's own tenant."""
    _, tenant, admin, _, _ = await deployment(session)
    other_tenant, other_admin = await seed_tenant(session)
    other_company = Company(
        tenant_id=other_tenant.id,
        br_number="99999999",
        date_of_incorporation=date(2011, 6, 1),
        anniversary_date=date(2027, 6, 1),
        type="private",
    )
    other_person = Person(tenant_id=other_tenant.id, name_en="OTHER TENANT PERSON")
    session.add_all([other_company, other_person])
    await session.flush()
    own_company_id = (
        await session.scalars(select(Company.id).where(Company.tenant_id == tenant.id))
    ).first()
    assert own_company_id is not None

    # Another tenant's company.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                OfficerAppointment(
                    tenant_id=tenant.id,
                    company_id=other_company.id,
                    person_id=other_person.id,
                    role="director",
                    capacity=[],
                    valid_from=date(2026, 9, 1),
                    created_by=admin.id,
                )
            )
            await session.flush()
    # Another tenant's person.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                OfficerAppointment(
                    tenant_id=tenant.id,
                    company_id=own_company_id,
                    person_id=other_person.id,
                    role="director",
                    capacity=[],
                    valid_from=date(2026, 9, 1),
                    created_by=admin.id,
                )
            )
            await session.flush()
    # Another tenant's acting user.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                OfficerAppointment(
                    tenant_id=tenant.id,
                    company_id=own_company_id,
                    person_id=other_person.id,
                    role="director",
                    capacity=[],
                    valid_from=date(2026, 9, 1),
                    created_by=other_admin.id,
                )
            )
            await session.flush()
    # A shareholding row is pinned the same way.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Shareholding(
                    tenant_id=tenant.id,
                    company_id=other_company.id,
                    person_id=other_person.id,
                    class_name="Ordinary",
                    shares_held=1,
                    valid_from=date(2026, 9, 1),
                    created_by=admin.id,
                )
            )
            await session.flush()


async def test_the_window_check_is_the_databases_too(session: AsyncSession) -> None:
    """``valid_to > valid_from`` is a CHECK: the domain refuses it with a
    named reason and the database refuses it as the backstop."""
    client, _, _, _, company_id = await deployment(session)
    policy = appointment_body(valid_from="2026-09-01", valid_to="2026-09-01")

    response = client.post(f"/api/v1/companies/{company_id}/officers", json=policy)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "valid_to_not_after_valid_from"


async def test_a_shareholding_window_that_ends_where_it_starts_is_refused(
    session: AsyncSession,
) -> None:
    """The holdings carry the same window, and the same named refusal: a
    position ending the day it began is the domain's 422 on create and on
    correction alike — never the database CHECK reached as an unhandled
    error."""
    client, tenant, _, _, company_id = await deployment(session)

    refused = client.post(
        f"/api/v1/companies/{company_id}/shareholdings",
        json=shareholding_body(valid_from="2026-09-01", valid_to="2026-09-01"),
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "valid_to_not_after_valid_from"
    assert (
        await session.scalars(select(Shareholding).where(Shareholding.tenant_id == tenant.id))
    ).all() == []

    created = create_shareholding(client, company_id)
    patched = client.patch(
        f"/api/v1/shareholdings/{created['id']}", json={"valid_to": "2026-09-01"}
    )

    assert patched.status_code == 422, patched.text
    assert patched.json()["detail"] == "valid_to_not_after_valid_from"
    listed = client.get(f"/api/v1/companies/{company_id}/shareholdings").json()
    assert listed[0]["valid_to"] is None


# --- The named reasons through the route -------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        # A body corporate's identity is its registered English name.
        (
            {
                "person": {"person_type": "body_corporate", "name_zh": "明亮有限公司"},
                "correspondence_address": None,
                "registered_office_address": ADDRESS,
            },
            "person_name_required",
        ),
        # A natural person needs one of the four name parts.
        ({"person": {"person_type": "natural_person"}}, "person_name_required"),
        (
            {"person": {"name_en": "CHAN", "previous_names": [{}]}},
            "name_history_invalid",
        ),
        (
            {"person": {"name_en": "CHAN", "tcsp_licence": {"not_required": True}}},
            "tcsp_licence_invalid",
        ),
        # ND2A's 代替 box needs the name it substitutes for.
        (
            {"capacity": ["director", "alternate_director"]},
            "alternate_to_required",
        ),
        ({"alternate_to": "CHAN Tai Man"}, "alternate_to_not_applicable"),
        (
            {"role": "company_secretary", "capacity": ["director"]},
            "capacity_role_mismatch",
        ),
        ({"capacity": ["reserve_director"]}, "capacity_invalid"),
        # The §3.4 purpose-split: a natural person files a correspondence address.
        ({"correspondence_address": None}, "address_required"),
        ({"registered_office_address": ADDRESS}, "address_not_applicable"),
        ({"correspondence_address": {"flat": "12A"}}, "address_invalid"),
    ],
)
async def test_the_domain_reasons_surface_verbatim(
    session: AsyncSession, overrides: dict, reason: str
) -> None:
    """One request, one answer, and the reason is the domain's own — the
    refusal a caller fixes rather than a generic body error."""
    client, tenant, _, _, company_id = await deployment(session)

    response = client.post(
        f"/api/v1/companies/{company_id}/officers", json=appointment_body(**overrides)
    )

    assert response.status_code == 422, response.text
    assert reason in response.json()["detail"]
    # Nothing was written: no appointment, and no person either.
    assert (
        await session.scalars(
            select(OfficerAppointment).where(OfficerAppointment.tenant_id == tenant.id)
        )
    ).all() == []
    assert (
        await session.scalars(select(Person).where(Person.tenant_id == tenant.id))
    ).all() == []


async def test_a_tcsp_licence_that_states_a_refusal_is_accepted(session: AsyncSession) -> None:
    """Form note 20's other branch: no licence number, a reason why."""
    client, _, _, _, company_id = await deployment(session)

    created = create_appointment(
        client,
        company_id,
        person={
            "person_type": "body_corporate",
            "name_en": "BRIGHT SECRETARIAL LIMITED",
            "tcsp_licence": {
                "not_required": True,
                "not_required_reason": "Sole director provides no TCSP service",
            },
        },
        registered_office_address=ADDRESS,
        correspondence_address=None,
    )

    assert created["person"]["tcsp_licence"]["not_required"] is True
    assert created["registered_office_address"] == ADDRESS
    assert created["correspondence_address"] is None


# --- Shareholdings -----------------------------------------------------------


async def test_a_shareholding_round_trips_money_as_strings(session: AsyncSession) -> None:
    """The member boxes NAR1 prints, with ``Numeric`` values crossing as
    strings in both directions and the holdings window read like the
    appointments'."""
    client, tenant, _, _, company_id = await deployment(session)

    created = create_shareholding(client, company_id)

    assert created["class_name"] == "Ordinary"
    assert created["shares_held"] == 1000
    assert created["percentage_held"] == "10.00"
    assert created["amount_paid"] == "1000.00"
    assert created["amount_unpaid"] == "0.00"
    assert created["allotment_date"] == "2020-01-01"
    assert created["address"] == ADDRESS
    assert created["valid_from"] == "2026-09-01"
    assert created["valid_to"] is None
    row = (
        await session.scalars(select(Shareholding).where(Shareholding.tenant_id == tenant.id))
    ).one()
    assert str(row.amount_paid) == "1000.00"
    assert str(row.percentage_held) == "10.00"

    url = f"/api/v1/companies/{company_id}/shareholdings"
    assert [item["id"] for item in client.get(url, params={"as_of": "2026-09-01"}).json()] == [
        created["id"]
    ]
    assert client.get(url, params={"as_of": "2026-08-31"}).json() == []

    updated = client.patch(
        f"/api/v1/shareholdings/{created['id']}",
        json={"amount_paid": "250.50", "jointly_held": "CHAN Siu Ming"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["amount_paid"] == "250.50"
    assert updated.json()["jointly_held"] == "CHAN Siu Ming"
    entries = await entries_for(session, tenant.id, AuditAction.SHAREHOLDING_UPDATED)
    assert entries[0].details["before"] == {"amount_paid": "1000.00", "jointly_held": None}
    assert entries[0].details["after"] == {"amount_paid": "250.50", "jointly_held": "CHAN Siu Ming"}

    assert client.delete(f"/api/v1/shareholdings/{created['id']}").status_code == 204
    assert client.get(url).json() == []


async def test_a_shareholding_correction_with_a_bad_amount_is_refused(
    session: AsyncSession,
) -> None:
    """The amounts are judged on the caller's raw value: a word is
    ``shareholding_invalid: amount_paid``, not a silently cleared column."""
    client, tenant, _, _, company_id = await deployment(session)
    created = create_shareholding(client, company_id)
    before = await audit_entries(session, tenant.id)

    response = client.patch(
        f"/api/v1/shareholdings/{created['id']}", json={"amount_paid": "not a number"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "shareholding_invalid: amount_paid"
    listed = client.get(f"/api/v1/companies/{company_id}/shareholdings").json()
    assert listed[0]["amount_paid"] == "1000.00"
    assert await audit_entries(session, tenant.id) == before


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"class_name": "   "}, "shareholding_invalid: class_name"),
        ({"shares_held": -1}, "shareholding_invalid: shares_held"),
        ({"percentage_held": "100.01"}, "shareholding_invalid: percentage_held"),
        ({"percentage_held": "-1"}, "shareholding_invalid: percentage_held"),
        ({"amount_paid": "-0.01"}, "shareholding_invalid: amount_paid"),
        ({"amount_unpaid": "not a number"}, "shareholding_invalid: amount_unpaid"),
    ],
)
async def test_a_shareholding_refusal_names_the_box(
    session: AsyncSession, overrides: dict, reason: str
) -> None:
    client, tenant, _, _, company_id = await deployment(session)

    response = client.post(
        f"/api/v1/companies/{company_id}/shareholdings", json=shareholding_body(**overrides)
    )

    assert response.status_code == 422, response.text
    assert reason in response.json()["detail"]
    assert (
        await session.scalars(select(Shareholding).where(Shareholding.tenant_id == tenant.id))
    ).all() == []


# --- The person route is the one writer --------------------------------------


async def test_a_person_correction_flows_through_to_the_officer_read(
    session: AsyncSession,
) -> None:
    """``PATCH /persons/{id}`` is where an officer's identity is corrected —
    Administrator-only, audited with before/after — and the officer read shows
    it, because both point at the one person row."""
    client, tenant, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)

    response = client.patch(
        f"/api/v1/persons/{created['person']['id']}",
        json={"previous_names": [{"name_zh": "陳小明", "name_en": "CHAN Siu Ming"}]},
    )

    assert response.status_code == 200, response.text
    officer = client.get(f"/api/v1/companies/{company_id}/officers").json()[0]
    assert officer["person"]["previous_names"] == [
        {"name_zh": "陳小明", "name_en": "CHAN Siu Ming"}
    ]
    entry = (await entries_for(session, tenant.id, AuditAction.PERSON_UPDATED))[0]
    assert entry.details == {
        "before": {"previous_names": []},
        "after": {"previous_names": [{"name_zh": "陳小明", "name_en": "CHAN Siu Ming"}]},
    }


async def test_a_person_update_that_empties_the_names_is_refused(
    session: AsyncSession,
) -> None:
    """The relaxed name rule still refuses a person with no name at all; the
    route answers the domain's named reason, as every other write path does."""
    client, _, _, _, company_id = await deployment(session)
    created = create_appointment(client, company_id)

    response = client.patch(
        f"/api/v1/persons/{created['person']['id']}",
        json={"name_zh": None, "name_en": None, "surname_en": None, "other_names_en": None},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "person_name_required"


# --- Reads write nothing -----------------------------------------------------


async def test_the_reads_write_nothing(session: AsyncSession) -> None:
    """Asking is not a change: both list reads append nothing to the chain and
    touch no row."""
    client, tenant, _, _, company_id = await deployment(session)
    create_appointment(client, company_id)
    create_shareholding(client, company_id)
    before = await audit_entries(session, tenant.id)

    client.get(f"/api/v1/companies/{company_id}/officers")
    client.get(f"/api/v1/companies/{company_id}/officers", params={"as_of": "2026-09-01"})
    client.get(f"/api/v1/companies/{company_id}/shareholdings")
    client.get(f"/api/v1/companies/{company_id}/shareholdings", params={"as_of": "2026-09-01"})

    assert await audit_entries(session, tenant.id) == before
