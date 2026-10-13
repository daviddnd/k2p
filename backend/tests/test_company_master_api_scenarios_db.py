"""DB-backed scenarios for the company-master endpoints (opt-in; needs a
migrated PostgreSQL — same setup as ``test_identity_scenarios_db.py``).

These exercise the real router stack (``create_app``) with only the auth
seam and session overridden, so every request flows through the real
``get_actor_context`` → ``load_actor_context`` → ``is_allowed`` path against
the database: tenant isolation, duplicate-BRN 409, effective-dated PATCH
appends, as-of reads, and the grant-gated rule (create/list are
Administrator-only; General staff can read and append facts only for
companies they hold an access grant for).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.models import CompanyAccessGrant, Tenant, User
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


CREATE_PAYLOAD = {
    "br_number": "1234 5678",  # normalized to 12345678 on write
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}

FULL_PATCH_PAYLOAD = {
    "effective_date": "2026-01-01",
    "name": {"name_en": "EXAMPLE LIMITED", "name_zh": "示例有限公司", "business_name": "EXAMPLE"},
    "registered_office": {
        "flat": "12/F",
        "building": "EXAMPLE TOWER",
        "street": "1 EXAMPLE ROAD",
        "district": "Central",
    },
    "status": {"status": "active"},
    "business_nature": {
        "business_nature_code": "620",
        "business_nature_description": "Computer programming",
    },
    "contact": {"email": "co@example.com", "contact_phone_hk": "23456789"},
    "filing": {"mortgages_and_charges_total": "1500.25", "member_count": None},
    "records_kept": {
        "records_kept": [
            {
                "description": "Registers",
                "address": {
                    "flat": "12/F",
                    "building": "EXAMPLE TOWER",
                    "street": "1 EXAMPLE ROAD",
                    "district": "Central",
                    "country": None,
                },
            }
        ]
    },
    "share_capital_classes": {
        "classes": [
            {
                "class_name": "Ordinary",
                "currency": "HKD",
                "total_shares": 10000,
                "total_amount": "10000.00",
                "total_paid_up": "10000.00",
            }
        ]
    },
}


def create_company(client: TestClient, **overrides: object) -> dict:
    payload = {**CREATE_PAYLOAD, **overrides}
    response = client.post("/api/v1/companies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- POST /api/v1/companies ---


async def test_create_company_persists_statics_with_normalized_brn(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    body = create_company(admin_client(session, tenant, admin))

    assert UUID(body["tenant_id"]) == tenant.id
    assert body["br_number"] == "12345678"
    assert body["cr_number"] == "1234567"
    assert body["date_of_incorporation"] == "2010-03-01"
    assert body["anniversary_date"] == "2026-03-01"
    assert body["type"] == "private"
    assert body["ai_disabled"] is False


async def test_create_company_rejects_duplicate_brn_in_tenant(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    create_company(client)

    response = client.post("/api/v1/companies", json={**CREATE_PAYLOAD, "br_number": "12345678"})
    assert response.status_code == 409


async def test_create_company_allows_same_brn_in_another_tenant(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    create_company(admin_client(session, tenant_a, admin_a))

    body = create_company(admin_client(session, tenant_b, admin_b))
    assert UUID(body["tenant_id"]) == tenant_b.id


# --- GET /api/v1/companies ---


async def test_list_companies_is_scoped_to_the_actors_tenant(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    create_company(admin_client(session, tenant_a, admin_a))

    listing_a = admin_client(session, tenant_a, admin_a).get("/api/v1/companies")
    listing_b = admin_client(session, tenant_b, admin_b).get("/api/v1/companies")

    assert listing_a.status_code == 200
    assert [row["br_number"] for row in listing_a.json()] == ["12345678"]
    assert listing_b.status_code == 200
    assert listing_b.json() == []


# --- GET /api/v1/companies/{id} ---


async def test_get_company_returns_statics(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    response = client.get(f"/api/v1/companies/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_company_unknown_id_is_404(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    response = admin_client(session, tenant, admin).get(f"/api/v1/companies/{uuid4()}")
    assert response.status_code == 404


async def test_get_company_never_leaks_cross_tenant_existence(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    created = create_company(admin_client(session, tenant_a, admin_a))

    # Tenant B's administrator gets 404 for tenant A's company id, exactly as
    # for an id that does not exist at all.
    response = admin_client(session, tenant_b, admin_b).get(f"/api/v1/companies/{created['id']}")
    assert response.status_code == 404


# --- PATCH + as-of reads ---


async def test_patch_appends_all_fact_groups_and_master_data_resolves_them(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    patch = client.patch(f"/api/v1/companies/{created['id']}", json=FULL_PATCH_PAYLOAD)
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert body["as_of"] == "2026-01-01"
    assert body["name"] == {"effective_date": "2026-01-01", **FULL_PATCH_PAYLOAD["name"]}
    assert body["registered_office"] == {
        "effective_date": "2026-01-01",
        **FULL_PATCH_PAYLOAD["registered_office"],
        "country": None,
    }
    assert body["status"] == {"effective_date": "2026-01-01", "status": "active"}
    assert body["business_nature"] == {
        "effective_date": "2026-01-01",
        **FULL_PATCH_PAYLOAD["business_nature"],
    }
    assert body["contact"] == {"effective_date": "2026-01-01", **FULL_PATCH_PAYLOAD["contact"]}
    assert Decimal(str(body["filing"]["mortgages_and_charges_total"])) == Decimal("1500.25")
    assert body["filing"]["member_count"] is None
    assert body["records_kept"]["records_kept"] == FULL_PATCH_PAYLOAD["records_kept"][
        "records_kept"
    ]
    (class_row,) = body["share_capital_classes"]["classes"]
    assert class_row["class_name"] == "Ordinary"
    assert Decimal(str(class_row["total_amount"])) == Decimal("10000.00")

    # Provenance: recorded as a manual entry by the acting Administrator.
    name_row = await session.scalar(
        select(CompanyNameRecord).where(CompanyNameRecord.company_id == UUID(created["id"]))
    )
    assert name_row is not None
    assert name_row.created_by == admin.id
    assert name_row.source_type == "manual"


async def test_master_data_resolves_as_of_the_given_date(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)
    client.patch(
        f"/api/v1/companies/{created['id']}",
        json={"effective_date": "2026-01-01", "name": {"name_en": "FIRST NAME LIMITED"}},
    )
    client.patch(
        f"/api/v1/companies/{created['id']}",
        json={"effective_date": "2026-06-01", "name": {"name_en": "RENAMED LIMITED"}},
    )

    before = client.get(f"/api/v1/companies/{created['id']}/master-data?as_of=2025-12-31")
    between = client.get(f"/api/v1/companies/{created['id']}/master-data?as_of=2026-03-01")
    after = client.get(f"/api/v1/companies/{created['id']}/master-data?as_of=2026-06-01")

    assert before.json()["name"] is None
    assert between.json()["name"]["name_en"] == "FIRST NAME LIMITED"
    assert after.json()["name"]["name_en"] == "RENAMED LIMITED"

    # The supersession chain was maintained: the first record is closed.
    rows = (
        await session.scalars(
            select(CompanyNameRecord)
            .where(CompanyNameRecord.company_id == UUID(created["id"]))
            .order_by(CompanyNameRecord.effective_date)
        )
    ).all()
    assert rows[0].valid_to == date(2026, 6, 1)
    assert rows[1].valid_to is None


async def test_master_data_defaults_as_of_to_today(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)
    client.patch(
        f"/api/v1/companies/{created['id']}",
        json={"effective_date": "2020-01-01", "name": {"name_en": "EXAMPLE LIMITED"}},
    )

    response = client.get(f"/api/v1/companies/{created['id']}/master-data")
    assert response.status_code == 200
    assert response.json()["name"]["name_en"] == "EXAMPLE LIMITED"


async def test_patch_unknown_company_is_404(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    response = admin_client(session, tenant, admin).patch(
        f"/api/v1/companies/{uuid4()}", json=FULL_PATCH_PAYLOAD
    )
    assert response.status_code == 404


async def test_patch_cross_tenant_company_is_404(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    created = create_company(admin_client(session, tenant_a, admin_a))

    response = admin_client(session, tenant_b, admin_b).patch(
        f"/api/v1/companies/{created['id']}", json=FULL_PATCH_PAYLOAD
    )
    assert response.status_code == 404


# --- PATCH /api/v1/companies/{id}/statics (anchor-statics correction) ---


async def test_correct_statics_updates_brn_with_normalization(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    response = client.patch(
        f"/api/v1/companies/{created['id']}/statics", json={"br_number": "8765 4321"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == created["id"]
    assert body["br_number"] == "87654321"
    # Untouched statics are unchanged.
    assert body["cr_number"] == "1234567"
    assert body["date_of_incorporation"] == "2010-03-01"

    # The overwrite is persisted.
    reread = client.get(f"/api/v1/companies/{created['id']}")
    assert reread.json()["br_number"] == "87654321"


async def test_correct_statics_updates_multiple_fields(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    response = client.patch(
        f"/api/v1/companies/{created['id']}/statics",
        json={
            "cr_number": "7654321",
            "date_of_incorporation": "2011-04-02",
            "anniversary_date": "2026-04-02",
            "type": "public",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cr_number"] == "7654321"
    assert body["date_of_incorporation"] == "2011-04-02"
    assert body["anniversary_date"] == "2026-04-02"
    assert body["type"] == "public"
    assert body["br_number"] == "12345678"


async def test_correct_statics_explicit_null_clears_cr_number(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    response = client.patch(
        f"/api/v1/companies/{created['id']}/statics", json={"cr_number": None}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cr_number"] is None


async def test_correct_statics_rejects_brn_held_by_another_company(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    create_company(client)  # holds normalized BRN 12345678
    other = create_company(client, br_number="87654321", cr_number="7654321")

    response = client.patch(
        f"/api/v1/companies/{other['id']}/statics", json={"br_number": "1234 5678"}
    )
    assert response.status_code == 409


async def test_correct_statics_own_brn_reformatted_is_not_a_conflict(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    created = create_company(client)

    response = client.patch(
        f"/api/v1/companies/{created['id']}/statics", json={"br_number": "1234-5678"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["br_number"] == "12345678"


async def test_correct_statics_unknown_company_is_404(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    response = admin_client(session, tenant, admin).patch(
        f"/api/v1/companies/{uuid4()}/statics", json={"cr_number": "7654321"}
    )
    assert response.status_code == 404


async def test_correct_statics_cross_tenant_company_is_404(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    created = create_company(admin_client(session, tenant_a, admin_a))

    response = admin_client(session, tenant_b, admin_b).patch(
        f"/api/v1/companies/{created['id']}/statics", json={"br_number": "87654321"}
    )
    assert response.status_code == 404


# --- The grant-gated rule through the real RBAC stack ---


async def seed_general(
    session: AsyncSession, tenant: Tenant, *, grant_company_id: UUID | None = None
) -> User:
    """A General account holding every §2.9 permission, optionally with a
    per-company access grant."""
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
    if grant_company_id is not None:
        session.add(
            CompanyAccessGrant(
                user_id=general.id, company_id=grant_company_id, tenant_id=tenant.id
            )
        )
        await session.flush()
    return general


async def test_granted_general_can_read_and_append_facts(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    created = create_company(admin_client(session, tenant, admin))
    general = await seed_general(session, tenant, grant_company_id=UUID(created["id"]))
    staff = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    statics = staff.get(f"/api/v1/companies/{created['id']}")
    assert statics.status_code == 200
    assert statics.json()["br_number"] == "12345678"

    patch = staff.patch(
        f"/api/v1/companies/{created['id']}",
        json={"effective_date": "2026-01-01", "name": {"name_en": "STAFF ENTRY LIMITED"}},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["name"]["name_en"] == "STAFF ENTRY LIMITED"

    master = staff.get(f"/api/v1/companies/{created['id']}/master-data?as_of=2026-01-01")
    assert master.status_code == 200
    assert master.json()["name"]["name_en"] == "STAFF ENTRY LIMITED"

    # Provenance: the staff member is recorded as the creator.
    name_row = await session.scalar(
        select(CompanyNameRecord).where(CompanyNameRecord.company_id == UUID(created["id"]))
    )
    assert name_row is not None and name_row.created_by == general.id


async def test_granted_general_stays_forbidden_on_create_and_list(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    created = create_company(admin_client(session, tenant, admin))
    general = await seed_general(session, tenant, grant_company_id=UUID(created["id"]))
    staff = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    assert staff.post("/api/v1/companies", json=CREATE_PAYLOAD).status_code == 403
    assert staff.get("/api/v1/companies").status_code == 403


async def test_granted_general_stays_forbidden_on_statics_correction(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    created = create_company(admin_client(session, tenant, admin))
    general = await seed_general(session, tenant, grant_company_id=UUID(created["id"]))
    staff = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    response = staff.patch(
        f"/api/v1/companies/{created['id']}/statics", json={"cr_number": "7654321"}
    )
    assert response.status_code == 403


async def test_general_without_grant_is_forbidden_on_company_routes(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    created = create_company(admin_client(session, tenant, admin))
    general = await seed_general(session, tenant)  # all permissions, no access grant
    staff = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    assert staff.get(f"/api/v1/companies/{created['id']}").status_code == 403
    assert staff.get(f"/api/v1/companies/{created['id']}/master-data").status_code == 403
    assert (
        staff.patch(f"/api/v1/companies/{created['id']}", json=FULL_PATCH_PAYLOAD).status_code
        == 403
    )
