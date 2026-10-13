"""DB-backed sending scenarios (opt-in; needs a migrated PostgreSQL).

The sending slice end to end against a real database, the real routes, and the
two fakes: ``tests/fake_google.py`` for the provider API (no socket) and
``tests/fake_smtp.py`` for the SMTP fallback (a real conversation on loopback,
with the test-only certificate under ``tests/fixtures/smtp-tls``).

What this file exists to prove, in the brief's own order:

- **Outbox idempotency.** A crash between the provider's accept and the row's
  record replays without a second send: the fake files the message and then
  loses the connection, the row is left waiting with an unconfirmed outcome,
  and the next cycle finds the message in Sent by its RFC 5322 ``Message-ID``
  and records it instead of sending it again (ADR-0011 §1).
- **The release gate at send time** — the third caller ADR-0017 names. A
  refused release leaves the row unsent with the gate's own reason recorded,
  writes one ``document_release_refused`` entry **committed** rather than
  rolled back, and does not write a second entry for a retry that met the same
  refusal. Recording the continuation and the fee lets the same row go out.
- **The send modes.** The shipped defaults are the founder's two answers, the
  government destination refuses ``automatic`` on write *and* on read, and a
  draft held for review waits for the send route while an ``automatic`` one is
  queued the moment it is written.
- **The send route's idempotence.** A second send of a sent request is a typed
  409, not a second email.
- **The outbound ``Communication`` row** and the per-company read that shows
  it without changes — and the custody claim, asserted by planting markers in
  the app password and the master data and searching the whole chain and every
  captured log record for them.
- **Overlapping cycles** (the 2026-09-25 follow-up): two consumers racing one
  pending row deliver it exactly once — the queue's locked select keeps the
  loser from taking a row being delivered, a stale reference is answered
  ``skipped`` before anything leaves, a duplicate record of one exchange
  answers with the winner's row, and ``mark_sent`` refuses a row superseded
  mid-delivery instead of overwriting it.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
from email.policy import default as default_policy
from email.utils import format_datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail import smtp_client
from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.gmail_client import GmailClient
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import AttributionCase, Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import attribution, communications, documents, mail, mail_send, outbox
from app.core.config import settings
from app.domain.attribution import AttributionAskKind, ask_subject, registration_ask_letter
from app.domain.audit import AuditAction
from app.domain.communications import CommunicationKind
from app.domain.confirmation import RequestStatus
from app.domain.mail import MailboxCredential, MailConnectionState
from app.domain.mail_send import OutboxKind, OutboxStatus
from app.main import create_app
from tests.fake_google import FakeGoogle, consent_challenge
from tests.fake_scanning import FakeScanner
from tests.fake_smtp import FakeSmtpServer, trusting_context

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

MAILBOX = "ai-filings@tcsp.example"
# A mailbox whose domain is long enough that the ``Message-ID`` the SMTP leg
# stamps — ``make_msgid`` in the mailbox's own domain, the generator's rule —
# is wider than the 128 characters ``communications.mailbox_message_id``
# admitted before migration ``0023`` widened it to the outbox row's own 320.
# Domains that long are ordinary; the mismatch was not.
LONG_MAILBOX = "ai-filings@" + "a-very-long-mailbox-domain." * 4 + "tcsp.example"
CONTACT = "director@brightlantern.example.hk"
CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
APP_PASSWORD = "app-password-marker-never-logged"
# The anniversary still ahead of today, as the M3 suite derives it: the
# generator refuses a period whose reply-by would fall after CR's deadline, so
# a fixed past period would make every scenario here impossible.
ANNIVERSARY = date(date.today().year + 1, 3, 1)
TARGET_DATE = ANNIVERSARY.isoformat()
COVERAGE = {"start": f"{ANNIVERSARY.year - 1}-01-01", "end": f"{ANNIVERSARY.year}-12-31"}
# The instant the request-less ask scenario enqueues and delivers at; the SMTP
# leg records it as the acceptance moment.
AT = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": ANNIVERSARY.isoformat(),
    "type": "private",
}
NAME_EN = "Bright Lantern Design Limited"
NAME_MARKER = "ZED-MARKER-4471"


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes, deterministic URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def tls_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the SMTP leg trust ``tests/fake_smtp.py``'s own certificate.

    The seam is the client's ``tls_context`` factory; hostname verification
    stays on, so a scenario that sends over SMTP exercises the same checks a
    deployment does, against the local server instead of a real provider.
    """
    monkeypatch.setattr(smtp_client, "tls_context", trusting_context)


# --- Harness -------------------------------------------------------------


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


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


def provision(monkeypatch: pytest.MonkeyPatch, tier: str = "standard") -> None:
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)


def configure_mailbox(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    """Provision the mailbox: the address, and the OAuth client to connect with.

    The address is what every client-facing message is written from (§2.5) and
    the ``Message-ID``'s domain; the client id/secret are what the consent flow
    the scenarios run needs.
    """
    monkeypatch.setattr(settings, "mail_address", address)
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", CLIENT_SECRET)


def use_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
    """Point the deployment's mail client at the fake provider."""
    google.client_id = CLIENT_ID
    google.client_secret = CLIENT_SECRET
    monkeypatch.setattr(
        mail,
        "client",
        lambda: GmailClient(timeout=1.0, transport=httpx.ASGITransport(app=google.app)),
    )


def connect_mailbox(client: TestClient, google: FakeGoogle) -> None:
    """Run the whole consent flow, the way an administrator's browser would."""
    consent_url = client.post("/api/v1/mail/connection").json()["authorization_url"]
    code = google.authorize(consent_challenge(consent_url))
    state = httpx.URL(consent_url).params["state"]
    response = client.get(f"/api/v1/mail/oauth/callback?code={code}&state={state}")
    assert response.status_code == 200, response.text
    assert MailCredentialStore(settings.mail_credential_file).load().credential is not None


def use_smtp(monkeypatch: pytest.MonkeyPatch, server: FakeSmtpServer) -> None:
    """Provision the SMTP fallback (ADR-0005 path A) against a local server."""
    monkeypatch.setattr(settings, "smtp_host", "localhost")
    monkeypatch.setattr(settings, "smtp_port", server.port)
    monkeypatch.setattr(settings, "mail_app_password", server.password)


def make_company(client: TestClient, **overrides: object) -> str:
    response = client.post("/api/v1/companies", json={**COMPANY, **overrides})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def name_company(client: TestClient, company_id: str, *, name_en: str = NAME_EN) -> None:
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": None}},
    )
    assert response.status_code == 200, response.text


def add_registered_contact(client: TestClient, company_id: str, *, email: str = CONTACT) -> str:
    """A person with an email channel, associated with the company."""
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={"channel_type": "email", "identifier": email},
    )
    assert channel.status_code == 201, channel.text
    associated = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    assert associated.status_code in (200, 201), associated.text
    return person_id


def cover(client: TestClient, company_id: str) -> dict:
    """Assign a package covering confirmation generation and NAR1 work."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": ["confirmation_generation"],
            "covered_forms": ["NAR1"],
        },
    )
    assert package.status_code == 201, package.text
    assignment = client.post(
        f"/api/v1/companies/{company_id}/service-assignments",
        json={
            "package_id": package.json()["id"],
            "coverage_start": COVERAGE["start"],
            "coverage_end": COVERAGE["end"],
        },
    )
    assert assignment.status_code == 201, assignment.text
    return assignment.json()


def record_continuation(
    api: TestClient,
    company_id: str,
    *,
    year: int,
    decided: bool = True,
    paid: bool = True,
) -> None:
    """Record the period's answer — and the fee — as far as the caller wants.

    The release gate's two period legs are what a scenario chooses between:
    ``decided=False`` leaves the row ``pending`` (``continuation_not_recorded``),
    ``decided=True, paid=False`` records the client's answer without the fee
    (``payment_not_received``), and both ``True`` is the period a message may
    leave for.
    """
    created = api.post(
        f"/api/v1/companies/{company_id}/service-renewals", json={"anniversary_year": year}
    )
    assert created.status_code == 200, created.text
    if not decided:
        return
    decision = api.post(
        f"/api/v1/companies/{company_id}/service-renewals/{year}/decision",
        json={"decision": "confirmed"},
    )
    assert decision.status_code == 200, decision.text
    if paid:
        payment = api.post(
            f"/api/v1/companies/{company_id}/service-renewals/{year}/payment", json={}
        )
        assert payment.status_code == 200, payment.text


def draftable(
    client: TestClient,
    *,
    decided: bool = True,
    paid: bool = True,
    br_number: str | None = None,
    contact_email: str | None = None,
) -> str:
    """A company the generator will draft for, covered and (usually) paid.

    ``br_number`` and ``contact_email`` distinguish a second company in one
    tenant (both are unique per tenant), which a scenario about a *queue* of
    two needs.
    """
    company_id = make_company(
        client, **({} if br_number is None else {"br_number": br_number})
    )
    name_company(client, company_id)
    add_registered_contact(
        client, company_id, email=CONTACT if contact_email is None else contact_email
    )
    cover(client, company_id)
    record_continuation(client, company_id, year=ANNIVERSARY.year, decided=decided, paid=paid)
    return company_id


def set_send_modes(api: TestClient, **modes: str) -> dict:
    """PUT the whole configuration with the send modes the caller names.

    ``api`` rather than ``client`` because one of the modes is *named* client.
    """
    current = api.get("/api/v1/tenant-config").json()
    payload = {
        "naming_templates": current["naming_templates"],
        "milestones": current["milestones"],
        "send_modes": {"client": "automatic", "government": "review_required", **modes},
    }
    response = api.put("/api/v1/tenant-config", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def generate(client: TestClient, company_id: str) -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": TARGET_DATE},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def latest_request(session: AsyncSession, company_id: str) -> ConfirmationRequest:
    row: ConfirmationRequest | None = await session.scalar(
        select(ConfirmationRequest)
        .where(ConfirmationRequest.company_id == UUID(company_id))
        .order_by(ConfirmationRequest.version_no.desc())
        .limit(1)
    )
    assert row is not None
    return row


async def outbox_rows(session: AsyncSession, company_id: str) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox)
        .where(MailOutbox.company_id == UUID(company_id))
        .order_by(MailOutbox.created_at, MailOutbox.id)
    )
    return list(rows.all())


async def audit_details(session: AsyncSession, action: AuditAction) -> list[dict]:
    rows = await session.scalars(select(AuditLog).where(AuditLog.action == action.value))
    return [row.details for row in rows.all()]


def _revoke_the_grant(google: FakeGoogle) -> None:
    """Make the next Gmail call have to renew, and fail to.

    Two halves, both needed: the refresh token is revoked at the fake (so the
    token endpoint answers ``invalid_grant``), and the stored access token is
    written back as expired (so ``usable_credential`` renews before it reads —
    an hour of validity would otherwise hide the dead grant for the whole test).
    """
    credential = MailCredentialStore(settings.mail_credential_file).load().credential
    assert credential is not None
    google.revoked_refresh_tokens.add(credential.refresh_token)
    MailCredentialStore(settings.mail_credential_file).save(
        MailConnectionState(
            credential=replace(
                credential, token_expires_at=datetime.now(UTC) - timedelta(minutes=1)
            )
        )
    )


def _message_text(raw: bytes) -> str:
    """The plain-text body of a stored or transmitted message, decoded."""
    body = message_from_bytes(raw, policy=default_policy).get_body(preferencelist=("plain",))
    return "" if body is None else str(body.get_content())


def sent_headers(google: FakeGoogle, index: int = 0) -> dict[str, str]:
    """The headers of one message the fake filed in Sent."""
    parsed = message_from_bytes(google.sent_raw[index], policy=default_policy)
    return {name: str(parsed[name]) for name in ("Message-ID", "To", "From", "Subject")}


# --- The release: modes, and what generation does -------------------------


async def test_the_shipped_defaults_release_a_client_draft_the_moment_it_is_written(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The founder's default for the client destination: ``automatic``.

    The generation is the release: the request is ``queued`` and an outbox row
    is pending before the response — written in the generation's own
    transaction, which is ADR-0011's rule.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)

    request = generate(client, company_id)

    assert request["status"] == RequestStatus.QUEUED.value
    rows = await outbox_rows(session, company_id)
    assert len(rows) == 1
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].destination == "client"
    # The draft entry carries the mode that released it, and the release has
    # its own entry: two acts, both on the record.
    drafted = await audit_details(session, AuditAction.CONFIRMATION_REQUEST_DRAFTED)
    assert drafted[-1]["send_mode"] == "automatic"
    assert drafted[-1]["outbox_id"] == str(rows[0].id)
    released = await audit_details(session, AuditAction.CONFIRMATION_REQUEST_RELEASED)
    assert released[-1]["released_by"] == "system"
    assert released[-1]["destination"] == "client"


async def test_a_draft_held_for_review_waits_for_a_person(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """``review_required`` is the tenant's, and it holds the message.

    The mode is written through the real configuration route; the generator
    then records a draft and nothing else — no outbox row, no release entry —
    which is what "drafted and held" means.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    set_send_modes(client, client="review_required")
    company_id = draftable(client)

    request = generate(client, company_id)

    assert request["status"] == RequestStatus.DRAFT.value
    assert await outbox_rows(session, company_id) == []
    assert await audit_details(session, AuditAction.CONFIRMATION_REQUEST_RELEASED) == []


async def test_the_government_destination_refuses_automatic_and_stays_pinned(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """Ruling 2: not a switch a tenant may turn off.

    The write is refused 422 with the rule's own message, the stored block is
    unchanged, and the default block is the founder's pair.
    """
    provision(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    current = client.get("/api/v1/tenant-config").json()
    assert current["send_modes"] == {
        "client": "automatic",
        "government": "review_required",
    }

    response = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
            "send_modes": {"client": "automatic", "government": "automatic"},
        },
    )

    assert response.status_code == 422, response.text
    assert any("government" in violation for violation in response.json()["detail"])
    assert client.get("/api/v1/tenant-config").json()["send_modes"]["government"] == (
        "review_required"
    )


async def test_a_stored_government_automatic_fails_closed_on_read(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A hand-edited row cannot arm the government destination.

    The narrow read re-validates every stored block, and the rule that refuses
    the write refuses the value — so the generator refuses (409
    ``tenant_config_invalid``) rather than releasing anything on the strength of
    a block nobody can read.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)
    client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": client.get("/api/v1/tenant-config").json()["naming_templates"],
            "milestones": client.get("/api/v1/tenant-config").json()["milestones"],
            "send_modes": {"client": "automatic", "government": "review_required"},
        },
    )
    # The bypass the CHECK cannot see: a row written straight into the column.
    await session.execute(
        text(
            "UPDATE tenant_config SET send_modes = "
            "CAST('{\"client\": \"automatic\", \"government\": \"automatic\"}' AS json)"
        )
    )

    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": TARGET_DATE},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("tenant_config_invalid: send_modes.government")
    assert await outbox_rows(session, company_id) == []


async def test_a_put_without_the_block_keeps_the_stored_modes(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The one departure from full-replace, and the reason for it.

    A client built before this block existed sends the two blocks it knows
    about; reading that as "reset the send modes" would silently re-arm
    automatic sending an Administrator had turned off.
    """
    provision(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    set_send_modes(client, client="review_required")
    current = client.get("/api/v1/tenant-config").json()

    response = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["send_modes"]["client"] == "review_required"
    modes_entries = await audit_details(session, AuditAction.TENANT_SEND_MODES_UPDATED)
    assert len(modes_entries) == 1  # the second PUT did not touch the block


# --- The send route -------------------------------------------------------


async def test_the_send_route_delivers_and_a_second_call_is_refused(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The idempotence that matters: no second email, ever."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    set_send_modes(client, client="review_required")
    company_id = draftable(client)
    request = generate(client, company_id)

    sent = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )

    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == RequestStatus.SENT.value
    assert len(google.sent_raw) == 1
    again = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "already_sent"
    assert len(google.sent_raw) == 1  # one message, one call that sent it


async def test_the_sent_message_is_the_stored_draft_and_becomes_an_outbound_record(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """What went out is what the registry holds, and the exchange is recorded.

    The outbound ``Communication`` row points at the request, carries the
    recipients the draft was addressed to and the provider's ids, and the
    per-company read shows it without any change to that read.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    set_send_modes(client, client="review_required")
    company_id = draftable(client)
    request = generate(client, company_id)
    stored = (await session.get(Document, UUID(request["document_id"]))).storage_key

    sent = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )

    assert sent.status_code == 200, sent.text
    assert google.sent_raw == [store.objects[stored]]
    row = await session.scalar(
        select(Communication).where(Communication.confirmation_request_id == UUID(request["id"]))
    )
    assert row is not None
    assert row.direction == "outbound"
    assert row.sender_address == MAILBOX
    assert row.recipient_addresses == CONTACT
    assert row.confirmation_request_id == UUID(request["id"])
    assert row.mailbox_message_id == "sent-1"
    assert row.raw_document_id == UUID(request["document_id"])

    listed = client.get(f"/api/v1/companies/{company_id}/communications")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(row.id)]
    assert listed.json()[0]["direction"] == "outbound"
    assert listed.json()[0]["recipient_addresses"] == CONTACT


async def test_the_send_route_refuses_an_unknown_or_cross_tenant_request(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)

    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{uuid4()}/send"
    )

    assert response.status_code == 404, response.text


async def test_a_draft_gated_company_cannot_be_released_by_hand_either(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The send route is not a way around the release gate.

    The gate is consulted before the message leaves, whoever asked — the route
    included — and its refusal is the wire's own detail, committed as a record.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    set_send_modes(client, client="review_required")
    # Covered but with no continuation on record: drafting is allowed, the
    # release is not.
    company_id = draftable(client, decided=False)
    request = generate(client, company_id)

    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == f"continuation_not_recorded: {ANNIVERSARY.year}"
    assert google.sent_raw == []
    rows = await outbox_rows(session, company_id)
    assert rows[-1].status == OutboxStatus.PENDING.value
    assert rows[-1].blocked_reason == f"continuation_not_recorded: {ANNIVERSARY.year}"
    entries = await audit_details(session, AuditAction.DOCUMENT_RELEASE_REFUSED)
    assert entries[-1]["reason"] == "continuation_not_recorded"
    assert entries[-1]["anniversary_year"] == ANNIVERSARY.year
    assert entries[-1]["outbox_id"] == str(rows[-1].id)


# --- The queue: delivery, the cycle, and the gate -------------------------


async def test_the_gate_refusal_waits_and_recording_what_it_wants_lets_it_go(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The gate's refusal leaves the row waiting with the reason recorded.

    One entry for the refusal, committed; a second cycle that meets the same
    refusal writes no second entry (an append-only chain is not a retry log);
    and once staff record the answer and the fee, the *same* row goes out —
    which is what makes a blocked automatic send recoverable rather than lost.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client, decided=False)
    generate(client, company_id)

    waiting = await _run_cycle(session, store, tenant.id)

    assert waiting.count(mail_send.SendDisposition.REFUSED) == 1
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].blocked_reason == f"continuation_not_recorded: {ANNIVERSARY.year}"
    assert google.sent_raw == []
    entries = await audit_details(session, AuditAction.DOCUMENT_RELEASE_REFUSED)
    assert len(entries) == 1

    # The same refusal again: the row keeps waiting, and the chain gains no
    # entry for an unchanged fact.
    again = await _run_cycle(session, store, tenant.id)
    assert again.count(mail_send.SendDisposition.REFUSED) == 1
    assert len(await audit_details(session, AuditAction.DOCUMENT_RELEASE_REFUSED)) == 1

    # The remedy the refusal names: the client's answer and the fee.
    client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{ANNIVERSARY.year}/decision",
        json={"decision": "confirmed"},
    )
    client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{ANNIVERSARY.year}/payment", json={}
    )

    sent = await _run_cycle(session, store, tenant.id)

    assert sent.count(mail_send.SendDisposition.SENT) == 1
    assert len(google.sent_raw) == 1
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.SENT.value
    assert rows[0].blocked_reason is None


async def test_a_gate_refusal_that_changes_its_reason_is_recorded_again(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A *different* refusal is a new fact, and the chain says so."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client, decided=False)
    generate(client, company_id)
    await _run_cycle(session, store, tenant.id)

    # The client answers but the fee is still missing: a new refusal.
    client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{ANNIVERSARY.year}/decision",
        json={"decision": "confirmed"},
    )
    outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.REFUSED) == 1
    assert outcome.outcomes[0].reason == f"payment_not_received: {ANNIVERSARY.year}"
    reasons = [
        entry["reason"]
        for entry in await audit_details(session, AuditAction.DOCUMENT_RELEASE_REFUSED)
    ]
    assert reasons == ["continuation_not_recorded", "payment_not_received"]


async def test_a_crash_between_accept_and_record_replays_without_a_second_send(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """ADR-0011's promise, exercised end to end.

    The provider files the message and then the transport dies, so the
    deployment never hears the answer (`ambiguous_send`). The row is left
    waiting with an unconfirmed outcome — never "sent", never "failed" — and
    the next cycle finds the message in Sent by its ``Message-ID`` and records
    the send instead of repeating it: **one message, two attempts**.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    request = generate(client, company_id)
    google.ambiguous_send = True

    first = await _run_cycle(session, store, tenant.id)

    assert first.count(mail_send.SendDisposition.WAITING) == 1
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].last_error == mail_send.REASON_UNCONFIRMED
    assert len(google.sent_raw) == 1  # the provider has it; we do not know it
    assert await audit_details(session, AuditAction.MAIL_MESSAGE_SENT) == []

    second = await _run_cycle(session, store, tenant.id)

    assert second.count(mail_send.SendDisposition.SENT) == 1
    assert second.outcomes[0].reconciled is True
    assert len(google.sent_raw) == 1  # and nothing was sent twice
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.SENT.value
    assert rows[0].provider_message_id == "sent-1"
    sent_entries = await audit_details(session, AuditAction.MAIL_MESSAGE_SENT)
    assert sent_entries[-1]["reconciled"] is True
    assert sent_entries[-1]["provider_message_id"] == "sent-1"
    assert sent_entries[-1]["outbox_id"] == str(rows[0].id)
    assert sent_entries[-1]["confirmation_request_id"] == request["id"]
    assert sent_entries[-1]["recipients"] == [CONTACT]


async def test_a_permanent_provider_refusal_ends_the_row_and_says_why(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A refusal that will not change ends the row rather than retrying for ever."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    google.send_status = 403
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    generate(client, company_id)

    outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.FAILED) == 1
    assert outcome.outcomes[0].reason.startswith("mail_send_failed:")
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.FAILED.value
    entries = await audit_details(session, AuditAction.MAIL_SEND_FAILED)
    assert entries[-1]["reason"] == rows[0].last_error
    # The request stays released: the release is not what failed, and the
    # letter is still owed to the client.
    request = await latest_request(session, company_id)
    assert request.status == RequestStatus.QUEUED.value


async def test_an_unconfigured_provider_leaves_the_row_waiting(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A deployment that has not set up sending queues rather than fails."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "mail_app_password", "")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)
    generate(client, company_id)

    outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.WAITING) == 1
    assert outcome.outcomes[0].reason == mail_send.REASON_NOT_CONFIGURED
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].last_error == mail_send.REASON_NOT_CONFIGURED


async def test_the_smtp_leg_sends_the_stored_draft_when_no_credential_is_connected(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """ADR-0005 path A, end to end: the same mailbox, an app password.

    No Gmail credential is connected, so the provider rule falls to SMTP. The
    message the server receives is the stored draft byte for byte, and the
    exchange records the ``Message-ID`` the deployment stamped — the only
    identity a leg without provider ids has.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        company_id = draftable(client)
        request = generate(client, company_id)
        stored = (await session.get(Document, UUID(request["document_id"]))).storage_key

        outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.SENT) == 1
    assert len(server.messages) == 1
    assert server.messages[0].data == store.objects[stored]
    assert server.messages[0].recipients == [CONTACT]
    row = await session.scalar(
        select(Communication).where(Communication.confirmation_request_id == UUID(request["id"]))
    )
    assert row is not None
    assert row.mailbox_message_id is not None
    assert row.mailbox_message_id.startswith("<") and "@tcsp.example>" in row.mailbox_message_id
    assert row.thread_id is None  # SMTP has no thread
    outbox = await outbox_rows(session, company_id)
    assert outbox[0].provider_message_id is None
    assert outbox[0].message_id == row.mailbox_message_id


async def test_the_smtp_leg_records_a_message_id_a_long_mailbox_domain_stamped(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The exchange row holds the identity the SMTP leg writes into it.

    A ``Message-ID`` is built in the mailbox's own domain, so a long domain
    makes a long header, and the sender stores that header in
    ``communications.mailbox_message_id`` — which is why the two widths have to
    agree. Under the 128 characters the column admitted before migration
    ``0023`` widened it to the outbox row's own 320, the insert fails *after*
    the server accepted the message: a 500, and a duplicate on the retry.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch, address=LONG_MAILBOX)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    # The fake authenticates the mailbox the deployment is configured with, so
    # the long address is the one it accepts an app password for.
    with FakeSmtpServer(username=LONG_MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        company_id = draftable(client)
        request = generate(client, company_id)

        outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.SENT) == 1
    assert len(server.messages) == 1
    stamped = str(message_from_bytes(server.messages[0].data, policy=default_policy)["Message-ID"])
    assert len(stamped) > 128
    row = await session.scalar(
        select(Communication).where(Communication.confirmation_request_id == UUID(request["id"]))
    )
    assert row is not None
    assert row.mailbox_message_id == stamped
    outbox = await outbox_rows(session, company_id)
    assert outbox[0].message_id == row.mailbox_message_id


async def test_a_superseded_release_is_cancelled_rather_than_sent(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A regeneration supersedes a released-but-undelivered letter.

    The pending send is cancelled in the generation's own transaction, so the
    old version cannot leave afterwards — and the new one can.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    first = generate(client, company_id)

    second = generate(client, company_id)

    rows = await outbox_rows(session, company_id)
    assert len(rows) == 2
    assert rows[0].status == OutboxStatus.CANCELLED.value
    assert rows[0].last_error == "superseded"
    assert rows[1].status == OutboxStatus.PENDING.value
    await session.refresh(await session.get(ConfirmationRequest, UUID(first["id"])))
    assert (await latest_request(session, company_id)).id == UUID(second["id"])

    outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.count(mail_send.SendDisposition.SENT) == 1
    assert len(google.sent_raw) == 1
    # The message that left is the live version's.
    stored = (await session.get(Document, UUID(second["document_id"]))).storage_key
    assert google.sent_raw == [store.objects[stored]]


async def _run_cycle(
    session: AsyncSession, store: FakeStore, tenant_id: UUID
) -> mail_send.SendCycleReport:
    """One delivery cycle, with the retry pause stubbed out."""
    waits: list[float] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)

    report = await mail_send.run_cycle(
        session, store, tenant_id=tenant_id, sleeper=sleeper
    )
    return report


# --- The request-less ask: delivered without a request or the gate ----------


def ask_letter(*, to: str, message_id: str, in_reply_to: str) -> bytes:
    """A stored attribution ask, composed the way ``attribution.enqueue_ask``
    composes one (the delivery scenario is about the sender, so the letter is
    staged rather than enqueued through the gate)."""
    message = EmailMessage(policy=SMTP)
    message["From"] = MAILBOX
    message["To"] = to
    message["Subject"] = ask_subject(
        company_name=NAME_EN,
        cr_number=str(COMPANY["cr_number"]),
        br_number=str(COMPANY["br_number"]),
        kind=AttributionAskKind.REGISTERED_CHANNEL,
    )
    message["Date"] = format_datetime(AT)
    message["Message-ID"] = message_id
    message["In-Reply-To"] = in_reply_to
    message["References"] = in_reply_to
    message.set_content(
        registration_ask_letter(
            company_name=NAME_EN,
            cr_number=str(COMPANY["cr_number"]),
            br_number=str(COMPANY["br_number"]),
        )
    )
    return message.as_bytes()


async def test_an_attribution_ask_delivers_without_a_request_or_the_release_gate(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """An ``attribution_ask`` row rides the same queue, letter and provider
    legs as every other kind (ADR-0019 §1's ruling R4) — with no confirmation
    request behind it and **without** §5.1.9's release gate: it is a notice,
    and a deployment with nothing licensed and no renewal recorded was still
    owed it. The accepted delivery records the exchange with
    ``kind=attribution_ask`` and writes the one ``mail_message_sent`` entry.
    """
    provision(monkeypatch, tier="free")  # nothing licensed: no gate may matter
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = make_company(client)
    case = AttributionCase(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        mailbox_message_id="provider-ask-1",
        sender_address=CONTACT,
        status="awaiting_registered_channel",
        thread_id="thread-ask",
        original_rfc_message_id="<original@client.example>",
        ask_kind="registered_channel",
    )
    session.add(case)
    await session.flush()
    raw = ask_letter(
        to=CONTACT,
        message_id=f"<attribution-ask@{MAILBOX.rpartition('@')[2]}>",
        in_reply_to="<original@client.example>",
    )
    letter = Document(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="e" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=len(raw),
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(letter)
    await session.flush()
    await store.put(letter.storage_key, raw, "message/rfc822")
    row = MailOutbox(
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        confirmation_request_id=None,
        attribution_case_id=case.id,
        destination="client",
        status="pending",
        kind="attribution_ask",
        letter_document_id=letter.id,
        not_before=None,
    )
    session.add(row)
    await session.flush()
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    assert [job.id for job in due] == [row.id]

    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        outcome = await mail_send.deliver_one(session, store, job=row, now=AT)

    assert outcome.disposition is mail_send.SendDisposition.SENT
    assert outcome.confirmation_request_id is None
    assert outcome.reason is None
    assert len(server.messages) == 1
    assert server.messages[0].recipients == [CONTACT]
    assert server.messages[0].data == raw

    # The exchange record: the ask's own kind, nobody's request, the stored
    # letter it delivered, and the SMTP leg's identity anchor.
    (exchange,) = (
        await session.scalars(
            select(Communication).where(
                Communication.tenant_id == tenant.id,
                Communication.direction == "outbound",
            )
        )
    ).all()
    assert exchange.kind == CommunicationKind.ATTRIBUTION_ASK.value
    assert exchange.confirmation_request_id is None
    assert exchange.sender_address == MAILBOX
    assert exchange.recipient_addresses == CONTACT
    assert exchange.raw_document_id == letter.id
    assert exchange.mailbox_message_id is not None

    await session.flush()
    (sent_row,) = await outbox_rows(session, company_id)
    assert sent_row.status == OutboxStatus.SENT.value
    assert sent_row.sent_at == AT
    assert sent_row.communication_id == exchange.id
    assert sent_row.message_id == exchange.mailbox_message_id
    assert sent_row.last_error is None
    assert sent_row.blocked_reason is None

    entries = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    chain = list(entries.all())
    actions = [entry.action for entry in chain]
    assert AuditAction.MAIL_MESSAGE_SENT.value in actions
    assert AuditAction.DOCUMENT_RELEASE_REFUSED.value not in actions  # no gate ran
    sent = next(entry for entry in chain if entry.action == AuditAction.MAIL_MESSAGE_SENT.value)
    assert sent.details["confirmation_request_id"] is None
    assert sent.details["outbox_id"] == str(row.id)
    assert sent.details["communication_id"] == str(exchange.id)
    assert sent.details["recipients"] == [CONTACT]
    assert sent.details["document_id"] == str(letter.id)
    assert sent.details["destination"] == "client"


# --- Custody, and the schema's own backstops ------------------------------


async def test_the_app_password_and_the_letter_reach_no_entry_and_no_log(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The custody claim, asserted rather than promised.

    Two markers are planted: the app password (a credential under ADR-0014,
    which must live in the environment and nowhere else) and the company's
    business-nature description (which the letter quotes back to the client).
    Every audit entry and every captured log record is searched for both. The
    description is expected in exactly one kind of entry — the
    ``company_fact_recorded`` entries the master-data write has always
    written — so the search proves where the marker *is* as well as where it
    is not; the password is expected nowhere at all. The API responses are
    searched too: a report is a place a letter could leak.
    """
    caplog.set_level(logging.DEBUG)
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        company_id = make_company(client)
        name_company(client, company_id)
        nature = client.patch(
            f"/api/v1/companies/{company_id}",
            json={
                "effective_date": "2026-01-01",
                "business_nature": {
                    "business_nature_code": "74990",
                    "business_nature_description": f"Design ({NAME_MARKER})",
                },
            },
        )
        assert nature.status_code == 200, nature.text
        add_registered_contact(client, company_id)
        cover(client, company_id)
        record_continuation(client, company_id, year=ANNIVERSARY.year)
        generate(client, company_id)

        report = client.post("/api/v1/mail/send")

    assert report.status_code == 200, report.text
    assert len(server.messages) == 1
    # The letter really does carry the marker: the search is not looking in
    # the wrong place. (The body travels base64-encoded, so it is read back
    # the way a client would read it.)
    assert NAME_MARKER in _message_text(server.messages[0].data)

    entries = (await session.scalars(select(AuditLog))).all()
    carriers = {entry.action for entry in entries if NAME_MARKER in json.dumps(entry.details)}
    assert carriers == {AuditAction.COMPANY_FACT_RECORDED.value}
    for entry in entries:
        assert APP_PASSWORD not in json.dumps(entry.details)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert APP_PASSWORD not in logged
    assert NAME_MARKER not in logged
    assert NAME_MARKER not in report.text
    listed = client.get(f"/api/v1/companies/{company_id}/communications")
    assert NAME_MARKER not in listed.text
    assert APP_PASSWORD not in listed.text


async def test_the_task_credentials_never_reach_the_send_chain(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The Gmail half of the same claim: no token, no scope value, no body.

    The Send entry records ids, addresses and outcomes; the access token the
    fake minted (its marker) must appear in no entry.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    generate(client, company_id)

    report = client.post("/api/v1/mail/send")

    assert report.status_code == 200, report.text
    assert report.json()["sent"] == 1
    entries = (await session.scalars(select(AuditLog))).all()
    for entry in entries:
        assert google.marker not in json.dumps(entry.details)
    assert google.marker not in report.text


async def test_a_drifted_reconcile_response_is_typed_too(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A 200 that is not the promised shape is a refusal, not a send.

    The lookup answers with a body the wire contract does not allow, so the
    reconciliation cannot be trusted and the sender must not exist without it:
    the row waits with ``invalid_response:<reason>`` — the ingestion trigger's
    own string for the same situation — instead of a 500 or a guess.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    request = generate(client, company_id)
    google.list_body = {"messages": "not-a-list"}

    outcome = await _run_cycle(session, store, tenant.id)
    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )

    assert outcome.count(mail_send.SendDisposition.WAITING) == 1
    assert outcome.outcomes[0].reason == "invalid_response:invalid_field:messages"
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "invalid_response:invalid_field:messages"
    assert google.sent_raw == []


async def test_one_rows_failure_does_not_hide_the_rows_behind_it(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A queue is a queue: a refused release must not stop the next delivery.

    Two companies are queued; the older one's period has **no answer on record
    at all** (the release gate refuses it with `continuation_not_recorded`)
    while the newer one's is confirmed and paid: the cycle reports **both** — a
    refusal and a send — because a failing row that aborted the cycle would
    hide exactly the letters the operator was asking about.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    blocked_company = draftable(client, decided=False)
    sendable_company = draftable(
        client, br_number="87654321", contact_email="secretary@brightlantern.example.hk"
    )
    generate(client, blocked_company)
    generate(client, sendable_company)

    outcome = await _run_cycle(session, store, tenant.id)

    assert outcome.examined == 2
    assert outcome.count(mail_send.SendDisposition.REFUSED) == 1
    assert outcome.count(mail_send.SendDisposition.SENT) == 1
    assert len(google.sent_raw) == 1
    blocked = await outbox_rows(session, blocked_company)
    deliverable = await outbox_rows(session, sendable_company)
    assert blocked[0].status == OutboxStatus.PENDING.value
    assert deliverable[0].status == OutboxStatus.SENT.value


async def test_a_revoked_grant_is_a_typed_outcome_not_an_untyped_500(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A dead grant is a *typed* failure on the send path too, and not a silent
    fallback to the other leg.

    The deployment has both legs configured and the Gmail grant has died: the
    row waits with `mail_refresh_failed:<code>` (the ingestion trigger's own
    vocabulary), the route answers the same detail as a 502, and the SMTP
    server — which is up and would work — is deliberately left untouched,
    because an operator whose connection is broken must see that rather than
    have it hidden behind another mailbox identity. Nothing is sent, and the row
    is not failed: the letter is still owed.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        company_id = draftable(client)
        request = generate(client, company_id)  # queued: the client mode is automatic
        _revoke_the_grant(google)

        outcome = await _run_cycle(session, store, tenant.id)
        response = client.post(
            f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
        )

    assert outcome.count(mail_send.SendDisposition.WAITING) == 1
    assert outcome.outcomes[0].reason == "mail_refresh_failed:invalid_grant"
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "mail_refresh_failed:invalid_grant"
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].last_error == "mail_refresh_failed:invalid_grant"
    assert google.sent_raw == []  # nothing left the deployment
    assert server.messages == []  # and the working SMTP leg was not used instead


async def test_a_refused_reconcile_lookup_defers_the_send_and_retries_in_cycle(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The lookup is part of the delivery, so its failures are classified too.

    A provider that refuses the `in:sent` search (a rate limit, an outage) must
    not become an untyped 500: nothing was sent, the check could not be made,
    and the sender **never sends without the check** — so the row waits with the
    provider's code in the ingestion trigger's own read vocabulary
    (`mail_read_refused:<code>`), retried inside the cycle first.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = draftable(client)
    request = generate(client, company_id)
    # The lookup is refused; the send endpoint itself is fine.
    google.messages_status = 429

    outcome = await _run_cycle(session, store, tenant.id)
    lookups_in_the_cycle = google.list_attempts
    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"
    )

    assert outcome.count(mail_send.SendDisposition.WAITING) == 1
    assert outcome.outcomes[0].reason == "mail_read_refused:FAILED_PRECONDITION"
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "mail_read_refused:FAILED_PRECONDITION"
    rows = await outbox_rows(session, company_id)
    assert rows[0].status == OutboxStatus.PENDING.value
    assert rows[0].last_error == "mail_read_refused:FAILED_PRECONDITION"
    # A transient classification retried inside the cycle before waiting: one
    # lookup per attempt, and `attempts` counts take-ups (the cycle's, then the
    # route's), not the tries inside one.
    assert lookups_in_the_cycle == mail_send.ATTEMPTS_PER_CYCLE
    assert rows[0].attempts == 2
    assert google.sent_raw == []  # never a send without the reconciliation


async def test_pressing_send_again_after_a_permanent_refusal_is_a_new_attempt(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """A failed row is history; the remedy is another attempt, not a reopened row.

    The first attempt ends `failed` (the provider refused it), the request stays
    `queued` because the release is not what failed, and pressing send again
    writes a **second** row — the partial unique index only forbids two
    *pending* sends, so the old row stays as the record of what was tried.
    """
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    google = FakeGoogle()
    google.send_status = 403
    use_google(monkeypatch, google)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    set_send_modes(client, client="review_required")
    company_id = draftable(client)
    request = generate(client, company_id)
    send_url = f"/api/v1/companies/{company_id}/confirmation-requests/{request['id']}/send"

    refused = client.post(send_url)

    assert refused.status_code == 502, refused.text
    assert refused.json()["detail"].startswith("mail_send_failed:")
    rows = await outbox_rows(session, company_id)
    assert [row.status for row in rows] == [OutboxStatus.FAILED.value]

    google.send_status = 200  # the cause is fixed; the letter is still owed

    sent = client.post(send_url)

    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == RequestStatus.SENT.value
    rows = await outbox_rows(session, company_id)
    assert [row.status for row in rows] == [
        OutboxStatus.FAILED.value,
        OutboxStatus.SENT.value,
    ]
    assert len(google.sent_raw) == 1


async def test_one_pending_send_per_request_at_the_database(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The partial unique index, which is the release's idempotency."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)
    request = generate(client, company_id)
    row = (await outbox_rows(session, company_id))[0]

    duplicate = MailOutbox(
        tenant_id=tenant.id,
        company_id=row.company_id,
        confirmation_request_id=UUID(request["id"]),
        destination="client",
        status=OutboxStatus.PENDING.value,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_sent_row_must_carry_its_moment_and_no_reason(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, store: FakeStore
) -> None:
    """The CHECK keeps the three outcomes from blurring in the table itself."""
    provision(monkeypatch)
    configure_mailbox(monkeypatch)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = draftable(client)
    generate(client, company_id)
    row = (await outbox_rows(session, company_id))[0]

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE mail_outbox SET status = 'sent' WHERE id = :id").bindparams(id=row.id)
        )
    await session.rollback()




# --- Overlapping cycles: one pending row is delivered exactly once ----------
#
# The 2026-09-25 follow-up, closed here: two cycles could read one pending row
# and both deliver it. These scenarios seed a due reminder row **committed for
# real** (the encryption suite's concurrency precedent: savepoint-joined test
# transactions cannot observe each other's locks, so the seed commits) and race
# two consumers on separate connections. Cleanup deletes everything the
# scenario committed. The one deliberate restraint: a delivery that runs the
# real sender writes its ``mail_message_sent`` entry, and the audit chain is
# append-only — a committed entry could never be cleaned — so the tests that
# deliver through ``deliver_one`` assert the full record (exchange, outcome,
# entry) inside a transaction they roll back, and let committed winners be the
# audit-silent writes only (``record_outbound_message`` / ``mark_sent``).


@dataclass(frozen=True)
class SeededReminder:
    """The committed rows one overlap scenario races its consumers over."""

    tenant_id: UUID
    company_id: UUID
    request_id: UUID
    document_id: UUID
    outbox_id: UUID
    store: FakeStore


# The letter the seeded row delivers. The ``Message-ID`` is what the SMTP leg
# would record as the exchange's identity — and what a racing duplicate's
# insert collides on (the per-tenant unique index).
OVERLAP_LETTER = (
    b"From: ai-filings@tcsp.example\r\n"
    b"To: director@brightlantern.example.hk\r\n"
    b"Message-ID: <overlap-reminder-1@tcsp.example>\r\n"
    b"Subject: Annual confirmation - reminder\r\n"
    b"\r\nbody\r\n"
)


async def _seed_committed_reminder_job() -> SeededReminder:
    """A sent request with one due reminder row, committed for real.

    Seeded through the ORM on its own connection (the API fixtures keep their
    transaction uncommitted, which a second connection cannot see) and deleted
    by the caller in a ``finally``. A reminder row — not the request's own —
    keeps the release gate out of the delivery order, so the scenario races
    exactly the outbox's locking and nothing else.
    """
    store = FakeStore()
    key = f"overlap/{uuid4()}"
    store.objects[key] = OVERLAP_LETTER
    async with (
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as session,
    ):
        tenant = Tenant(name=f"overlap-{uuid4()}")
        session.add(tenant)
        await session.flush()
        company = Company(
            tenant_id=tenant.id,
            br_number=f"{uuid4().int % 100_000_000:08d}",
            date_of_incorporation=date(2010, 3, 1),
            anniversary_date=ANNIVERSARY,
            type="private",
        )
        session.add(company)
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            company_id=company.id,
            kind="generated_draft",
            origin="generated",
            retention_class="client_communication",
            sha256="0" * 64,
            storage_key=key,
            byte_size=len(OVERLAP_LETTER),
            content_type="message/rfc822",
            scan_status="clean",
        )
        session.add(document)
        await session.flush()
        request = ConfirmationRequest(
            tenant_id=tenant.id,
            company_id=company.id,
            form_type="nar1",
            target_date=ANNIVERSARY,
            reply_by=ANNIVERSARY,
            statutory_due=ANNIVERSARY,
            status=RequestStatus.SENT.value,
            version_no=1,
            document_id=document.id,
            checklist=[],
            recipients=[{"person_id": None, "address": CONTACT}],
        )
        session.add(request)
        await session.flush()
        job = MailOutbox(
            tenant_id=tenant.id,
            company_id=company.id,
            confirmation_request_id=request.id,
            destination="client",
            status=OutboxStatus.PENDING.value,
            kind=OutboxKind.REMINDER.value,
            letter_document_id=document.id,
            not_before=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(job)
        await session.flush()
        seeded = SeededReminder(
            tenant_id=tenant.id,
            company_id=company.id,
            request_id=request.id,
            document_id=document.id,
            outbox_id=job.id,
            store=store,
        )
        await connection.commit()
    return seeded


async def _delete_seeded_reminder(seeded: SeededReminder) -> None:
    """The rows the scenario committed, in FK-safe order.

    The ``audit_log`` delete matches nothing by design — a delivery that ran
    the real sender asserts its entry inside a rolled-back transaction (the
    chain is append-only and a committed entry could never be cleaned) — and
    if it ever matches, the append-only trigger refuses and the failure is
    visible.
    """
    async with engine.connect() as connection:
        for table in (
            MailOutbox.__table__,
            Communication.__table__,
            AuditLog.__table__,
            ConfirmationRequest.__table__,
            Document.__table__,
            Company.__table__,
        ):
            await connection.execute(delete(table).where(table.c.tenant_id == seeded.tenant_id))
        await connection.execute(delete(Tenant).where(Tenant.id == seeded.tenant_id))
        await connection.commit()


def _seam_connected_mailbox(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
    """The connected-credential answer and the provider client, without the
    consent flow — the scenario races delivery, not connection."""

    async def usable_credential(*, now: datetime | None = None) -> MailboxCredential:
        return MailboxCredential(
            access_token="overlap-access",
            refresh_token="overlap-refresh",
            address=MAILBOX,
            scopes=("https://mail.google.com/",),
            connected_at=datetime.now(UTC),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    google.accept_access_token("overlap-access")
    monkeypatch.setattr(mail, "usable_credential", usable_credential)
    monkeypatch.setattr(
        mail,
        "client",
        lambda: GmailClient(timeout=1.0, transport=httpx.ASGITransport(app=google.app)),
    )


async def _pending_jobs_fresh(tenant_id: UUID) -> list[UUID]:
    """The overlapping consumer's own queue read, on its own transaction."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                jobs = await outbox.pending_jobs(session, tenant_id=tenant_id)
                return [job.id for job in jobs]
        finally:
            await transaction.rollback()


async def _deliver_one_fresh(seeded: SeededReminder, job_id: UUID) -> mail_send.SendOutcome:
    """One delivery attempt on its own connection and transaction.

    The shape a second cycle has — its own session and its own commit edge —
    so a loser that wrote nothing releases everything when it answers. The job
    id is what an earlier batch read handed it, and may be stale.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                job = await session.get(MailOutbox, job_id)
                assert job is not None
                return await mail_send.deliver_one(session, seeded.store, job=job)
        finally:
            await transaction.rollback()


async def _seeded_row(seeded: SeededReminder) -> MailOutbox:
    """The outbox row as it now stands, read on a fresh connection."""
    async with (
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as session,
    ):
        row = await session.get(MailOutbox, seeded.outbox_id)
        assert row is not None
        return row


async def _recorded_exchanges(seeded: SeededReminder) -> list[Communication]:
    async with (
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as session,
    ):
        rows = await session.scalars(
            select(Communication).where(
                Communication.tenant_id == seeded.tenant_id,
                Communication.confirmation_request_id == seeded.request_id,
            )
        )
        return list(rows.all())


async def test_two_overlapping_cycles_cannot_take_one_pending_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue's locked select: the loser sees the row gone, not a copy.

    A takes the pending row and holds it; B asks for the queue while A holds
    it and finds nothing — so B never reaches ``deliver_one``. A then rolls
    its take back unused, and one clean delivery shows what the exclusion
    protects: exactly one letter out, one exchange recorded, one entry on the
    chain. That delivery's assertions run inside the transaction that is
    rolled back at the end — the chain is append-only, so the scenario never
    commits an entry it could not clean.
    """
    seeded = await _seed_committed_reminder_job()
    google = FakeGoogle()
    _seam_connected_mailbox(monkeypatch, google)
    try:
        async with engine.connect() as connection_a:
            transaction_a = await connection_a.begin()
            async with AsyncSession(bind=connection_a, expire_on_commit=False) as session_a:
                jobs_a = await outbox.pending_jobs(session_a, tenant_id=seeded.tenant_id)
                assert [job.id for job in jobs_a] == [seeded.outbox_id]

                # B asks while A holds the row: the locked select must answer
                # "gone", not queue a second delivery of the same letter.
                waiter = asyncio.create_task(_pending_jobs_fresh(seeded.tenant_id))
                await asyncio.sleep(0.3)
                assert await asyncio.wait_for(waiter, timeout=30) == []
                assert google.sent_raw == []  # nothing left for anyone
            await transaction_a.rollback()  # A's take was never a delivery

        # What the exclusion protects, proven on the one consumer left: one
        # delivery, one record of it, and the row's outcome agrees with both.
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    (job,) = await outbox.pending_jobs(session, tenant_id=seeded.tenant_id)
                    outcome = await mail_send.deliver_one(session, seeded.store, job=job)
                    assert outcome.disposition is mail_send.SendDisposition.SENT
                    assert len(google.sent_raw) == 1
                    row = await session.get(MailOutbox, seeded.outbox_id)
                    assert row is not None
                    assert row.status == OutboxStatus.SENT.value
                    assert row.attempts == 1
                    exchanges = (
                        await session.scalars(
                            select(Communication).where(
                                Communication.tenant_id == seeded.tenant_id,
                                Communication.confirmation_request_id == seeded.request_id,
                            )
                        )
                    ).all()
                    assert len(exchanges) == 1
                    assert exchanges[0].mailbox_message_id == "sent-1"
                    entries = (
                        await session.scalars(
                            select(AuditLog).where(
                                AuditLog.tenant_id == seeded.tenant_id,
                                AuditLog.action == AuditAction.MAIL_MESSAGE_SENT.value,
                            )
                        )
                    ).all()
                    assert len(entries) == 1
            finally:
                await transaction.rollback()
    finally:
        await _delete_seeded_reminder(seeded)


async def test_a_delivery_blocks_on_a_row_another_cycle_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery-time lock serializes two consumers of one row.

    A takes the row (its cycle will commit per row, so the batch read's lock
    cannot cover the delivery — the row lock that matters is the delivery-time
    one); B's ``deliver_one`` of the same row must wait, not run alongside.
    A rolls back without sending anything, B's wait ends, and B — now the one
    holder — delivers the row: exactly one letter leaves, and B's own
    transaction rolls back at the answer's edge.
    """
    seeded = await _seed_committed_reminder_job()
    google = FakeGoogle()
    _seam_connected_mailbox(monkeypatch, google)
    try:
        async with engine.connect() as connection_a:
            transaction_a = await connection_a.begin()
            async with AsyncSession(bind=connection_a, expire_on_commit=False) as session_a:
                (job_a,) = await outbox.pending_jobs(session_a, tenant_id=seeded.tenant_id)
                b_waiter = asyncio.create_task(_deliver_one_fresh(seeded, job_a.id))
                await asyncio.sleep(0.3)
                assert not b_waiter.done()  # serialized: B waits on the row lock
                await transaction_a.rollback()  # A sent nothing; the row is free
            outcome_b = await asyncio.wait_for(b_waiter, timeout=30)

        assert outcome_b.disposition is mail_send.SendDisposition.SENT
        assert len(google.sent_raw) == 1  # one letter left, by the one holder
    finally:
        await _delete_seeded_reminder(seeded)


async def test_a_stale_delivery_reference_is_skipped_not_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch read earlier in the cycle is no license to deliver.

    B holds a reference it read before the winner delivered (the shape
    ``run_cycle`` produces: the batch is read once, each row's outcome
    committed per delivery). The winner's committed writes are the audit-silent
    ones — the exchange and the row's ``sent`` outcome; B's ``deliver_one`` of
    the stale reference re-reads the row under the lock, refreshes the
    reference in place, and answers ``skipped`` **before** anything leaves for
    the provider a second time.
    """
    seeded = await _seed_committed_reminder_job()
    google = FakeGoogle()
    _seam_connected_mailbox(monkeypatch, google)
    try:
        async with engine.connect() as connection_b:
            transaction_b = await connection_b.begin()
            async with AsyncSession(bind=connection_b, expire_on_commit=False) as session_b:
                stale = await session_b.get(MailOutbox, seeded.outbox_id)
                assert stale is not None
                assert stale.status == OutboxStatus.PENDING.value

                # The winner: another session records the exchange and marks
                # the row sent — the two writes a delivery commits, and both
                # audit-silent, so the committed state is cleanable.
                async with engine.connect() as connection_w, AsyncSession(
                    bind=connection_w, expire_on_commit=False
                ) as session_w:
                    job_w = await session_w.get(MailOutbox, seeded.outbox_id)
                    assert job_w is not None
                    request_w = await session_w.get(ConfirmationRequest, seeded.request_id)
                    assert request_w is not None
                    exchange = await communications.record_outbound_message(
                        session_w,
                        tenant_id=seeded.tenant_id,
                        company_id=seeded.company_id,
                        confirmation_request_id=seeded.request_id,
                        mailbox_message_id="sent-1",
                        thread_id=None,
                        sender_address=MAILBOX,
                        recipient_addresses=CONTACT,
                        received_at=datetime.now(UTC),
                        raw_document_id=seeded.document_id,
                        kind=CommunicationKind.REMINDER,
                    )
                    await outbox.mark_sent(
                        session_w,
                        job=job_w,
                        request=request_w,
                        communication_id=exchange.id,
                        provider_message_id="sent-1",
                        message_id="<overlap-reminder-1@tcsp.example>",
                    )
                    await connection_w.commit()

                outcome = await mail_send.deliver_one(session_b, seeded.store, job=stale)
                assert outcome.disposition is mail_send.SendDisposition.SKIPPED
                assert outcome.reason == "not_pending"
            await transaction_b.rollback()  # B wrote nothing

        assert google.sent_raw == []  # the loser never reached the provider
        row = await _seeded_row(seeded)
        assert row.status == OutboxStatus.SENT.value
        assert row.provider_message_id == "sent-1"
        assert len(await _recorded_exchanges(seeded)) == 1
    finally:
        await _delete_seeded_reminder(seeded)


async def test_mark_sent_refuses_a_row_superseded_mid_delivery() -> None:
    """The second half of the recorded follow-up: no overwrite under the lock.

    A delivery that raced a regeneration holds a row the supersede has since
    cancelled. ``mark_sent`` re-reads the row under its lock; the guard refuses
    the move, and the cancelled row stays cancelled — the record of what
    happened to the letter.
    """
    seeded = await _seed_committed_reminder_job()
    try:
        async with engine.connect() as connection_b:
            transaction_b = await connection_b.begin()
            async with AsyncSession(bind=connection_b, expire_on_commit=False) as session_b:
                job = await session_b.get(MailOutbox, seeded.outbox_id)
                assert job is not None
                request = await session_b.get(ConfirmationRequest, seeded.request_id)
                assert request is not None
                # The exchange the racing delivery recorded in its own
                # transaction (the sender's order: record, then mark).
                exchange = await communications.record_outbound_message(
                    session_b,
                    tenant_id=seeded.tenant_id,
                    company_id=seeded.company_id,
                    confirmation_request_id=seeded.request_id,
                    mailbox_message_id="<overlap-reminder-1@tcsp.example>",
                    thread_id=None,
                    sender_address=MAILBOX,
                    recipient_addresses=CONTACT,
                    received_at=datetime.now(UTC),
                    raw_document_id=seeded.document_id,
                    kind=CommunicationKind.REMINDER,
                )

                # A regeneration supersedes the request and cancels every
                # pending row of it, committed before the late mark arrives.
                async with engine.connect() as connection_a, AsyncSession(
                    bind=connection_a, expire_on_commit=False
                ) as session_a:
                    live = await session_a.get(ConfirmationRequest, seeded.request_id)
                    assert live is not None
                    await outbox.supersede(session_a, request=live, reason="superseded")
                    await connection_a.commit()

                with pytest.raises(outbox.OutboxTransitionError):
                    await outbox.mark_sent(
                        session_b,
                        job=job,
                        request=request,
                        communication_id=exchange.id,
                        provider_message_id="late-1",
                        message_id=None,
                    )
            await transaction_b.rollback()

        row = await _seeded_row(seeded)
        assert row.status == OutboxStatus.CANCELLED.value
        assert row.provider_message_id is None
    finally:
        await _delete_seeded_reminder(seeded)


async def test_a_duplicate_outbound_record_returns_the_winners_row() -> None:
    """A caught ``IntegrityError``, answered truthfully.

    The SMTP leg's identity is the stamped ``Message-ID``: two racing
    deliveries of one letter record the same ``mailbox_message_id``, and the
    per-tenant unique index lets exactly one insert through. The loser's
    re-read answers with the winner's row — the exchange one letter has one
    record — instead of the uncaught error the follow-up recorded.
    """
    seeded = await _seed_committed_reminder_job()
    try:
        async with (
            engine.connect() as connection_a,
            AsyncSession(bind=connection_a, expire_on_commit=False) as session_a,
        ):
            winner = await communications.record_outbound_message(
                session_a,
                tenant_id=seeded.tenant_id,
                company_id=seeded.company_id,
                confirmation_request_id=seeded.request_id,
                mailbox_message_id="<overlap-reminder-1@tcsp.example>",
                thread_id=None,
                sender_address=MAILBOX,
                recipient_addresses=CONTACT,
                received_at=datetime.now(UTC),
                raw_document_id=seeded.document_id,
                kind=CommunicationKind.REMINDER,
            )
            await connection_a.commit()

        async with engine.connect() as connection_b:
            async with AsyncSession(bind=connection_b, expire_on_commit=False) as session_b:
                answer = await communications.record_outbound_message(
                    session_b,
                    tenant_id=seeded.tenant_id,
                    company_id=seeded.company_id,
                    confirmation_request_id=seeded.request_id,
                    mailbox_message_id="<overlap-reminder-1@tcsp.example>",
                    thread_id=None,
                    sender_address=MAILBOX,
                    recipient_addresses=CONTACT,
                    received_at=datetime.now(UTC),
                    raw_document_id=seeded.document_id,
                    kind=CommunicationKind.REMINDER,
                )
                assert answer.id == winner.id
            await connection_b.rollback()

        assert len(await _recorded_exchanges(seeded)) == 1
    finally:
        await _delete_seeded_reminder(seeded)


async def test_a_company_less_ask_delivers_end_to_end_over_the_queue(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
) -> None:
    """The 0035 switch, send-path complete (ruling R4): the company-less ask
    the cycle's enqueue registers tenant-scoped rides the same queue, letter
    and provider legs as ①'s asks — ``deliver_one`` reads the stored letter
    through the company-less document read, the fake SMTP leg accepts it, and
    the outbound exchange is recorded with ``company_id = NULL`` under the
    tenant fallback. On a Free deployment: no gate may hold a notice
    (ruling D3), and the letter's bytes are the founder-gated draft."""
    provision(monkeypatch, tier="free")
    configure_mailbox(monkeypatch)
    tenant, _admin = await seed_tenant_with_admin(session)
    case = AttributionCase(
        tenant_id=tenant.id,
        company_id=None,
        mailbox_message_id="provider-company-less-ask",
        sender_address=CONTACT,
        status="awaiting_clarification",
        thread_id="thread-company-less",
        original_rfc_message_id="<original-company-less@client.example>",
        ask_kind="clarification",
    )
    session.add(case)
    await session.flush()

    report = await attribution.enqueue_ask(
        session,
        store,
        scanner_seam,
        tenant_id=tenant.id,
        case=case,
        recipients=(CONTACT,),
        rfc_message_ids=[],
        original_subject="Which company?",
        original_received_at=AT,
        auto_submitted=None,
        return_path=None,
        now=AT,
    )
    assert report.enqueued is True
    assert report.reason is None
    assert case.ask_sent_at == AT
    letter = await documents.get_document(
        session, tenant_id=tenant.id, company_id=None, document_id=report.document_id
    )
    assert letter.storage_key.startswith("_correspondence/")
    row = await session.get(MailOutbox, report.outbox_id)
    assert row.company_id is None

    (job,) = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        outcome = await mail_send.deliver_one(session, store, job=job, now=AT)

    assert outcome.disposition is mail_send.SendDisposition.SENT
    assert outcome.reason is None
    assert len(server.messages) == 1
    assert server.messages[0].recipients == [CONTACT]
    assert server.messages[0].data == store.objects[letter.storage_key]

    # The exchange record: the ask's own kind, no company, the tenant-scoped
    # letter it delivered, and the SMTP leg's identity anchor.
    (exchange,) = (
        await session.scalars(
            select(Communication).where(
                Communication.tenant_id == tenant.id,
                Communication.direction == "outbound",
            )
        )
    ).all()
    assert exchange.kind == CommunicationKind.ATTRIBUTION_ASK.value
    assert exchange.company_id is None
    assert exchange.confirmation_request_id is None
    assert exchange.sender_address == MAILBOX
    assert exchange.recipient_addresses == CONTACT
    assert exchange.raw_document_id == letter.id
    assert exchange.mailbox_message_id is not None
    await session.flush()
