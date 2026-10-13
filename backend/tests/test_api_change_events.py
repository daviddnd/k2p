"""API-layer tests for the change-event routes (contract §4.1, §5.1).

These run without a database or object store: the auth seam, RBAC context,
session and storage are overridden and the application service is
monkeypatched, so only fail-closed authentication, the grant-gated
authorization checks, request validation and the typed-error → HTTP mappings
are exercised. The lifecycle itself — the lock, the version rows, the audit
chain, the tenancy keys — lives in ``test_change_events_scenarios_db.py``.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import change_events, reconfirmation
from app.application.company_master import CompanyNotFoundError
from app.domain.change_events import (
    AllowedAction,
    ChangeEventAction,
    ChangeEventStatus,
    ChangeEventType,
)
from app.domain.compliance import ComplianceEventKind, RiskLevel
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
OTHER_COMPANY_ID = uuid4()
EVENT_ID = uuid4()
USER_ID = uuid4()
TENANT_ID = uuid4()

ADMIN = ActorRef(user_id=USER_ID, tenant_id=TENANT_ID)
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset({Permission.REVIEW_EVENTS, Permission.APPROVE_EVENTS}),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({OTHER_COMPANY_ID}),
)
INACTIVE = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

LIST_URL = f"/api/v1/companies/{COMPANY_ID}/change-events"
QUEUE_URL = "/api/v1/change-events"
TRANSITIONS_URL = f"/api/v1/change-events/{EVENT_ID}/transitions"
ASSESSMENTS_URL = f"/api/v1/change-events/{EVENT_ID}/assessments"


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


def stored_event(
    *,
    status: str = ChangeEventStatus.PENDING.value,
    version_no: int = 1,
    payload: dict[str, Any] | None = None,
) -> ChangeEvent:
    """One version row as the service returns it (never persisted here)."""
    return ChangeEvent(
        id=EVENT_ID,
        version_no=version_no,
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        type=ChangeEventType.DIRECTOR_APPOINTMENT.value,
        status=status,
        effective_date=date(2026, 9, 1),
        effective_date_text="1 September 2026",
        informed_date=date(2026, 9, 2),
        payload=payload
        if payload is not None
        else {"appointment": {"officer": {"role": "director"}}},
        source_type="manual",
        source_id=None,
        related_event_id=None,
        relation=None,
        reviewed_by=None,
        reviewed_at=None,
        approved_by=None,
        approved_at=None,
        rationale=None,
        created_by=USER_ID,
        created_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )


def window(**overrides: object) -> change_events.ChangeWindowView:
    """The computed window as the service hands it to the route."""
    values: dict[str, object] = {
        "deadline": date(2026, 9, 16),
        "days_remaining": -10,
        "is_late": True,
        "risk": RiskLevel.RED,
        "alert": ComplianceEventKind.ALERT_7D,
    }
    values.update(overrides)
    return change_events.ChangeWindowView(**values)  # type: ignore[arg-type]


def view(
    row: ChangeEvent | None = None,
    history: Sequence[ChangeEvent] = (),
    *,
    computed: change_events.ChangeWindowView | None = None,
) -> change_events.ChangeEventView:
    return change_events.ChangeEventView(
        current=row or stored_event(),
        history=list(history),
        window=computed if computed is not None else window(),
    )


def _raise(error: Exception):
    async def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


def _return(result: object):
    async def fake(*args: object, **kwargs: object) -> object:
        return result

    return fake


def _record_calls(store: dict[str, object], result: object):
    async def fake(*args: object, **kwargs: object) -> object:
        store.update(kwargs)
        return result

    return fake


# --- Fail closed on every route ---


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()
    assert client.get(LIST_URL).status_code == 401
    assert (
        client.post(
            LIST_URL,
            json={"type": "director_appointment", "effective_date": "2026-09-01"},
        ).status_code
        == 401
    )
    assert client.get(QUEUE_URL).status_code == 401
    assert client.get(TRANSITIONS_URL).status_code == 401
    assert client.post(TRANSITIONS_URL, json={"action": "confirm"}).status_code == 401
    assert client.get(ASSESSMENTS_URL).status_code == 401


def test_general_without_the_company_grant_is_forbidden() -> None:
    """The company-scoped routes use the path's company; 403 before anything."""
    client = build_client(actor=ADMIN, context=GENERAL_WITHOUT_GRANT)
    assert client.get(LIST_URL).status_code == 403
    assert (
        client.post(
            LIST_URL,
            json={"type": "director_appointment", "effective_date": "2026-09-01"},
        ).status_code
        == 403
    )


def test_the_id_scoped_routes_refuse_an_actor_without_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id-scoped routes have no company in the path, so the application
    layer resolves the event's company and asks the same single decision; the
    route answers its refusal."""
    monkeypatch.setattr(
        change_events,
        "transitions_for",
        _raise(change_events.ChangeEventForbiddenError()),
    )
    monkeypatch.setattr(
        change_events, "transition", _raise(change_events.ChangeEventForbiddenError())
    )
    monkeypatch.setattr(
        change_events, "list_assessments", _raise(change_events.ChangeEventForbiddenError())
    )
    client = build_client(actor=ADMIN, context=GENERAL_WITHOUT_GRANT)

    assert client.get(TRANSITIONS_URL).status_code == 403
    assert client.post(TRANSITIONS_URL, json={"action": "confirm"}).status_code == 403
    assert client.get(ASSESSMENTS_URL).status_code == 403


def test_an_unknown_or_cross_tenant_event_is_a_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One answer for an unknown id and another tenant's, deliberately (a
    deployment must not be able to tell them apart)."""
    monkeypatch.setattr(
        change_events, "transitions_for", _raise(change_events.ChangeEventNotFoundError())
    )
    monkeypatch.setattr(
        change_events, "transition", _raise(change_events.ChangeEventNotFoundError())
    )
    monkeypatch.setattr(
        change_events, "list_assessments", _raise(change_events.ChangeEventNotFoundError())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.get(TRANSITIONS_URL).status_code == 404
    assert client.post(TRANSITIONS_URL, json={"action": "confirm"}).status_code == 404
    assert client.get(ASSESSMENTS_URL).status_code == 404


# --- Manual entry ---


def test_a_protected_class_field_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1's protected class is refused until field-level encryption exists
    (§8 scope, Phase 7): the answer names the field to drop rather than the
    class's absence being discovered later, and it holds for an edit as much
    as for an entry — both go through the same grammar."""
    monkeypatch.setattr(
        change_events,
        "create",
        _raise(
            change_events.ChangeEventProtectedClassError(
                ["payload.appointment.officer.hkid_full"]
            )
        ),
    )
    monkeypatch.setattr(
        change_events,
        "transition",
        _raise(
            change_events.ChangeEventProtectedClassError(
                [
                    "payload.appointment.officer.hkid_full",
                    "payload.appointment.officer.hkid_check_digit",
                ]
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    entered = client.post(
        LIST_URL,
        json={
            "type": "director_appointment",
            "effective_date": "2026-09-01",
            "payload": {
                "appointment": {
                    "officer": {
                        "role": "director",
                        "name_en": "CHAN Tai Man",
                        "appointment_date": "2026-09-01",
                        "hkid_full": "A123456",
                    }
                }
            },
        },
    )
    edited = client.post(
        TRANSITIONS_URL,
        json={
            "action": "edit",
            "payload": {
                "appointment": {
                    "officer": {
                        "role": "director",
                        "name_en": "CHAN Tai Man",
                        "appointment_date": "2026-09-01",
                        "hkid_full": "A123456",
                        "hkid_check_digit": "7",
                    }
                }
            },
        },
    )

    assert entered.status_code == 422, entered.text
    assert entered.json()["detail"] == (
        "protected_class_unavailable: payload.appointment.officer.hkid_full"
    )
    assert edited.status_code == 422, edited.text
    assert edited.json()["detail"] == (
        "protected_class_unavailable: payload.appointment.officer.hkid_full, "
        "payload.appointment.officer.hkid_check_digit"
    )


def test_manual_entry_reaches_the_service_as_the_manual_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route is scope §2.7's fallback: it names no source and no status —
    the service writes ``manual`` and ``pending``, and the route's job is the
    request shape and the actor."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(change_events, "create", _record_calls(calls, view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    relational_id = uuid4()

    response = client.post(
        LIST_URL,
        json={
            "type": "director_appointment",
            "effective_date": "2026-09-01",
            "effective_date_text": "1 September 2026",
            "informed_date": "2026-09-02",
            "payload": {"appointment": {"officer": {"role": "director"}}},
            "related_event_id": str(relational_id),
            "relation": "corrects",
        },
    )

    assert response.status_code == 201, response.text
    assert calls["tenant_id"] == TENANT_ID
    assert calls["company_id"] == COMPANY_ID
    assert calls["event_type"] is ChangeEventType.DIRECTOR_APPOINTMENT
    assert calls["effective_date"] == date(2026, 9, 1)
    assert calls["informed_date"] == date(2026, 9, 2)
    assert calls["related_event_id"] == relational_id
    assert calls["actor_user_id"] == USER_ID
    body = response.json()
    assert body["status"] == "pending"
    assert body["version_no"] == 1
    assert body["source_type"] == "manual"
    assert body["history"] == []
    assert body["payload"] == {"appointment": {"officer": {"role": "director"}}}


def test_the_entry_body_refuses_a_type_it_cannot_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body's enum is §2.6's whole vocabulary, so an unknown type is a 422
    from the declaration, and the reserved one is the service's named refusal
    (422 ``reserved_event_type``)."""
    monkeypatch.setattr(
        change_events,
        "create",
        _raise(change_events.ChangeEventReservedTypeError(ChangeEventType.COMPANY_NAME_CHANGE)),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    unknown = client.post(
        LIST_URL, json={"type": "share_transfer", "effective_date": "2026-09-01"}
    )
    reserved = client.post(
        LIST_URL, json={"type": "company_name_change", "effective_date": "2026-09-01"}
    )

    assert unknown.status_code == 422
    assert reserved.status_code == 422
    assert reserved.json()["detail"] == "reserved_event_type"


def test_entry_validation_violations_travel_in_one_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grammar's violations are the wire's detail — every one of them, in
    one refusal, the way the tenant-configuration block's travel."""
    monkeypatch.setattr(
        change_events,
        "create",
        _raise(
            change_events.ChangeEventInvalidError(
                [
                    "payload.appointment.officer.role: not one of company_secretary, director, "
                    "reserve_director",
                    "informed_date: before effective_date "
                    "(contract §2.6 requires informed_date >= effective_date)",
                ]
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        LIST_URL, json={"type": "director_appointment", "effective_date": "2026-09-01"}
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.startswith("change_event_invalid: ")
    assert "payload.appointment.officer.role" in detail
    assert "informed_date" in detail


def test_an_unknown_company_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(change_events, "create", _raise(CompanyNotFoundError()))
    monkeypatch.setattr(change_events, "list_events", _raise(CompanyNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert (
        client.post(
            LIST_URL, json={"type": "director_appointment", "effective_date": "2026-09-01"}
        ).status_code
        == 404
    )
    assert client.get(LIST_URL).status_code == 404


def test_the_company_read_returns_the_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The event read: the current version flat, the versions before it in
    ``history`` (newest first), and every §2.6 field present. The computed
    ``change_window`` is the event's own — the history's versions carry none,
    because the countdown belongs to the current version."""
    old = stored_event(version_no=1, status=ChangeEventStatus.UNDER_REVIEW.value)
    new = stored_event(version_no=2, status=ChangeEventStatus.UNDER_REVIEW.value)
    monkeypatch.setattr(change_events, "list_events", _return([view(new, [old])]))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(LIST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body[0]["version_no"] == 2
    assert [version["version_no"] for version in body[0]["history"]] == [1]
    assert set(body[0]) >= {
        "id",
        "company_id",
        "version_no",
        "type",
        "status",
        "effective_date",
        "effective_date_text",
        "informed_date",
        "payload",
        "source_type",
        "source_id",
        "related_event_id",
        "relation",
        "reviewed_by",
        "reviewed_at",
        "approved_by",
        "approved_at",
        "rationale",
        "created_by",
        "created_at",
        "history",
        "change_window",
    }
    assert body[0]["change_window"] == {
        "deadline": "2026-09-16",
        "days_remaining": -10,
        "is_late": True,
        "risk": "red",
        "alert": "alert_7d",
    }
    assert "change_window" not in body[0]["history"][0]


# --- The cross-company review queue ---


def test_the_review_queue_returns_the_company_and_the_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue item: the current version flat, the company it belongs to,
    and the computed window. No ``history`` (the company read carries the
    lineage) and no action list — ``GET /change-events/{id}/transitions``
    stays the sole allowed-actions authority."""
    item = change_events.ChangeEventQueueItem(
        current=stored_event(),
        br_number="12345678",
        company_name="Bright Lantern Limited",
        window=window(),
    )
    monkeypatch.setattr(change_events, "queue", _return([item]))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(QUEUE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert set(entry) == {
        "id",
        "version_no",
        "company_id",
        "type",
        "status",
        "effective_date",
        "effective_date_text",
        "informed_date",
        "payload",
        "source_type",
        "source_id",
        "related_event_id",
        "relation",
        "reviewed_by",
        "reviewed_at",
        "approved_by",
        "approved_at",
        "rationale",
        "created_by",
        "created_at",
        "br_number",
        "company_name",
        "change_window",
    }
    assert entry["id"] == str(EVENT_ID)
    assert entry["company_id"] == str(COMPANY_ID)
    assert entry["status"] == "pending"
    assert entry["br_number"] == "12345678"
    assert entry["company_name"] == "Bright Lantern Limited"
    assert entry["change_window"] == {
        "deadline": "2026-09-16",
        "days_remaining": -10,
        "is_late": True,
        "risk": "red",
        "alert": "alert_7d",
    }


def test_the_review_queue_passes_the_actors_context_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's gate is "any active account" and the scoping is the
    application layer's, exactly like the dashboard: the context the route
    resolved is what the queue read sees."""
    seen: list[ActorContext] = []

    async def fake(session: object, **kwargs: object) -> list[object]:
        seen.append(kwargs["context"])  # type: ignore[arg-type]
        return []

    monkeypatch.setattr(change_events, "queue", fake)
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    assert client.get(QUEUE_URL).status_code == 200
    assert seen == [GENERAL_WITH_GRANT]


# --- The transitions read ---


def test_the_transitions_read_names_the_actors_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read the frontend renders buttons from: the event's state and the
    actor's wired actions, each with its target status and whether a rationale
    comes with it."""
    monkeypatch.setattr(
        change_events,
        "transitions_for",
        _return(
            change_events.ChangeEventTransitions(
                row=stored_event(),
                actions=[
                    AllowedAction(
                        action=ChangeEventAction.MARK_UNDER_REVIEW,
                        to_status=ChangeEventStatus.UNDER_REVIEW,
                        rationale_required=False,
                    ),
                    AllowedAction(
                        action=ChangeEventAction.CLIENT_WITHDRAW,
                        to_status=ChangeEventStatus.WITHDRAWN,
                        rationale_required=True,
                    ),
                ],
            )
        ),
    )
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.get(TRANSITIONS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == str(EVENT_ID)
    assert body["company_id"] == str(COMPANY_ID)
    assert body["version_no"] == 1
    assert body["status"] == "pending"
    assert body["actions"] == [
        {
            "action": "mark_under_review",
            "to_status": "under_review",
            "rationale_required": False,
        },
        {
            "action": "client_withdraw",
            "to_status": "withdrawn",
            "rationale_required": True,
        },
    ]


def test_the_transitions_read_reflects_the_actor_of_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two actors, one event, two action lists: the read passes the requester's
    context through to the application, so the same event answers a reviewer
    and an approver differently — and an actor with neither leg gets an empty
    list, never someone else's authority. (The leg computation itself is the
    domain's own test; this is the wiring.)"""
    seen: list[ActorContext] = []

    async def fake(*args: object, **kwargs: object) -> change_events.ChangeEventTransitions:
        context = kwargs["context"]
        assert isinstance(context, ActorContext)
        seen.append(context)
        if Permission.REVIEW_EVENTS in context.permissions:
            actions = [
                AllowedAction(
                    action=ChangeEventAction.MARK_UNDER_REVIEW,
                    to_status=ChangeEventStatus.UNDER_REVIEW,
                    rationale_required=False,
                )
            ]
        elif Permission.APPROVE_EVENTS in context.permissions:
            actions = [
                AllowedAction(
                    action=ChangeEventAction.CONFIRM,
                    to_status=ChangeEventStatus.CONFIRMED,
                    rationale_required=False,
                )
            ]
        else:
            actions = []
        return change_events.ChangeEventTransitions(row=stored_event(), actions=actions)

    monkeypatch.setattr(change_events, "transitions_for", fake)
    reviewer = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_ID}),
    )
    approver = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.APPROVE_EVENTS}),
        company_ids=frozenset({COMPANY_ID}),
    )
    neither = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(),
        company_ids=frozenset({COMPANY_ID}),
    )

    reviewer_actions = build_client(actor=ADMIN, context=reviewer).get(TRANSITIONS_URL).json()
    approver_actions = build_client(actor=ADMIN, context=approver).get(TRANSITIONS_URL).json()
    neither_actions = build_client(actor=ADMIN, context=neither).get(TRANSITIONS_URL).json()

    assert [item["action"] for item in reviewer_actions["actions"]] == ["mark_under_review"]
    assert [item["action"] for item in approver_actions["actions"]] == ["confirm"]
    assert neither_actions["actions"] == []
    assert seen == [reviewer, approver, neither]


# --- Performing a transition ---


def test_an_unwired_action_is_refused_with_its_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never silently accepted: each unwired action answers the §5.1.3 name of
    the piece of work its side effects wait on. M5b wired the reconfirmation
    exchange, so ``reconfirmation_not_wired`` names ``override`` alone, and
    P6-4 wired ``submit`` as the system row the draft's export performs, so
    ``draft_not_wired`` left the vocabulary."""
    cases = {
        ChangeEventAction.OVERRIDE: "reconfirmation_not_wired",
        ChangeEventAction.SUPERSEDE: "supersede_not_wired",
    }
    for action, reason in cases.items():
        monkeypatch.setattr(
            change_events,
            "transition",
            _raise(change_events.ChangeEventActionNotWiredError(action=action, reason=reason)),
        )
        client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
        response = client.post(TRANSITIONS_URL, json={"action": action.value})
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == reason


def test_a_system_row_is_a_named_409_for_any_staff_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5b wired ``client_reply`` as the system row the reply parser's landing
    performs: a staff POST answers the same refusal ``apply`` does — the
    system-row ``transition_not_allowed`` — never a move."""
    monkeypatch.setattr(
        change_events,
        "transition",
        _raise(
            change_events.ChangeEventTransitionError(
                status=ChangeEventStatus.CLIENT_RECONFIRMING,
                action=ChangeEventAction.CLIENT_REPLY,
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(TRANSITIONS_URL, json={"action": "client_reply"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "transition_not_allowed"


def test_a_staff_post_of_submit_is_the_system_row_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6-4 wired ``submit`` as the system row the draft's export performs:
    a staff POST answers the system-row ``transition_not_allowed`` — the
    ``apply`` precedent — never a move, and the filing's events move only
    through the export itself (the scenario suite pins the real move)."""

    async def refused(*args: object, **kwargs: object) -> object:
        raise change_events.ChangeEventTransitionError(
            status=ChangeEventStatus.APPLIED_TO_DRAFT,
            action=ChangeEventAction.SUBMIT,
        )

    monkeypatch.setattr(change_events, "transition", refused)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(TRANSITIONS_URL, json={"action": "submit"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "transition_not_allowed"


def test_a_reconfirmation_refusal_maps_the_letter_preconditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The send_reconfirmation transition composes the letter before it moves
    the event, and a letter that cannot compose refuses with the
    reconfirmer's own bounded reasons — the confirmation trigger's mapping:
    a missing record is a 422, an unconfigured mailbox a 503. The event does
    not move (the executor's letter-first order)."""
    cases = {
        "no_confirmation_request": 422,
        "company_name_unavailable": 422,
        "tenant_config_invalid": 422,
        "mail_not_configured": 503,
    }
    for reason, status in cases.items():
        monkeypatch.setattr(
            change_events,
            "transition",
            _raise(reconfirmation.ReconfirmationError(reason)),
        )
        client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
        response = client.post(TRANSITIONS_URL, json={"action": "send_reconfirmation"})
        assert response.status_code == status, response.text
        assert response.json()["detail"] == reason


def test_an_action_the_status_does_not_admit_is_a_named_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's refusal is a name, not a no-op: ``transition_not_allowed``
    — the caller re-reads the event rather than believing it moved."""
    monkeypatch.setattr(
        change_events,
        "transition",
        _raise(
            change_events.ChangeEventTransitionError(
                status=ChangeEventStatus.PENDING, action=ChangeEventAction.CONFIRM
            )
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(TRANSITIONS_URL, json={"action": "confirm"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "transition_not_allowed"


def test_a_rejection_without_a_rationale_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1 marks ``reject`` (and the staff-recorded withdrawal) "rationale
    required": the refusal names the missing reason, and a blank one answers
    the same code as a missing one."""
    monkeypatch.setattr(
        change_events, "transition", _raise(change_events.ChangeEventRationaleRequiredError())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    for body in ({"action": "reject"}, {"action": "reject", "rationale": "   "}):
        response = client.post(TRANSITIONS_URL, json=body)
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "rationale_required"


def test_a_lost_version_race_is_a_named_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """The primary key refused the revision's insert (another writer got the
    version number first): the caller re-reads and retries, the same remedy
    the confirmation-request draft conflict answers (§5.1.3)."""
    monkeypatch.setattr(
        change_events, "transition", _raise(change_events.ChangeEventConflictError())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        TRANSITIONS_URL, json={"action": "edit", "informed_date": "2026-09-05"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "change_event_conflict"


def test_an_edit_passes_only_the_fields_the_request_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent field keeps the version's value and an explicit null clears —
    the partial-update rule — so the service receives exactly the stated
    fields and never a full body of nulls."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(change_events, "transition", _record_calls(calls, view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        TRANSITIONS_URL,
        json={
            "action": "edit",
            "payload": {"appointment": {"officer": {"role": "director"}}},
            "informed_date": None,
            "rationale": "the date on the letter was misread",
        },
    )

    assert response.status_code == 200, response.text
    assert calls["action"] is ChangeEventAction.EDIT
    assert calls["rationale"] == "the date on the letter was misread"
    assert calls["changes"] == {
        "payload": {"appointment": {"officer": {"role": "director"}}},
        "informed_date": None,
    }


def test_the_transition_route_hands_the_store_and_scanner_seams_to_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``send_reconfirmation`` composes and stores the letter inside the
    executor, so the route must pass the same storage and scanner seams the
    other letter-writing routes take — the TestClient's FakeStore rides
    ``get_storage``, and every action receives both."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(change_events, "transition", _record_calls(calls, view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(TRANSITIONS_URL, json={"action": "send_reconfirmation"})

    assert response.status_code == 200, response.text
    assert isinstance(calls["store"], FakeStore)
    assert calls["scanner"] is not None


def test_a_non_edit_action_carrying_edit_fields_is_refused_by_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule lives in the executor (one place), and the route reports it:
    edit fields on a ``confirm`` are a body the caller misunderstood, not
    something to ignore."""
    monkeypatch.setattr(
        change_events,
        "transition",
        _raise(change_events.ChangeEventInvalidError(["confirm: carries edit fields"])),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        TRANSITIONS_URL, json={"action": "confirm", "payload": {"anything": 1}}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "change_event_invalid: confirm: carries edit fields"


def test_the_transition_body_refuses_an_unknown_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelled field must not read as an edit that changed nothing: the
    body is closed (``extra="forbid"``), so it is a 422 before the service is
    called at all."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(change_events, "transition", _record_calls(calls, view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(TRANSITIONS_URL, json={"action": "edit", "paylod": {}})

    assert response.status_code == 422
    assert calls == {}


def test_the_transition_body_needs_an_action_from_the_vocabulary() -> None:
    """The body's action enum is §4.1's whole vocabulary — an action nobody
    defined is a 422 from the declaration, and the unwired ones reach the
    service (they answer their own 409)."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(TRANSITIONS_URL, json={}).status_code == 422
    assert client.post(TRANSITIONS_URL, json={"action": "frobnicate"}).status_code == 422


def test_a_transition_returns_the_whole_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write answers with the same shape the read does — the current
    version and its lineage — so a caller needs no second request to render
    the event it just moved."""
    old = stored_event(version_no=1)
    new = stored_event(version_no=2, status=ChangeEventStatus.UNDER_REVIEW.value)
    monkeypatch.setattr(change_events, "transition", _return(view(new, [old])))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(TRANSITIONS_URL, json={"action": "mark_under_review"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "under_review"
    assert body["version_no"] == 2
    assert [version["version_no"] for version in body["history"]] == [1]


# --- Assessments ---


def test_the_assessments_read_is_truthfully_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No writer exists for a change event's assessments yet (the reply parser
    is M5b), so the read answers an empty list rather than a fabricated one."""
    monkeypatch.setattr(change_events, "list_assessments", _return([]))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(ASSESSMENTS_URL)

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_the_assessments_read_carries_the_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one exists it is the §2.7 shape — scores, signals, routing — with
    no field the batch view does not also carry."""
    assessment = ExtractionAssessment(
        id=uuid4(),
        tenant_id=TENANT_ID,
        subject_type="change_event",
        subject_id=EVENT_ID,
        component="reply_parser",
        model="pinned-model/v1",
        field_scores={"officer.role": 0.4},
        signal_log={"fields": {"officer.role": ["value_unvalidated"]}},
        overall_score=None,
        routing=None,
        created_at=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(change_events, "list_assessments", _return([assessment]))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(ASSESSMENTS_URL)

    assert response.status_code == 200, response.text
    body = response.json()[0]
    assert body["subject_type"] == "change_event"
    assert body["subject_id"] == str(EVENT_ID)
    assert body["field_scores"] == {"officer.role": 0.4}
    assert body["overall_score"] is None
    assert body["routing"] is None
    assert body["model"] == "pinned-model/v1"


def test_the_source_of_a_manual_entry_is_never_the_requesters_to_choose() -> None:
    """Scope §2.7's route writes ``manual``, and the body has nowhere to say
    otherwise: an extra ``source_type`` is refused as an unknown field rather
    than accepted and ignored."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        LIST_URL,
        json={
            "type": "director_appointment",
            "effective_date": "2026-09-01",
            "source_type": "communication",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] != ""


def test_an_inactive_account_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: an inactive Administrator is refused on the company-scoped
    routes by the RBAC dependency, and the id-scoped routes answer the
    application's refusal (the domain decision denies inactive accounts)."""
    monkeypatch.setattr(
        change_events, "transitions_for", _raise(change_events.ChangeEventForbiddenError())
    )
    monkeypatch.setattr(
        change_events, "list_assessments", _raise(change_events.ChangeEventForbiddenError())
    )
    client = build_client(actor=ADMIN, context=INACTIVE)

    assert client.get(LIST_URL).status_code == 403
    assert client.get(QUEUE_URL).status_code == 403
    assert client.get(TRANSITIONS_URL).status_code == 403
    assert client.get(ASSESSMENTS_URL).status_code == 403
