"""API-layer tests for the compliance-calendar and dashboard endpoints
(scope §2.3/§2.4; contract §5.1 Dashboard and Calendar rows).

These run without a database: the auth seam, the RBAC context, and the
session are overridden, and the application services are monkeypatched, so
only fail-closed authentication, the inactive-account gate, argument
validation (the calendar's range), and the typed response shapes are
exercised here. Scoping, dormant exclusion, status resolution and the gate
flags are database scenarios in ``test_compliance_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import compliance
from app.domain.compliance import ComplianceEventKind, LateFeeExposure, RiskLevel
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import AutomationType, GatingReason
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
COMPANY_ID = uuid4()
CALENDAR_URL = "/api/v1/compliance/calendar"
DASHBOARD_URL = "/api/v1/dashboard"

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),
)
INACTIVE_GENERAL_CONTEXT = ActorContext(
    is_active=False,
    role=Role.GENERAL,
    permissions=frozenset(),
    company_ids=frozenset({COMPANY_ID}),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

ROUTES = [
    ("GET", f"{CALENDAR_URL}?from=2026-01-01&to=2026-12-31"),
    ("GET", DASHBOARD_URL),
]


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def entry(**overrides: object) -> compliance.CalendarEntry:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "br_number": "12345678",
        "company_name": "Acme Limited",
        "kind": ComplianceEventKind.NAR1_DUE,
        "anniversary_year": 2026,
        "due_date": date(2026, 10, 15),
        "days_remaining": 24,
        "risk": RiskLevel.GREEN,
        "alert": ComplianceEventKind.ALERT_30D,
        "overdue": False,
        "late_fee_exposure": LateFeeExposure(fee_hkd=105, late=False),
    }
    values.update(overrides)
    return compliance.CalendarEntry(**values)  # type: ignore[arg-type]


def empty_dashboard(**overrides: object) -> compliance.Dashboard:
    values: dict[str, object] = {
        "as_of": date(2026, 9, 21),
        "window_from": date(2026, 8, 22),
        "window_to": date(2027, 9, 22),
        "portfolio": compliance.PortfolioOverview(0, 0, 0, 0, 0, 0),
        "upcoming_deadlines": [],
        "risk_alerts": [],
        "pending_ingestion": compliance.PendingIngestion(0, []),
        "pending_event_review": compliance.PendingEventReview(0, []),
        "pending_draft_review": compliance.PendingDraftReview(0, []),
        "pending_confirmations": compliance.PendingConfirmations([]),
        "company_cards": [],
        "service_gaps": [],
        "not_generated": [],
    }
    values.update(overrides)
    return compliance.Dashboard(**values)  # type: ignore[arg-type]


# --- Fail closed; portfolio reads need an active account ---


def test_unauthenticated_fails_closed_on_both_routes() -> None:
    client = build_client()
    for method, url in ROUTES:
        assert client.request(method, url).status_code == 401, url


@pytest.mark.parametrize("context", [INACTIVE_ADMIN_CONTEXT, INACTIVE_GENERAL_CONTEXT])
def test_inactive_account_is_forbidden(context: ActorContext) -> None:
    client = build_client(actor=ADMIN, context=context)
    for method, url in ROUTES:
        assert client.request(method, url).status_code == 403, url


def test_general_account_may_read_and_its_context_reaches_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role is not the gate; the application layer scopes by grant."""
    seen: list[ActorContext] = []

    async def fake_dashboard(session: object, **kwargs: object) -> compliance.Dashboard:
        seen.append(kwargs["context"])  # type: ignore[arg-type]
        return empty_dashboard()

    monkeypatch.setattr(compliance, "dashboard", fake_dashboard)
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)

    assert client.get(DASHBOARD_URL).status_code == 200
    assert seen == [GENERAL_CONTEXT]


# --- Calendar: range validation and response shape ---


def test_calendar_requires_a_range_and_valid_dates() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(CALENDAR_URL).status_code == 422  # both params required
    assert client.get(f"{CALENDAR_URL}?from=2026-01-01").status_code == 422
    assert client.get(f"{CALENDAR_URL}?from=not-a-date&to=2026-01-02").status_code == 422


def test_calendar_range_errors_map_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inverted(session: object, **kwargs: object) -> compliance.Calendar:
        raise compliance.CalendarRangeError("to must not be before from")

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(compliance, "calendar", inverted)
    response = client.get(f"{CALENDAR_URL}?from=2026-12-31&to=2026-01-01")
    assert response.status_code == 422
    assert "before" in response.json()["detail"]


def test_calendar_rejects_a_range_wider_than_the_read_allows() -> None:
    """The cap is enforced by the real service, not monkeypatched: a wider
    range is refused, never truncated."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    wide = compliance.MAX_CALENDAR_RANGE_DAYS + 1
    response = client.get(
        f"{CALENDAR_URL}?from=2026-01-01&to={date(2026, 1, 1) + timedelta(days=wide - 1)}"
    )
    assert response.status_code == 422
    assert str(compliance.MAX_CALENDAR_RANGE_DAYS) in response.json()["detail"]


def test_calendar_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_calendar(session: object, **kwargs: object) -> compliance.Calendar:
        assert kwargs["date_from"] == date(2026, 9, 1)
        assert kwargs["date_to"] == date(2026, 12, 31)
        return compliance.Calendar(
            date_from=date(2026, 9, 1),
            date_to=date(2026, 12, 31),
            as_of=date(2026, 9, 21),
            entries=[
                entry(),
                entry(
                    due_date=date(2026, 9, 22),
                    days_remaining=-1,
                    risk=RiskLevel.RED,
                    alert=ComplianceEventKind.ALERT_7D,
                    overdue=True,
                    late_fee_exposure=LateFeeExposure(fee_hkd=870, late=True),
                ),
            ],
            not_generated=[
                compliance.NotGenerated(
                    company_id=COMPANY_ID,
                    company_type="public",
                    reason=compliance.NAR1_RULE_NOT_ESTABLISHED,
                )
            ],
        )

    monkeypatch.setattr(compliance, "calendar", fake_calendar)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{CALENDAR_URL}?from=2026-09-01&to=2026-12-31")

    assert response.status_code == 200, response.text
    body = response.json()
    # The range echoes under the request's own names.
    assert body["from"] == "2026-09-01"
    assert body["to"] == "2026-12-31"
    assert body["as_of"] == "2026-09-21"
    first = body["entries"][0]
    assert first == {
        "company_id": str(COMPANY_ID),
        "br_number": "12345678",
        "company_name": "Acme Limited",
        "kind": "nar1_due",
        "due_date": "2026-10-15",
        "days_remaining": 24,
        "risk": "green",
        "alert": "alert_30d",
        "overdue": False,
        "late_fee_exposure": {"fee_hkd": 105, "late": False},
    }
    assert body["entries"][1]["overdue"] is True
    assert body["entries"][1]["late_fee_exposure"]["late"] is True
    assert body["not_generated"] == [
        {
            "company_id": str(COMPANY_ID),
            "company_type": "public",
            "reason": "nar1_rule_not_established",
        }
    ]


# --- Dashboard: response shape ---


def test_dashboard_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dashboard(session: object, **kwargs: object) -> compliance.Dashboard:
        return empty_dashboard(
            portfolio=compliance.PortfolioOverview(
                total_companies=3,
                due_within_30_days=2,
                due_within_15_days=1,
                due_within_7_days=1,
                overdue=1,
                late_fee_exposure_hkd=870,
            ),
            upcoming_deadlines=[entry()],
            risk_alerts=[entry(days_remaining=3, risk=RiskLevel.RED)],
            pending_ingestion=compliance.PendingIngestion(
                pending_records=5,
                companies=[
                    compliance.PendingIngestionCompany(
                        company_id=COMPANY_ID, company_name="Acme Limited", pending_records=5
                    )
                ],
            ),
            pending_event_review=compliance.PendingEventReview(
                pending_events=3,
                companies=[
                    compliance.PendingEventReviewCompany(
                        company_id=COMPANY_ID, company_name="Acme Limited", pending_events=3
                    )
                ],
            ),
            pending_draft_review=compliance.PendingDraftReview(
                pending_drafts=2,
                companies=[
                    compliance.PendingDraftReviewCompany(
                        company_id=COMPANY_ID, company_name="Acme Limited", pending_drafts=2
                    )
                ],
            ),
            company_cards=[
                compliance.CompanyCard(
                    company_id=COMPANY_ID,
                    br_number="12345678",
                    cr_number="9876543",
                    company_name="Acme Limited",
                    status="active",
                    next_deadline=entry(),
                    pending_ingestion_records=5,
                ),
                compliance.CompanyCard(
                    company_id=uuid4(),
                    br_number="87654321",
                    cr_number=None,
                    company_name="No CR Number Limited",
                    status=None,
                    next_deadline=None,
                    pending_ingestion_records=0,
                ),
            ],
            service_gaps=[
                compliance.ServiceGap(
                    company_id=COMPANY_ID,
                    automation=AutomationType.AI_REPLY_PARSING,
                    reason=GatingReason.SERVICE_NOT_INCLUDED,
                )
            ],
            not_generated=[
                compliance.NotGenerated(
                    company_id=COMPANY_ID,
                    company_type="public",
                    reason=compliance.NAR1_RULE_NOT_ESTABLISHED,
                )
            ],
        )

    monkeypatch.setattr(compliance, "dashboard", fake_dashboard)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(DASHBOARD_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == "2026-09-21"
    assert body["window_from"] == "2026-08-22"
    assert body["window_to"] == "2027-09-22"
    assert body["portfolio"] == {
        "total_companies": 3,
        "due_within_30_days": 2,
        "due_within_15_days": 1,
        "due_within_7_days": 1,
        "overdue": 1,
        "late_fee_exposure_hkd": 870,
    }
    assert body["upcoming_deadlines"][0]["kind"] == "nar1_due"
    assert body["risk_alerts"][0]["risk"] == "red"
    assert body["pending_ingestion"]["pending_records"] == 5
    assert body["pending_ingestion"]["companies"][0]["company_name"] == "Acme Limited"
    assert body["pending_event_review"] == {
        "pending_events": 3,
        "companies": [
            {"company_id": str(COMPANY_ID), "company_name": "Acme Limited", "pending_events": 3}
        ],
    }
    assert body["pending_draft_review"] == {
        "pending_drafts": 2,
        "companies": [
            {"company_id": str(COMPANY_ID), "company_name": "Acme Limited", "pending_drafts": 2}
        ],
    }
    card = body["company_cards"][0]
    assert card["status"] == "active"
    assert card["cr_number"] == "9876543"
    assert card["next_deadline"]["due_date"] == "2026-10-15"
    assert card["next_deadline"]["late_fee_exposure"] == {"fee_hkd": 105, "late": False}
    assert card["pending_ingestion_records"] == 5
    # CR numbers are optional; a card without one carries null, not a guess.
    assert body["company_cards"][1]["cr_number"] is None
    assert body["service_gaps"] == [
        {
            "company_id": str(COMPANY_ID),
            "automation": "ai_reply_parsing",
            "reason": "service_not_included",
        }
    ]
    assert body["not_generated"][0]["reason"] == "nar1_rule_not_established"
    assert body["not_generated"][0]["company_type"] == "public"
