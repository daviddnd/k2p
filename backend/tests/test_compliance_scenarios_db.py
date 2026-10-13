"""DB-backed compliance-calendar and dashboard scenarios (opt-in; needs a
migrated PostgreSQL).

These verify the V1 read model against a real database and the real stack:
the NAR1 due dates a range produces, the as-of status resolution behind the
dormant exclusion (and the regeneration when a company returns to active),
the 30/15/7-day bands at read time, the portfolio scoping for a General
staffer versus an Administrator (through the real HTTP routes and the real
RBAC context loader), the pending data-ingestion count reflecting
``pending`` records only, and the "service not covered" flags matching the
§5.1 gate's own ``GatingReason`` per company and automation type. Skipped
unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to head.

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.company_master import CompanyNameRecord, CompanyStatusRecord
from app.adapters.persistence.documents import Document
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.prior_filing import ImportBatch, ImportBatchRecord
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import change_events, company_master, compliance, service_packages
from app.core.config import settings
from app.domain.change_events import ChangeEventAction, ChangeEventType
from app.domain.compliance import LateFeeExposure
from app.domain.documents import DocumentKind, DocumentOrigin, RetentionClass
from app.domain.rbac import ActorContext, Role
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

# The read's own clock is always explicit, so scenarios do not depend on the
# machine's date; HTTP-level assertions are id-based or fixed-range for the
# same reason.
AS_OF = date(2026, 4, 1)
FROM_2026, TO_2026 = date(2026, 1, 1), date(2026, 12, 31)
FROM_2027, TO_2027 = date(2027, 1, 1), date(2027, 12, 31)
CALENDAR_2026 = "from=2026-01-01&to=2026-12-31"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with only the auth seam and session overridden.

    The RBAC context is **not** overridden: grants and roles load from the
    database through the real dependency chain, which is the point of the
    scoping scenarios.
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def admin_context() -> ActorContext:
    return ActorContext(
        is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
    )


async def make_tenant(session: AsyncSession, *, role: str = "administrator") -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role=role,
    )
    session.add(user)
    await session.flush()
    return tenant, user


async def make_company(
    session: AsyncSession,
    tenant: Tenant,
    *,
    anniversary: date = date(2026, 3, 1),
    type: str = "private",
    cr_number: str | None = None,
) -> Company:
    return await company_master.create_company(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2010, 3, 1),
        anniversary_date=anniversary,
        type=type,
        cr_number=cr_number,
    )


async def set_status(
    session: AsyncSession, tenant: Tenant, company: Company, *, status: str, effective: date
) -> None:
    await company_master.record_fact(
        session,
        model=CompanyStatusRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=effective,
        created_by=None,
        status=status,
    )


async def set_name(
    session: AsyncSession, tenant: Tenant, company: Company, *, name_en: str, effective: date
) -> None:
    await company_master.record_fact(
        session,
        model=CompanyNameRecord,
        tenant_id=tenant.id,
        company_id=company.id,
        effective_date=effective,
        created_by=None,
        name_en=name_en,
    )


async def make_batch(session: AsyncSession, tenant: Tenant, company: Company) -> ImportBatch:
    document = Document(
        tenant_id=tenant.id,
        company_id=company.id,
        kind=DocumentKind.PRIOR_FILING.value,
        origin=DocumentOrigin.UPLOAD.value,
        retention_class=RetentionClass.STATUTORY_FILING.value,
        sha256="0" * 64,
        storage_key=f"fixtures/{uuid4()}",
        byte_size=1024,
        content_type="application/pdf",
    )
    session.add(document)
    await session.flush()
    batch = ImportBatch(
        tenant_id=tenant.id,
        company_id=company.id,
        document_id=document.id,
        form="nar1",
        shape="acroform",
    )
    session.add(batch)
    await session.flush()
    return batch


async def add_record(
    session: AsyncSession, tenant: Tenant, batch: ImportBatch, *, status: str
) -> ImportBatchRecord:
    record = ImportBatchRecord(
        batch_id=batch.id,
        tenant_id=tenant.id,
        target="company.email",
        value="staff@example.com",
        confidence=Decimal("0.990"),
        status=status,
        decided_at=None if status == "pending" else datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    return record


# The statuses the executor walks for each target status, so a seeded event's
# current version really stands where the test says it does.
_EVENT_PATH: dict[str, list[ChangeEventAction]] = {
    "pending": [],
    "under_review": [ChangeEventAction.MARK_UNDER_REVIEW],
    "confirmed": [
        ChangeEventAction.MARK_UNDER_REVIEW,
        ChangeEventAction.MARK_REVIEWED,
        ChangeEventAction.CONFIRM,
    ],
}


async def add_change_event(
    session: AsyncSession,
    tenant: Tenant,
    company: Company,
    *,
    effective: date = date(2026, 3, 1),
    status: str = "pending",
    actor_user_id: UUID | None = None,
) -> UUID:
    """One change event driven to ``status`` through the product's own paths.

    The service's manual entry and its one executor, so the widget is counted
    over rows the product itself would produce. Each event is revised once
    before its status walk, so every seeded event carries a superseded first
    version whose status stays ``pending`` — the row a read that filtered rows
    rather than current versions would miscount. ``actor_user_id`` is the
    account the executor stamps (a review stamp needs its account, the
    database's own pair check).
    """
    view = await change_events.create(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        event_type=ChangeEventType.OTHER,
        effective_date=effective,
        payload={"summary": "the client reported a change of auditors"},
        actor_user_id=actor_user_id,
        as_of=AS_OF,
    )
    await change_events.transition(
        session,
        tenant_id=tenant.id,
        context=admin_context(),
        event_id=view.id,
        action=ChangeEventAction.EDIT,
        changes={"effective_date_text": "reported by telephone"},
        actor_user_id=actor_user_id,
        as_of=AS_OF,
    )
    for action in _EVENT_PATH[status]:
        await change_events.transition(
            session,
            tenant_id=tenant.id,
            context=admin_context(),
            event_id=view.id,
            action=action,
            actor_user_id=actor_user_id,
            as_of=AS_OF,
        )
    return view.id


async def add_draft(
    session: AsyncSession,
    tenant: Tenant,
    company: Company,
    *,
    status: str = "generated",
    form_type: str = "nar1",
    version_no: int = 1,
    draft_id: UUID | None = None,
) -> UUID:
    """One draft version row in ``status``, inserted directly.

    The widget's count is a SQL aggregation over draft version rows, so what a
    scenario has to place is the rows themselves: the generation path and its
    resolution are ``test_drafts_scenarios_db``'s subject, and driving it here
    would only add master-data fixtures to a read that never looks at them.
    A second call with the same ``draft_id`` (and the next ``version_no``)
    builds a lineage whose earlier row stays where it stood.
    """
    row = Draft(
        id=draft_id if draft_id is not None else uuid4(),
        version_no=version_no,
        tenant_id=tenant.id,
        company_id=company.id,
        form_type=form_type,
        target_date=date(2026, 3, 31),
        status=status,
        resolved_state={},
        field_provenance={},
        qc_flags=[],
        filing_metadata={},
        applied_event_ids=[],
        form_version={"specification_no": "2/2025", "template_sha256": "0" * 64},
        signature_status="not_sent",
    )
    session.add(row)
    await session.flush()
    return row.id


async def calendar_of(
    session: AsyncSession,
    tenant: Tenant,
    date_from: date,
    date_to: date,
    *,
    context: ActorContext | None = None,
) -> compliance.Calendar:
    return await compliance.calendar(
        session,
        tenant_id=tenant.id,
        context=context if context is not None else admin_context(),
        date_from=date_from,
        date_to=date_to,
        as_of=AS_OF,
    )


# --- Calendar: the range read ---


async def test_calendar_lists_due_dates_in_range_with_bands_at_read_time(
    session: AsyncSession,
) -> None:
    tenant, _ = await make_tenant(session)
    company = await make_company(session, tenant, anniversary=date(2026, 3, 1))
    await set_name(session, tenant, company, name_en="Acme Limited", effective=date(2020, 1, 1))

    view = await calendar_of(session, tenant, FROM_2026, TO_2026)

    assert view.date_from == FROM_2026 and view.date_to == TO_2026
    (entry,) = view.entries
    assert entry.company_id == company.id
    assert entry.br_number == company.br_number
    assert entry.company_name == "Acme Limited"
    assert entry.kind == "nar1_due"
    assert entry.due_date == date(2026, 4, 12)  # anniversary + 42 days
    # AS_OF is 2026-04-01: 11 days out → amber, 15-day alert, not late.
    assert (entry.days_remaining, entry.risk, entry.alert, entry.overdue) == (
        11,
        "amber",
        "alert_15d",
        False,
    )
    # Eleven days before the due date the delivery is still inside the 42-day
    # window: the on-time fee, not late (slice L's read-time exposure).
    assert entry.late_fee_exposure == LateFeeExposure(fee_hkd=105, late=False)
    assert view.not_generated == []


async def test_calendar_is_range_driven_not_truncated(session: AsyncSession) -> None:
    """The largest range the read allows (366 days) returns both annual
    deadlines it contains — this year's and the next cycle's — rather than a
    fixed internal window."""
    tenant, _ = await make_tenant(session)
    await make_company(session, tenant, anniversary=date(2026, 3, 1))

    view = await calendar_of(session, tenant, date(2026, 4, 12), date(2027, 4, 12))

    assert [entry.due_date for entry in view.entries] == [
        date(2026, 4, 12),
        date(2027, 4, 12),
    ]
    # AS_OF is 2026-04-01: the first deadline is still ahead, the second a
    # full cycle out — both banded at read time.
    assert (view.entries[0].risk, view.entries[0].alert) == ("amber", "alert_15d")
    second = view.entries[1]
    assert (second.risk, second.alert, second.overdue) == ("green", None, False)
    assert second.days_remaining > 30


async def test_calendar_range_limits(session: AsyncSession) -> None:
    tenant, _ = await make_tenant(session)
    await make_company(session, tenant)
    with pytest.raises(compliance.CalendarRangeError):
        await calendar_of(session, tenant, TO_2026, FROM_2026)
    # Exactly at the cap (2026-01-01 .. 2027-01-01 inclusive = 366 days).
    at_cap = await calendar_of(session, tenant, FROM_2026, date(2027, 1, 1))
    assert [entry.due_date for entry in at_cap.entries] == [date(2026, 4, 12)]
    # One day wider: refused, never truncated.
    with pytest.raises(compliance.CalendarRangeError):
        await calendar_of(session, tenant, FROM_2026, date(2027, 1, 2))


async def test_company_type_without_a_rule_is_reported_not_guessed(
    session: AsyncSession,
) -> None:
    tenant, _ = await make_tenant(session)
    private = await make_company(session, tenant)
    public = await make_company(session, tenant, type="public")

    view = await calendar_of(session, tenant, FROM_2026, TO_2026)
    dashboard = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    expected = [(public.id, "public", compliance.NAR1_RULE_NOT_ESTABLISHED)]
    assert [entry.company_id for entry in view.entries] == [private.id]
    assert [
        (item.company_id, item.company_type, item.reason) for item in view.not_generated
    ] == expected
    # The dashboard refuses the same way, so a card with no next deadline is
    # never silently unexplained.
    assert [
        (item.company_id, item.company_type, item.reason) for item in dashboard.not_generated
    ] == expected
    public_card = next(
        card for card in dashboard.company_cards if card.company_id == public.id
    )
    assert public_card.next_deadline is None


# --- Dormant exclusion, as-of resolution, regeneration ---


async def test_dormant_company_is_excluded_and_reactivation_regenerates(
    session: AsyncSession,
) -> None:
    tenant, _ = await make_tenant(session)
    company = await make_company(session, tenant, anniversary=date(2026, 3, 1))
    await set_status(session, tenant, company, status="dormant", effective=date(2019, 1, 1))

    assert (await calendar_of(session, tenant, FROM_2026, TO_2026)).entries == []

    # Returning to active before the next due date regenerates it.
    await set_status(session, tenant, company, status="active", effective=date(2026, 3, 1))
    view = await calendar_of(session, tenant, FROM_2026, TO_2026)
    assert [entry.due_date for entry in view.entries] == [date(2026, 4, 12)]


async def test_status_is_resolved_as_of_each_due_date(session: AsyncSession) -> None:
    """A status change effective after a deadline leaves that deadline
    standing; the next year's deadline is read under the new status."""
    tenant, _ = await make_tenant(session)
    company = await make_company(session, tenant, anniversary=date(2026, 3, 1))
    await set_status(session, tenant, company, status="active", effective=date(2019, 1, 1))
    await set_status(session, tenant, company, status="dormant", effective=date(2026, 5, 1))

    view_2026 = await calendar_of(session, tenant, FROM_2026, TO_2026)
    assert [entry.due_date for entry in view_2026.entries] == [
        date(2026, 4, 12)  # active on 2026-04-12; dormant only from May
    ]
    assert (await calendar_of(session, tenant, FROM_2027, TO_2027)).entries == []


async def test_no_status_record_generates_the_deadline(session: AsyncSession) -> None:
    """An unknown status is not dormant: the reminder is the safe side."""
    tenant, _ = await make_tenant(session)
    await make_company(session, tenant)
    assert len((await calendar_of(session, tenant, FROM_2026, TO_2026)).entries) == 1


# --- Scoping: Administrator vs General staffer, through the real routes ---


async def test_general_staff_sees_only_granted_companies(session: AsyncSession) -> None:
    tenant, admin = await make_tenant(session)
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
    )
    session.add(general)
    await session.flush()
    granted = await make_company(session, tenant)
    other = await make_company(session, tenant)
    session.add(
        CompanyAccessGrant(user_id=general.id, company_id=granted.id, tenant_id=tenant.id)
    )
    await session.flush()

    admin_client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    general_client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    admin_body = admin_client.get("/api/v1/dashboard").json()
    assert admin_body["portfolio"]["total_companies"] == 2
    assert {card["company_id"] for card in admin_body["company_cards"]} == {
        str(granted.id),
        str(other.id),
    }

    general_body = general_client.get("/api/v1/dashboard").json()
    assert general_body["portfolio"]["total_companies"] == 1
    assert [card["company_id"] for card in general_body["company_cards"]] == [str(granted.id)]

    calendar = general_client.get(f"/api/v1/compliance/calendar?{CALENDAR_2026}")
    assert calendar.status_code == 200
    assert [entry["company_id"] for entry in calendar.json()["entries"]] == [str(granted.id)]
    assert calendar.json()["from"] == "2026-01-01"


async def test_general_staff_without_grants_sees_an_empty_portfolio(
    session: AsyncSession,
) -> None:
    tenant, _ = await make_tenant(session)
    await make_company(session, tenant)
    stranger = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Stranger",
        role="general",
    )
    session.add(stranger)
    await session.flush()

    client = make_client(session, ActorRef(user_id=stranger.id, tenant_id=tenant.id))

    body = client.get("/api/v1/dashboard").json()
    assert body["portfolio"]["total_companies"] == 0
    assert body["company_cards"] == []
    assert body["service_gaps"] == []
    calendar = client.get(f"/api/v1/compliance/calendar?{CALENDAR_2026}").json()
    assert calendar["entries"] == []


# --- Pending data-ingestion review ---


async def test_pending_ingestion_counts_pending_records_only(session: AsyncSession) -> None:
    tenant, _ = await make_tenant(session)
    busy = await make_company(session, tenant)
    settled = await make_company(session, tenant)
    await set_name(session, tenant, busy, name_en="Busy Limited", effective=date(2020, 1, 1))
    busy_batch = await make_batch(session, tenant, busy)
    await add_record(session, tenant, busy_batch, status="pending")
    await add_record(session, tenant, busy_batch, status="pending")
    await add_record(session, tenant, busy_batch, status="confirmed")
    await add_record(session, tenant, busy_batch, status="rejected")
    settled_batch = await make_batch(session, tenant, settled)
    await add_record(session, tenant, settled_batch, status="confirmed")

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    assert view.pending_ingestion.pending_records == 2
    assert [
        (item.company_id, item.company_name, item.pending_records)
        for item in view.pending_ingestion.companies
    ] == [(busy.id, "Busy Limited", 2)]
    cards = {card.company_id: card for card in view.company_cards}
    assert cards[busy.id].pending_ingestion_records == 2
    assert cards[settled.id].pending_ingestion_records == 0


# --- Pending event review (DoD §9 item 10, CE2) ------------------------------


async def test_pending_event_review_counts_awaiting_events_per_company(
    session: AsyncSession,
) -> None:
    """The widget lists the same set the cross-company queue does: events
    whose **current version** is ``pending`` or ``under_review``, per company.
    A decided event is not work — including one whose superseded first version
    still says ``pending``, which is why the current version is selected in
    SQL rather than filtered row by row."""
    tenant, admin = await make_tenant(session)
    busy = await make_company(session, tenant)
    settled = await make_company(session, tenant)
    await set_name(session, tenant, busy, name_en="Busy Limited", effective=date(2020, 1, 1))
    await add_change_event(
        session, tenant, busy, effective=date(2026, 3, 20), actor_user_id=admin.id
    )
    await add_change_event(
        session,
        tenant,
        busy,
        effective=date(2026, 3, 10),
        status="under_review",
        actor_user_id=admin.id,
    )
    # Decided through a revision: version 1 stays `pending` on its own row.
    decided = await add_change_event(
        session,
        tenant,
        busy,
        effective=date(2026, 3, 1),
        status="confirmed",
        actor_user_id=admin.id,
    )
    await add_change_event(
        session, tenant, settled, effective=date(2026, 3, 25), actor_user_id=admin.id
    )

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    assert view.pending_event_review.pending_events == 3
    assert [
        (item.company_id, item.company_name, item.pending_events)
        for item in view.pending_event_review.companies
    ] == [
        (busy.id, "Busy Limited", 2),
        (settled.id, None, 1),
    ]
    rows = await session.scalars(
        select(ChangeEvent).where(ChangeEvent.id == decided).order_by(ChangeEvent.version_no)
    )
    assert [(row.version_no, row.status) for row in rows.all()] == [
        (1, "pending"),
        (2, "confirmed"),
    ]


async def test_pending_event_review_is_grant_scoped_through_the_route(
    session: AsyncSession,
) -> None:
    """The widget is the dashboard's scoping rule over the events: an
    Administrator sees the tenant's awaiting-review events, a General account
    exactly its granted companies'."""
    tenant, admin = await make_tenant(session)
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
    )
    session.add(general)
    await session.flush()
    granted = await make_company(session, tenant)
    other = await make_company(session, tenant)
    session.add(
        CompanyAccessGrant(user_id=general.id, company_id=granted.id, tenant_id=tenant.id)
    )
    await session.flush()
    await add_change_event(
        session, tenant, granted, effective=date(2026, 3, 20), actor_user_id=admin.id
    )
    await add_change_event(
        session, tenant, other, effective=date(2026, 3, 20), actor_user_id=admin.id
    )

    admin_client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    general_client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))

    assert admin_client.get("/api/v1/dashboard").json()["pending_event_review"][
        "pending_events"
    ] == 2
    general_body = general_client.get("/api/v1/dashboard").json()
    assert general_body["pending_event_review"] == {
        "pending_events": 1,
        "companies": [
            {"company_id": str(granted.id), "company_name": None, "pending_events": 1}
        ],
    }


# --- Pending draft review (DoD §9 item 13, F3) -------------------------------


async def test_pending_draft_review_counts_awaiting_current_versions(
    session: AsyncSession,
) -> None:
    """The widget counts the drafts whose **current version** is ``generated``
    or ``under_review``, per company — the set ``app.domain.drafts`` names once.
    A decided lineage is not work, including one whose superseded earlier
    version still says ``generated``, which is why the current version is
    selected in SQL rather than filtered row by row."""
    tenant, _ = await make_tenant(session)
    busy = await make_company(session, tenant)
    settled = await make_company(session, tenant)
    await set_name(session, tenant, busy, name_en="Busy Limited", effective=date(2020, 1, 1))
    await add_draft(session, tenant, busy)
    await add_draft(session, tenant, busy, status="under_review")
    # Regenerated, then invalidated: version 1 stays `generated` on its own row.
    lineage = await add_draft(session, tenant, busy, form_type="nd2a")
    await add_draft(
        session,
        tenant,
        busy,
        form_type="nd2a",
        status="invalidated",
        version_no=2,
        draft_id=lineage,
    )
    awaiting = await add_draft(session, tenant, settled)

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    assert view.pending_draft_review.pending_drafts == 3
    assert [
        (item.company_id, item.company_name, item.pending_drafts)
        for item in view.pending_draft_review.companies
    ] == [
        (busy.id, "Busy Limited", 2),
        (settled.id, None, 1),
    ]
    rows = await session.scalars(
        select(Draft).where(Draft.id == lineage).order_by(Draft.version_no)
    )
    assert [(row.version_no, row.status) for row in rows.all()] == [
        (1, "generated"),
        (2, "invalidated"),
    ]
    # The awaiting lineage's own current version is what the count stands on.
    assert (
        await session.scalar(
            select(Draft.status).where(Draft.id == awaiting, Draft.version_no == 1)
        )
        == "generated"
    )


# --- Service-package "not covered" flags ---


@pytest.fixture
def licensed_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate scenarios run on a licensed tier; the Free denial is explicit."""
    monkeypatch.setattr(settings, "license_tier", "standard")


async def assign_package(
    session: AsyncSession, tenant: Tenant, company: Company, *, services: list[str]
) -> None:
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=services,
        covered_forms=["NAR1", "ND2A"],
    )
    await service_packages.create_assignment(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        package_id=package.id,
        actor_user_id=None,
        coverage_start=date(2026, 1, 1),
        coverage_end=date(2026, 12, 31),
    )


async def test_service_gaps_name_the_gate_reason_per_company_and_type(
    session: AsyncSession, licensed_tier: None
) -> None:
    tenant, _ = await make_tenant(session)
    covered = await make_company(session, tenant)
    uncovered = await make_company(session, tenant)
    await assign_package(
        session, tenant, covered, services=[AutomationType.AI_REPLY_PARSING.value]
    )

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    gaps = {(gap.company_id, gap.automation): gap.reason for gap in view.service_gaps}
    # Per automation type: the package covers exactly one service.
    expected_missing = {
        automation
        for automation in AutomationType
        if automation is not AutomationType.AI_REPLY_PARSING
    }
    assert {
        automation for (company_id, automation) in gaps if company_id == covered.id
    } == expected_missing
    assert all(
        gaps[(covered.id, automation)] is GatingReason.SERVICE_NOT_INCLUDED
        for automation in expected_missing
    )
    # Per company: no assignment at all denies every type on the company leg.
    assert {automation for (company_id, automation) in gaps if company_id == uncovered.id} == set(
        AutomationType
    )
    assert all(
        gaps[(uncovered.id, automation)] is GatingReason.NO_ASSIGNMENT
        for automation in AutomationType
    )
    # Every flag is exactly what the single-company gate would say.
    for (company_id, automation), reason in gaps.items():
        decision = await service_packages.check_automation(
            session,
            tenant_id=tenant.id,
            company_id=company_id,
            automation_type=automation,
            as_of=AS_OF,
        )
        assert decision.allowed is False
        assert decision.reason is reason


async def test_service_gaps_follow_the_free_tier_fail_closed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlicensed deployment denies every automation to every company —
    the dashboard says so rather than implying coverage that will not run."""
    monkeypatch.setattr(settings, "license_tier", "free")
    tenant, _ = await make_tenant(session)
    await make_company(session, tenant)

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    assert len(view.service_gaps) == len(AutomationType)
    assert all(gap.reason is GatingReason.TIER_CAPABILITY_MISSING for gap in view.service_gaps)


async def test_portfolio_gate_matches_the_single_company_gate(
    session: AsyncSession, licensed_tier: None
) -> None:
    tenant, _ = await make_tenant(session)
    assigned = await make_company(session, tenant)
    idle = await make_company(session, tenant)
    await assign_package(
        session, tenant, assigned, services=[AutomationType.DOCUMENT_EXTRACTION.value]
    )

    bulk = await service_packages.gating_decisions_for_companies(
        session, company_ids=[assigned.id, idle.id], as_of=AS_OF
    )

    for company in (assigned, idle):
        for automation in AutomationType:
            single = await service_packages.check_automation(
                session,
                tenant_id=tenant.id,
                company_id=company.id,
                automation_type=automation,
                as_of=AS_OF,
            )
            assert bulk[company.id][automation] == single
    assert bulk[assigned.id][AutomationType.DOCUMENT_EXTRACTION].allowed is True
    assert (
        bulk[assigned.id][AutomationType.AI_REPLY_PARSING].reason
        is GatingReason.SERVICE_NOT_INCLUDED
    )
    assert bulk[idle.id][AutomationType.DOCUMENT_EXTRACTION].reason is GatingReason.NO_ASSIGNMENT


# --- Late-fee exposure (slice L; contract §5.1.4's note) ----------------------


async def test_late_fee_exposure_rides_the_generated_deadlines(
    session: AsyncSession,
) -> None:
    """The fee in force today, composed over exactly the generated deadlines:
    overdue entries escalate by band since the anniversary; the dormant and
    the type-without-a-rule carry none; the dashboard total sums the overdue
    entries in its window only — a stale deadline outside the overdue
    lookback prices on the calendar but never inflates the dashboard."""
    tenant, _ = await make_tenant(session)
    soon = await make_company(session, tenant, anniversary=date(2026, 3, 1))  # due 04-12
    stale = await make_company(session, tenant, anniversary=date(2026, 1, 1))  # due 02-12
    dormant = await make_company(session, tenant, anniversary=date(2026, 3, 1))
    await set_status(session, tenant, dormant, status="dormant", effective=date(2019, 1, 1))
    public = await make_company(
        session, tenant, anniversary=date(2026, 3, 1), type="public"
    )

    as_of = date(2026, 5, 1)
    calendar = await compliance.calendar(
        session,
        tenant_id=tenant.id,
        context=admin_context(),
        date_from=FROM_2026,
        date_to=TO_2026,
        as_of=as_of,
    )
    dashboard = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=as_of
    )

    # The calendar prices both overdue deadlines: the recent one sits in the
    # first late band, the months-old one has escalated past its 3-month edge.
    entries = {entry.company_id: entry for entry in calendar.entries}
    assert entries[soon.id].late_fee_exposure == LateFeeExposure(fee_hkd=870, late=True)
    assert entries[stale.id].late_fee_exposure == LateFeeExposure(fee_hkd=1740, late=True)
    assert dormant.id not in entries
    assert [item.company_id for item in calendar.not_generated] == [public.id]

    # The dashboard total is the sum over the entries its overdue lookback
    # admits: the recent deadline (870) — the stale one is 78 days past, so
    # it prices on the calendar but never inflates the portfolio number.
    assert dashboard.portfolio.late_fee_exposure_hkd == 870
    cards = {card.company_id: card for card in dashboard.company_cards}
    assert cards[soon.id].next_deadline is not None
    assert cards[soon.id].next_deadline.late_fee_exposure == LateFeeExposure(
        fee_hkd=870, late=True
    )
    # The stale company's card points at its next cycle: a future obligation
    # priced today reads the on-time fee, never a late band.
    assert cards[stale.id].next_deadline is not None
    assert cards[stale.id].next_deadline.due_date == date(2027, 2, 12)
    assert cards[stale.id].next_deadline.late_fee_exposure == LateFeeExposure(
        fee_hkd=105, late=False
    )
    assert cards[dormant.id].next_deadline is None
    assert cards[public.id].next_deadline is None


# --- Dashboard aggregation over the same generated set ---


async def test_dashboard_counts_and_lists_agree_with_the_calendar(
    session: AsyncSession, licensed_tier: None
) -> None:
    tenant, _ = await make_tenant(session)
    soon = await make_company(session, tenant, anniversary=date(2026, 3, 1))  # due 04-12
    later = await make_company(session, tenant, anniversary=date(2026, 3, 20))  # due 05-01
    dormant = await make_company(session, tenant, anniversary=date(2026, 3, 25))  # due 05-06
    await set_status(session, tenant, dormant, status="dormant", effective=date(2019, 1, 1))

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    # AS_OF 2026-04-01: due 04-12 is 11 days out (amber); due 05-01 is 30.
    assert view.portfolio.total_companies == 3
    assert view.portfolio.due_within_30_days == 2
    assert view.portfolio.due_within_15_days == 1
    assert view.portfolio.due_within_7_days == 0
    assert view.portfolio.overdue == 0
    assert [entry.company_id for entry in view.upcoming_deadlines] == [soon.id, later.id]
    # Alerts are the amber-and-red subset; only the 11-day deadline qualifies.
    assert [entry.company_id for entry in view.risk_alerts] == [soon.id]
    assert view.risk_alerts[0].risk == "amber"
    assert view.window_from == date(2026, 3, 2)
    assert view.window_to == date(2027, 4, 2)
    cards = {card.company_id: card for card in view.company_cards}
    assert cards[soon.id].next_deadline is not None
    assert cards[soon.id].next_deadline.due_date == date(2026, 4, 12)
    assert cards[later.id].next_deadline.due_date == date(2026, 5, 1)
    # A dormant company has no next deadline and a card that says so.
    assert cards[dormant.id].next_deadline is None
    assert cards[dormant.id].status == "dormant"


async def test_dashboard_overdue_lookback_and_card_horizon(session: AsyncSession) -> None:
    tenant, _ = await make_tenant(session)
    overdue = await make_company(session, tenant, anniversary=date(2026, 3, 1))  # due 04-12
    later = await make_company(session, tenant, anniversary=date(2026, 4, 20))  # due 06-01

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=date(2026, 5, 1)
    )

    # due 2026-04-12 passed 19 days ago — inside the lookback, so overdue.
    assert view.portfolio.overdue == 1
    assert [entry.company_id for entry in view.risk_alerts] == [overdue.id]
    assert view.risk_alerts[0].overdue is True
    assert view.risk_alerts[0].risk == "red"
    # due 2026-06-01 is 31 days out — beyond the alert horizon, but the card
    # still carries the company's next deadline.
    assert view.portfolio.due_within_30_days == 0
    cards = {card.company_id: card for card in view.company_cards}
    assert cards[later.id].next_deadline is not None
    assert cards[later.id].next_deadline.due_date == date(2026, 6, 1)


async def test_company_cards_carry_the_cr_number_for_search(session: AsyncSession) -> None:
    """Scope §2.3 searches by company name **or CR number**, and the card is
    the one portfolio-wide read carrying both. The CR number is optional —
    most companies have none recorded — and stays null rather than blank."""
    tenant, _ = await make_tenant(session)
    with_number = await make_company(session, tenant, cr_number="9876543")
    without = await make_company(session, tenant)

    view = await compliance.dashboard(
        session, tenant_id=tenant.id, context=admin_context(), as_of=AS_OF
    )

    cards = {card.company_id: card for card in view.company_cards}
    assert cards[with_number.id].cr_number == "9876543"
    assert cards[without.id].cr_number is None
