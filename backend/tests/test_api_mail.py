"""API-layer tests for the mailbox (ADR-0005; Phase 3 mailbox and ingestion
slices).

These run without a database: the auth seam, RBAC context, and session are
overridden, so only fail-closed authentication, the single Administrator-only
authorization check (``require_admin``), the one open route (the loopback
callback, which a browser reaches from Google), the typed refusals of the
ingestion trigger, and the shapes the operator reads are exercised. The token
*exchange* end to end — and the audit entry that only a real session can
carry — lives in ``test_mail_scenarios_db.py``, and the ingestion cycle itself
in ``test_mail_ingest_scenarios_db.py``; here the provider is faked for the
refusal paths, the ingestion service is stubbed for the response shape, and
the credential file is written directly for the reads that need one.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.gmail_client import GmailClient
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import mail, mail_ingest
from app.core.config import settings
from app.domain.mail import GMAIL_SCOPES, MailboxCredential, MailConnectionState
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app
from tests.fake_google import FakeGoogle

CONNECTION_URL = "/api/v1/mail/connection"
CALLBACK_URL = "/api/v1/mail/oauth/callback"
PROBE_URL = "/api/v1/mail/probe"
INGEST_URL = "/api/v1/mail/ingest"
CLIENT_ID = "the-deployment-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "the-deployment-client-secret"
MAILBOX = "ai-filings@tcsp.example"
TOKEN_MARKER = "mail-token-marker"
NOW = datetime.now(UTC)

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


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # these routes never reach the session

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def configure(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    """Provision the deployment configuration the connect flow reads."""
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "mail_address", address)


def use_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
    """Point the provider seam at the fake, in-process.

    The fake is paired with the deployment's own client id and secret,
    because it refuses a client it was not configured for exactly as Google
    does.
    """
    google.client_id = settings.google_client_id
    google.client_secret = settings.google_client_secret
    monkeypatch.setattr(
        mail,
        "client",
        lambda: GmailClient(timeout=1.0, transport=httpx.ASGITransport(app=google.app)),
    )


def store() -> MailCredentialStore:
    return MailCredentialStore(settings.mail_credential_file)


def plant_credential(
    *, address: str = MAILBOX, token_expires_at: datetime | None = None
) -> MailboxCredential:
    """A credential as the completed flow would have left it."""
    credential = MailboxCredential(
        access_token=f"{TOKEN_MARKER}-access",
        refresh_token=f"{TOKEN_MARKER}-refresh",
        address=address,
        scopes=GMAIL_SCOPES,
        connected_at=NOW,
        token_expires_at=token_expires_at or NOW + timedelta(hours=1),
    )
    store().save(MailConnectionState(credential=credential))
    return credential


def start_connection(client: TestClient) -> dict[str, object]:
    response = client.post(CONNECTION_URL)
    assert response.status_code == 201, response.text
    return dict(response.json())


def consent_query(client: TestClient) -> dict[str, str]:
    started = start_connection(client)
    return dict(parse_qsl(urlsplit(str(started["authorization_url"])).query))


# --- Fail closed; Administrator-only on everything but the callback ---


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_the_connection_routes_are_administrator_only(method: str) -> None:
    assert getattr(build_client(), method)(CONNECTION_URL).status_code == 401
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)
    assert getattr(client, method)(CONNECTION_URL).status_code == 403


def test_the_probe_is_administrator_only() -> None:
    assert build_client().get(PROBE_URL).status_code == 401
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)
    assert client.get(PROBE_URL).status_code == 403


def test_the_ingest_route_is_administrator_only() -> None:
    assert build_client().post(INGEST_URL).status_code == 401
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)
    assert client.post(INGEST_URL).status_code == 403


def test_the_callback_is_open_by_necessity() -> None:
    # The browser arrives from Google carrying no session, so this route must
    # not demand one; its authority is the pending attempt (see below).
    response = build_client().get(f"{CALLBACK_URL}?code=made-up&state=made-up")

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "no_pending_connection" in response.text


# --- Starting the flow ---


def test_the_connect_flow_refuses_while_the_mailbox_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-safe default: no Google client and no expected mailbox means the
    # flow refuses rather than sending a browser to consent for *something*.
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(settings, "mail_address", "")
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(CONNECTION_URL)

    assert response.status_code == 503
    assert response.json()["detail"] == "mail_not_configured"


def test_starting_the_flow_returns_a_consent_url_for_this_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    started = start_connection(client)

    query = dict(parse_qsl(urlsplit(str(started["authorization_url"])).query))
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == settings.google_redirect_uri
    assert query["scope"].split() == list(GMAIL_SCOPES)
    assert query["code_challenge_method"] == "S256"
    assert query["state"]
    # The attempt is on disk, and its verifier is what makes the one-time
    # code redeemable — it exists nowhere else.
    pending = store().load().pending
    assert pending is not None
    assert pending.state == query["state"]
    assert datetime.fromisoformat(str(started["expires_at"])) == pending.expires_at


def test_starting_the_flow_again_replaces_the_pending_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    first = consent_query(client)
    second = consent_query(client)

    assert first["state"] != second["state"]
    pending = store().load().pending
    assert pending is not None
    assert pending.state == second["state"]


# --- The callback's refusals ---


def test_the_callback_refuses_a_state_this_deployment_did_not_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    start_connection(client)

    response = build_client().get(f"{CALLBACK_URL}?code=whatever&state=not-our-state")

    assert response.status_code == 400
    assert "state_mismatch" in response.text
    # A mismatch is not an abandoned attempt: the real browser may still be
    # in flight, so the attempt stays usable.
    assert store().load().pending is not None


def test_the_callback_refuses_an_expired_attempt_and_forgets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    query = consent_query(client)
    pending = store().load().pending
    assert pending is not None
    store().save(
        MailConnectionState(pending=replace(pending, expires_at=NOW - timedelta(seconds=1)))
    )

    response = build_client().get(f"{CALLBACK_URL}?code=whatever&state={query['state']}")

    assert response.status_code == 400
    assert "connection_expired" in response.text
    assert store().load().pending is None


def test_the_callback_reports_a_provider_refusal_as_a_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = FakeGoogle()
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    query = consent_query(client)

    response = build_client().get(f"{CALLBACK_URL}?code=never-issued&state={query['state']}")

    assert response.status_code == 400
    assert "code_refused:invalid_grant" in response.text
    assert store().load().credential is None
    # Google consumes a code only on a successful redemption; nothing local
    # changed, so a legitimate retry is still possible.
    assert store().load().pending is not None


# --- The operator's reads ---


def test_the_status_read_has_no_field_that_could_carry_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    plant_credential()
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(CONNECTION_URL).json()

    assert set(body) == {"connected", "address", "scopes", "connected_at", "token_expires_at"}
    assert body["connected"] is True
    assert body["address"] == MAILBOX
    assert body["scopes"] == list(GMAIL_SCOPES)
    assert TOKEN_MARKER not in json.dumps(body)


def test_the_status_read_says_not_connected_before_the_flow_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.get(CONNECTION_URL).json()

    assert body == {
        "connected": False,
        "address": None,
        "scopes": [],
        "connected_at": None,
        "token_expires_at": None,
    }


def test_the_probe_refuses_while_no_mailbox_is_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(PROBE_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == "not_connected"


def test_the_probe_reports_the_profile_and_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="NAR1 confirmation", sender="Director <d@client.example>", date="Mon, 22 Sep 2026"
    )
    # The count is the profile's own, not the length of the metadata list.
    google.messages_total = 7
    configure(monkeypatch)
    use_google(monkeypatch, google)
    credential = plant_credential()
    google.accept_access_token(credential.access_token)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(PROBE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"address", "messages_total", "messages"}
    assert body["address"] == MAILBOX
    assert body["messages_total"] == 7
    assert len(body["messages"]) == 1
    # Metadata only: the message's snippet — the part of a real response that
    # would carry client content — never reaches the response.
    assert set(body["messages"][0]) == {"id", "subject", "from", "date"}
    assert body["messages"][0]["from"] == "Director <d@client.example>"
    assert "never part of the probe" not in response.text


# --- The ingestion cycle ---


def test_the_ingest_route_refuses_while_no_mailbox_is_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(INGEST_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == "not_connected"


def test_the_ingest_route_reports_the_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.application.mail_ingest import AttachmentOutcome, CycleReport, MessageOutcome
    from app.domain.communications import AttachmentDisposition, IngestOutcome

    document_id, company_id, communication_id, case_id = uuid4(), uuid4(), uuid4(), uuid4()

    async def fake_cycle(*args: object, **kwargs: object) -> CycleReport:
        return CycleReport(
            cursor_received_at=NOW,
            query="after:1790111700",
            examined=3,
            ingested=1,
            outcomes=(
                MessageOutcome(
                    mailbox_message_id="msg-1",
                    outcome=IngestOutcome.INGESTED,
                    company_id=company_id,
                    communication_id=communication_id,
                    raw_document_id=document_id,
                    scan_status="clean",
                    attachments=(
                        AttachmentOutcome(
                            disposition=AttachmentDisposition.STORED,
                            document_id=document_id,
                            scan_status="infected",
                        ),
                    ),
                ),
                MessageOutcome(
                    mailbox_message_id="msg-2",
                    outcome=IngestOutcome.DENIED,
                    reason="service_not_included",
                    company_id=company_id,
                ),
                MessageOutcome(
                    mailbox_message_id="msg-3",
                    outcome=IngestOutcome.AWAITING_REGISTERED_CHANNEL,
                    company_id=company_id,
                    attribution_case_id=case_id,
                ),
            ),
        )

    monkeypatch.setattr(mail_ingest, "run_cycle", fake_cycle)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.post(INGEST_URL).json()

    assert body["examined"] == 3
    assert body["ingested"] == 1
    assert body["skipped_known"] == 0
    assert body["refused"] == 2
    assert body["query"] == "after:1790111700"
    assert body["outcomes"][0] == {
        "mailbox_message_id": "msg-1",
        "outcome": "ingested",
        "reason": None,
        "company_id": str(company_id),
        "communication_id": str(communication_id),
        "raw_document_id": str(document_id),
        "scan_status": "clean",
        # No quarantine row: the message was attributed and ingested (§2.5).
        "quarantine_id": None,
        # And no resolver decision: a single-company sender never reaches it,
        # and the report says so rather than implying the rule ran.
        "resolution": None,
        # No attribution case either: the message went in.
        "attribution_case_id": None,
        # Nor a split parent: one company never becomes the several-match.
        "split_parent_id": None,
        "attachments": [
            {
                "document_id": str(document_id),
                "disposition": "stored",
                "scan_status": "infected",
                "reason": None,
            }
        ],
    }
    # A denial carries the gate's own reason, and nothing else changed.
    assert body["outcomes"][1]["reason"] == "service_not_included"
    # A threaded message whose sender is not the chain company's registered
    # channel opens a case instead of quarantining (ADR-0019 §1): the outcome
    # names the case, so a reviewer can follow the ask's thread.
    assert body["outcomes"][2] == {
        "mailbox_message_id": "msg-3",
        "outcome": "awaiting_registered_channel",
        "reason": None,
        "company_id": str(company_id),
        "communication_id": None,
        "raw_document_id": None,
        "scan_status": None,
        "quarantine_id": None,
        "resolution": None,
        "attribution_case_id": str(case_id),
        "split_parent_id": None,
        "attachments": [],
    }


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        ("refresh", "mail_refresh_failed:invalid_grant"),
        ("unavailable", "mail_unavailable"),
        ("refused", "mail_read_refused:FAILED_PRECONDITION"),
        ("invalid", "invalid_response:missing_field:id"),
    ],
)
def test_the_ingest_route_reports_provider_failures_typed(
    monkeypatch: pytest.MonkeyPatch, raised: str, expected: str
) -> None:
    from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
    from app.application import mail_ingest
    from app.application.mail import MailRefreshFailed
    from app.domain.mail import MailResponseInvalid

    def failure() -> Exception:
        if raised == "refresh":
            return MailRefreshFailed("invalid_grant")
        if raised == "unavailable":
            return MailProviderUnavailable("GET /gmail/v1/users/me/messages")
        if raised == "refused":
            return MailProviderRefused("FAILED_PRECONDITION")
        return MailResponseInvalid("missing_field:id")

    async def failing_cycle(*args: object, **kwargs: object) -> object:
        raise failure()

    monkeypatch.setattr(mail_ingest, "run_cycle", failing_cycle)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(INGEST_URL)

    assert response.status_code == 502
    assert response.json()["detail"] == expected
