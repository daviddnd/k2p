"""DB-backed external-contact scenarios (opt-in; needs a migrated PostgreSQL).

These verify migration ``0005_external_contacts`` and the contact services /
endpoints against a real database: composite-FK tenant pinning (the F1
pattern, both mismatch directions), the per-tenant unique channel
identifier, cross-tenant invisibility, the idempotent association round
trip, PATCH explicit-null vs omitted, and the documented status/verified_at
semantics. Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database
migrated to head (see ``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.contacts import (
    CompanyExternalContact,
    ContactChannel,
    Person,
)
from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def make_admin(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


def admin_client(session: AsyncSession, tenant: Tenant, admin: User) -> TestClient:
    return make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))


COMPANY_PAYLOAD = {
    "br_number": "12345678",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}
PERSON_PAYLOAD = {"name_en": "CHAN TAI MAN", "name_zh": "陳大文", "email": "chantm@example.com"}
CHANNEL_PAYLOAD = {"channel_type": "email", "identifier": "chantm@example.com"}


def create_company(client: TestClient, **overrides: object) -> dict:
    response = client.post("/api/v1/companies", json={**COMPANY_PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def create_person(client: TestClient, **overrides: object) -> dict:
    response = client.post("/api/v1/persons", json={**PERSON_PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def add_channel(client: TestClient, person_id: str, **overrides: object) -> dict:
    response = client.post(
        f"/api/v1/persons/{person_id}/contact-channels", json={**CHANNEL_PAYLOAD, **overrides}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Person CRUD through the real stack ---


async def test_person_round_trip_with_channels(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)
    channel = add_channel(client, created["id"])
    add_channel(client, created["id"], channel_type="whatsapp", identifier="+852 2345 6789")

    detail = client.get(f"/api/v1/persons/{created['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["name_en"] == "CHAN TAI MAN"
    assert body["name_zh"] == "陳大文"
    assert body["email"] == "chantm@example.com"
    assert body["is_external_contact"] is True
    assert len(body["channels"]) == 2

    # A freshly-added channel defaults to active with verified_at NULL.
    assert channel["status"] == "active"
    assert channel["verified_at"] is None

    listing = client.get("/api/v1/persons")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()] == [created["id"]]


async def test_patch_person_explicit_null_vs_omitted(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)

    # Omitted fields are unchanged.
    response = client.patch(f"/api/v1/persons/{created['id']}", json={"name_en": "CHAN TAI-MAN"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name_en"] == "CHAN TAI-MAN"
    assert body["email"] == "chantm@example.com"  # untouched

    # Explicit null clears the nullable email.
    response = client.patch(f"/api/v1/persons/{created['id']}", json={"email": None})
    assert response.status_code == 200, response.text
    assert response.json()["email"] is None


async def test_patch_person_cannot_clear_the_last_name(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client, name_zh=None)  # name_en is the only name

    response = client.patch(f"/api/v1/persons/{created['id']}", json={"name_en": None})
    assert response.status_code == 422


async def test_create_person_with_a_blank_name_is_refused_by_the_domain(
    session: AsyncSession,
) -> None:
    """A whitespace-only name passes the body's length check and the relaxed
    database CHECK, so the domain's name rule is what refuses it — answered as
    the route's 422 with the reason, and nothing is written."""
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.post("/api/v1/persons", json={"name_en": "   ", "name_zh": "  "})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "person_name_required"
    assert client.get("/api/v1/persons").json() == []


# --- Cross-tenant invisibility ---


async def test_cross_tenant_person_is_indistinguishable_from_unknown(
    session: AsyncSession,
) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    created = create_person(admin_client(session, tenant_a, admin_a))
    client_b = admin_client(session, tenant_b, admin_b)

    assert client_b.get(f"/api/v1/persons/{created['id']}").status_code == 404
    assert client_b.get(f"/api/v1/persons/{uuid4()}").status_code == 404
    assert (
        client_b.patch(f"/api/v1/persons/{created['id']}", json={"name_en": "X"}).status_code == 404
    )
    assert (
        client_b.post(
            f"/api/v1/persons/{created['id']}/contact-channels", json=CHANNEL_PAYLOAD
        ).status_code
        == 404
    )
    # Tenant B's listing stays empty.
    assert client_b.get("/api/v1/persons").json() == []


async def test_cross_tenant_channel_update_is_404(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    client_a = admin_client(session, tenant_a, admin_a)
    created = create_person(client_a)
    channel = add_channel(client_a, created["id"])
    # Tenant B's admin creates a person of their own, then aims tenant A's
    # channel id at it: the channel lookup pins person + tenant, so 404.
    own = create_person(admin_client(session, tenant_b, admin_b), email=None)

    response = admin_client(session, tenant_b, admin_b).patch(
        f"/api/v1/persons/{own['id']}/contact-channels/{channel['id']}", json={"status": "bounced"}
    )
    assert response.status_code == 404


# --- Channel uniqueness ---


async def test_duplicate_channel_identifier_in_tenant_is_409(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    first = create_person(client)
    second = create_person(client, email="other@example.com")
    add_channel(client, first["id"])

    # Same (channel_type, identifier) on another person in the tenant → 409.
    response = client.post(f"/api/v1/persons/{second['id']}/contact-channels", json=CHANNEL_PAYLOAD)
    assert response.status_code == 409
    # The same identifier under a different channel type is fine.
    response = client.post(
        f"/api/v1/persons/{second['id']}/contact-channels",
        json={"channel_type": "wechat", "identifier": "chantm@example.com"},
    )
    assert response.status_code == 201, response.text


async def test_second_tenant_may_reuse_the_same_email(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    person_a = create_person(admin_client(session, tenant_a, admin_a))
    person_b = create_person(admin_client(session, tenant_b, admin_b))
    add_channel(admin_client(session, tenant_a, admin_a), person_a["id"])

    channel = add_channel(admin_client(session, tenant_b, admin_b), person_b["id"])
    assert channel["identifier"] == "chantm@example.com"


async def test_channel_identifier_is_stored_trimmed(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)

    channel = add_channel(client, created["id"], identifier="  chantm@example.com  ")
    assert channel["identifier"] == "chantm@example.com"


async def test_invalid_email_identifier_is_422(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)

    response = client.post(
        f"/api/v1/persons/{created['id']}/contact-channels",
        json={"channel_type": "email", "identifier": "not-an-email"},
    )
    assert response.status_code == 422


async def test_update_channel_identifier_conflict_is_409(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)
    add_channel(client, created["id"])  # email chantm@example.com
    other = add_channel(client, created["id"], channel_type="email", identifier="other@example.com")

    response = client.patch(
        f"/api/v1/persons/{created['id']}/contact-channels/{other['id']}",
        json={"identifier": "chantm@example.com"},
    )
    assert response.status_code == 409


async def test_update_channel_to_its_own_identifier_is_not_a_conflict(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)
    channel = add_channel(client, created["id"])

    response = client.patch(
        f"/api/v1/persons/{created['id']}/contact-channels/{channel['id']}",
        json={"identifier": "  chantm@example.com "},  # trims to its own value
    )
    assert response.status_code == 200, response.text


# --- Status transitions and verified_at ---


async def test_status_transitions_drive_verified_at(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_person(client)
    channel = add_channel(client, created["id"])
    url = f"/api/v1/persons/{created['id']}/contact-channels/{channel['id']}"

    # active → bounced: verified_at stays NULL (was never set).
    bounced = client.patch(url, json={"status": "bounced"})
    assert bounced.status_code == 200, bounced.text
    assert bounced.json()["status"] == "bounced"
    assert bounced.json()["verified_at"] is None

    # bounced → active: verified_at is set to now.
    reactivated = client.patch(url, json={"status": "active"})
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["verified_at"] is not None

    # active → unverified: verified_at clears again.
    unverified = client.patch(url, json={"status": "unverified"})
    assert unverified.status_code == 200, unverified.text
    assert unverified.json()["verified_at"] is None

    # Setting the same status is a no-op on verified_at.
    still_unverified = client.patch(url, json={"status": "unverified"})
    assert still_unverified.status_code == 200
    assert still_unverified.json()["verified_at"] is None


# --- Associations ---


async def test_association_round_trip(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = create_company(client)
    person = create_person(client)
    add_channel(client, person["id"])

    first = client.post(
        f"/api/v1/companies/{company['id']}/contacts", json={"person_id": person["id"]}
    )
    assert first.status_code == 200, first.text
    assert first.json()["company_id"] == company["id"]

    # The company's contact list carries the person with their channels.
    listing = client.get(f"/api/v1/companies/{company['id']}/contacts")
    assert listing.status_code == 200
    (contact,) = listing.json()
    assert contact["id"] == person["id"]
    assert contact["channels"][0]["identifier"] == "chantm@example.com"

    # Re-adding is idempotent: 200 with the same association (same created_at).
    again = client.post(
        f"/api/v1/companies/{company['id']}/contacts", json={"person_id": person["id"]}
    )
    assert again.status_code == 200
    assert again.json() == first.json()

    # One person may serve many companies.
    other_company = create_company(client, br_number="87654321")
    second = client.post(
        f"/api/v1/companies/{other_company['id']}/contacts", json={"person_id": person["id"]}
    )
    assert second.status_code == 200, second.text

    # Remove → absent from the list; a second delete is 404.
    assert (
        client.delete(f"/api/v1/companies/{company['id']}/contacts/{person['id']}").status_code
        == 204
    )
    assert client.get(f"/api/v1/companies/{company['id']}/contacts").json() == []
    assert (
        client.delete(f"/api/v1/companies/{company['id']}/contacts/{person['id']}").status_code
        == 404
    )
    # The other company's association is untouched.
    assert len(client.get(f"/api/v1/companies/{other_company['id']}/contacts").json()) == 1


async def test_associate_cross_tenant_person_or_company_is_404(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    client_a = admin_client(session, tenant_a, admin_a)
    client_b = admin_client(session, tenant_b, admin_b)
    company_a = create_company(client_a)
    person_a = create_person(client_a)
    company_b = create_company(client_b, br_number="87654321")

    # Tenant B cannot attach tenant A's person to their own company.
    assert (
        client_b.post(
            f"/api/v1/companies/{company_b['id']}/contacts", json={"person_id": person_a["id"]}
        ).status_code
        == 404
    )
    # Tenant B cannot attach anyone to tenant A's company.
    assert (
        client_b.post(
            f"/api/v1/companies/{company_a['id']}/contacts", json={"person_id": str(uuid4())}
        ).status_code
        == 404
    )
    # Tenant B cannot list tenant A's company contacts.
    assert client_b.get(f"/api/v1/companies/{company_a['id']}/contacts").status_code == 404


# --- General role through the real RBAC stack ---


async def test_general_with_company_grant_is_forbidden_through_real_stack(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    company = create_company(client)
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role="general",
        review_events=True,
        approve_events=True,
        review_drafts=True,
        approve_drafts=True,
    )
    session.add(general)
    await session.flush()
    session.add(
        CompanyAccessGrant(user_id=general.id, company_id=UUID(company["id"]), tenant_id=tenant.id)
    )
    await session.flush()
    staff = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    # Contact-channel configuration is Administrator-only, even for granted staff.
    assert staff.get(f"/api/v1/companies/{company['id']}/contacts").status_code == 403
    assert staff.get("/api/v1/persons").status_code == 403
    assert staff.post("/api/v1/persons", json=PERSON_PAYLOAD).status_code == 403


# --- Schema backstops (direct ORM writes) ---


async def make_person_row(session: AsyncSession, tenant: Tenant, **overrides: object) -> Person:
    values: dict[str, object] = {
        "tenant_id": tenant.id,
        "name_en": "WONG SIU MING",
        "is_external_contact": True,
    }
    values.update(overrides)
    person = Person(**values)  # type: ignore[arg-type]
    session.add(person)
    await session.flush()
    return person


async def test_person_requires_at_least_one_name_at_db_level(session: AsyncSession) -> None:
    tenant = (await make_admin(session))[0]
    session.add(Person(tenant_id=tenant.id, name_en=None, name_zh=None, is_external_contact=True))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_channel_checks_and_status_default_at_db_level(session: AsyncSession) -> None:
    tenant = (await make_admin(session))[0]
    person = await make_person_row(session, tenant)

    session.add(
        ContactChannel(
            person_id=person.id,
            tenant_id=tenant.id,
            channel_type="fax",
            identifier="12345678",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_channel_duplicate_identifier_rejected_by_db_backstop(
    session: AsyncSession,
) -> None:
    tenant = (await make_admin(session))[0]
    person_a = await make_person_row(session, tenant)
    person_b = await make_person_row(session, tenant, name_en="LEE KA YAN")

    session.add(
        ContactChannel(
            person_id=person_a.id,
            tenant_id=tenant.id,
            channel_type="email",
            identifier="shared@example.com",
        )
    )
    await session.flush()
    session.add(
        ContactChannel(
            person_id=person_b.id,
            tenant_id=tenant.id,
            channel_type="email",
            identifier="shared@example.com",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_channel_with_cross_tenant_person_is_uninsertable(session: AsyncSession) -> None:
    tenant_a = (await make_admin(session))[0]
    tenant_b = (await make_admin(session))[0]
    person_a = await make_person_row(session, tenant_a)

    # Row claims tenant B but points at tenant A's person: the composite FK
    # (person_id, tenant_id) -> persons(id, tenant_id) must reject it.
    session.add(
        ContactChannel(
            person_id=person_a.id,
            tenant_id=tenant_b.id,
            channel_type="email",
            identifier="x@example.com",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_association_with_mismatched_tenant_is_uninsertable(
    session: AsyncSession,
) -> None:
    tenant_a = (await make_admin(session))[0]
    tenant_b = (await make_admin(session))[0]
    person_a = await make_person_row(session, tenant_a)
    person_b = await make_person_row(session, tenant_b, name_en="LEE KA YAN")
    company_a = Company(
        tenant_id=tenant_a.id,
        br_number="12345678",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=date(2026, 3, 1),
        type="private",
    )
    session.add(company_a)
    await session.flush()

    # Direction 1: tenant A's company + tenant A's person, but the row claims
    # tenant B — both composite FKs reject it.
    session.add(
        CompanyExternalContact(
            company_id=company_a.id, person_id=person_a.id, tenant_id=tenant_b.id
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    # Direction 2: the row claims tenant A but points at tenant B's person —
    # the person composite FK rejects it.
    session.add(
        CompanyExternalContact(
            company_id=company_a.id, person_id=person_b.id, tenant_id=tenant_a.id
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
