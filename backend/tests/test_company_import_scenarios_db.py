"""DB-backed scenarios for the CSV portfolio-import endpoint (opt-in; needs
a migrated PostgreSQL — same setup as ``test_company_master_api_scenarios_db.py``).

These exercise the real router stack (``create_app``) with only the auth
seam and session overridden, against the database: match-or-create on the
normalized BRN, derived vs explicit ``anniversary_date``, fact appends with
``source_type="import_batch"`` through the ADR-0007 write path, supersession
on re-import, per-row best-effort rejection, tenant isolation, and the
Administrator-only rule through the real RBAC stack.

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.company_master import CompanyNameRecord, CompanyStatusRecord
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

HEADER = "br_number,cr_number,name_en,name_zh,date_of_incorporation,anniversary_date,type"
MINIMAL_ROW = "1234 5678,,EXAMPLE LIMITED,,2010-03-01,,private"
DATA_AS_OF = "2026-06-30"
URL = f"/api/v1/companies/import?data_as_of={DATA_AS_OF}"


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


def post_csv(client: TestClient, csv_text: str, url: str = URL) -> Response:
    """POST the raw CSV body as ``text/csv``."""
    return client.post(url, content=csv_text.encode(), headers={"Content-Type": "text/csv"})


# --- Create path ---


async def test_minimal_row_creates_company_with_derived_anniversary(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = post_csv(client, f"{HEADER}\n{MINIMAL_ROW}\n")

    assert response.status_code == 200
    report = response.json()
    assert (report["created"], report["updated"], report["rejected"]) == (1, 0, 0)
    assert report["rows"] == [
        {
            "row": 1,
            "br_number": "1234 5678",
            "outcome": "created",
            "company_id": report["rows"][0]["company_id"],
            "reason": None,
        }
    ]
    company = await session.scalar(select(Company).where(Company.tenant_id == tenant.id))
    assert company is not None
    assert company.br_number == "12345678"  # normalized
    assert company.anniversary_date == date(2026, 3, 1)  # derived in the data_as_of year
    name = await session.scalar(
        select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
    )
    assert name is not None
    assert name.name_en == "EXAMPLE LIMITED"
    assert name.effective_date == date(2026, 6, 30)  # the file-level data_as_of
    assert name.source_type == "import_batch"
    assert name.created_by == admin.id


async def test_full_row_writes_every_fact_group(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    header = (
        "br_number,name_en,date_of_incorporation,type,status,"
        "ro_flat,ro_building,ro_street,ro_district,business_nature_code,"
        "business_nature_description,email,member_count"
    )
    row = (
        "87654321,FULL LIMITED,2015-06-15,private,dormant,"
        "12/F,EXAMPLE TOWER,1 EXAMPLE ROAD,Central,620,"
        "Computer programming,co@example.com,7"
    )

    response = post_csv(client, f"{header}\n{row}\n")

    assert response.status_code == 200
    assert response.json()["created"] == 1
    company_id = response.json()["rows"][0]["company_id"]
    snapshot = client.get(f"/api/v1/companies/{company_id}/master-data?as_of=2026-06-30")
    assert snapshot.status_code == 200
    master = snapshot.json()
    assert master["name"]["name_en"] == "FULL LIMITED"
    assert master["registered_office"]["district"] == "Central"
    assert master["status"]["status"] == "dormant"
    assert master["business_nature"]["business_nature_code"] == "620"
    assert master["contact"]["email"] == "co@example.com"
    assert master["filing"]["member_count"] == 7


async def test_per_row_effective_date_overrides_data_as_of(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    header = "br_number,name_en,date_of_incorporation,type,effective_date"
    row = "12345678,EXAMPLE LIMITED,2010-03-01,private,2024-01-15"

    response = post_csv(client, f"{header}\n{row}\n")

    assert response.status_code == 200
    company = await session.scalar(select(Company).where(Company.tenant_id == tenant.id))
    name = await session.scalar(
        select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
    )
    assert name is not None
    assert name.effective_date == date(2024, 1, 15)


# --- Match path: update, never overwrite statics, supersede facts ---


async def seed_company(
    session: AsyncSession, tenant: Tenant, *, br_number: str, cr_number: str | None = None
) -> Company:
    company = Company(
        tenant_id=tenant.id,
        br_number=br_number,
        cr_number=cr_number,
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=date(2026, 3, 1),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


async def test_matched_brn_updates_without_touching_statics(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    existing = await seed_company(session, tenant, br_number="12345678", cr_number="1111111")
    client = admin_client(session, tenant, admin)
    # The CSV carries a different cr_number and incorporation date: import
    # must not overwrite anchor statics on a matched company (D3).
    row = "1234 5678,9999999,RENAMED LIMITED,,2020-01-01,,private"

    response = post_csv(client, f"{HEADER}\n{row}\n")

    assert response.status_code == 200
    report = response.json()
    assert (report["created"], report["updated"], report["rejected"]) == (0, 1, 0)
    assert report["rows"][0]["company_id"] == str(existing.id)
    await session.refresh(existing)
    assert existing.cr_number == "1111111"
    assert existing.date_of_incorporation == date(2010, 3, 1)
    name = await session.scalar(
        select(CompanyNameRecord)
        .where(CompanyNameRecord.company_id == existing.id)
        .order_by(CompanyNameRecord.created_at.desc())
        .limit(1)
    )
    assert name is not None
    assert name.name_en == "RENAMED LIMITED"


async def test_reimport_supersedes_facts_by_created_at_tie_break(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    header = "br_number,name_en,date_of_incorporation,type,status"
    first = f"{header}\n12345678,EXAMPLE LIMITED,2010-03-01,private,active\n"
    second = f"{header}\n12345678,EXAMPLE LIMITED,2010-03-01,private,dormant\n"

    assert post_csv(client, first).json()["created"] == 1
    assert post_csv(client, second).json()["updated"] == 1

    company = await session.scalar(select(Company).where(Company.tenant_id == tenant.id))
    statuses = (
        await session.scalars(
            select(CompanyStatusRecord)
            .where(CompanyStatusRecord.company_id == company.id)
            .order_by(CompanyStatusRecord.created_at)
        )
    ).all()
    assert [record.status for record in statuses] == ["active", "dormant"]
    # Same effective_date: the later-learned record wins; the superseded one
    # is left with an empty [effective_date, effective_date) window.
    assert statuses[0].valid_to == statuses[0].effective_date
    company_id = str(company.id)
    snapshot = client.get(f"/api/v1/companies/{company_id}/master-data?as_of=2026-06-30")
    assert snapshot.json()["status"]["status"] == "dormant"


async def test_duplicate_brn_within_one_file_creates_then_updates(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    rows = f"{HEADER}\n{MINIMAL_ROW}\n12345678,,SAME COMPANY AGAIN,,2010-03-01,,private\n"

    response = post_csv(client, rows)

    report = response.json()
    assert (report["created"], report["updated"], report["rejected"]) == (1, 1, 0)
    assert report["rows"][0]["company_id"] == report["rows"][1]["company_id"]
    count = await session.scalar(
        select(func.count()).select_from(Company).where(Company.tenant_id == tenant.id)
    )
    assert count == 1


# --- Per-row best-effort ---


async def test_invalid_rows_are_rejected_individually_valid_rows_commit(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    csv_text = (
        f"{HEADER}\n"
        "12345678,,GOOD LIMITED,,2010-03-01,,private\n"
        "87654321,,BAD DATE,,not-a-date,,private\n"
        "99999999,,NO ANNIVERSARY,,2012-01-05,,public\n"
    )

    response = post_csv(client, csv_text)

    report = response.json()
    assert (report["created"], report["updated"], report["rejected"]) == (1, 0, 2)
    assert report["rows"][1]["outcome"] == "rejected"
    assert "date_of_incorporation" in report["rows"][1]["reason"]
    assert report["rows"][2]["outcome"] == "rejected"
    assert "anniversary_date" in report["rows"][2]["reason"]
    count = await session.scalar(
        select(func.count()).select_from(Company).where(Company.tenant_id == tenant.id)
    )
    assert count == 1


async def test_row_with_surplus_cells_is_rejected(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = post_csv(client, f"{HEADER}\n12345678,,EXAMPLE LIMITED,,2010-03-01,,private,EXTRA\n")

    report = response.json()
    assert report["rejected"] == 1
    assert "more cells" in report["rows"][0]["reason"]


# --- File-level validation ---


async def test_unknown_header_column_fails_the_file_with_no_writes(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    csv_text = (
        "br_number,name_en,date_of_incorporation,type,emal\n"
        "12345678,A,2010-03-01,private,x\n"
    )

    response = post_csv(client, csv_text)

    assert response.status_code == 422
    assert "emal" in response.json()["detail"]
    count = await session.scalar(
        select(func.count()).select_from(Company).where(Company.tenant_id == tenant.id)
    )
    assert count == 0


# --- Tenant isolation and authorization through the real stack ---


async def test_same_brn_in_another_tenant_creates_an_independent_company(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    other_tenant, _ = await make_admin(session)
    await seed_company(session, other_tenant, br_number="12345678")
    client = admin_client(session, tenant, admin)

    response = post_csv(client, f"{HEADER}\n{MINIMAL_ROW}\n")

    # The other tenant's BRN is invisible: this tenant's row is a create.
    assert response.json()["created"] == 1
    count = await session.scalar(
        select(func.count()).select_from(Company).where(Company.br_number == "12345678")
    )
    assert count == 2


async def test_general_account_is_403_through_the_real_stack(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
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
    client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    response = post_csv(client, f"{HEADER}\n{MINIMAL_ROW}\n")

    assert response.status_code == 403


async def test_unauthenticated_is_401(session: AsyncSession) -> None:
    client = make_client(session)  # no actor override, no Authorization header

    response = post_csv(client, f"{HEADER}\n{MINIMAL_ROW}\n")

    assert response.status_code == 401
