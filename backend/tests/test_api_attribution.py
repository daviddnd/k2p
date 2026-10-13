"""API-layer tests for the attribution-case surface (ADR-0019 §1; contract
§5.1.7's sibling).

These run without a database, a mailbox, or an object store: the auth seam,
the RBAC context, and the session are overridden and the application entry
points are monkeypatched, so only fail-closed authentication, the single
Administrator-only check (``require_admin``), the typed refusals, the list's
status filter, and the response shapes are exercised. What is behind them —
the rows the list reads, the dismissal's cancel-and-audit, the forced
processing's resolve-and-audit — runs against migrated PostgreSQL in
``test_attribution_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import AttributionCase
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import attribution
from app.application.attribution import ProcessReport
from app.domain.attribution import AttributionAskKind, AttributionCaseStatus
from app.domain.communications import IngestOutcome
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app
from tests.fake_scanning import FakeScanner

ATTRIBUTION_URL = "/api/v1/mail/attribution-cases"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),  # even holding every grantable permission
    company_ids=frozenset(),
)
COMPANY_ID = uuid4()


def build_client(
    actor: ActorRef | None = None,
    context: ActorContext | None = None,
    store: object | None = None,
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    if store is not None:
        app.dependency_overrides[get_storage] = lambda: store

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def dismiss_url(case_id: UUID) -> str:
    return f"{ATTRIBUTION_URL}/{case_id}/dismiss"


def process_url(case_id: UUID) -> str:
    return f"{ATTRIBUTION_URL}/{case_id}/process"


def case_row(
    *,
    case_id: UUID,
    company_id: UUID | None = COMPANY_ID,
    status: AttributionCaseStatus = AttributionCaseStatus.AWAITING_CONFIRMATION,
    ask_kind: AttributionAskKind | None = AttributionAskKind.CONFIRMATION,
    ask_sent_at: datetime | None = None,
    resolved_communication_id: UUID | None = None,
    closed_at: datetime | None = None,
    dismissed_at: datetime | None = None,
) -> AttributionCase:
    """One row as the table holds it, with the columns the surface reads
    (``company_id=None`` stages a clarification-loop case)."""
    return AttributionCase(
        id=case_id,
        tenant_id=ADMIN.tenant_id,
        company_id=company_id,
        mailbox_message_id=f"provider-{case_id}",
        sender_address="director@client.example",
        thread_id="thread-18cf",
        original_rfc_message_id="<original@client.example>",
        status=status.value,
        ask_kind=None if ask_kind is None else ask_kind.value,
        ask_sent_at=ask_sent_at,
        resolved_communication_id=resolved_communication_id,
        closed_at=closed_at,
        dismissed_at=dismissed_at,
        created_at=NOW,
    )


# --- Fail closed; Administrator-only on every route ---


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()

    assert client.get(ATTRIBUTION_URL).status_code == 401
    assert client.post(dismiss_url(uuid4())).status_code == 401
    assert client.post(process_url(uuid4())).status_code == 401


def test_the_surface_routes_are_administrator_only() -> None:
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)

    assert client.get(ATTRIBUTION_URL).status_code == 403
    assert client.post(dismiss_url(uuid4())).status_code == 403
    assert client.post(process_url(uuid4())).status_code == 403


# --- The list ---


def test_the_list_returns_the_rows_the_reviewer_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    newest = case_row(
        case_id=uuid4(),
        status=AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        ask_kind=AttributionAskKind.REGISTERED_CHANNEL,
        ask_sent_at=NOW,
    )
    oldest = case_row(case_id=uuid4())
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, statuses: object
    ) -> list[AttributionCase]:
        seen["tenant_id"] = tenant_id
        seen["statuses"] = statuses
        return [newest, oldest]

    monkeypatch.setattr(attribution, "list_cases", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(ATTRIBUTION_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    # The tenant is the actor's, decided here and never taken from the caller.
    assert seen["tenant_id"] == ADMIN.tenant_id
    # And the list is the open queue unless the caller says otherwise: the
    # cases staff still have to act on, all three awaits.
    assert seen["statuses"] == (
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        AttributionCaseStatus.AWAITING_CONFIRMATION,
        AttributionCaseStatus.AWAITING_CLARIFICATION,
    )
    # Newest first, as the service ordered them.
    assert [item["id"] for item in body] == [str(newest.id), str(oldest.id)]
    # Exactly the metadata a reviewer triages on: nothing here could carry what
    # a client wrote (the row holds no body at all).
    assert set(body[0]) == {
        "id",
        "company_id",
        "status",
        "ask_kind",
        "ask_sent_at",
        "sender_address",
        "thread_id",
        "original_rfc_message_id",
        "resolved_communication_id",
        "closed_at",
        "dismissed_at",
        "created_at",
    }
    assert body[0]["company_id"] == str(COMPANY_ID)
    assert body[0]["status"] == "awaiting_registered_channel"
    assert body[0]["ask_kind"] == "registered_channel"
    assert body[0]["ask_sent_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body[0]["sender_address"] == "director@client.example"
    assert body[0]["thread_id"] == "thread-18cf"
    assert body[0]["original_rfc_message_id"] == "<original@client.example>"
    assert body[0]["resolved_communication_id"] is None
    assert body[0]["closed_at"] is None
    assert body[0]["dismissed_at"] is None
    assert body[0]["created_at"] == NOW.isoformat().replace("+00:00", "Z")


def test_an_empty_list_is_an_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(session: object, *, tenant_id: UUID, statuses: object) -> list[object]:
        return []

    monkeypatch.setattr(attribution, "list_cases", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(ATTRIBUTION_URL)

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # ``awaiting`` is all three awaits: the open states are one queue.
        (
            "status=awaiting",
            (
                AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
                AttributionCaseStatus.AWAITING_CONFIRMATION,
                AttributionCaseStatus.AWAITING_CLARIFICATION,
            ),
        ),
        ("status=resolved", (AttributionCaseStatus.RESOLVED,)),
        ("status=closed", (AttributionCaseStatus.CLOSED,)),
        ("status=dismissed", (AttributionCaseStatus.DISMISSED,)),
        # ``all`` is not a status: it is the absence of the filter, which is
        # what the service reads as "every row".
        ("status=all", None),
    ],
)
def test_the_list_filters_to_the_statuses_the_caller_asks_for(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected: tuple[AttributionCaseStatus, ...] | None,
) -> None:
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, statuses: object
    ) -> list[AttributionCase]:
        seen["statuses"] = statuses
        return []

    monkeypatch.setattr(attribution, "list_cases", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{ATTRIBUTION_URL}?{query}")

    assert response.status_code == 200, response.text
    assert seen["statuses"] == expected


def test_an_unknown_status_filter_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def fake_list(session: object, *, tenant_id: UUID, statuses: object) -> list[object]:
        calls.append(statuses)
        return []

    monkeypatch.setattr(attribution, "list_cases", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{ATTRIBUTION_URL}?status=triage_later")

    # Refused by the closed vocabulary, not answered with an empty list: a
    # filter nobody understands must not read as "nothing is waiting".
    assert response.status_code == 422
    assert calls == []


# --- The dismissal ---


def test_a_dismiss_reports_the_row_as_it_now_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    case_id = uuid4()
    row = case_row(
        case_id=case_id,
        status=AttributionCaseStatus.DISMISSED,
        dismissed_at=NOW,
    )
    seen: dict[str, object] = {}

    async def fake_dismiss(
        session: object,
        *,
        tenant_id: UUID,
        case_id: UUID,
        actor_user_id: UUID,
    ) -> AttributionCase:
        seen.update(
            tenant_id=tenant_id,
            case_id=case_id,
            actor_user_id=actor_user_id,
        )
        return row

    monkeypatch.setattr(attribution, "dismiss_case", fake_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(case_id))

    assert response.status_code == 200, response.text
    # The tenant is the actor's and the acting administrator is recorded; the
    # route reads no mailbox, so it takes no store and no scanner.
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["case_id"] == case_id
    assert seen["actor_user_id"] == ADMIN.user_id
    body = response.json()
    assert body["id"] == str(case_id)
    assert body["status"] == "dismissed"
    assert body["dismissed_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["closed_at"] is None
    assert body["resolved_communication_id"] is None
    # The row as the review list shows it, and nothing else.
    assert set(body) == {
        "id",
        "company_id",
        "status",
        "ask_kind",
        "ask_sent_at",
        "sender_address",
        "thread_id",
        "original_rfc_message_id",
        "resolved_communication_id",
        "closed_at",
        "dismissed_at",
        "created_at",
    }


def test_a_dismiss_of_a_closed_case_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_dismiss(*args: object, **kwargs: object) -> AttributionCase:
        raise attribution.AttributionCaseNotOpenError

    monkeypatch.setattr(attribution, "dismiss_case", failing_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(uuid4()))

    assert response.status_code == 409
    # One code for the three terminal states: the case has left the awaits,
    # and which way it settled is the row's own fact.
    assert response.json()["detail"] == "already_closed"


def test_a_dismiss_of_an_unknown_or_cross_tenant_case_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_dismiss(*args: object, **kwargs: object) -> AttributionCase:
        raise attribution.AttributionCaseNotFoundError

    monkeypatch.setattr(attribution, "dismiss_case", failing_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(uuid4()))

    # One answer for both, deliberately: whether the id belongs to another
    # tenant is not this deployment's to reveal.
    assert response.status_code == 404


# --- The forced processing ---


def test_a_process_reports_the_outcome_and_the_resolved_case(
    monkeypatch: pytest.MonkeyPatch, scanner_seam: FakeScanner
) -> None:
    case_id, communication_id = uuid4(), uuid4()
    row = case_row(
        case_id=case_id,
        status=AttributionCaseStatus.RESOLVED,
        ask_kind=None,
        resolved_communication_id=communication_id,
    )
    report = ProcessReport(
        case_id=case_id,
        company_id=COMPANY_ID,
        outcome=IngestOutcome.INGESTED,
        resolved=True,
        communication_id=communication_id,
    )
    seen: dict[str, object] = {}

    async def fake_process(
        session: object,
        store: object,
        scanner: object,
        *,
        tenant_id: UUID,
        case_id: UUID,
        actor_user_id: UUID,
    ) -> attribution.CaseProcessResult:
        seen.update(
            tenant_id=tenant_id,
            case_id=case_id,
            actor_user_id=actor_user_id,
            store=store,
            scanner=scanner,
        )
        return attribution.CaseProcessResult(case=row, report=report)

    # The store the route hands on is the deployment's own client (here a
    # sentinel standing in for it) and the scanner is the one seam
    # (``app.application.scanning.scanner``, the fake the suite installs) — a
    # route that built either of its own would fail this.
    store = object()
    monkeypatch.setattr(attribution, "process_open_case", fake_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT, store=store)

    response = client.post(process_url(case_id))

    assert response.status_code == 200, response.text
    # The acting administrator and the actor's tenant are what the service is
    # given; a company id is not, and the route accepts none.
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["case_id"] == case_id
    assert seen["actor_user_id"] == ADMIN.user_id
    assert seen["store"] is store
    assert seen["scanner"] is scanner_seam
    body = response.json()
    assert set(body) == {
        "case_id",
        "company_id",
        "status",
        "outcome",
        "resolved",
        "reason",
        "communication_id",
    }
    assert body["case_id"] == str(case_id)
    assert body["company_id"] == str(COMPANY_ID)
    assert body["status"] == "resolved"
    assert body["outcome"] == "ingested"
    assert body["resolved"] is True
    assert body["reason"] is None
    assert body["communication_id"] == str(communication_id)


def test_a_process_that_the_gate_denied_keeps_the_case_awaiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = uuid4()
    row = case_row(case_id=case_id)
    report = ProcessReport(
        case_id=case_id,
        company_id=COMPANY_ID,
        outcome=IngestOutcome.DENIED,
        resolved=False,
        reason="service_not_included",
    )

    async def fake_process(*args: object, **kwargs: object) -> attribution.CaseProcessResult:
        return attribution.CaseProcessResult(case=row, report=report)

    monkeypatch.setattr(attribution, "process_open_case", fake_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(process_url(case_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["resolved"] is False
    assert body["communication_id"] is None
    # The gate's own bounded reason, passed through: staff can process again.
    assert body["reason"] == "service_not_included"


def test_a_process_of_a_closed_case_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_process(*args: object, **kwargs: object) -> attribution.CaseProcessResult:
        raise attribution.AttributionCaseNotOpenError

    monkeypatch.setattr(attribution, "process_open_case", failing_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(process_url(uuid4()))

    assert response.status_code == 409
    assert response.json()["detail"] == "already_closed"


def test_a_process_of_an_unknown_or_cross_tenant_case_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_process(*args: object, **kwargs: object) -> attribution.CaseProcessResult:
        raise attribution.AttributionCaseNotFoundError

    monkeypatch.setattr(attribution, "process_open_case", failing_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(process_url(uuid4()))

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("raised", "status", "expected"),
    [
        ("not_connected", 409, "not_connected"),
        ("refresh", 502, "mail_refresh_failed:invalid_grant"),
        ("unavailable", 502, "mail_unavailable"),
        ("refused", 502, "mail_read_refused:FAILED_PRECONDITION"),
        ("invalid", 502, "invalid_response:missing_field:id"),
    ],
)
def test_a_process_reports_mailbox_failures_typed(
    monkeypatch: pytest.MonkeyPatch, raised: str, status: int, expected: str
) -> None:
    from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
    from app.application.mail import MailNotConnected, MailRefreshFailed
    from app.domain.mail import MailResponseInvalid

    def failure() -> Exception:
        if raised == "not_connected":
            return MailNotConnected
        if raised == "refresh":
            return MailRefreshFailed("invalid_grant")
        if raised == "unavailable":
            return MailProviderUnavailable("GET /gmail/v1/users/me/messages")
        if raised == "refused":
            return MailProviderRefused("FAILED_PRECONDITION")
        return MailResponseInvalid("missing_field:id")

    async def failing_process(*args: object, **kwargs: object) -> attribution.CaseProcessResult:
        raise failure()

    monkeypatch.setattr(attribution, "process_open_case", failing_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(process_url(uuid4()))

    # The ingestion trigger's own vocabulary, mirrored: an unreachable mailbox
    # is refused the same way wherever the deployment met it.
    assert response.status_code == status
    assert response.json()["detail"] == expected


def test_a_process_of_an_awaiting_company_less_case_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling D9: staff cannot force-process what has no company. The typed
    refusal is the route's own 409 — ``attribution_case_has_no_company`` —
    beside ``already_closed``, and the mailbox is never touched (the service
    raises before it resolves a reader)."""

    async def failing_process(*args: object, **kwargs: object) -> attribution.CaseProcessResult:
        raise attribution.AttributionCaseHasNoCompanyError

    monkeypatch.setattr(attribution, "process_open_case", failing_process)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(process_url(uuid4()))

    assert response.status_code == 409
    assert response.json()["detail"] == "attribution_case_has_no_company"


def test_the_list_carries_a_company_less_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clarification loop's cases surface with ``company_id: null`` — the
    row names its company only when it has one (D2/D9's list shape)."""

    async def fake_list(
        session: object, *, tenant_id: UUID, statuses: object
    ) -> list[AttributionCase]:
        return [
            case_row(
                case_id=uuid4(),
                company_id=None,
                status=AttributionCaseStatus.AWAITING_CLARIFICATION,
                ask_kind=AttributionAskKind.CLARIFICATION,
            )
        ]

    monkeypatch.setattr(attribution, "list_cases", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(ATTRIBUTION_URL)

    assert response.status_code == 200, response.text
    (entry,) = response.json()
    assert entry["company_id"] is None
    assert entry["status"] == "awaiting_clarification"
    assert entry["ask_kind"] == "clarification"
