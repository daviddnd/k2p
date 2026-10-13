"""DB-backed audit-retrofit scenarios (opt-in; needs a migrated PostgreSQL).

Spot-checks that every pre-audit slice now emits its ADR-0009 events through
the real stack, in the same transaction as the mutation: identity/bootstrap,
login, company master (create / fact append / statics correction), CSV
import, tenant config, contacts, and service packages. Mechanism-level
guarantees (triggers, chaining, tenant pinning) live in
``test_audit_scenarios_db.py``; skipped unless ``AICS_TEST_DATABASE_URL``
points at a database migrated to head.

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import auth, identity
from app.domain.audit import AuditAction, verify_chain
from app.domain.milestones import default_milestones
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


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
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


async def tenant_entries(session: AsyncSession, tenant: Tenant) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return list(rows.all())


def entries_by_action(entries: list[AuditLog], action: AuditAction) -> list[AuditLog]:
    return [entry for entry in entries if entry.action == action.value]


# --- Identity and login ---


async def test_bootstrap_and_login_emit_events_in_chain_order(session: AsyncSession) -> None:
    tenant, admin = await identity.create_tenant_with_administrator(
        session,
        tenant_name="Bootstrap TCSP",
        admin_email=f"{uuid4()}@example.com",
        admin_display_name="Admin",
        admin_password="bootstrap-password-1",
    )
    result = await auth.login(session, email=admin.email, password="bootstrap-password-1")
    assert result is not None

    entries = await tenant_entries(session, tenant)
    assert [entry.action for entry in entries] == [
        AuditAction.TENANT_CREATED.value,
        AuditAction.USER_CREATED.value,
        AuditAction.STAFF_LOGIN.value,
    ]
    assert all(entry.actor_user_id == admin.id for entry in entries)
    assert entries[0].entity_type == "tenant" and entries[0].entity_id == tenant.id
    assert verify_chain(entries) == []


# --- Company master ---


async def test_company_create_fact_append_and_statics_correction_emit_events(
    session: AsyncSession,
) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    created = client.post(
        "/api/v1/companies",
        json={
            "br_number": "1234 5678",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": "2026-03-01",
            "type": "private",
        },
    )
    assert created.status_code == 201, created.text
    company_id = created.json()["id"]

    patched = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-08-24", "name": {"name_en": "EXAMPLE LIMITED"}},
    )
    assert patched.status_code == 200, patched.text

    corrected = client.patch(
        f"/api/v1/companies/{company_id}/statics", json={"cr_number": "7654321"}
    )
    assert corrected.status_code == 200, corrected.text

    entries = await tenant_entries(session, tenant)
    by_action = {entry.action: entry for entry in entries}

    create = by_action[AuditAction.COMPANY_CREATED.value]
    assert create.company_id == create.entity_id and str(create.company_id) == company_id
    assert create.details == {"br_number": "12345678", "type": "private"}

    fact = by_action[AuditAction.COMPANY_FACT_RECORDED.value]
    assert fact.entity_type == "company_name_records"
    assert fact.details["effective_date"] == "2026-08-24"
    assert fact.details["source_type"] == "manual"
    assert fact.details["payload"]["name_en"] == "EXAMPLE LIMITED"

    correction = by_action[AuditAction.COMPANY_STATICS_CORRECTED.value]
    assert correction.details == {"before": {"cr_number": None}, "after": {"cr_number": "7654321"}}

    assert all(entry.actor_user_id == admin.id for entry in entries)
    assert verify_chain(entries) == []


# --- CSV import ---


async def test_import_emits_batch_summary_and_import_sourced_writes(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.post(
        "/api/v1/companies/import?data_as_of=2026-08-24",
        content=(
            b"br_number,cr_number,name_en,name_zh,date_of_incorporation,anniversary_date,type\n"
            b"1234 5678,,EXAMPLE LIMITED,,2010-03-01,,private\n"
        ),
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 200, response.text

    entries = await tenant_entries(session, tenant)
    summary = entries_by_action(entries, AuditAction.COMPANIES_IMPORTED)
    assert len(summary) == 1
    assert summary[0].details == {
        "created": 1,
        "updated": 0,
        "rejected": 0,
        "data_as_of": "2026-08-24",
    }
    fact_events = entries_by_action(entries, AuditAction.COMPANY_FACT_RECORDED)
    assert fact_events and all(
        event.details["source_type"] == "import_batch" for event in fact_events
    )
    # Batch summary is the chain head: every per-row write precedes it.
    assert entries[-1].action == AuditAction.COMPANIES_IMPORTED.value
    assert verify_chain(entries) == []


# --- Tenant config ---


async def test_tenant_config_put_emits_before_after(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    payload = {
        "naming_templates": {
            "document": "{CR}/originals/{Form}_{DocType}_{YYYYMMDD}.pdf",
            "generated": "{CR}/generated/{Form}_{DocType}_{YYYYMMDD}_v{Version}.pdf",
        },
        # The configuration is one PUT, so both blocks travel: the
        # milestone block is the same before/after discipline (2026-09-24).
        "milestones": default_milestones().as_dict(),
    }
    assert client.put("/api/v1/tenant-config", json=payload).status_code == 200

    entries = entries_by_action(
        await tenant_entries(session, tenant), AuditAction.NAMING_TEMPLATES_UPDATED
    )
    assert len(entries) == 1
    # First PUT: no stored row — the shipped defaults were in effect.
    assert entries[0].details["before"] is None
    assert entries[0].details["after"] == payload["naming_templates"]

    milestones = entries_by_action(
        await tenant_entries(session, tenant), AuditAction.TENANT_MILESTONES_UPDATED
    )
    assert len(milestones) == 1
    assert milestones[0].details["before"] is None
    assert milestones[0].details["after"] == payload["milestones"]


# --- Contacts ---


async def test_idempotent_reassociation_emits_no_second_event(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    company = client.post(
        "/api/v1/companies",
        json={
            "br_number": "12345678",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": "2026-03-01",
            "type": "private",
        },
    ).json()
    person = client.post("/api/v1/persons", json={"name_en": "Chan Tai Man"}).json()

    url = f"/api/v1/companies/{company['id']}/contacts"
    assert client.post(url, json={"person_id": person["id"]}).status_code == 200
    assert client.post(url, json={"person_id": person["id"]}).status_code == 200  # idempotent

    entries = await tenant_entries(session, tenant)
    linked = entries_by_action(entries, AuditAction.COMPANY_CONTACT_LINKED)
    assert len(linked) == 1
    assert str(linked[0].company_id) == company["id"]
    assert linked[0].details == {"person_id": person["id"]}
    assert entries_by_action(entries, AuditAction.PERSON_CREATED)
    assert verify_chain(entries) == []


# --- Service packages ---


async def test_service_assignment_create_emits_company_scoped_event(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    company = client.post(
        "/api/v1/companies",
        json={
            "br_number": "12345678",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": "2026-03-01",
            "type": "private",
        },
    ).json()
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": "Annual retainer",
            "included_services": ["ai_reply_parsing"],
            "covered_forms": ["NAR1", "ND2A"],
        },
    ).json()
    assignment = client.post(
        f"/api/v1/companies/{company['id']}/service-assignments",
        json={
            "package_id": package["id"],
            "coverage_start": "2026-01-01",
            "coverage_end": "2026-12-31",
        },
    )
    assert assignment.status_code == 201, assignment.text

    entries = await tenant_entries(session, tenant)
    created = entries_by_action(entries, AuditAction.SERVICE_ASSIGNMENT_CREATED)
    assert len(created) == 1
    assert str(created[0].company_id) == company["id"]
    assert created[0].details["package_id"] == package["id"]
    assert created[0].details["coverage_start"] == "2026-01-01"
    assert verify_chain(entries) == []
