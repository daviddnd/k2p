"""DB-backed company-master persistence scenarios (opt-in; needs a migrated PostgreSQL).

These scenarios verify migration ``0003_company_master`` — the ``Company``
statics (data-model §2.3) and the eight effective-dated fact tables — against
a real database: composite-FK tenant pinning (the F1 pattern), check
constraints, and the ADR-0007 as-of resolution rule running over ORM rows.
They are skipped unless ``AICS_TEST_DATABASE_URL`` points at a database
migrated to head (see ``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.company_master import (
    BusinessNatureRecord,
    CompanyContactFact,
    CompanyFilingFact,
    CompanyNameRecord,
    CompanyStatusRecord,
    RecordsKeptRecord,
    RegisteredOfficeRecord,
    ShareCapitalClassRecord,
)
from app.adapters.persistence.models import Company, Tenant, User
from app.domain.effective_dating import resolve_as_of

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


async def make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Review TCSP")
    session.add(tenant)
    await session.flush()
    return tenant


async def make_company(session: AsyncSession, tenant: Tenant) -> Company:
    company = Company(
        tenant_id=tenant.id,
        cr_number="1234567",
        br_number="12345678",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=date(2026, 3, 1),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


def name_record(company: Company, **overrides: object) -> CompanyNameRecord:
    values: dict[str, object] = {
        "tenant_id": company.tenant_id,
        "company_id": company.id,
        "effective_date": date(2020, 1, 1),
        "source_type": "manual",
        "name_en": "EXAMPLE LIMITED",
        "name_zh": "例子有限公司",
    }
    values.update(overrides)
    return CompanyNameRecord(**values)


async def test_company_statics_persist_with_fail_safe_defaults(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)

    loaded = await session.get(Company, company.id)

    assert loaded is not None
    assert loaded.cr_number == "1234567"
    assert loaded.br_number == "12345678"
    assert loaded.date_of_incorporation == date(2010, 3, 1)
    assert loaded.anniversary_date == date(2026, 3, 1)
    assert loaded.type == "private"
    # AI guardrail defaults to allowed per company; §7 per-company flag.
    assert loaded.ai_disabled is False


async def test_company_cr_number_is_nullable(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = Company(
        tenant_id=tenant.id,
        cr_number=None,
        br_number="87654321",
        date_of_incorporation=date(2015, 6, 30),
        anniversary_date=date(2026, 6, 30),
        type="company_limited_by_guarantee",
    )
    session.add(company)
    await session.flush()

    assert company.cr_number is None


async def test_company_type_check_rejects_unknown_value(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = Company(
        tenant_id=tenant.id,
        br_number="87654321",
        date_of_incorporation=date(2015, 6, 30),
        anniversary_date=date(2026, 6, 30),
        type="llc",
    )
    session.add(company)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_duplicate_br_number_within_tenant_is_rejected(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    await make_company(session, tenant)  # br_number 12345678

    duplicate = Company(
        tenant_id=tenant.id,
        br_number="12345678",
        date_of_incorporation=date(2018, 1, 1),
        anniversary_date=date(2026, 1, 1),
        type="private",
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_same_br_number_in_another_tenant_is_allowed(session: AsyncSession) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    await make_company(session, tenant_a)

    other_tenant_company = Company(
        tenant_id=tenant_b.id,
        br_number="12345678",
        date_of_incorporation=date(2018, 1, 1),
        anniversary_date=date(2026, 1, 1),
        type="private",
    )
    session.add(other_tenant_company)
    await session.flush()

    assert other_tenant_company.id is not None


async def test_all_eight_fact_tables_round_trip(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    base: dict[str, object] = {
        "tenant_id": tenant.id,
        "company_id": company.id,
        "effective_date": date(2024, 1, 1),
        "source_type": "import_batch",
        "source_id": None,
    }
    session.add_all(
        [
            CompanyNameRecord(**base, name_en="EXAMPLE LIMITED", name_zh=None, business_name=None),
            RegisteredOfficeRecord(
                **base, flat="12/F", building="EXAMPLE TOWER", street="1 EXAMPLE ROAD",
                district="Central", country=None,
            ),
            CompanyStatusRecord(**base, status="active"),
            BusinessNatureRecord(
                **base, business_nature_code="62090", business_nature_description="IT services"
            ),
            CompanyContactFact(**base, email="co@example.com", contact_phone_hk="23456789"),
            CompanyFilingFact(**base, mortgages_and_charges_total=None, member_count=None),
            RecordsKeptRecord(
                **base,
                records_kept=[
                    {
                        "description": "Registers",
                        "address": {
                            "flat": "12/F",
                            "building": "EXAMPLE TOWER",
                            "street": "1 EXAMPLE ROAD",
                            "district": "Central",
                        },
                    }
                ],
            ),
            ShareCapitalClassRecord(
                **base,
                classes=[
                    {
                        "class_name": "Ordinary",
                        "currency": "HKD",
                        "total_shares": 10000,
                        "total_amount": "10000.00",
                        "total_paid_up": "10000.00",
                    }
                ],
            ),
        ]
    )
    await session.flush()

    for table in (
        CompanyNameRecord,
        RegisteredOfficeRecord,
        CompanyStatusRecord,
        BusinessNatureRecord,
        CompanyContactFact,
        CompanyFilingFact,
        RecordsKeptRecord,
        ShareCapitalClassRecord,
    ):
        rows = (await session.scalars(select(table).where(table.company_id == company.id))).all()
        assert len(rows) == 1
        assert rows[0].tenant_id == tenant.id


async def test_resolve_as_of_runs_over_orm_rows(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    session.add_all(
        [
            name_record(company, effective_date=date(2020, 1, 1), name_en="FIRST LIMITED"),
            name_record(company, effective_date=date(2022, 6, 1), name_en="SECOND LIMITED"),
            name_record(company, effective_date=date(2024, 1, 1), name_en="THIRD LIMITED"),
        ]
    )
    await session.flush()

    rows = (
        await session.scalars(
            select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
        )
    ).all()

    assert resolve_as_of(rows, date(2021, 12, 31)).name_en == "FIRST LIMITED"  # type: ignore[union-attr]
    assert resolve_as_of(rows, date(2022, 6, 1)).name_en == "SECOND LIMITED"  # type: ignore[union-attr]
    assert resolve_as_of(rows, date(2026, 8, 20)).name_en == "THIRD LIMITED"  # type: ignore[union-attr]
    assert resolve_as_of(rows, date(2019, 12, 31)) is None


async def test_resolve_as_of_same_day_correction_uses_created_at(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    same_day = date(2022, 6, 1)
    session.add_all(
        [
            name_record(
                company,
                effective_date=same_day,
                name_en="MISTAKE LIMITED",
                created_at=datetime(2022, 6, 2, 9, 0, tzinfo=UTC),
            ),
            name_record(
                company,
                effective_date=same_day,
                name_en="CORRECTED LIMITED",
                created_at=datetime(2022, 6, 3, 9, 0, tzinfo=UTC),
            ),
        ]
    )
    await session.flush()

    rows = (
        await session.scalars(
            select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
        )
    ).all()

    assert resolve_as_of(rows, same_day).name_en == "CORRECTED LIMITED"  # type: ignore[union-attr]


async def test_fact_row_with_cross_tenant_company_is_uninsertable(session: AsyncSession) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    company_a = await make_company(session, tenant_a)

    # Row claims tenant B but points at tenant A's company: the composite FK
    # (company_id, tenant_id) -> companies(id, tenant_id) must reject it.
    session.add(name_record(company_a, tenant_id=tenant_b.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_fact_row_with_cross_tenant_creator_is_uninsertable(session: AsyncSession) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    company_a = await make_company(session, tenant_a)
    other_tenant_user = User(
        tenant_id=tenant_b.id,
        email="other@example.com",
        display_name="Other Tenant User",
        role="administrator",
    )
    session.add(other_tenant_user)
    await session.flush()

    session.add(name_record(company_a, created_by=other_tenant_user.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_fact_row_with_same_tenant_creator_is_accepted(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    user = User(
        tenant_id=tenant.id,
        email="staff@example.com",
        display_name="Staff User",
        role="general",
    )
    session.add(user)
    await session.flush()

    record = name_record(company, created_by=user.id)
    session.add(record)
    await session.flush()

    assert record.created_by == user.id


async def test_name_record_requires_at_least_one_name(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)

    session.add(name_record(company, name_en=None, name_zh=None))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_status_record_check_rejects_unknown_value(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)

    session.add(
        CompanyStatusRecord(
            tenant_id=tenant.id,
            company_id=company.id,
            effective_date=date(2024, 1, 1),
            source_type="manual",
            status="unknown",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_fact_row_source_type_check_rejects_unknown_value(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)

    session.add(name_record(company, source_type="spreadsheet"))
    with pytest.raises(IntegrityError):
        await session.flush()
