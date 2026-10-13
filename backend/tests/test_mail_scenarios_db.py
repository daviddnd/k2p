"""DB-backed mailbox-connection scenarios (opt-in; needs a migrated PostgreSQL).

The Phase 3 mailbox slice end to end against a real database and a faithful
fake of Google (``tests/fake_google.py``, no socket): the consent flow
completing through the real routes, the credential landing in its 0600 file,
the address guard refusing a different mailbox, the probe returning metadata
and nothing else, an access token renewed when it is stale, a revoked grant
refused as a typed error, a replayed callback refused, and a disconnect that
leaves no token behind.

The custody claim is asserted rather than implied: after the flow, the
credential's marker is searched for in **every table** of the schema, in the
audit chain, and in every captured log record — a token that travelled
anywhere but its file fails the suite.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (``test_rbac_scenarios_db.py`` has the setup recipe). Every test runs
inside a transaction that is rolled back, so the review database stays clean.
"""

import json
import logging
import os
import stat
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.gmail_client import GmailClient
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.base import Base
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import mail
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.mail import GMAIL_SCOPES, MailConnectionState
from app.main import create_app
from tests.fake_google import FakeGoogle, consent_challenge

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

CONNECTION_URL = "/api/v1/mail/connection"
CALLBACK_URL = "/api/v1/mail/oauth/callback"
PROBE_URL = "/api/v1/mail/probe"
CLIENT_ID = "the-deployment-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "the-deployment-client-secret"
MAILBOX = "ai-filings@tcsp.example"
# Every token the fake mints carries it; the suite searches for it everywhere
# a token must never be.
MARKER = "mail-credential-marker"
NOW = datetime.now(UTC)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- Harness -------------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def configure(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "mail_address", address)


def use_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
    """Point the provider seam at the fake, in-process, under this client."""
    google.client_id = CLIENT_ID
    google.client_secret = CLIENT_SECRET
    google.marker = MARKER
    monkeypatch.setattr(
        mail,
        "client",
        lambda: GmailClient(timeout=1.0, transport=httpx.ASGITransport(app=google.app)),
    )


def store() -> MailCredentialStore:
    return MailCredentialStore(settings.mail_credential_file)


def credential_file() -> Path:
    return settings.mail_credential_file


async def seed_tenant_with_admin(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


def start(client: TestClient) -> str:
    """The consent URL, as the administrator's POST returns it."""
    response = client.post(CONNECTION_URL)
    assert response.status_code == 201, response.text
    return str(response.json()["authorization_url"])


def play_browser(client: TestClient, google: FakeGoogle, consent_url: str, *, code=None):
    """The browser's return leg: Google mints a code for the consent challenge."""
    issued = code if code is not None else google.authorize(consent_challenge(consent_url))
    state = httpx.URL(consent_url).params["state"]
    return client.get(f"{CALLBACK_URL}?code={issued}&state={state}", follow_redirects=False)


def connected(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    admin: User,
) -> TestClient:
    """Run the whole consent flow and hand back an administrator's client."""
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=admin.tenant_id))
    assert play_browser(client, google, start(client)).status_code == 200
    return client


def _stale(credential, *, minutes: int = 1):
    """The same credential with an access token that has run out."""
    return replace(credential, token_expires_at=datetime.now(UTC) - timedelta(minutes=minutes))


async def stored_credential():
    credential = store().load().credential
    assert credential is not None
    return credential


async def audit_actions(session: AsyncSession, tenant: Tenant) -> list[str]:
    """This tenant's chain, in order — other rows in the review database stay out."""
    return list(
        (
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.tenant_id == tenant.id)
                .order_by(AuditLog.seq)
            )
        ).all()
    )


async def rows_containing(session: AsyncSession, needle: str) -> list[str]:
    """Every table holding a row whose text form contains ``needle``.

    ``table::text`` renders the whole row, so this searches all columns of
    all tables — the claim is "no table in this schema", not "the tables this
    slice happens to know about".
    """
    found: list[str] = []
    for table in Base.metadata.sorted_tables:
        # Quoted identifiers keep a reserved word or a mixed-case name safe.
        query = text(f'SELECT count(*) FROM "{table.name}" WHERE "{table.name}"::text LIKE :needle')
        count = await session.scalar(query, {"needle": f"%{needle}%"})
        if count:
            found.append(f"{table.name} ({count})")
    return found


# --- The whole flow ------------------------------------------------------


async def test_the_consent_flow_connects_the_mailbox_and_keeps_the_tokens_local(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tenant, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="NAR1 annual return — questions",
        sender="Director <d@client.example>",
        date="Mon, 22 Sep 2026",
    )
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    consent_url = start(client)
    assert play_browser(client, google, consent_url).status_code == 200

    credential = await stored_credential()
    assert credential.address == MAILBOX
    assert credential.scopes == GMAIL_SCOPES
    assert credential.refresh_token in google.refresh_tokens
    # The credential file is the tokens' home, owner-only and outside git.
    assert stat.S_IMODE(credential_file().stat().st_mode) == 0o600
    assert MARKER in credential_file().read_text(encoding="utf-8")
    # The attempt was consumed: the callback is single-use.
    assert store().load().pending is None

    # The operator's read names the mailbox and the grant, never the tokens.
    status = client.get(CONNECTION_URL).json()
    assert status["connected"] is True
    assert status["address"] == MAILBOX
    assert status["scopes"] == list(GMAIL_SCOPES)
    assert MARKER not in json.dumps(status)

    # ADR-0009: the connection is an audited administrative change, and the
    # entry carries the mailbox, not the credential.
    entries = (
        await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant.id))
    ).all()
    assert [entry.action for entry in entries] == [AuditAction.MAIL_CONNECTION_CREATED.value]
    assert entries[0].details["address"] == MAILBOX
    assert entries[0].actor_user_id == admin.id

    # Custody, asserted: the token is in its file and nowhere else — not in
    # any table of the schema, not in a log record. Both searches are
    # non-vacuous: the first finds the tenant the scan is looking at (the row
    # is there to be found), the second sees the provider's own log lines.
    assert await rows_containing(session, tenant.name) == ["tenants (1)"]
    assert await rows_containing(session, MARKER) == []
    assert "oauth2.googleapis.com/token" in caplog.text
    assert MARKER not in caplog.text

    # And the probe works from what was stored.
    probe = client.get(PROBE_URL)
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["address"] == MAILBOX
    assert body["messages_total"] == 1
    assert [message["id"] for message in body["messages"]] == [message_id]


async def test_the_probe_returns_metadata_only_through_the_real_route_stack(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    google.with_message(subject="Older", sender="a@client.example", date="Mon, 22 Sep 2026")
    google.with_message(subject="Newest", sender="b@client.example", date="Tue, 23 Sep 2026")
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = connected(session, monkeypatch, google, admin=admin)

    response = client.get(PROBE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"address", "messages_total", "messages"}
    assert body["messages_total"] == 2
    assert [message["subject"] for message in body["messages"]] == ["Newest", "Older"]
    for message in body["messages"]:
        assert set(message) == {"id", "subject", "from", "date"}
        assert message["date"].startswith("Tue") or message["date"].startswith("Mon")
    # The response carries envelopes, never content: the fake's bodies are in
    # the provider's response and stop before this deployment's API.
    assert "never part of the probe" not in response.text
    # And the metadata request asked Google for metadata only, the whole
    # envelope the attribution decision reads (ADR-0019 §1).
    assert google.message_format == "metadata"
    assert sorted(google.message_headers_requested) == [
        "Auto-Submitted",
        "Cc",
        "Date",
        "From",
        "In-Reply-To",
        "Message-ID",
        "References",
        "Return-Path",
        "Subject",
        "To",
    ]


async def test_the_probe_renews_a_stale_access_token_before_reading(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = connected(session, monkeypatch, google, admin=admin)
    stale = await stored_credential()
    store().save(MailConnectionState(credential=_stale(stale)))

    response = client.get(PROBE_URL)

    assert response.status_code == 200, response.text
    assert google.refreshes == 1
    renewed = await stored_credential()
    assert renewed.access_token != stale.access_token
    assert renewed.refresh_token == stale.refresh_token
    assert renewed.token_expires_at > datetime.now(UTC)
    # A renewal is credential bookkeeping, not a change to the tenant's data:
    # no second audit entry.
    assert await audit_actions(session, tenant) == [AuditAction.MAIL_CONNECTION_CREATED.value]


async def test_a_refused_renewal_is_a_typed_error_and_leaves_the_connection_readable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = connected(session, monkeypatch, google, admin=admin)
    stale = await stored_credential()
    # The operator (or Google) revoked the grant: the refresh token is dead.
    google.revoked_refresh_tokens.add(stale.refresh_token)
    expired = _stale(stale)
    store().save(MailConnectionState(credential=expired))

    response = client.get(PROBE_URL)

    assert response.status_code == 502
    assert response.json()["detail"] == "mail_refresh_failed:invalid_grant"
    # The local state still answers: the operator can see what is stored and
    # disconnect it without another provider call.
    assert client.get(CONNECTION_URL).json()["connected"] is True
    # The stored credential is untouched: a failed renewal stores nothing.
    assert await stored_credential() == expired


async def test_a_read_the_provider_refuses_with_a_fresh_token_is_typed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = connected(session, monkeypatch, google, admin=admin)
    google.profile_status = 403

    response = client.get(PROBE_URL)

    assert response.status_code == 502
    assert response.json()["detail"] == "mail_probe_refused:FAILED_PRECONDITION"


async def test_the_address_guard_refuses_a_different_mailbox_and_stores_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address="someone-else@gmail.com")
    configure(monkeypatch, address=MAILBOX)
    use_google(monkeypatch, google)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = play_browser(client, google, start(client))

    assert response.status_code == 400
    assert "address_mismatch" in response.text
    # The refusal is the whole point: consent was granted for an account that
    # is not this deployment's mailbox, so nothing is stored and nothing is
    # recorded as a connection.
    assert store().load().credential is None
    assert await audit_actions(session, tenant) == []
    # Google issued a token for that account; none of it was stored — only the
    # attempt the administrator started is on disk, awaiting a new flow.
    assert MARKER not in credential_file().read_text(encoding="utf-8")
    assert store().load().pending is not None


async def test_a_replayed_callback_cannot_complete_twice(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=admin.tenant_id))
    consent_url = start(client)
    assert play_browser(client, google, consent_url).status_code == 200
    first = await stored_credential()

    replayed = play_browser(client, google, consent_url, code="the-same-code-again")

    assert replayed.status_code == 400
    assert "no_pending_connection" in replayed.text
    assert await stored_credential() == first
    assert await audit_actions(session, tenant) == [AuditAction.MAIL_CONNECTION_CREATED.value]


async def test_disconnecting_forgets_the_credential_and_is_audited_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    google = FakeGoogle(address=MAILBOX)
    configure(monkeypatch)
    use_google(monkeypatch, google)
    client = connected(session, monkeypatch, google, admin=admin)

    response = client.delete(CONNECTION_URL)

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert not credential_file().exists()
    assert store().load() == MailConnectionState()
    assert client.get(PROBE_URL).status_code == 409
    # Disconnecting twice is not two disconnections.
    assert client.delete(CONNECTION_URL).status_code == 200
    entries = (
        await session.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
        )
    ).all()
    assert [entry.action for entry in entries] == [
        AuditAction.MAIL_CONNECTION_CREATED.value,
        AuditAction.MAIL_CONNECTION_REMOVED.value,
    ]
    assert entries[1].details["address"] == MAILBOX
    assert await rows_containing(session, MARKER) == []
