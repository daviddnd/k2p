"""API-layer tests for the sending routes (contract §5.1.8, §5.1.11).

No database and no provider: the auth seam, RBAC context, session and storage
are overridden, and the application services are monkeypatched, so what runs is
the fail-closed authentication, the grant-gated authorization, and every typed
error → HTTP mapping the send path can answer — including the refusal classes
the delivery reports. The real flow against PostgreSQL (the outbox row, the
gate consult, the reconcile) lives in ``test_mail_send_scenarios_db.py``.

The mappings are the point: a caller has to be able to tell "the period is not
recorded" from "the deployment has no mailbox" from "the provider refused",
because the remedy is different in each case.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import mail_send, outbox
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
REQUEST_ID = uuid4()
ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True, role=Role.GENERAL, permissions=frozenset(), company_ids=frozenset({COMPANY_ID})
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True, role=Role.GENERAL, permissions=frozenset(Permission), company_ids=frozenset()
)

SEND_URL = f"/api/v1/companies/{COMPANY_ID}/confirmation-requests/{REQUEST_ID}/send"
CYCLE_URL = "/api/v1/mail/send"


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


class FakeSession:
    """The one thing the send route does with a session: commit it.

    Deliberately more than a ``None`` stub: the route commits between the
    release and the delivery (ADR-0011's rule, and §5.1.9's commit-before-the-
    refusal discipline), so the seam has to carry that call.
    """

    commits: int = 0

    async def commit(self) -> None:
        FakeSession.commits += 1


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield FakeSession()  # type: ignore[misc]  # the services are monkeypatched here

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def outcome(
    *,
    disposition: mail_send.SendDisposition,
    reason: str | None = None,
    provider_message_id: str | None = None,
) -> mail_send.SendOutcome:
    return mail_send.SendOutcome(
        outbox_id=uuid4(),
        confirmation_request_id=REQUEST_ID,
        company_id=COMPANY_ID,
        disposition=disposition,
        reason=reason,
        provider_message_id=provider_message_id,
    )


class FakeRelease:
    """What ``outbox.release`` hands back, without a database."""

    def __init__(self) -> None:
        self.request = _Request()
        self.job = object()


class _Request:
    """The request row as the route reads it back after a successful send."""

    id = REQUEST_ID
    company_id = COMPANY_ID
    form_type = "nar1"
    target_date = datetime(2027, 3, 1, tzinfo=UTC).date()
    reply_by = datetime(2027, 2, 9, tzinfo=UTC).date()
    statutory_due = datetime(2027, 4, 12, tzinfo=UTC).date()
    status = "sent"
    version_no = 1
    document_id = uuid4()
    recipients: ClassVar[list[dict[str, str]]] = [
        {"person_id": str(uuid4()), "address": "a@b.example"}
    ]
    checklist: ClassVar[list[dict[str, object]]] = []
    created_by = None
    created_at = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def patch_release(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_release(*args: object, **kwargs: object) -> FakeRelease:
        return FakeRelease()

    monkeypatch.setattr(outbox, "release", fake_release)


def patch_delivery(
    monkeypatch: pytest.MonkeyPatch, result: mail_send.SendOutcome
) -> None:
    async def fake_deliver(*args: object, **kwargs: object) -> mail_send.SendOutcome:
        return result

    monkeypatch.setattr(mail_send, "deliver_one", fake_deliver)


# --- The send route -------------------------------------------------------


def test_the_send_route_fails_closed_without_a_session_or_a_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_release(monkeypatch)
    assert build_client().post(SEND_URL).status_code == 401
    forbidden = build_client(actor=ADMIN, context=GENERAL_WITHOUT_GRANT).post(SEND_URL)
    assert forbidden.status_code == 403


def test_a_granted_account_releases_and_gets_the_request_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_release(monkeypatch)
    patch_delivery(
        monkeypatch,
        outcome(
            disposition=mail_send.SendDisposition.SENT, provider_message_id="provider-1"
        ),
    )
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.post(SEND_URL)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    assert response.json()["id"] == str(REQUEST_ID)


def test_an_unknown_request_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*args: object, **kwargs: object) -> FakeRelease:
        raise outbox.RequestNotFoundError

    monkeypatch.setattr(outbox, "release", missing)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 404


@pytest.mark.parametrize("reason", ["request_superseded"])
def test_a_request_that_cannot_be_released_again_is_a_typed_409(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    async def refuse(*args: object, **kwargs: object) -> FakeRelease:
        raise outbox.RequestNotSendableError(reason)

    monkeypatch.setattr(outbox, "release", refuse)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == reason


# --- The send route's reminder extension (§5.1.11, extended 2026-09-29) -----


def patch_already_sent_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """A press on a request whose letter has already left."""

    async def refuse(*args: object, **kwargs: object) -> FakeRelease:
        raise outbox.RequestNotSendableError("already_sent")

    async def fetch_request(*args: object, **kwargs: object) -> "_Request":
        return _Request()

    monkeypatch.setattr(outbox, "release", refuse)
    monkeypatch.setattr(outbox, "get_request", fetch_request)


def patch_held_reminders(
    monkeypatch: pytest.MonkeyPatch, jobs: tuple[object, ...]
) -> list[object]:
    """What ``outbox.release_reminders`` hands back, without a database."""

    async def fake_release_reminders(*args: object, **kwargs: object) -> tuple[object, ...]:
        return jobs

    monkeypatch.setattr(outbox, "release_reminders", fake_release_reminders)
    return list(jobs)


def test_a_sent_request_releases_and_delivers_its_held_reminders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §5.1.11 row's 2026-09-29 extension: the press moves the request's
    held reminder letters to the press instant and delivers each one, then
    answers 200 with the request (``status: sent``) exactly as a send does."""
    patch_already_sent_release(monkeypatch)
    jobs = patch_held_reminders(monkeypatch, (object(), object()))
    delivered: list[object] = []
    FakeSession.commits = 0

    async def fake_deliver(*args: object, **kwargs: object) -> mail_send.SendOutcome:
        delivered.append(kwargs["job"])
        return outcome(
            disposition=mail_send.SendDisposition.SENT, provider_message_id="provider-9"
        )

    monkeypatch.setattr(mail_send, "deliver_one", fake_deliver)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    assert response.json()["id"] == str(REQUEST_ID)
    assert delivered == jobs
    # The same session/commit discipline as the single-row path: the release
    # is durable before anything leaves, and each row's outcome commits.
    assert FakeSession.commits == 1 + len(jobs)


def test_a_sent_request_with_nothing_held_still_answers_already_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing held → the 409 the idempotence rule has always answered; the
    manual release stays legal only while a follow-up letter waits on it —
    and since the release path widened, "nothing held" means neither a
    reminder row nor a reconfirmation row (the empty release is one answer
    for both kinds)."""
    patch_already_sent_release(monkeypatch)
    patch_held_reminders(monkeypatch, ())

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == "already_sent"


def test_a_released_reminder_meets_the_same_typed_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery each released row runs is the sender's own: a refusal it
    reaches answers with the same mapping the single-row path uses."""
    patch_already_sent_release(monkeypatch)
    patch_held_reminders(monkeypatch, (object(),))

    async def fake_deliver(*args: object, **kwargs: object) -> mail_send.SendOutcome:
        return outcome(
            disposition=mail_send.SendDisposition.REFUSED,
            reason="continuation_not_recorded: 2027",
        )

    monkeypatch.setattr(mail_send, "deliver_one", fake_deliver)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == "continuation_not_recorded: 2027"


# --- The widened release path: held reconfirmation letters (M5b task 4) ------


def test_a_sent_request_releases_and_delivers_its_held_reconfirmation_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release path Task 4 widened is one mechanism: a press on the sent
    request moves the held reconfirmation row (a review_required deployment's
    parked letter) to the press instant and delivers it with the sender's own
    per-row order — 200 and the request back, exactly as a reminder's release."""
    patch_already_sent_release(monkeypatch)
    jobs = patch_held_reminders(monkeypatch, (object(),))
    delivered: list[object] = []
    FakeSession.commits = 0

    async def fake_deliver(*args: object, **kwargs: object) -> mail_send.SendOutcome:
        delivered.append(kwargs["job"])
        return outcome(disposition=mail_send.SendDisposition.SENT, provider_message_id="provider-7")

    monkeypatch.setattr(mail_send, "deliver_one", fake_deliver)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    assert delivered == jobs
    assert FakeSession.commits == 1 + len(jobs)


@pytest.mark.parametrize(
    ("result", "status", "detail"),
    [
        # The release gate: its own detail, exactly as the request's own send.
        (
            outcome(
                disposition=mail_send.SendDisposition.REFUSED,
                reason="continuation_not_recorded: 2027",
            ),
            409,
            "continuation_not_recorded: 2027",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.REFUSED,
                reason="service_not_in_scope: form_not_covered",
            ),
            409,
            "service_not_in_scope: form_not_covered",
        ),
        # The sender's own answers: no mailbox, a deferred send, an
        # unconfirmed outcome — the same classes a reminder's delivery maps.
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason=mail_send.REASON_NOT_CONFIGURED,
            ),
            503,
            "mail_not_configured",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="document_not_available:pending",
            ),
            409,
            "document_not_available:pending",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="mail_send_deferred:UNAVAILABLE",
            ),
            502,
            "mail_send_deferred:UNAVAILABLE",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason=mail_send.REASON_UNCONFIRMED,
            ),
            502,
            "mail_send_unconfirmed",
        ),
    ],
)
def test_a_released_reconfirmation_letter_meets_the_same_typed_refusals(
    monkeypatch: pytest.MonkeyPatch, result: mail_send.SendOutcome, status: int, detail: str
) -> None:
    """Task 4's flag, answered: the widened release path delivers the letter
    through the sender's own delivery, so the route's refusal mapping holds
    for a letter row unaltered — 409 for the queue and the gate's refusals,
    503 for no mailbox, 502 for the provider, each with its own code."""
    patch_already_sent_release(monkeypatch)
    patch_held_reminders(monkeypatch, (object(),))

    async def fake_deliver(*args: object, **kwargs: object) -> mail_send.SendOutcome:
        return result

    monkeypatch.setattr(mail_send, "deliver_one", fake_deliver)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail


@pytest.mark.parametrize(
    ("result", "status", "detail"),
    [
        # The release gate: its own detail, both period legs and a scope leg.
        (
            outcome(
                disposition=mail_send.SendDisposition.REFUSED,
                reason="continuation_not_recorded: 2027",
            ),
            409,
            "continuation_not_recorded: 2027",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.REFUSED, reason="payment_not_received: 2027"
            ),
            409,
            "payment_not_received: 2027",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.REFUSED,
                reason="service_not_in_scope: form_not_covered",
            ),
            409,
            "service_not_in_scope: form_not_covered",
        ),
        # The queue's own answers.
        (outcome(disposition=mail_send.SendDisposition.CANCELLED, reason="request_superseded"), 409,
         "request_superseded"),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason=mail_send.REASON_NOT_CONFIGURED,
            ),
            503,
            "mail_not_configured",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="document_not_available:pending",
            ),
            409,
            "document_not_available:pending",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="mail_send_deferred:UNAVAILABLE",
            ),
            502,
            "mail_send_deferred:UNAVAILABLE",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason=mail_send.REASON_UNCONFIRMED,
            ),
            502,
            "mail_send_unconfirmed",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.FAILED,
                reason="mail_send_failed:smtp_550",
            ),
            502,
            "mail_send_failed:smtp_550",
        ),
        # The guard-only outcomes (§5.1.3): facts about the request's own
        # state, not provider failures, so the truthful answer is 409. The
        # raced-delivery answer — another cycle had the row between this
        # route's release and its delivery, and the sender answered ``skipped``
        # before anything left (the outbox's locked select) — and the letter's
        # document row gone.
        (
            outcome(disposition=mail_send.SendDisposition.SKIPPED, reason="not_pending"),
            409,
            "not_pending",
        ),
        (
            outcome(disposition=mail_send.SendDisposition.WAITING, reason="document_missing"),
            409,
            "document_missing",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="mail_refresh_failed:invalid_grant",
            ),
            502,
            "mail_refresh_failed:invalid_grant",
        ),
        # The provider failures *before* a message exists to send: the
        # credential could not be renewed, the reconciliation lookup was
        # refused, and the provider could not be reached — the ingestion
        # trigger's own strings, because one situation is named one way.
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="mail_read_refused:RESOURCE_EXHAUSTED",
            ),
            502,
            "mail_read_refused:RESOURCE_EXHAUSTED",
        ),
        (
            outcome(disposition=mail_send.SendDisposition.WAITING, reason="mail_unavailable"),
            502,
            "mail_unavailable",
        ),
        (
            outcome(
                disposition=mail_send.SendDisposition.WAITING,
                reason="invalid_response:invalid_field:messages",
            ),
            502,
            "invalid_response:invalid_field:messages",
        ),
    ],
)
def test_every_delivery_outcome_maps_to_a_typed_answer(
    monkeypatch: pytest.MonkeyPatch, result: mail_send.SendOutcome, status: int, detail: str
) -> None:
    """The remedy is different in each case, so the answer is too."""
    patch_release(monkeypatch)
    patch_delivery(monkeypatch, result)

    response = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(SEND_URL)

    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail


# --- The cycle route ------------------------------------------------------


def test_the_cycle_route_is_administrator_only() -> None:
    assert build_client().post(CYCLE_URL).status_code == 401
    assert build_client(actor=ADMIN, context=GENERAL_WITH_GRANT).post(CYCLE_URL).status_code == 403


def test_the_cycle_route_reports_every_row_it_looked_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts and codes, never a message — and no row's failure is an HTTP 500."""
    report = mail_send.SendCycleReport(
        examined=3,
        sent=1,
        outcomes=(
            outcome(disposition=mail_send.SendDisposition.SENT, provider_message_id="provider-1"),
            outcome(
                disposition=mail_send.SendDisposition.REFUSED,
                reason="payment_not_received: 2027",
            ),
            outcome(
                disposition=mail_send.SendDisposition.WAITING, reason="mail_send_unconfirmed"
            ),
        ),
    )

    async def fake_cycle(*args: object, **kwargs: object) -> mail_send.SendCycleReport:
        return report

    monkeypatch.setattr(mail_send, "run_cycle", fake_cycle)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(CYCLE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["sent"], body["refused"], body["waiting"]) == (3, 1, 1, 1)
    assert body["outcomes"][0]["provider_message_id"] == "provider-1"
    assert body["outcomes"][1]["reason"] == "payment_not_received: 2027"
    assert body["outcomes"][2]["disposition"] == "waiting"
