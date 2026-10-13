"""API-layer tests for the communication read (data-model §2.9).

These run without a database: the auth seam, RBAC context, session, and the
service are overridden, so what is exercised is fail-closed authentication,
the grant-gated authorization check (``require_company_access``), the unknown
company mapping, and the shape an operator reads.

The property this file guards is negative and deliberate: a communication
response carries ids and metadata, and **never** a mail body. The service is
what reads the row; the response model has no field a body could travel in.
A real ingestion cycle and its stored message live in
``test_mail_ingest_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.communications import Communication
from app.adapters.persistence.reply_parsing import ReplyParse
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import communications, reply_parsing
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
COMMUNICATION_ID = uuid4()
COMPANY_COMMUNICATIONS_URL = f"/api/v1/companies/{COMPANY_ID}/communications"
PARSE_URL = f"{COMPANY_COMMUNICATIONS_URL}/{COMMUNICATION_ID}/parse"

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

RECEIVED_AT = datetime(2026, 9, 23, 1, 31, tzinfo=UTC)


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is stubbed in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def row() -> Communication:
    """A communication row as the service would return one (no session needed)."""
    return Communication(
        id=uuid4(),
        tenant_id=ADMIN.tenant_id,
        company_id=COMPANY_ID,
        direction="inbound",
        channel="email",
        mailbox_message_id="msg-1",
        thread_id="thread-msg-1",
        sender_address="director@client.example",
        language=None,
        received_at=RECEIVED_AT,
        created_at=RECEIVED_AT,
        raw_document_id=uuid4(),
    )


def test_the_read_fails_closed_without_a_session_or_a_grant() -> None:
    assert build_client().get(COMPANY_COMMUNICATIONS_URL).status_code == 401
    assert (
        build_client(
            actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
            context=GENERAL_WITHOUT_GRANT,
        ).get(COMPANY_COMMUNICATIONS_URL).status_code
        == 403
    )


def test_a_granted_account_reads_metadata_and_never_a_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = row()

    async def fake_list(*args: object, **kwargs: object) -> list[Communication]:
        return [record]

    async def no_parses(
        session: object, *, tenant_id: UUID, company_id: UUID
    ) -> dict[UUID, ReplyParse]:
        return {}

    monkeypatch.setattr(communications, "list_company_communications", fake_list)
    monkeypatch.setattr(reply_parsing, "latest_parses", no_parses)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )

    response = client.get(COMPANY_COMMUNICATIONS_URL)

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": str(record.id),
            "company_id": str(COMPANY_ID),
            "direction": "inbound",
            "channel": "email",
            "mailbox_message_id": "msg-1",
            "thread_id": "thread-msg-1",
            "sender_address": "director@client.example",
            # Inbound: the counterparty is the sender, and neither of the
            # outbound fields applies.
            "recipient_addresses": None,
            "confirmation_request_id": None,
            "language": None,
            # Nothing has parsed this reply yet.
            "parse_status": None,
            "parse_routing": None,
            "received_at": "2026-09-23T01:31:00Z",
            "created_at": "2026-09-23T01:31:00Z",
            "raw_document_id": str(record.raw_document_id),
        }
    ]


def test_an_unknown_company_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(*args: object, **kwargs: object) -> list[Communication]:
        raise CompanyNotFoundError

    monkeypatch.setattr(communications, "list_company_communications", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(COMPANY_COMMUNICATIONS_URL)

    assert response.status_code == 404
    assert response.json()["detail"] == "company not found"


def test_the_route_passes_the_actors_tenant_and_the_path_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, UUID] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, company_id: UUID
    ) -> list[Communication]:
        seen["tenant_id"] = tenant_id
        seen["company_id"] = company_id
        return []

    async def fake_parses(
        session: object, *, tenant_id: UUID, company_id: UUID
    ) -> dict[UUID, ReplyParse]:
        return {}

    monkeypatch.setattr(communications, "list_company_communications", fake_list)
    monkeypatch.setattr(reply_parsing, "latest_parses", fake_parses)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.get(COMPANY_COMMUNICATIONS_URL).json() == []

    assert seen == {"tenant_id": ADMIN.tenant_id, "company_id": COMPANY_ID}


# --- The communications list's parse fields (slice M5b) ---------------------


def parse_row(communication_id: UUID) -> ReplyParse:
    """One parse row as the service hands it back — evidence_span included,
    because the response model's job is to refuse it."""
    return ReplyParse(
        id=uuid4(),
        tenant_id=ADMIN.tenant_id,
        company_id=COMPANY_ID,
        communication_id=communication_id,
        confirmation_request_id=uuid4(),
        status="changes_reported",
        language="en",
        routing="auto_accept",
        confirmations=[
            {
                "fact_id": "company.registered_office.flat",
                "answer": "confirmed",
                # Verbatim client words: they may live in the database, and must
                # not reach this response.
                "evidence_span": "our registered office is unchanged",
                "uncertain": False,
            },
            {
                "fact_id": "filing.member_count",
                "answer": "not_addressed",
                "evidence_span": None,
                "uncertain": True,
            },
        ],
        event_ids=[str(uuid4())],
        created_at=RECEIVED_AT,
    )


def test_the_list_carries_the_newest_parses_status_and_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = row()
    parsed = parse_row(record.id)

    async def fake_list(*args: object, **kwargs: object) -> list[Communication]:
        return [record]

    async def fake_parses(
        session: object, *, tenant_id: UUID, company_id: UUID
    ) -> dict[UUID, ReplyParse]:
        return {record.id: parsed}

    monkeypatch.setattr(communications, "list_company_communications", fake_list)
    monkeypatch.setattr(reply_parsing, "latest_parses", fake_parses)
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.get(COMPANY_COMMUNICATIONS_URL)

    assert response.status_code == 200, response.text
    (item,) = response.json()
    assert item["parse_status"] == "changes_reported"
    assert item["parse_routing"] == "auto_accept"


def test_the_list_reads_null_parse_fields_before_anything_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(*args: object, **kwargs: object) -> list[Communication]:
        return [row()]

    async def fake_parses(
        session: object, *, tenant_id: UUID, company_id: UUID
    ) -> dict[UUID, ReplyParse]:
        return {}

    monkeypatch.setattr(communications, "list_company_communications", fake_list)
    monkeypatch.setattr(reply_parsing, "latest_parses", fake_parses)
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.get(COMPANY_COMMUNICATIONS_URL)

    assert response.status_code == 200, response.text
    (item,) = response.json()
    assert item["parse_status"] is None
    assert item["parse_routing"] is None


# --- The newest-parse read (slice M5b) ---------------------------------------


def test_the_parse_read_fails_closed_without_a_session_or_a_grant() -> None:
    assert build_client().get(PARSE_URL).status_code == 401
    assert (
        build_client(
            actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id),
            context=GENERAL_WITHOUT_GRANT,
        ).get(PARSE_URL).status_code
        == 403
    )


def test_the_parse_read_is_metadata_only_and_never_carries_a_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custody at the wire: coordinates only. The stored confirmations carry
    the client's verbatim words; this response's field set has no place for
    a span or a body — the test pins the exact key set at every depth."""
    parsed = parse_row(COMMUNICATION_ID)

    async def fake_latest(
        session: object, *, tenant_id: UUID, company_id: UUID, communication_id: UUID
    ) -> ReplyParse:
        return parsed

    monkeypatch.setattr(reply_parsing, "latest_parse", fake_latest)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )

    response = client.get(PARSE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "communication_id",
        "confirmation_request_id",
        "status",
        "language",
        "routing",
        "confirmations",
        "event_ids",
        "created_at",
    }
    assert body["id"] == str(parsed.id)
    assert body["communication_id"] == str(COMMUNICATION_ID)
    assert body["confirmation_request_id"] == str(parsed.confirmation_request_id)
    assert body["status"] == "changes_reported"
    assert body["language"] == "en"
    assert body["routing"] == "auto_accept"
    assert body["event_ids"] == parsed.event_ids
    assert body["created_at"] == "2026-09-23T01:31:00Z"
    assert [set(item) for item in body["confirmations"]] == [
        {"fact_id", "answer"},
        {"fact_id", "answer"},
    ]
    assert body["confirmations"][0] == {
        "fact_id": "company.registered_office.flat",
        "answer": "confirmed",
    }
    # The stored span must appear nowhere in the payload.
    assert "evidence_span" not in response.text
    assert "our registered office is unchanged" not in response.text


def test_the_parse_read_is_404_for_an_unknown_communication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(
        session: object, *, tenant_id: UUID, company_id: UUID, communication_id: UUID
    ) -> ReplyParse:
        raise reply_parsing.CommunicationNotFoundError

    monkeypatch.setattr(reply_parsing, "latest_parse", missing)
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.get(PARSE_URL)

    assert response.status_code == 404
    assert response.json()["detail"] == "communication not found"


def test_the_parse_read_is_404_not_parsed_until_a_cycle_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(
        session: object, *, tenant_id: UUID, company_id: UUID, communication_id: UUID
    ) -> ReplyParse:
        raise reply_parsing.ParseNotFoundError

    monkeypatch.setattr(reply_parsing, "latest_parse", missing)
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    response = client.get(PARSE_URL)

    assert response.status_code == 404
    assert response.json()["detail"] == "not_parsed"


def test_the_parse_read_scopes_the_read_to_the_path_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-company and cross-tenant ids are one 404, the tenant-scoped read
    the service owns — the route passes the actor's tenant and the path's
    company through and reports what the service decided."""
    seen: dict[str, UUID] = {}

    async def fake_latest(
        session: object, *, tenant_id: UUID, company_id: UUID, communication_id: UUID
    ) -> ReplyParse:
        seen["tenant_id"] = tenant_id
        seen["company_id"] = company_id
        seen["communication_id"] = communication_id
        raise reply_parsing.CommunicationNotFoundError

    monkeypatch.setattr(reply_parsing, "latest_parse", fake_latest)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )

    response = client.get(PARSE_URL)

    assert response.status_code == 404
    assert seen == {
        "tenant_id": ADMIN.tenant_id,
        "company_id": COMPANY_ID,
        "communication_id": COMMUNICATION_ID,
    }
