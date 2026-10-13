"""API-layer tests for the §2.5 quarantine review surface (scope §2.5).

These run without a database, a mailbox, or an object store: the auth seam,
the RBAC context, and the session are overridden and the application service
is monkeypatched, so only fail-closed authentication, the single
Administrator-only check (``require_admin``), the typed refusals, the list's
status filter, and the response shapes are exercised. What is behind them —
re-examining one provider message id through the ingestion cycle's own
per-message order, the row's resolution, the dismissal, and the audit entries
that ride them — runs against migrated PostgreSQL in
``test_mail_ingest_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.quarantine import QuarantinedMessage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import quarantine
from app.application.mail_ingest import MessageOutcome, ResolutionSummary
from app.domain.communications import IngestOutcome
from app.domain.quarantine import QuarantineReason, QuarantineStatus
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app
from tests.fake_scanning import FakeScanner

QUARANTINE_URL = "/api/v1/mail/quarantine"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)

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


def retry_url(quarantine_id: UUID) -> str:
    return f"{QUARANTINE_URL}/{quarantine_id}/retry"


def dismiss_url(quarantine_id: UUID) -> str:
    return f"{QUARANTINE_URL}/{quarantine_id}/dismiss"


def quarantined_row(
    *,
    quarantine_id: UUID,
    mailbox_message_id: str = "msg-1",
    reason: QuarantineReason = QuarantineReason.UNREGISTERED_SENDER,
    status: QuarantineStatus = QuarantineStatus.PENDING,
    sender_address: str = "stranger@elsewhere.example",
    resolved_at: datetime | None = None,
    dismissed_at: datetime | None = None,
    communication_id: UUID | None = None,
) -> QuarantinedMessage:
    """One row as the table holds it, with the columns the review surface reads."""
    return QuarantinedMessage(
        id=quarantine_id,
        tenant_id=ADMIN.tenant_id,
        mailbox_message_id=mailbox_message_id,
        thread_id=f"thread-{mailbox_message_id}",
        sender_address=sender_address,
        received_at=NOW,
        reason=reason.value,
        status=status.value,
        resolved_at=resolved_at,
        dismissed_at=dismissed_at,
        resolved_communication_id=communication_id,
    )


# --- Fail closed; Administrator-only on every route ---


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()

    assert client.get(QUARANTINE_URL).status_code == 401
    assert client.post(retry_url(uuid4())).status_code == 401
    assert client.post(dismiss_url(uuid4())).status_code == 401


def test_the_review_routes_are_administrator_only() -> None:
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)

    assert client.get(QUARANTINE_URL).status_code == 403
    assert client.post(retry_url(uuid4())).status_code == 403
    assert client.post(dismiss_url(uuid4())).status_code == 403


# --- The list ---


def test_the_list_returns_the_rows_the_reviewer_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    newest = quarantined_row(
        quarantine_id=uuid4(),
        mailbox_message_id="msg-2",
        reason=QuarantineReason.MULTIPLE_COMPANIES,
        sender_address="director@client.example",
    )
    oldest = quarantined_row(quarantine_id=uuid4(), mailbox_message_id="msg-1")
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, status: QuarantineStatus | None
    ) -> list[QuarantinedMessage]:
        seen["tenant_id"] = tenant_id
        seen["status"] = status
        return [newest, oldest]

    monkeypatch.setattr(quarantine, "list_quarantined", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(QUARANTINE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    # The tenant is the actor's, decided here and never taken from the caller.
    assert seen["tenant_id"] == ADMIN.tenant_id
    # And the list is the open queue unless the caller says otherwise: it is
    # what staff work from, and closed rows accumulating in it is how a queue
    # stops being read.
    assert seen["status"] is QuarantineStatus.PENDING
    # Newest first, as the service ordered them.
    assert [item["id"] for item in body] == [str(newest.id), str(oldest.id)]
    # Exactly the metadata a reviewer triages on: nothing here could carry what
    # a client wrote (no subject, no body, no attachment).
    assert set(body[0]) == {
        "id",
        "mailbox_message_id",
        "thread_id",
        "sender_address",
        "received_at",
        "reason",
        "status",
        "resolved_at",
        "dismissed_at",
        "communication_id",
    }
    assert body[0]["mailbox_message_id"] == "msg-2"
    assert body[0]["thread_id"] == "thread-msg-2"
    assert body[0]["sender_address"] == "director@client.example"
    assert body[0]["received_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body[0]["reason"] == "multiple_companies"
    assert body[0]["status"] == "pending"
    assert body[0]["resolved_at"] is None
    assert body[0]["dismissed_at"] is None
    assert body[0]["communication_id"] is None


def test_the_list_reports_a_resolved_row_with_the_communication_it_became(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    communication_id = uuid4()
    resolved = quarantined_row(
        quarantine_id=uuid4(),
        status=QuarantineStatus.RESOLVED,
        resolved_at=NOW,
        communication_id=communication_id,
    )

    async def fake_list(
        session: object, *, tenant_id: UUID, status: QuarantineStatus | None
    ) -> list[QuarantinedMessage]:
        return [resolved]

    monkeypatch.setattr(quarantine, "list_quarantined", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(f"{QUARANTINE_URL}?status=resolved").json()

    assert body[0]["status"] == "resolved"
    assert body[0]["communication_id"] == str(communication_id)


def test_an_empty_list_is_an_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(
        session: object, *, tenant_id: UUID, status: QuarantineStatus | None
    ) -> list[QuarantinedMessage]:
        return []

    monkeypatch.setattr(quarantine, "list_quarantined", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(QUARANTINE_URL)

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=pending", QuarantineStatus.PENDING),
        ("status=resolved", QuarantineStatus.RESOLVED),
        ("status=dismissed", QuarantineStatus.DISMISSED),
        # ``all`` is not a status: it is the absence of the filter, which is
        # what the service reads as "every row".
        ("status=all", None),
    ],
)
def test_the_list_filters_to_the_status_the_caller_asks_for(
    monkeypatch: pytest.MonkeyPatch, query: str, expected: QuarantineStatus | None
) -> None:
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, status: QuarantineStatus | None
    ) -> list[QuarantinedMessage]:
        seen["status"] = status
        return []

    monkeypatch.setattr(quarantine, "list_quarantined", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{QUARANTINE_URL}?{query}")

    assert response.status_code == 200, response.text
    assert seen["status"] is expected


def test_an_unknown_status_filter_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[QuarantineStatus | None] = []

    async def fake_list(
        session: object, *, tenant_id: UUID, status: QuarantineStatus | None
    ) -> list[QuarantinedMessage]:
        calls.append(status)
        return []

    monkeypatch.setattr(quarantine, "list_quarantined", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{QUARANTINE_URL}?status=triage_later")

    # Refused by the closed vocabulary, not answered with an empty list: a
    # filter nobody understands must not read as "nothing is waiting".
    assert response.status_code == 422
    assert calls == []


# --- The retry ---


def test_a_retry_reports_the_outcome_and_the_resolved_row(
    monkeypatch: pytest.MonkeyPatch, scanner_seam: FakeScanner
) -> None:
    quarantine_id, communication_id, company_id, document_id = uuid4(), uuid4(), uuid4(), uuid4()
    row = quarantined_row(
        quarantine_id=quarantine_id,
        status=QuarantineStatus.RESOLVED,
        resolved_at=NOW,
        communication_id=communication_id,
    )
    outcome = MessageOutcome(
        mailbox_message_id="msg-1",
        outcome=IngestOutcome.INGESTED,
        company_id=company_id,
        communication_id=communication_id,
        raw_document_id=document_id,
        scan_status="clean",
        quarantine_id=quarantine_id,
    )
    seen: dict[str, object] = {}

    async def fake_retry(
        session: object,
        store: object,
        scanner: object,
        *,
        tenant_id: UUID,
        quarantine_id: UUID,
        actor_user_id: UUID,
    ) -> quarantine.RetryResult:
        seen.update(
            tenant_id=tenant_id,
            quarantine_id=quarantine_id,
            actor_user_id=actor_user_id,
            store=store,
            scanner=scanner,
        )
        return quarantine.RetryResult(row=row, outcome=outcome)

    # The store the route hands on is the deployment's own client (here a
    # sentinel standing in for it) and the scanner is the one seam
    # (``app.application.scanning.scanner``, the fake the suite installs) — a
    # route that built either of its own would fail this.
    store = object()
    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT, store=store)

    response = client.post(retry_url(quarantine_id))

    assert response.status_code == 200, response.text
    # The acting administrator and the actor's tenant are what the service is
    # given; a company id is not, and the route accepts none.
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["quarantine_id"] == quarantine_id
    assert seen["actor_user_id"] == ADMIN.user_id
    assert seen["store"] is store
    assert seen["scanner"] is scanner_seam
    body = response.json()
    assert body["quarantine_id"] == str(quarantine_id)
    assert body["status"] == "resolved"
    assert body["resolved_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["communication_id"] == str(communication_id)
    assert body["outcome"]["outcome"] == "ingested"
    assert body["outcome"]["company_id"] == str(company_id)
    assert body["outcome"]["communication_id"] == str(communication_id)
    assert body["outcome"]["raw_document_id"] == str(document_id)
    assert body["outcome"]["scan_status"] == "clean"
    assert body["outcome"]["attachments"] == []


def test_a_retry_reports_the_resolvers_decision_as_a_typed_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the rule saw, mapped onto the wire field for field.

    Nothing here can prove the matched text is absent — this fixture carries
    none, so a summary that leaked some would still pass. That check is the
    DB scenario ``test_the_matched_text_never_travels_in_a_report``, which
    plants real matched text and searches both candidates' words in the cycle
    report, in the retry response and in the audit entry. What this test pins
    is the shape: named fields, not a free-form dict.
    """
    quarantine_id = uuid4()
    first, second = uuid4(), uuid4()
    row = quarantined_row(quarantine_id=quarantine_id)
    outcome = MessageOutcome(
        mailbox_message_id="msg-1",
        outcome=IngestOutcome.AMBIGUOUS_SENDER,
        reason="multiple_companies",
        quarantine_id=quarantine_id,
        resolution=ResolutionSummary(
            case="ambiguous",
            evidence=None,
            score=0.0,
            routing="client_clarification",
            candidate_ids=(first, second),
        ),
    )

    async def fake_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        return quarantine.RetryResult(row=row, outcome=outcome)

    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(quarantine_id))

    assert response.status_code == 200, response.text
    # A typed shape with named fields, not a free-form dict: the case the rule
    # reached, the tier that decided it, the score, the band it routes to, and
    # the candidates it considered.
    assert response.json()["outcome"]["resolution"] == {
        "case": "ambiguous",
        "evidence_tier": None,
        "score": 0.0,
        "routing": "client_clarification",
        "candidate_company_ids": [str(first), str(second)],
    }


def test_a_retry_that_still_cannot_attribute_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine_id = uuid4()
    row = quarantined_row(quarantine_id=quarantine_id)
    outcome = MessageOutcome(
        mailbox_message_id="msg-1",
        outcome=IngestOutcome.AMBIGUOUS_SENDER,
        reason="multiple_companies",
        quarantine_id=quarantine_id,
    )

    async def fake_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        return quarantine.RetryResult(row=row, outcome=outcome)

    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(quarantine_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["resolved_at"] is None
    assert body["communication_id"] is None
    assert body["outcome"]["outcome"] == "ambiguous_sender"
    assert body["outcome"]["reason"] == "multiple_companies"
    assert body["outcome"]["quarantine_id"] == str(quarantine_id)
    # The resolver did not run for this outcome, and the report says so rather
    # than inventing a decision: ``null`` is the honest answer.
    assert body["outcome"]["resolution"] is None


def test_a_retry_of_an_already_resolved_row_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        raise quarantine.QuarantineAlreadyResolvedError

    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(uuid4()))

    assert response.status_code == 409
    # The detail is the whole difference between this 409 and the one a
    # deployment without a mailbox answers (``not_connected``).
    assert response.json()["detail"] == "already_resolved"


def test_a_retry_of_a_dismissed_row_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row staff dismissed is closed to the retry, and says which way it closed."""

    async def fake_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        raise quarantine.QuarantineAlreadyDismissedError

    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(uuid4()))

    # Not ``already_resolved``: the two are different situations — a resolved
    # row has a Communication behind it, a dismissed one never will.
    assert response.status_code == 409
    assert response.json()["detail"] == "already_dismissed"


def test_a_retry_of_an_unknown_or_cross_tenant_row_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        raise quarantine.QuarantineNotFoundError

    monkeypatch.setattr(quarantine, "retry", fake_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(uuid4()))

    # One answer for both, deliberately: whether the id belongs to another
    # tenant is not this deployment's to reveal.
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
def test_a_retry_reports_provider_failures_typed(
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

    async def failing_retry(*args: object, **kwargs: object) -> quarantine.RetryResult:
        raise failure()

    monkeypatch.setattr(quarantine, "retry", failing_retry)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(retry_url(uuid4()))

    assert response.status_code == status
    assert response.json()["detail"] == expected


# --- The dismiss ---


def test_a_dismiss_reports_the_row_as_it_now_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine_id = uuid4()
    row = quarantined_row(
        quarantine_id=quarantine_id,
        status=QuarantineStatus.DISMISSED,
        dismissed_at=NOW,
    )
    seen: dict[str, object] = {}

    async def fake_dismiss(
        session: object,
        *,
        tenant_id: UUID,
        quarantine_id: UUID,
        actor_user_id: UUID,
    ) -> QuarantinedMessage:
        seen.update(
            tenant_id=tenant_id,
            quarantine_id=quarantine_id,
            actor_user_id=actor_user_id,
        )
        return row

    monkeypatch.setattr(quarantine, "dismiss", fake_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(quarantine_id))

    assert response.status_code == 200, response.text
    # The tenant is the actor's and the acting administrator is recorded; the
    # route reads no mailbox, so it takes no store and no scanner.
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["quarantine_id"] == quarantine_id
    assert seen["actor_user_id"] == ADMIN.user_id
    body = response.json()
    assert body["id"] == str(quarantine_id)
    assert body["status"] == "dismissed"
    assert body["dismissed_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["resolved_at"] is None
    assert body["communication_id"] is None
    # The row as the review list shows it, and nothing else: a dismiss answers
    # with the record staff acted on, not with a report about the mail.
    assert set(body) == {
        "id",
        "mailbox_message_id",
        "thread_id",
        "sender_address",
        "received_at",
        "reason",
        "status",
        "resolved_at",
        "dismissed_at",
        "communication_id",
    }


@pytest.mark.parametrize(
    ("raised", "expected"),
    [("resolved", "already_resolved"), ("dismissed", "already_dismissed")],
)
def test_a_dismiss_of_a_closed_row_is_a_typed_conflict(
    monkeypatch: pytest.MonkeyPatch, raised: str, expected: str
) -> None:
    async def failing_dismiss(*args: object, **kwargs: object) -> QuarantinedMessage:
        if raised == "resolved":
            raise quarantine.QuarantineAlreadyResolvedError
        raise quarantine.QuarantineAlreadyDismissedError

    monkeypatch.setattr(quarantine, "dismiss", failing_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(uuid4()))

    assert response.status_code == 409
    assert response.json()["detail"] == expected


def test_a_dismiss_of_an_unknown_or_cross_tenant_row_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_dismiss(*args: object, **kwargs: object) -> QuarantinedMessage:
        raise quarantine.QuarantineNotFoundError

    monkeypatch.setattr(quarantine, "dismiss", failing_dismiss)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(dismiss_url(uuid4()))

    # One answer for both, deliberately: whether the id belongs to another
    # tenant is not this deployment's to reveal.
    assert response.status_code == 404
