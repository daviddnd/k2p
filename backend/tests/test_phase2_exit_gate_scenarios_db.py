"""Phase 2 exit-gate acceptance walks (opt-in; needs a migrated PostgreSQL).

The Phase 2 plan's exit-gate verification
(``.agents/plans/2026-09-21-phase-2-visibility-and-control-plane.md``) as
three rerunnable demonstrations driven through the real endpoint stack
against the real database:

1. DoD §9 item 3 — the dashboard over a mixed portfolio: companies in each
   alert band, a dormant company, a company whose type has no established
   due-date rule, a prior filing waiting in the ingestion review queue,
   change events and a regenerated draft awaiting staff review, and
   service-package gaps carrying the §5.1 gate's own reasons.
2. DoD §9 item 4 — the compliance calendar: the range read, the 30/15/7-day
   bands, the dormant exclusion, the refusal for a type whose rule is not
   established, and the range boundaries including the 366-day cap.
3. DoD §9 item 23, the half this phase owns — NAR1 due-date generation, the
   dormant exclusion and the regeneration when a company returns to active,
   the alert bands, and the 15-day change-window arithmetic. The arithmetic
   is first demonstrated as the pure rule (the ``ChangeEvent`` entity arrived
   with Phase 4), and CE2's half — the countdown over real change events
   through the routes — is walked at the end of the same item.

Each walk prints a transcript (run with ``-s``) and asserts its outcome.
The two portfolio reads carry the wall clock (``as_of`` is today), so the
deadlines are built relative to ``date.today()`` and every walk reruns on
any day. Each test runs inside a transaction that is rolled back, so the
review database stays clean. Skipped unless ``AICS_TEST_DATABASE_URL``
points at a database migrated to head.
"""

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import service_packages
from app.core.config import settings
from app.domain.compliance import (
    NAR1_FILING_WINDOW_DAYS,
    change_window_countdown,
    change_window_deadline,
    risk_band,
)
from app.domain.service_packages import AutomationType
from app.main import create_app
from tests.pdf_fixtures import acroform_pdf

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

DASHBOARD_URL = "/api/v1/dashboard"
CALENDAR_URL = "/api/v1/compliance/calendar"

# Facts are recorded with an effective date far enough back to be in force on
# every date a walk names (the oldest is the overdue lookback's start).
IN_FORCE_SINCE = date(2019, 1, 1)

AUTOMATION_TYPES = [automation.value for automation in AutomationType]

# The dashboard response's own shape, asserted key by key: the widgets whose
# entities arrive with later phases must be *absent* from the payload, not
# present-and-empty. ``pending_event_review`` landed with Phase 4's CE2 slice,
# ``pending_draft_review`` with Phase 5's F3 (the ChangeEvent and Draft
# entities exist), and ``pending_confirmations`` with the 2026-09-29 reminder
# slice R (contract §5.1.12); the signature and activity widgets are still
# absent.
DASHBOARD_KEYS = {
    "as_of",
    "window_from",
    "window_to",
    "portfolio",
    "upcoming_deadlines",
    "risk_alerts",
    "pending_ingestion",
    "pending_event_review",
    "pending_draft_review",
    "pending_confirmations",
    "company_cards",
    "service_gaps",
    "not_generated",
}
CARD_KEYS = {
    "company_id",
    "br_number",
    "cr_number",
    "company_name",
    "status",
    "next_deadline",
    "pending_ingestion_records",
}
ENTRY_KEYS = {
    "company_id",
    "br_number",
    "company_name",
    "kind",
    "due_date",
    "days_remaining",
    "risk",
    "alert",
    "overdue",
    "late_fee_exposure",
}

# One prior NAR1 for the ingestion widget: enough targets to produce pending
# records, with the header BRN repeated on every page like the real form.
FILING = {
    "fill_2_P.1": "ALPINE TRADING LIMITED 阿爾卑斯貿易有限公司",
    "fill_3_P.1": "ALPINE BIZ",
    "fill_15_P.1": "FLAT 3",
    "fill_16_P.1": "ALPINE TOWER",
    "fill_17_P.1": "1 ALPINE ROAD",
    "fill_18_P.1": "CENTRAL",
    "fill_2_P.2": "ops@alpine.example",
    "fill_5_P.2": "2,048",
    **{f"fill_1_P.{page}": "20260001" for page in range(1, 9)},
}


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes, deterministic URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def make_client(
    session: AsyncSession, *, actor: ActorRef, store: FakeStore | None = None
) -> TestClient:
    """The real app, overridden only at the seams a walk does not exercise.

    The actor is seeded (these walks are about the reads, not about login),
    the RBAC context loads from the database through the real dependency
    chain, and the blob store is the in-memory fake the other scenario
    modules use (the boto3 adapter has its own opt-in MinIO round trip).
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    if store is not None:
        app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def announce(title: str) -> None:
    """The banner of one walk or step, so ``pytest -s`` reads as a demo."""
    print(f"\n=== {title} ===")


def say(text: str) -> None:
    print(f"    {text}")


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    """A committed-shape tenant with one Administrator account."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role="administrator",
    )
    session.add(user)
    await session.flush()
    return tenant, user


def provision(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    """Provision the deployment configuration the entitlement seam reads.

    No activation credential exists in a test run (``conftest`` points the
    credential file at a fresh temporary path), so configuration is the
    source — the same precedence rule the deployment runs on pre-activation.
    """
    monkeypatch.setattr(settings, "license_tier", tier)


def anchor_for_due(due: date) -> date:
    """The anniversary anchor whose NAR1 deadline is ``due``.

    ``nar1_due_date`` uses only the anchor's month and day, so the anchor is
    ``due`` minus the 42-day filing window.
    """
    return due - timedelta(days=NAR1_FILING_WINDOW_DAYS)


def register_company(
    client: TestClient,
    *,
    br_number: str,
    name: str,
    due: date,
    type: str = "private",
    cr_number: str | None = None,
) -> dict[str, Any]:
    """Create one company through the real endpoint and name it.

    ``due`` is the NAR1 deadline the anniversary is chosen to produce; the
    name fact is recorded in force, so the card/calendar can display it.
    """
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": br_number,
            "cr_number": cr_number,
            "date_of_incorporation": date(2010, 1, 1).isoformat(),
            "anniversary_date": anchor_for_due(due).isoformat(),
            "type": type,
        },
    )
    assert response.status_code == 201, response.text
    company = response.json()
    named = client.patch(
        f"/api/v1/companies/{company['id']}",
        json={
            "effective_date": IN_FORCE_SINCE.isoformat(),
            "name": {"name_en": name},
        },
    )
    assert named.status_code == 200, named.text
    say(
        f"company {company['id']} {name!r} ({type}): BRN {company['br_number']}, "
        f"anniversary anchor {company['anniversary_date']} → NAR1 due {due}"
    )
    return company


def set_status(
    client: TestClient, company_id: str, *, status: str, effective: date
) -> dict[str, Any]:
    """Record a company status through the real fact-append write path."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": effective.isoformat(), "status": {"status": status}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def dashboard(client: TestClient) -> dict[str, Any]:
    response = client.get(DASHBOARD_URL)
    assert response.status_code == 200, response.text
    return response.json()


def calendar(client: TestClient, *, date_from: date, date_to: date) -> Response:
    return client.get(
        f"{CALENDAR_URL}?from={date_from.isoformat()}&to={date_to.isoformat()}"
    )


def calendar_body(client: TestClient, *, date_from: date, date_to: date) -> dict[str, Any]:
    response = calendar(client, date_from=date_from, date_to=date_to)
    assert response.status_code == 200, response.text
    return response.json()


def entry_line(entry: dict[str, Any]) -> str:
    """One deadline as a readable transcript line."""
    days = entry["days_remaining"]
    when = f"{days} days out" if days >= 0 else f"{-days} days past due"
    return (
        f"{entry['br_number']} {entry['company_name']}: {entry['kind']} due "
        f"{entry['due_date']} ({when}) → {entry['risk']} / {entry['alert'] or 'no alert'}"
        f"{' OVERDUE' if entry['overdue'] else ''}"
    )


def entry_shape(entry: dict[str, Any]) -> tuple[str, int, str, str | None]:
    return (
        entry["br_number"],
        entry["days_remaining"],
        entry["risk"],
        entry["alert"],
    )


def upload_prior_filing(client: TestClient, company_id: str, data: bytes) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing&form=nar1",
        content=data,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def extract(client: TestClient, company_id: str, document_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/{company_id}/import-batches",
        json={"document_id": document_id, "form": "nar1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_package(client: TestClient, *, name: str, services: list[str]) -> dict[str, Any]:
    response = client.post(
        "/api/v1/service-packages",
        json={
            "name": name,
            "included_services": services,
            "covered_forms": ["NAR1", "ND2A"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def assign_package(
    client: TestClient,
    company_id: str,
    package_id: str,
    *,
    coverage_start: date,
    coverage_end: date,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package_id,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def enter_change_event(client: TestClient, company_id: str, *, effective: date) -> dict[str, Any]:
    """One hand-entered change event through the real route (scope §2.7)."""
    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={
            "type": "other",
            "effective_date": effective.isoformat(),
            "payload": {"summary": "the client reported a change of auditors"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def decide_change_event(client: TestClient, event_id: str, *actions: str) -> None:
    """Walk an event through the §4.1 actions, failing loudly on a refusal."""
    for action in actions:
        response = client.post(
            f"/api/v1/change-events/{event_id}/transitions", json={"action": action}
        )
        assert response.status_code == 200, response.text


def generate_draft(
    client: TestClient, company_id: str, *, target: date, form: str = "nar1"
) -> dict[str, Any]:
    """One draft generation through the real route (the gated automation)."""
    response = client.post(
        f"/api/v1/companies/{company_id}/drafts",
        json={"form_type": form, "target_date": target.isoformat()},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- DoD §9 item 3: the dashboard over a mixed portfolio ---


async def test_item3_dashboard_widgets_over_a_mixed_portfolio(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 2 exit gate — DoD §9 item 3: the dashboard over a mixed portfolio")
    today = date.today()
    tenant, admin = await seed_tenant(session)
    client = make_client(
        session, store=store, actor=ActorRef(user_id=admin.id, tenant_id=tenant.id)
    )
    provision(monkeypatch, "standard")
    say(f"reads run against the real endpoint stack; the clock the payload reports is {today}")

    # Five private companies — one per alert position — plus a dormant company
    # and a public company whose due-date rule is not established.
    alpine = register_company(
        client,
        br_number="20260001",
        cr_number="9000001",
        name="ALPINE TRADING LIMITED",
        due=today + timedelta(days=5),
    )
    beacon = register_company(
        client,
        br_number="20260002",
        name="BEACON SERVICES LIMITED",
        due=today + timedelta(days=12),
    )
    cedar = register_company(
        client,
        br_number="20260003",
        name="CEDAR HOLDINGS LIMITED",
        due=today + timedelta(days=25),
    )
    delta = register_company(
        client,
        br_number="20260004",
        name="DELTA LOGISTICS LIMITED",
        due=today - timedelta(days=8),
    )
    elmwood = register_company(
        client,
        br_number="20260005",
        name="ELMWOOD INVESTMENTS LIMITED",
        due=today + timedelta(days=120),
    )
    fircrest = register_company(
        client,
        br_number="20260006",
        name="FIRCREST PROPERTIES LIMITED",
        due=today + timedelta(days=9),
    )
    public = register_company(
        client,
        br_number="20260007",
        name="GOLDEN BRIDGE HOLDINGS LIMITED",
        due=today + timedelta(days=9),
        type="public",
    )
    for company in (alpine, beacon, cedar, delta, elmwood, public):
        set_status(client, company["id"], status="active", effective=IN_FORCE_SINCE)
    set_status(client, fircrest["id"], status="dormant", effective=IN_FORCE_SINCE)
    say("every company carries its status in force; Fircrest is dormant and must not generate")

    # Service coverage: Alpine's package omits one automation type, Beacon's
    # coverage window has already ended, the rest have no assignment at all.
    full_minus_one = create_package(
        client,
        name="Full service (no confirmation drafting)",
        services=[
            automation.value
            for automation in AutomationType
            if automation is not AutomationType.CONFIRMATION_GENERATION
        ],
    )
    assign_package(
        client,
        alpine["id"],
        full_minus_one["id"],
        coverage_start=today - timedelta(days=365),
        coverage_end=today + timedelta(days=365),
    )
    lapsed = create_package(
        client, name="Retainer that ended", services=list(AUTOMATION_TYPES)
    )
    assign_package(
        client,
        beacon["id"],
        lapsed["id"],
        coverage_start=today - timedelta(days=365),
        coverage_end=today - timedelta(days=1),
    )
    say(
        "Alpine covered for everything but confirmation_generation; Beacon's retainer "
        "expired yesterday; the rest have no assignment"
    )

    document = upload_prior_filing(client, alpine["id"], acroform_pdf(FILING))
    batch = extract(client, alpine["id"], document["id"])
    assert {record["status"] for record in batch["records"]} == {"pending"}
    pending_records = len(batch["records"])
    say(
        f"prior NAR1 read into batch {batch['id']} ({batch['shape']}): "
        f"{pending_records} proposals, all pending staff review"
    )

    # The pending-event-review widget (CE2): change events on the real route,
    # one Cedar event already decided so the widget's number is work waiting,
    # not history.
    enter_change_event(client, alpine["id"], effective=today - timedelta(days=3))
    enter_change_event(client, alpine["id"], effective=today - timedelta(days=1))
    enter_change_event(client, beacon["id"], effective=today - timedelta(days=2))
    settled_event = enter_change_event(client, cedar["id"], effective=today - timedelta(days=2))
    decide_change_event(
        client, settled_event["id"], "mark_under_review", "mark_reviewed", "confirm"
    )
    say(
        "three change events entered by hand (two on Alpine, one on Beacon) and Cedar's "
        "walked to confirmed: the widget must count the three awaiting staff review"
    )

    # The pending-draft-review widget (F3): Alpine's package carries
    # automated_prefill, so generation is allowed — and a second generation is
    # a new version of the same lineage, which the widget counts once.
    first_draft = generate_draft(client, alpine["id"], target=today)
    second_draft = generate_draft(client, alpine["id"], target=today)
    assert first_draft["id"] == second_draft["id"]
    assert (first_draft["version_no"], second_draft["version_no"]) == (1, 2)
    assert second_draft["status"] == "generated"
    say(
        f"POST /api/v1/companies/{alpine['id']}/drafts → 201 twice: one NAR1 lineage at "
        "version 2 — the widget's 'awaiting review' is the lineage's current version, "
        "not its rows"
    )

    body = dashboard(client)
    assert set(body) == DASHBOARD_KEYS
    say(f"GET /api/v1/dashboard → 200; the payload's keys are exactly {sorted(body)}")
    assert body["as_of"] == today.isoformat()
    assert body["window_from"] == (today - timedelta(days=30)).isoformat()
    assert body["window_to"] == (today + timedelta(days=366)).isoformat()
    say(
        f"the window it was computed over: {body['window_from']} .. {body['window_to']} "
        "(a 30-day overdue lookback, a 366-day card horizon)"
    )

    assert body["portfolio"] == {
        "total_companies": 7,
        "due_within_30_days": 3,
        "due_within_15_days": 2,
        "due_within_7_days": 1,
        "overdue": 1,
        "late_fee_exposure_hkd": 870,
    }
    say(
        "portfolio overview: 7 companies; due within 30/15/7 days = 3/2/1; overdue = 1 "
        "(the dormant and the rule-less company contribute no deadline); late fees if "
        "filed today = HK$870 (the one overdue company, first late band)"
    )

    assert [entry_shape(entry) for entry in body["upcoming_deadlines"]] == [
        ("20260001", 5, "red", "alert_7d"),
        ("20260002", 12, "amber", "alert_15d"),
        ("20260003", 25, "green", "alert_30d"),
    ]
    for entry in body["upcoming_deadlines"]:
        assert set(entry) == ENTRY_KEYS
        assert entry["late_fee_exposure"] == {"fee_hkd": 105, "late": False}
        say(f"upcoming  {entry_line(entry)}")

    assert [
        (entry["br_number"], entry["days_remaining"], entry["risk"], entry["overdue"])
        for entry in body["risk_alerts"]
    ] == [
        ("20260004", -8, "red", True),
        ("20260001", 5, "red", False),
        ("20260002", 12, "amber", False),
    ]
    assert body["risk_alerts"][0]["late_fee_exposure"] == {"fee_hkd": 870, "late": True}
    for entry in body["risk_alerts"]:
        say(f"risk alert {entry_line(entry)}")

    assert body["pending_ingestion"]["pending_records"] == pending_records
    assert body["pending_ingestion"]["companies"] == [
        {
            "company_id": alpine["id"],
            "company_name": "ALPINE TRADING LIMITED",
            "pending_records": pending_records,
        }
    ]
    say(
        f"pending data-ingestion review: {pending_records} records on Alpine — the "
        "extraction slice's proposals, counted per company and never loaded"
    )

    assert body["pending_event_review"] == {
        "pending_events": 3,
        "companies": [
            {
                "company_id": alpine["id"],
                "company_name": "ALPINE TRADING LIMITED",
                "pending_events": 2,
            },
            {
                "company_id": beacon["id"],
                "company_name": "BEACON SERVICES LIMITED",
                "pending_events": 1,
            },
        ],
    }
    say(
        "pending event review: 3 events awaiting staff review (Alpine 2, Beacon 1) — "
        "Cedar's confirmed event is not work, and the widget counts the same set "
        "GET /change-events queues"
    )

    assert body["pending_draft_review"] == {
        "pending_drafts": 1,
        "companies": [
            {
                "company_id": alpine["id"],
                "company_name": "ALPINE TRADING LIMITED",
                "pending_drafts": 1,
            }
        ],
    }
    say(
        "pending draft review: Alpine's regenerated NAR1 is one draft awaiting staff "
        "review — two version rows, one lineage, so a decided draft could not be "
        "counted off an earlier row"
    )

    cards = {card["company_id"]: card for card in body["company_cards"]}
    assert len(cards) == 7
    for card in body["company_cards"]:
        assert set(card) == CARD_KEYS
    assert [card["br_number"] for card in body["company_cards"]] == [
        f"2026000{index}" for index in range(1, 8)
    ]
    assert cards[alpine["id"]]["cr_number"] == "9000001"
    assert cards[alpine["id"]]["status"] == "active"
    assert cards[alpine["id"]]["next_deadline"]["due_date"] == (
        today + timedelta(days=5)
    ).isoformat()
    assert cards[alpine["id"]]["pending_ingestion_records"] == pending_records
    assert cards[elmwood["id"]]["next_deadline"]["due_date"] == (
        today + timedelta(days=120)
    ).isoformat()
    assert cards[fircrest["id"]]["status"] == "dormant"
    assert cards[fircrest["id"]]["next_deadline"] is None
    assert cards[public["id"]]["next_deadline"] is None
    for card in body["company_cards"]:
        deadline = (
            "no next deadline"
            if card["next_deadline"] is None
            else f"next NAR1 {card['next_deadline']['due_date']}"
        )
        say(
            f"card {card['br_number']} {card['company_name']}: status "
            f"{card['status']}, {deadline}, {card['pending_ingestion_records']} pending "
            f"record(s), CR {card['cr_number'] or '—'}"
        )
    say(
        "the card horizon reaches months past the dashboard window: Elmwood's card carries "
        "its next deadline while no count or list mentions it"
    )

    assert body["not_generated"] == [
        {
            "company_id": public["id"],
            "company_type": "public",
            "reason": "nar1_rule_not_established",
        }
    ]
    say(
        "not_generated: the public company — the domain refuses an anniversary-based date "
        "rather than guessing one, and the card says no deadline rather than nothing"
    )

    gaps = {
        (gap["company_id"], gap["automation"]): gap["reason"]
        for gap in body["service_gaps"]
    }
    assert {
        automation for (company_id, automation) in gaps if company_id == alpine["id"]
    } == {"confirmation_generation"}
    assert gaps[(alpine["id"], "confirmation_generation")] == "service_not_included"
    assert all(
        gaps[(beacon["id"], automation)] == "out_of_coverage"
        for automation in AUTOMATION_TYPES
    )
    assert all(
        gaps[(cedar["id"], automation)] == "no_assignment" for automation in AUTOMATION_TYPES
    )
    # Every flag is exactly the single-company gate's decision, and every
    # allowed pair is one the gate allows — read in both directions.
    for card in body["company_cards"]:
        for automation in AutomationType:
            decision = await service_packages.check_automation(
                session,
                tenant_id=tenant.id,
                company_id=UUID(card["company_id"]),
                automation_type=automation,
                as_of=today,
            )
            key = (card["company_id"], automation.value)
            if decision.allowed:
                assert key not in gaps
            else:
                assert gaps[key] == decision.reason
    say(
        f"{len(gaps)} 'service not covered' flags, each one exactly what the single "
        "automation gate decides for that company and type (and every pair the gate allows "
        "is unflagged):"
    )
    for company, group in (
        ("Alpine", alpine),
        ("Beacon", beacon),
        ("Cedar", cedar),
    ):
        reasons = sorted(
            {reason for (company_id, _), reason in gaps.items() if company_id == group["id"]}
        )
        say(f"  {company}: {', '.join(reasons)}")
    say(
        "Alpine's document_extraction is covered — which is why the extraction trigger was "
        "allowed — while the automation its package omits is named, not implied"
    )

    provision(monkeypatch, "free")
    unlicensed = dashboard(client)
    assert len(unlicensed["service_gaps"]) == 7 * len(AUTOMATION_TYPES)
    assert {gap["reason"] for gap in unlicensed["service_gaps"]} == {
        "tier_capability_missing"
    }
    assert unlicensed["portfolio"] == body["portfolio"]
    # Reads are ungated in every tier (ADR-0012), so the widgets stand as
    # they were while every automated action is refused.
    assert unlicensed["pending_draft_review"] == body["pending_draft_review"]
    assert unlicensed["pending_event_review"] == body["pending_event_review"]
    say(
        "the same portfolio on a Free deployment: every company and type flagged "
        "'tier_capability_missing' (fail closed) while the reads still work"
    )
    provision(monkeypatch, "standard")


# --- DoD §9 item 4: the compliance calendar ---


async def test_item4_compliance_calendar_matches_the_statutory_rules(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 2 exit gate — DoD §9 item 4: the compliance calendar")
    today = date.today()
    tenant, admin = await seed_tenant(session)
    client = make_client(session, actor=ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    soon = register_company(
        client,
        br_number="20261001",
        name="HARBOURLIGHT TRADING LIMITED",
        due=today + timedelta(days=10),
    )
    boundary = register_company(
        client,
        br_number="20261002",
        name="ISLANDCREST SERVICES LIMITED",
        due=today + timedelta(days=30),
    )
    dormant = register_company(
        client,
        br_number="20261003",
        name="JUNIPER PROPERTIES LIMITED",
        due=today + timedelta(days=12),
    )
    guarantee = register_company(
        client,
        br_number="20261004",
        name="KOWLOON BRIDGE FOUNDATION LIMITED",
        due=today + timedelta(days=12),
        type="company_limited_by_guarantee",
    )
    for company in (soon, boundary):
        set_status(client, company["id"], status="active", effective=IN_FORCE_SINCE)
    set_status(client, dormant["id"], status="dormant", effective=IN_FORCE_SINCE)

    date_from, date_to = today, today + timedelta(days=30)
    body = calendar_body(client, date_from=date_from, date_to=date_to)
    assert body["from"] == date_from.isoformat() and body["to"] == date_to.isoformat()
    assert body["as_of"] == today.isoformat()
    say(
        f"GET {CALENDAR_URL}?from={date_from}&to={date_to} → 200; the range and its "
        "as-of clock echo back"
    )
    assert [entry_shape(entry) for entry in body["entries"]] == [
        ("20261001", 10, "amber", "alert_15d"),
        ("20261002", 30, "green", "alert_30d"),
    ]
    for entry in body["entries"]:
        assert set(entry) == ENTRY_KEYS
        assert entry["late_fee_exposure"] == {"fee_hkd": 105, "late": False}
        say(f"in range {entry_line(entry)}")
    dormant_ids = {entry["company_id"] for entry in body["entries"]}
    assert dormant["id"] not in dormant_ids
    say(
        "Juniper (dormant since 2019) is excluded: no deadline is generated for a dormant "
        "company, so it is absent rather than shown as risk-free"
    )
    assert body["not_generated"] == [
        {
            "company_id": guarantee["id"],
            "company_type": "company_limited_by_guarantee",
            "reason": "nar1_rule_not_established",
        }
    ]
    say(
        "not_generated: the guarantee company — CR dates its return from the return date "
        "(PAM 8B), which this product does not hold, so the read refuses instead of guessing"
    )

    narrow = calendar_body(client, date_from=date_from, date_to=date_to - timedelta(days=1))
    assert [entry["br_number"] for entry in narrow["entries"]] == ["20261001"]
    say(
        f"range to {date_to - timedelta(days=1)}: the 30-day deadline is out — both "
        "boundaries are inclusive and exact, never rounded to a whole month"
    )
    later = calendar_body(client, date_from=date_from + timedelta(days=11), date_to=date_to)
    assert [entry["br_number"] for entry in later["entries"]] == ["20261002"]
    say(
        f"range from {date_from + timedelta(days=11)}: the 10-day deadline is out, the "
        "30-day one still in"
    )

    inverted = calendar(client, date_from=date_to, date_to=date_from)
    assert inverted.status_code == 422, inverted.text
    say(f"inverted range → 422 {inverted.json()['detail']!r}")

    at_cap = calendar(client, date_from=date_from, date_to=date_from + timedelta(days=365))
    assert at_cap.status_code == 200, at_cap.text
    assert [entry["br_number"] for entry in at_cap.json()["entries"]] == [
        "20261001",
        "20261002",
    ]
    assert at_cap.json()["not_generated"][0]["reason"] == "nar1_rule_not_established"
    say(
        f"the whole annual cycle (366 days, {date_from} .. "
        f"{date_from + timedelta(days=365)}) → 200 with both deadlines"
    )
    over_cap = calendar(client, date_from=date_from, date_to=date_from + timedelta(days=366))
    assert over_cap.status_code == 422, over_cap.text
    assert over_cap.json()["detail"] == "calendar range is limited to 366 days"
    say(
        f"one day wider → 422 {over_cap.json()['detail']!r}: the read refuses a window it "
        "cannot serve rather than silently truncating it"
    )


# --- DoD §9 item 23 (the half this phase owns) ---


async def test_item23_due_dates_dormant_regeneration_and_the_15_day_window(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    announce("Phase 2 exit gate — DoD §9 item 23: due dates, dormancy and the 15-day window")
    today = date.today()
    tenant, admin = await seed_tenant(session)
    client = make_client(session, actor=ActorRef(user_id=admin.id, tenant_id=tenant.id))
    provision(monkeypatch, "standard")

    companies = {}
    for label, br_number, name, offset in (
        ("7-day", "20262007", "PINEWOOD TRADING LIMITED", 6),
        ("15-day", "20262015", "QUARRY BAY SERVICES LIMITED", 14),
        ("30-day", "20262030", "RIVERSTONE HOLDINGS LIMITED", 29),
        ("overdue", "20262000", "SUMMIT LOGISTICS LIMITED", -6),
    ):
        companies[label] = register_company(
            client,
            br_number=br_number,
            name=name,
            due=today + timedelta(days=offset),
        )
        set_status(
            client, companies[label]["id"], status="active", effective=IN_FORCE_SINCE
        )
    dormant = register_company(
        client,
        br_number="20262018",
        name="TANGLEWOOD PROPERTIES LIMITED",
        due=today + timedelta(days=18),
    )
    set_status(client, dormant["id"], status="dormant", effective=IN_FORCE_SINCE)
    say(
        "each anniversary anchor is its due date minus the statutory "
        f"{NAR1_FILING_WINDOW_DAYS}-day window — the date the calendar must generate"
    )

    date_from, date_to = today - timedelta(days=30), today + timedelta(days=40)
    body = calendar_body(client, date_from=date_from, date_to=date_to)
    assert [entry_shape(entry) for entry in body["entries"]] == [
        ("20262000", -6, "red", "alert_7d"),
        ("20262007", 6, "red", "alert_7d"),
        ("20262015", 14, "amber", "alert_15d"),
        ("20262030", 29, "green", "alert_30d"),
    ]
    for entry in body["entries"]:
        say(f"band {entry_line(entry)}")
    assert {entry["company_id"] for entry in body["entries"]} == {
        companies[label]["id"] for label in companies
    }
    say(
        "the past-due deadline is red and flagged overdue (the lookback start is the "
        "range's own left edge); the dormant company produces nothing"
    )

    # The exact thresholds, computed directly: the wall clock moves between
    # runs, so the boundary days cannot be pinned through the endpoints.
    for days_remaining, alert, level, overdue in (
        (31, None, "green", False),
        (30, "alert_30d", "green", False),
        (15, "alert_15d", "amber", False),
        (7, "alert_7d", "red", False),
        (0, "alert_7d", "red", False),
        (-1, "alert_7d", "red", True),
    ):
        band = risk_band(days_remaining)
        assert (band.alert, band.level, band.overdue) == (alert, level, overdue)
        say(
            f"risk_band({days_remaining:+d}) → {band.level} / {band.alert or 'no alert'}"
            f"{' / overdue' if band.overdue else ''}"
        )
    say(
        "on the day exactly 30 (15, 7) days remain, that alert is in force: the thresholds "
        "are inclusive, and the alert vocabulary is not the colour vocabulary"
    )

    before = calendar_body(client, date_from=date_from, date_to=date_to)
    assert dormant["id"] not in {entry["company_id"] for entry in before["entries"]}
    say("Tanglewood is dormant: its 18-day deadline is not generated")

    reactivated = set_status(client, dormant["id"], status="active", effective=today)
    assert reactivated["status"]["status"] == "active"
    say(
        f"PATCH /api/v1/companies/{dormant['id']} → 200; the status fact appended effective "
        f"{today} through the single master-data write path"
    )
    after = calendar_body(client, date_from=date_from, date_to=date_to)
    assert [entry_shape(entry) for entry in after["entries"]] == [
        ("20262000", -6, "red", "alert_7d"),
        ("20262007", 6, "red", "alert_7d"),
        ("20262015", 14, "amber", "alert_15d"),
        ("20262018", 18, "green", "alert_30d"),
        ("20262030", 29, "green", "alert_30d"),
    ]
    regenerated = next(
        entry for entry in after["entries"] if entry["company_id"] == dormant["id"]
    )
    say(f"regenerated {entry_line(regenerated)}")
    say(
        "the status is resolved as of each due date, so returning to active regenerates the "
        "deadline with no other write"
    )

    effective = today - timedelta(days=14)
    window = change_window_countdown(effective, as_of=today)
    assert window.deadline == change_window_deadline(effective)
    assert window.deadline == effective + timedelta(days=15)
    assert (window.days_remaining, window.is_late) == (1, False)
    say(
        f"a change effective {effective}: the 15-day window closes {window.deadline}, "
        f"{window.days_remaining} day(s) left, not yet late"
    )
    on_the_deadline = change_window_countdown(
        effective, as_of=change_window_deadline(effective)
    )
    assert (on_the_deadline.days_remaining, on_the_deadline.is_late) == (0, False)
    day_after = change_window_countdown(
        effective, as_of=change_window_deadline(effective) + timedelta(days=1)
    )
    assert (day_after.days_remaining, day_after.is_late) == (-1, True)
    say(
        "countdown on the deadline day itself → 0 days, not late; the next day → late: the "
        "arithmetic the change-event reads compute"
    )

    # Item 23's second half (CE2): the same countdown over real change events,
    # through the real routes — approaching, on the deadline day, and late.
    approaching_event = enter_change_event(
        client, companies["7-day"]["id"], effective=today - timedelta(days=7)
    )
    due_event = enter_change_event(
        client, companies["7-day"]["id"], effective=today - timedelta(days=15)
    )
    late_event = enter_change_event(
        client, companies["7-day"]["id"], effective=today - timedelta(days=16)
    )
    assert approaching_event["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=7)).isoformat(),
        "days_remaining": 8,
        "is_late": False,
        "risk": "amber",
        "alert": "alert_15d",
    }
    assert due_event["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=15)).isoformat(),
        "days_remaining": 0,
        "is_late": False,
        "risk": "red",
        "alert": "alert_7d",
    }
    assert late_event["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=16)).isoformat(),
        "days_remaining": -1,
        "is_late": True,
        "risk": "red",
        "alert": "alert_7d",
    }
    for event in (late_event, due_event, approaching_event):
        window = event["change_window"]
        say(
            f"change effective {event['effective_date']}: window closes {window['deadline']} "
            f"({window['days_remaining']:+d} days)"
            f"{' LATE' if window['is_late'] else ''} → {window['risk']} / {window['alert']}"
        )
    queued = client.get("/api/v1/change-events")
    assert queued.status_code == 200, queued.text
    assert [item["id"] for item in queued.json()] == [
        late_event["id"],
        due_event["id"],
        approaching_event["id"],
    ]
    say(
        "GET /api/v1/change-events queues the three soonest deadline first, each item "
        "carrying the same window its own read computes — the countdown over real events"
    )
