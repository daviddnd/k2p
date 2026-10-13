"""API-layer tests for the service-renewal routes and the §5.1.9 panel.

These run without a database or object store: the auth seam, RBAC context,
session, and storage are overridden and the application service is
monkeypatched, so only fail-closed authentication, the grant-gated
authorization check (``require_company_access``), request validation and the
typed-error → HTTP mappings are exercised. The record itself — the unique
index, the CHECK, the locked read, the panel's derivation — lives in
``test_service_renewals_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.service_renewals import ServiceRenewal
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import service_renewals
from app.application.company_master import CompanyNotFoundError
from app.domain.milestones import MilestoneKind, MilestoneValidationError
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_renewals import (
    MilestoneBlockReason,
    MilestoneState,
    RenewalAction,
    RenewalStatus,
    RenewalTransitionError,
)
from app.main import create_app

COMPANY_ID = uuid4()
RENEWAL_ID = uuid4()
YEAR = 2027
ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)
INACTIVE = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

LIST_URL = f"/api/v1/companies/{COMPANY_ID}/service-renewals"
DECISION_URL = f"{LIST_URL}/{YEAR}/decision"
PAYMENT_URL = f"{LIST_URL}/{YEAR}/payment"
CORRECTION_URL = f"{LIST_URL}/{YEAR}/correction"
PANEL_URL = "/api/v1/client-actions"


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def stored_row(status: str = RenewalStatus.PENDING.value) -> ServiceRenewal:
    """One renewal row as the service returns it (never persisted here)."""
    decided = status != RenewalStatus.PENDING.value
    paid = status == RenewalStatus.PAYMENT_RECEIVED.value
    return ServiceRenewal(
        id=RENEWAL_ID,
        tenant_id=ADMIN.tenant_id,
        company_id=COMPANY_ID,
        anniversary_year=YEAR,
        status=status,
        client_decided_at=datetime(2026, 11, 30, 8, 0, tzinfo=UTC) if decided else None,
        payment_received_at=datetime(2026, 12, 4, 8, 0, tzinfo=UTC) if paid else None,
        decided_by=ADMIN.user_id if decided else None,
        payment_by=ADMIN.user_id if paid else None,
        note="client called on the 30th",
        created_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
    )


def _raise(error: Exception):
    async def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


def _return(row: ServiceRenewal):
    async def fake(*args: object, **kwargs: object) -> ServiceRenewal:
        return row

    return fake


def _record_calls(store: dict[str, object], row: ServiceRenewal | None = None):
    async def fake(*args: object, **kwargs: object) -> ServiceRenewal:
        store.update(kwargs)
        return stored_row() if row is None else row

    return fake


# --- Fail closed on every route ---


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()
    assert client.get(LIST_URL).status_code == 401
    assert client.post(LIST_URL, json={"anniversary_year": YEAR}).status_code == 401
    assert client.post(DECISION_URL, json={"decision": "confirmed"}).status_code == 401
    assert client.post(PAYMENT_URL, json={}).status_code == 401
    assert (
        client.post(CORRECTION_URL, json={"decision": "declined", "note": "no"}).status_code == 401
    )
    assert client.get(PANEL_URL).status_code == 401


def test_general_without_the_company_grant_is_forbidden() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITHOUT_GRANT
    )
    assert client.get(LIST_URL).status_code == 403
    assert client.post(LIST_URL, json={"anniversary_year": YEAR}).status_code == 403
    assert client.post(DECISION_URL, json={"decision": "confirmed"}).status_code == 403
    assert client.post(PAYMENT_URL, json={}).status_code == 403
    assert (
        client.post(CORRECTION_URL, json={"decision": "confirmed", "note": "typo"}).status_code
        == 403
    )


def test_a_general_with_the_grant_may_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording a renewal is recording a fact about the company: the same
    rule as a fact append, so a General account with the company's grant works
    without a permission flag — **including recording the payment** (the
    founder's call of 2026-09-24: it is tracking, not money handling)."""
    monkeypatch.setattr(service_renewals, "list_for_company", _return_rows([]))
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    assert client.get(LIST_URL).status_code == 200

    monkeypatch.setattr(service_renewals, "record_payment", _return(stored_row("confirmed")))
    assert client.post(PAYMENT_URL, json={}).status_code == 200


def _return_rows(rows: list[ServiceRenewal]):
    async def fake(*args: object, **kwargs: object) -> list[ServiceRenewal]:
        return rows

    return fake


def test_the_panel_needs_an_active_account_not_an_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel spans the portfolio and is scoped in the application layer, so
    it is open to every active account (the dashboard's rule) and refused to an
    inactive one — which is the only thing this layer decides about it."""
    view = service_renewals.ClientActions(
        as_of=date(2026, 12, 1),
        due_within=90,
        window_from=date(2026, 11, 1),
        window_to=date(2027, 3, 1),
        periods=[],
    )

    async def fake(*args: object, **kwargs: object) -> service_renewals.ClientActions:
        return view

    monkeypatch.setattr(service_renewals, "pending_actions", fake)
    granted = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    assert granted.get(PANEL_URL).status_code == 200
    inactive = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=INACTIVE
    )
    assert inactive.get(PANEL_URL).status_code == 403


# --- Create-or-return ---


def test_create_or_return_answers_the_row_and_carries_the_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "get_or_create", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(LIST_URL, json={"anniversary_year": YEAR})

    assert response.status_code == 200, response.text
    assert calls["anniversary_year"] == YEAR
    assert calls["company_id"] == COMPANY_ID
    assert calls["actor_user_id"] == ADMIN.user_id
    body = response.json()
    assert body["anniversary_year"] == YEAR
    assert body["status"] == "pending"
    assert body["client_decided_at"] is None
    assert body["payment_received_at"] is None


def test_the_create_body_needs_a_plausible_year() -> None:
    """A period year outside the range a company's anniversary can fall in is a
    422 from the declared bound, not a row nothing will ever read."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(LIST_URL, json={"anniversary_year": 1776}).status_code == 422
    assert client.post(LIST_URL, json={"anniversary_year": 3000}).status_code == 422
    assert client.post(LIST_URL, json={}).status_code == 422


def test_create_or_return_maps_an_unknown_company_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_renewals, "get_or_create", _raise(CompanyNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(LIST_URL, json={"anniversary_year": YEAR}).status_code == 404


def test_the_list_maps_an_unknown_company_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_renewals, "list_for_company", _raise(CompanyNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(LIST_URL).status_code == 404


# --- The decision ---


def test_a_decline_without_a_reason_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that stops a period needs a reason: the note is required on
    the wire, and the requirement is checked **before** the service is called,
    so a note-less decline cannot reach the record at all."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "decide", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(DECISION_URL, json={"decision": "declined"}).status_code == 422
    assert (
        client.post(DECISION_URL, json={"decision": "declined", "note": "   "}).status_code == 422
    )
    assert calls == {}

    # A confirmation needs no note: it is not a refusal.
    assert client.post(DECISION_URL, json={"decision": "confirmed"}).status_code == 200


def test_the_decision_reaches_the_service_as_the_lifecycle_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "decide", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    client.post(DECISION_URL, json={"decision": "declined", "note": "moving provider"})

    assert calls["decision"] is RenewalStatus.DECLINED
    assert calls["note"] == "moving provider"
    assert calls["anniversary_year"] == YEAR


def test_a_second_decision_is_already_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decision stands: the guard refuses a second answer to the same
    question, whatever it would have been."""
    monkeypatch.setattr(
        service_renewals,
        "decide",
        _raise(
            RenewalTransitionError(status=RenewalStatus.CONFIRMED, action=RenewalAction.CONFIRM)
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(DECISION_URL, json={"decision": "declined", "note": "no"})

    assert response.status_code == 409
    assert response.json()["detail"] == "already_closed"


def test_a_decision_on_a_period_with_no_row_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_renewals, "decide", _raise(service_renewals.RenewalNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(DECISION_URL, json={"decision": "confirmed"}).status_code == 404


def test_an_impossible_period_year_in_the_path_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path parameter is bounded the way the body is: a mistyped period is
    a 422 from the router, not a lookup for a row no anniversary can have."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "decide", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert (
        client.post(f"{LIST_URL}/1776/decision", json={"decision": "confirmed"}).status_code == 422
    )
    assert calls == {}


# --- The payment ---


def test_a_payment_before_the_client_has_confirmed_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The founder's ordering, on the wire: a payment against a period the
    client has not confirmed is ``service_not_confirmed`` — the remedy is to
    record the answer — and it is a *different* refusal from a closed row."""
    monkeypatch.setattr(
        service_renewals,
        "record_payment",
        _raise(
            RenewalTransitionError(
                status=RenewalStatus.PENDING, action=RenewalAction.RECORD_PAYMENT
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(PAYMENT_URL, json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_confirmed"


def test_a_payment_on_a_declined_period_is_also_service_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined period is not a period to pay for: the reply is the same
    "the row is not confirmed" rather than a second name for the same
    situation."""
    monkeypatch.setattr(
        service_renewals,
        "record_payment",
        _raise(
            RenewalTransitionError(
                status=RenewalStatus.DECLINED, action=RenewalAction.RECORD_PAYMENT
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(PAYMENT_URL, json={}).json()["detail"] == "service_not_confirmed"


def test_a_second_payment_is_already_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_renewals,
        "record_payment",
        _raise(
            RenewalTransitionError(
                status=RenewalStatus.PAYMENT_RECEIVED, action=RenewalAction.RECORD_PAYMENT
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(PAYMENT_URL, json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "already_closed"


def test_recording_a_payment_carries_a_timestamp_an_actor_and_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole of what payment recording is (ruling 2026-09-24): no amount,
    no currency, no line item — the request body has nowhere to put one."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        service_renewals,
        "record_payment",
        _record_calls(calls, stored_row(RenewalStatus.PAYMENT_RECEIVED.value)),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(PAYMENT_URL, json={"note": "cheque cleared"})

    assert response.status_code == 200, response.text
    assert calls["note"] == "cheque cleared"
    assert calls["actor_user_id"] == ADMIN.user_id
    body = response.json()
    assert body["payment_received_at"] is not None
    assert body["payment_by"] == str(ADMIN.user_id)
    assert body["status"] == "payment_received"


def test_an_unknown_period_year_on_the_payment_route_is_a_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_renewals, "record_payment", _raise(service_renewals.RenewalNotFoundError())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(PAYMENT_URL, json={}).status_code == 404


def test_an_oversized_note_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "record_payment", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(PAYMENT_URL, json={"note": "x" * 501}).status_code == 422
    assert calls == {}


# --- The correction ---


def test_a_correction_without_a_reason_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction changes a regulated client record, so it needs a reason —
    and the requirement is checked **before** the service is called, so a
    note-less correction cannot reach the record at all. A missing note and a
    blank one answer the same code, which is why the body's ``note`` is
    optional and the route's check is not."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "correct", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(CORRECTION_URL, json={"decision": "declined"}).status_code == 422
    blank = client.post(CORRECTION_URL, json={"decision": "declined", "note": "  "})
    assert blank.status_code == 422
    assert client.post(
        CORRECTION_URL, json={"decision": "declined"}
    ).json()["detail"] == "note_required"
    assert blank.json()["detail"] == "note_required"
    assert calls == {}


def test_the_correction_reaches_the_service_as_the_lifecycle_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route hands the service the new decision as a ``RenewalStatus``, the
    reason, and the actor — the same vocabulary the decision route uses,
    because a correction is an act on the same record."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "correct", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        CORRECTION_URL, json={"decision": "confirmed", "note": "mis-clicked on the 3rd"}
    )

    assert response.status_code == 200, response.text
    assert calls["decision"] is RenewalStatus.CONFIRMED
    assert calls["note"] == "mis-clicked on the 3rd"
    assert calls["actor_user_id"] == ADMIN.user_id
    assert calls["anniversary_year"] == YEAR


@pytest.mark.parametrize(
    ("status", "action", "expected"),
    [
        # Nothing has been answered for the period yet: there is no decision to
        # put right, and the remedy is the decision route.
        (RenewalStatus.PENDING, RenewalAction.CORRECT_TO_DECLINED, "not_decided"),
        # The correction names the answer the row already holds: it would
        # change nothing, so it is refused rather than written.
        (RenewalStatus.CONFIRMED, RenewalAction.CORRECT_TO_CONFIRMED, "decision_unchanged"),
        (RenewalStatus.DECLINED, RenewalAction.CORRECT_TO_DECLINED, "decision_unchanged"),
    ],
)
def test_the_corrections_own_refusals_are_named(
    monkeypatch: pytest.MonkeyPatch,
    status: RenewalStatus,
    action: RenewalAction,
    expected: str,
) -> None:
    """Two refusals, two remedies, neither of them the decision route's
    ``already_closed``: the caller is told which situation it is."""
    monkeypatch.setattr(
        service_renewals,
        "correct",
        _raise(RenewalTransitionError(status=status, action=action)),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(CORRECTION_URL, json={"decision": "confirmed", "note": "typo"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == expected


def test_a_correction_on_a_period_with_no_row_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_renewals, "correct", _raise(service_renewals.RenewalNotFoundError())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(CORRECTION_URL, json={"decision": "declined", "note": "no"})
    assert response.status_code == 404


def test_a_correction_body_needs_one_of_the_two_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``payment_received`` is not an answer a correction can carry (it is what
    staff record about a fee, not a decision), so the body accepts exactly the
    two decisions — and nothing at all is refused as missing."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(service_renewals, "correct", _record_calls(calls))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(CORRECTION_URL, json={"note": "x"}).status_code == 422
    assert (
        client.post(
            CORRECTION_URL, json={"decision": "payment_received", "note": "x"}
        ).status_code
        == 422
    )
    assert calls == {}


# --- The panel ---


def test_the_panel_returns_every_milestone_of_every_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read model the panel is: per company per year, every configured
    milestone with its date, its days remaining and its state — and the window
    it was computed over, so a reader can see what was left out."""
    view = service_renewals.ClientActions(
        as_of=date(2026, 12, 1),
        due_within=90,
        window_from=date(2026, 11, 1),
        window_to=date(2027, 3, 1),
        periods=[
            service_renewals.ClientActionPeriod(
                company_id=COMPANY_ID,
                br_number="12345678",
                company_name="Bright Lantern Design Limited",
                anniversary_year=YEAR,
                anniversary_date=date(2027, 3, 1),
                renewal_status=None,
                milestones=[
                    service_renewals.ClientActionMilestone(
                        kind=MilestoneKind.CONTINUATION_REQUEST,
                        date=date(2026, 12, 31),
                        days_remaining=30,
                        state=MilestoneState.PENDING,
                        blocked_reason=None,
                    ),
                    service_renewals.ClientActionMilestone(
                        kind=MilestoneKind.SIGNATURE_POST,
                        date=date(2027, 2, 8),
                        days_remaining=69,
                        state=MilestoneState.BLOCKED,
                        blocked_reason=MilestoneBlockReason.OUT_OF_COVERAGE,
                    ),
                ],
            )
        ],
    )

    async def fake(*args: object, **kwargs: object) -> service_renewals.ClientActions:
        return view

    monkeypatch.setattr(service_renewals, "pending_actions", fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(PANEL_URL, params={"due_within": 90})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == "2026-12-01"
    assert body["due_within"] == 90
    assert body["window_from"] == "2026-11-01"
    assert body["window_to"] == "2027-03-01"
    assert body["periods"][0]["br_number"] == "12345678"
    assert body["periods"][0]["renewal_status"] is None
    assert [item["kind"] for item in body["periods"][0]["milestones"]] == [
        "continuation_request",
        "signature_post",
    ]
    assert body["periods"][0]["milestones"][1]["state"] == "blocked"
    # A blocked milestone names the leg that refused, in the gate's own
    # vocabulary; an unblocked one carries null. A bare `blocked` would leave
    # staff to guess at the remedy, which is the standard the panel's refusal
    # surface is held to (the download's 409 names its reason the same way).
    assert body["periods"][0]["milestones"][1]["blocked_reason"] == "out_of_coverage"
    assert body["periods"][0]["milestones"][0]["blocked_reason"] is None


def test_the_panel_horizon_is_bounded_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The horizon is the caller's — a year at most, since the panel is a
    working view rather than a report — and its default is the shipped 90
    days. An out-of-range horizon is a 422, never a silently clamped read."""
    calls: dict[str, object] = {}

    async def fake(*args: object, **kwargs: object) -> service_renewals.ClientActions:
        calls.update(kwargs)
        return service_renewals.ClientActions(
            as_of=date(2026, 12, 1),
            due_within=int(kwargs["due_within"]),
            window_from=date(2026, 11, 1),
            window_to=date(2027, 3, 1),
            periods=[],
        )

    monkeypatch.setattr(service_renewals, "pending_actions", fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.get(PANEL_URL).status_code == 200
    assert calls["due_within"] == service_renewals.DEFAULT_DUE_WITHIN_DAYS
    assert service_renewals.DEFAULT_DUE_WITHIN_DAYS == 90

    assert client.get(PANEL_URL, params={"due_within": 365}).status_code == 200
    assert client.get(PANEL_URL, params={"due_within": 0}).status_code == 422
    assert client.get(PANEL_URL, params={"due_within": 366}).status_code == 422


def test_the_panel_refuses_a_stored_block_it_cannot_read_with_a_typed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's milestones come from the tenant's block, so a block this
    build cannot read cannot yield a panel — and the refusal says so, naming
    every field that failed, rather than raising an untyped error through the
    route. Same code and shape as the configuration read's (contract §5.1.3):
    one stored row, one remedy (a PUT with a valid block, or a repair)."""

    async def refused(*args: object, **kwargs: object) -> service_renewals.ClientActions:
        raise MilestoneValidationError(
            [
                "milestones.continuation_request_days_before: must be greater than "
                "data_confirmation_days_before"
            ]
        )

    monkeypatch.setattr(service_renewals, "pending_actions", refused)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(PANEL_URL)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "tenant_config_invalid: milestones.continuation_request_days_before: must be "
        "greater than data_confirmation_days_before"
    )
