"""DB-backed entitlement scenarios (opt-in; needs a migrated PostgreSQL).

These verify the entitlement slice end to end against a real database: the
Free-tier creation caps (2 seats, the bootstrap Administrator counting;
100 companies) at their boundaries, that an over-cap tenant keeps
full use of the rows it already has (ADR-0012: the customer is never locked
out of their own data), the CSV import's per-row cap behaviour, and both the
§5.1 gating seam and the read endpoint following the configured tier across
the §4.3 licence states — including the Standard → Free degradation the
Phase 1 exit gate calls for. The tier is provisioned through the settings
seam, the same path a deployment uses. Skipped unless
``AICS_TEST_DATABASE_URL`` points at a database migrated to head (see
``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import service_packages
from app.core.config import settings
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

AS_OF = date(2026, 6, 30)
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
FREE_COMPANY_CAP = 100
USER_PAYLOAD = {
    "email": "staff@tcsp.example",
    "display_name": "Staff User",
    "role": "general",
    "password": "staff-password-1",
}


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


def provision(
    monkeypatch: pytest.MonkeyPatch, *, tier: str, expires_at: datetime | None = None
) -> None:
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


async def seed_tenant_with_admin(session: AsyncSession) -> tuple[Tenant, User]:
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


async def seed_companies(session: AsyncSession, tenant: Tenant, *, count: int) -> list[Company]:
    """Portfolio rows seeded directly — the cap counts existing rows, not writes."""
    companies = [
        Company(
            tenant_id=tenant.id,
            br_number=f"{index:08d}",
            date_of_incorporation=date(2010, 3, 1),
            anniversary_date=date(2026, 3, 1),
            type="private",
        )
        for index in range(1, count + 1)
    ]
    session.add_all(companies)
    await session.flush()
    return companies


async def make_company(session: AsyncSession, tenant: Tenant) -> Company:
    company = Company(
        tenant_id=tenant.id,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2020, 1, 2),
        anniversary_date=date(2026, 1, 2),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


async def cover(
    session: AsyncSession, tenant: Tenant, company_id: UUID, *, services: list[str]
) -> None:
    """A package covering ``services``, assigned to the company in coverage."""
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        name=f"pkg-{uuid4()}",
        included_services=services,
        covered_forms=["NAR1", "ND2A"],
    )
    await service_packages.create_assignment(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        package_id=package.id,
        actor_user_id=None,
        coverage_start=COVERAGE_START,
        coverage_end=COVERAGE_END,
    )


async def gate(
    session: AsyncSession, tenant: Tenant, company_id: UUID, automation_type: AutomationType
):
    return await service_packages.check_automation(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        automation_type=automation_type,
        as_of=AS_OF,
    )


# --- Free-tier creation caps (design D6) ---


async def test_free_seat_cap_counts_the_bootstrap_admin(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    # The bootstrap Administrator counts against the Free tier's two seats:
    # the second account is allowed, the third is refused with the stable cap
    # code.
    second = client.post("/api/v1/users", json={**USER_PAYLOAD, "email": "second@tcsp.example"})
    assert second.status_code == 201, second.text
    refused = client.post("/api/v1/users", json=USER_PAYLOAD)
    assert refused.status_code == 409, refused.text
    assert "seat_cap_exceeded" in refused.json()["detail"]

    # Caps gate creation only: the existing account keeps full use.
    assert client.get("/api/v1/entitlement").status_code == 200

    # The refusal is entitlement-driven, not structural.
    provision(monkeypatch, tier="standard")
    created = client.post("/api/v1/users", json=USER_PAYLOAD)
    assert created.status_code == 201, created.text


async def test_free_company_cap_boundary_and_over_cap_usability(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    await seed_companies(session, tenant, count=FREE_COMPANY_CAP - 1)

    def create_company(br_number: str):
        return client.post(
            "/api/v1/companies",
            json={
                "br_number": br_number,
                "date_of_incorporation": "2010-03-01",
                "anniversary_date": "2026-03-01",
                "type": "private",
            },
        )

    assert create_company("00000100").status_code == 201  # the 100th: last one allowed
    refused = create_company("00000101")
    assert refused.status_code == 409, refused.text
    assert "company_cap_exceeded" in refused.json()["detail"]

    # Over the cap the existing portfolio stays fully usable: listing and
    # fact appends are never gated.
    listing = client.get("/api/v1/companies")
    assert listing.status_code == 200
    assert len(listing.json()) == FREE_COMPANY_CAP
    first_id = listing.json()[0]["id"]
    appended = client.patch(
        f"/api/v1/companies/{first_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": "STILL USABLE"}},
    )
    assert appended.status_code == 200, appended.text

    # The cap is per tenant: another tenant is unaffected.
    other_tenant, other_admin = await seed_tenant_with_admin(session)
    other_client = make_client(session, ActorRef(user_id=other_admin.id, tenant_id=other_tenant.id))
    assert (
        other_client.post(
            "/api/v1/companies",
            json={
                "br_number": "00000099",
                "date_of_incorporation": "2010-03-01",
                "anniversary_date": "2026-03-01",
                "type": "private",
            },
        ).status_code
        == 201
    )


async def test_over_cap_import_rejects_only_the_creates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    existing = await seed_companies(session, tenant, count=FREE_COMPANY_CAP)
    csv_text = (
        "br_number,name_en,date_of_incorporation,type\n"
        f"{existing[0].br_number},UPDATED NAME,2010-03-01,private\n"
        "00000999,NEW COMPANY,2011-04-05,private\n"
    )

    response = client.post(
        "/api/v1/companies/import?data_as_of=2026-06-30", content=csv_text.encode()
    )
    assert response.status_code == 200, response.text
    report = response.json()
    # Best-effort per row: the update commits, the over-cap create is refused.
    assert (report["created"], report["updated"], report["rejected"]) == (0, 1, 1)
    create_row = next(row for row in report["rows"] if row["outcome"] == "rejected")
    assert "company_cap_exceeded" in create_row["reason"]

    updated = client.get(f"/api/v1/companies/{existing[0].id}/master-data?as_of=2026-06-30")
    assert updated.status_code == 200
    assert updated.json()["name"]["name_en"] == "UPDATED NAME"


# --- The §5.1 gate and the read endpoint follow the configured tier ---


async def test_gate_follows_the_configured_tier(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _ = await seed_tenant_with_admin(session)
    company = await make_company(session, tenant)
    await cover(
        session,
        tenant,
        company.id,
        services=[AutomationType.AI_REPLY_PARSING.value, AutomationType.DOCUMENT_EXTRACTION.value],
    )

    # Unlicensed deployment (the default): the tier leg fails closed even with
    # the commercial coverage in place.
    provision(monkeypatch, tier="free")
    denied = await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    assert denied.allowed is False
    assert denied.reason is GatingReason.TIER_CAPABILITY_MISSING

    provision(monkeypatch, tier="standard")
    for automation_type in (AutomationType.AI_REPLY_PARSING, AutomationType.DOCUMENT_EXTRACTION):
        decision = await gate(session, tenant, company.id, automation_type)
        assert decision.allowed is True
        assert decision.reason is None


async def test_lapsed_lease_degrades_the_view_and_the_gate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company = await make_company(session, tenant)
    await cover(session, tenant, company.id, services=[AutomationType.DOCUMENT_EXTRACTION.value])
    now = datetime.now(UTC)

    # Past the 7-day grace window the lease is dead: the tier stays
    # provisioned, the capability set collapses to Free (§4.3), and gated
    # automation is refused — the Standard → Free degradation.
    provision(monkeypatch, tier="standard", expires_at=now - timedelta(days=8))
    degraded = client.get("/api/v1/entitlement").json()
    assert degraded["tier"] == "standard"
    assert degraded["license_state"] == "free"
    assert degraded["capabilities"] == []
    decision = await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    assert decision.allowed is False
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING

    # Inside the grace window the tier's capabilities are retained.
    provision(monkeypatch, tier="standard", expires_at=now - timedelta(days=3))
    in_grace = client.get("/api/v1/entitlement").json()
    assert in_grace["license_state"] == "grace"
    assert in_grace["capabilities"] == sorted(member.value for member in AutomationType)
    decision = await gate(session, tenant, company.id, AutomationType.DOCUMENT_EXTRACTION)
    assert decision.allowed is True
    assert decision.reason is None
