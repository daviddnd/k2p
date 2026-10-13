"""DB-backed company-master service scenarios (opt-in; needs a migrated PostgreSQL).

These scenarios verify ``app.application.company_master`` — the only write
path for company facts, owning the ADR-0007 supersession chain — against a
real database: BRN normalization and duplicate rejection, half-open
``valid_from``/``valid_to`` windows (including backdated inserts and
same-day corrections), tenant ownership checks, and the as-of read. They
are skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated
to head (see ``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.company_master import (
    CompanyContactFact,
    CompanyNameRecord,
    RegisteredOfficeRecord,
)
from app.adapters.persistence.models import Company, Tenant, User
from app.application.company_master import (
    BrnAlreadyInUseError,
    CompanyNotFoundError,
    create_company,
    get_company_master,
    record_fact,
)
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
    return await create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        br_number="12345678",
        cr_number="1234567",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=date(2026, 3, 1),
        type="private",
    )


def office_payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "flat": "12/F",
        "building": "EXAMPLE TOWER",
        "street": "1 EXAMPLE ROAD",
        "district": "Central",
    }
    values.update(overrides)
    return values


# --- create_company ---


async def test_create_company_persists_statics_with_normalized_brn(
    session: AsyncSession,
) -> None:
    tenant = await make_tenant(session)

    company = await create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        br_number="1234 5678",
        cr_number=None,
        date_of_incorporation=date(2015, 6, 30),
        anniversary_date=date(2026, 6, 30),
        type="company_limited_by_guarantee",
    )

    assert company.tenant_id == tenant.id
    assert company.br_number == "12345678"
    assert company.cr_number is None
    assert company.date_of_incorporation == date(2015, 6, 30)
    assert company.anniversary_date == date(2026, 6, 30)
    assert company.type == "company_limited_by_guarantee"


async def test_create_company_rejects_duplicate_brn_after_normalization(
    session: AsyncSession,
) -> None:
    tenant = await make_tenant(session)
    await make_company(session, tenant)  # br_number 12345678

    with pytest.raises(BrnAlreadyInUseError):
        await create_company(
            session,
            tenant_id=tenant.id,
            actor_user_id=None,
            br_number="1234 5678",  # normalizes to the same BRN
            date_of_incorporation=date(2018, 1, 1),
            anniversary_date=date(2026, 1, 1),
            type="private",
        )


async def test_create_company_allows_same_brn_in_another_tenant(session: AsyncSession) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    await make_company(session, tenant_a)

    other = await create_company(
        session,
        tenant_id=tenant_b.id,
        actor_user_id=None,
        br_number="12345678",
        date_of_incorporation=date(2018, 1, 1),
        anniversary_date=date(2026, 1, 1),
        type="private",
    )

    assert other.id is not None


# --- record_fact: provenance and windows ---


async def test_record_fact_records_default_provenance_and_creator(
    session: AsyncSession,
) -> None:
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

    record = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2020, 1, 1),
        created_by=user.id,
        name_en="EXAMPLE LIMITED",
        name_zh="例子有限公司",
    )

    assert record.source_type == "manual"
    assert record.source_id is None
    assert record.created_by == user.id
    assert record.tenant_id == tenant.id
    assert record.company_id == company.id
    assert record.valid_from == date(2020, 1, 1)
    assert record.valid_to is None


async def test_record_fact_backdated_insert_maintains_half_open_windows(
    session: AsyncSession,
) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)

    first = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2020, 1, 1),
        created_by=None,
        name_en="FIRST LIMITED",
    )
    latest = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2024, 1, 1),
        created_by=None,
        name_en="THIRD LIMITED",
    )
    # Backdated insert between the two: it closes the previously-current
    # record and is itself closed by the next record's effective_date.
    backdated = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2022, 6, 1),
        created_by=None,
        name_en="SECOND LIMITED",
    )

    assert (first.valid_from, first.valid_to) == (date(2020, 1, 1), date(2022, 6, 1))
    assert (backdated.valid_from, backdated.valid_to) == (date(2022, 6, 1), date(2024, 1, 1))
    assert (latest.valid_from, latest.valid_to) == (date(2024, 1, 1), None)


async def test_record_fact_same_day_correction_supersedes_with_empty_window(
    session: AsyncSession,
) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    same_day = date(2022, 6, 1)

    original = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=same_day,
        created_by=None,
        name_en="MISTAKE LIMITED",
    )
    correction = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=same_day,
        created_by=None,
        name_en="CORRECTED LIMITED",
    )

    # The superseded record's window is [same_day, same_day) — fully
    # superseded by the same-day correction.
    assert (original.valid_from, original.valid_to) == (same_day, same_day)
    assert (correction.valid_from, correction.valid_to) == (same_day, None)

    # Resolution picks the correction: the system learned about it later.
    # (func.now() is transaction time, so the two rows share a created_at
    # in this test; pin distinct values to model separate transactions.)
    original.created_at = datetime(2022, 6, 2, 9, 0, tzinfo=UTC)
    correction.created_at = datetime(2022, 6, 3, 9, 0, tzinfo=UTC)
    await session.flush()
    rows = (
        await session.scalars(
            select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
        )
    ).all()
    assert resolve_as_of(rows, same_day).name_en == "CORRECTED LIMITED"  # type: ignore[union-attr]


async def test_record_fact_never_leaks_cross_tenant_company_existence(
    session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    company_a = await make_company(session, tenant_a)

    with pytest.raises(CompanyNotFoundError):
        await record_fact(
            session,
            model=CompanyNameRecord,
            tenant_id=tenant_b.id,
            company_id=company_a.id,
            effective_date=date(2020, 1, 1),
            created_by=None,
            name_en="INTRUDER LIMITED",
        )


# --- get_company_master ---


async def test_get_company_master_resolves_each_group_as_of(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2020, 1, 1),
        created_by=None,
        name_en="FIRST LIMITED",
    )
    await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2024, 1, 1),
        created_by=None,
        name_en="SECOND LIMITED",
    )
    await record_fact(
        session,
        model=RegisteredOfficeRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2021, 6, 1),
        created_by=None,
        **office_payload(),
    )
    await record_fact(
        session,
        model=CompanyContactFact,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=date(2025, 1, 1),
        created_by=None,
        email="co@example.com",
    )

    # Before anything is in effect: statics present, every group None.
    early = await get_company_master(
        session, tenant_id=tenant.id, company_id=company.id, as_of=date(2019, 12, 31)
    )
    assert early.company.id == company.id
    assert early.company.br_number == "12345678"
    assert early.name is None
    assert early.registered_office is None
    assert early.status is None
    assert early.business_nature is None
    assert early.contact is None
    assert early.filing is None
    assert early.records_kept is None
    assert early.share_capital_classes is None

    # Groups resolve independently; never-written groups stay None.
    mid = await get_company_master(
        session, tenant_id=tenant.id, company_id=company.id, as_of=date(2022, 1, 1)
    )
    assert mid.name is not None and mid.name.name_en == "FIRST LIMITED"
    assert mid.registered_office is not None and mid.registered_office.building == "EXAMPLE TOWER"
    assert mid.contact is None

    late = await get_company_master(
        session, tenant_id=tenant.id, company_id=company.id, as_of=date(2026, 8, 23)
    )
    assert late.name is not None and late.name.name_en == "SECOND LIMITED"
    assert late.registered_office is not None
    assert late.contact is not None and late.contact.email == "co@example.com"


async def test_get_company_master_never_leaks_cross_tenant_company_existence(
    session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(session)
    tenant_b = await make_tenant(session)
    company_a = await make_company(session, tenant_a)

    with pytest.raises(CompanyNotFoundError):
        await get_company_master(
            session, tenant_id=tenant_b.id, company_id=company_a.id, as_of=date(2026, 1, 1)
        )


async def test_same_transaction_correction_tie_breaks_by_created_at(
    session: AsyncSession,
) -> None:
    """Two appends, same effective_date, ONE transaction: clock_timestamp()
    defaults keep created_at distinct, so the later-learned record still
    supersedes (migration 0004; with the old now() default both rows would
    tie on the transaction timestamp)."""
    tenant = await make_tenant(session)
    company = await make_company(session, tenant)
    same_day = date(2026, 6, 1)

    first = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=same_day,
        created_by=None,
        name_en="EXAMPLE TRADING LIMTED",
    )
    second = await record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=same_day,
        created_by=None,
        name_en="EXAMPLE TRADING LIMITED",
    )

    # created_at is server-generated: refresh to read the stored values.
    await session.refresh(first)
    await session.refresh(second)

    assert first.created_at < second.created_at
    # The correction supersedes: the typo'd record's window is empty.
    assert first.valid_from == same_day
    assert first.valid_to == same_day
    assert second.valid_to is None

    rows = (
        await session.scalars(
            select(CompanyNameRecord).where(CompanyNameRecord.company_id == company.id)
        )
    ).all()
    resolved = resolve_as_of(rows, same_day)
    assert resolved is not None
    assert resolved.name_en == "EXAMPLE TRADING LIMITED"
