"""API-layer tests for the confirmation-request endpoints (contract §5.1.8).

These run without a database or object store: the auth seam, RBAC context,
session, and storage are overridden, and the application service is
monkeypatched, so only fail-closed authentication, the grant-gated
authorization check (``require_company_access``), request validation, and the
typed-error → HTTP mappings are exercised — including every ``GatingReason``
surfacing verbatim. The real flow against PostgreSQL (the stored message, the
checklist, the custody of what reaches an audit entry) lives in
``test_confirmation_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import confirmation
from app.application.company_master import CompanyNotFoundError
from app.domain.compliance import DueDateRuleNotEstablishedError
from app.domain.confirmation import (
    CompanyNameUnavailableError,
    NoClientReplyWindowError,
    UnsupportedFormError,
)
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import GatingReason
from app.main import create_app

COMPANY_ID = uuid4()
DOCUMENT_ID = uuid4()
REQUEST_ID = uuid4()
PERSON_ID = uuid4()
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

REQUESTS_URL = f"/api/v1/companies/{COMPANY_ID}/confirmation-requests"
BODY = {"form_type": "nar1", "target_date": "2026-03-01"}


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


def stored_row() -> ConfirmationRequest:
    """One request row as the service returns it (never persisted here)."""
    return ConfirmationRequest(
        id=REQUEST_ID,
        tenant_id=ADMIN.tenant_id,
        company_id=COMPANY_ID,
        form_type="nar1",
        target_date=date(2026, 3, 1),
        reply_by=date(2026, 2, 9),
        statutory_due=date(2026, 4, 12),
        status="draft",
        version_no=1,
        document_id=DOCUMENT_ID,
        checklist=[
            {
                "fact_id": "company.registered_office",
                "question": "Is the company's registered office still at \"Suite 1201\"?",
                "expected_value": "Suite 1201",
                "source_record_id": None,
            }
        ],
        recipients=[{"person_id": str(PERSON_ID), "address": "director@client.example.hk"}],
        created_by=ADMIN.user_id,
        created_at=datetime(2026, 9, 23, 8, 0, tzinfo=UTC),
    )


def _raise(error: Exception):
    async def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


def _return(row: ConfirmationRequest):
    async def fake(*args: object, **kwargs: object) -> ConfirmationRequest:
        return row

    return fake


# --- Fail closed on both routes ---


def test_unauthenticated_fails_closed_on_both_routes() -> None:
    client = build_client()
    assert client.post(REQUESTS_URL, json=BODY).status_code == 401
    assert client.get(REQUESTS_URL).status_code == 401


def test_general_without_a_company_grant_is_forbidden_on_both_routes() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITHOUT_GRANT
    )
    assert client.post(REQUESTS_URL, json=BODY).status_code == 403
    assert client.get(REQUESTS_URL).status_code == 403


def test_an_admin_and_a_granted_general_may_both_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant-only authorization: drafting is ordinary staff work (§2.16)."""
    monkeypatch.setattr(confirmation, "generate", _return(stored_row()))

    for context in (ADMIN_CONTEXT, GENERAL_WITH_GRANT):
        client = build_client(
            actor=ActorRef(user_id=ADMIN.user_id, tenant_id=ADMIN.tenant_id), context=context
        )
        assert client.post(REQUESTS_URL, json=BODY).status_code == 201


def test_the_trigger_needs_no_renewal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverted ruling 3, pinned on the wire: drafting is not gated, so a
    period nothing has answered — or one the client declined — is drafted like
    any other, and the renewal's vocabulary no longer appears in this route's
    mappings at all. (What such a period cannot do is *release* the letter: the
    download gate reads the record.)"""
    monkeypatch.setattr(confirmation, "generate", _return(stored_row()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(REQUESTS_URL, json=BODY)

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "draft"


# --- Request validation ---


def test_the_trigger_requires_a_known_form_and_a_target_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirmation, "generate", _return(stored_row()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(REQUESTS_URL, json={**BODY, "form_type": "nar2"}).status_code == 422
    assert client.post(REQUESTS_URL, json={"form_type": "nar1"}).status_code == 422
    bad_contact = client.post(REQUESTS_URL, json={**BODY, "contact_person_id": "nope"})
    assert bad_contact.status_code == 422


def test_the_trigger_passes_the_request_through_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route hands the service the actor's ids, the form, the date and today."""
    seen: dict[str, object] = {}

    async def fake(*args: object, **kwargs: object) -> ConfirmationRequest:
        seen.update(kwargs)
        return stored_row()

    monkeypatch.setattr(confirmation, "generate", fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(
        REQUESTS_URL, json={**BODY, "contact_person_id": str(PERSON_ID)}
    )

    assert response.status_code == 201, response.text
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["company_id"] == COMPANY_ID
    assert seen["actor_user_id"] == ADMIN.user_id
    assert seen["form_type"] == "nar1"
    assert seen["target_date"] == date(2026, 3, 1)
    assert seen["contact_person_id"] == PERSON_ID
    assert seen["as_of"] == date.today()


# --- Service error mappings ---


@pytest.mark.parametrize("reason", list(GatingReason))
def test_a_gate_denial_surfaces_its_reason_verbatim(
    monkeypatch: pytest.MonkeyPatch, reason: GatingReason
) -> None:
    monkeypatch.setattr(
        confirmation, "generate", _raise(confirmation.AutomationDeniedError(reason))
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(REQUESTS_URL, json=BODY)

    assert response.status_code == 403
    assert response.json()["detail"] == f"automation_denied: {reason.value}"


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (confirmation.MailNotConfiguredError(), 503, "mail_not_configured"),
        (confirmation.NoClientContactError(), 422, "no_client_contact"),
        (confirmation.ClientContactNotFoundError(), 422, "client_contact_not_found"),
        (confirmation.DraftConflictError(), 409, "draft_conflict"),
        (UnsupportedFormError("nd2a"), 422, "unsupported_form"),
        (DueDateRuleNotEstablishedError("public"), 422, "nar1_rule_not_established"),
        (
            NoClientReplyWindowError(reply_by=date(2026, 4, 19), statutory_due=date(2026, 4, 12)),
            422,
            "no_client_reply_window",
        ),
        (CompanyNameUnavailableError(), 422, "company_name_unavailable"),
    ],
    ids=[
        "mail-not-configured",
        "no-client-contact",
        "client-contact-not-found",
        "draft-conflict",
        "unsupported-form",
        "rule-not-established",
        "reply-window-closed",
        "name-unavailable",
    ],
)
def test_every_refusal_maps_to_its_status_and_code(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, detail: str
) -> None:
    monkeypatch.setattr(confirmation, "generate", _raise(error))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(REQUESTS_URL, json=BODY)

    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_an_unknown_or_cross_tenant_company_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirmation, "generate", _raise(CompanyNotFoundError()))
    monkeypatch.setattr(confirmation, "list_requests", _raise(CompanyNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.post(REQUESTS_URL, json=BODY).status_code == 404
    assert client.get(REQUESTS_URL).status_code == 404


# --- The response shape ---


def test_the_created_request_answers_with_its_parameters_and_its_checklist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirmation, "generate", _return(stored_row()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.post(REQUESTS_URL, json=BODY).json()

    assert body == {
        "id": str(REQUEST_ID),
        "company_id": str(COMPANY_ID),
        "form_type": "nar1",
        "target_date": "2026-03-01",
        "reply_by": "2026-02-09",
        "statutory_due": "2026-04-12",
        "status": "draft",
        "version_no": 1,
        "document_id": str(DOCUMENT_ID),
        "recipients": [
            {"person_id": str(PERSON_ID), "address": "director@client.example.hk"}
        ],
        "checklist": [
            {
                "fact_id": "company.registered_office",
                "question": "Is the company's registered office still at \"Suite 1201\"?",
                "expected_value": "Suite 1201",
                "source_record_id": None,
            }
        ],
        "created_by": str(ADMIN.user_id),
        "created_at": "2026-09-23T08:00:00Z",
    }


def test_the_list_answers_metadata_and_the_checklist_never_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body is the stored document, reached through the documents route.

    The same metadata-only discipline as the inbound-mail read: a response
    here carries the request's parameters and questions, and no rendered
    letter.
    """
    monkeypatch.setattr(confirmation, "list_requests", _return_row_list(stored_row()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(REQUESTS_URL).json()

    assert len(body) == 1
    assert set(body[0]) == {
        "id",
        "company_id",
        "form_type",
        "target_date",
        "reply_by",
        "statutory_due",
        "status",
        "version_no",
        "document_id",
        "recipients",
        "checklist",
        "created_by",
        "created_at",
    }
    assert body[0]["status"] == "draft"
    assert "message" not in body[0]


def _return_row_list(row: ConfirmationRequest):
    async def fake(*args: object, **kwargs: object) -> list[ConfirmationRequest]:
        return [row]

    return fake
