"""Slice R acceptance walk: the reminder engine end to end (opt-in; needs a
migrated PostgreSQL).

R7's rerunnable evidence for contract §5.1.12 — the cadence, the sweep, and
exhaustion — as walks driven through the real endpoint stack against the real
database, one walk per scenario:

1. **request sent → +3d reminder enqueued and delivered.** An entitled tenant
   (Standard tier, package covering ``confirmation_generation`` and
   ``email_ingestion``, the period's renewal decided and paid), a company with
   an email contact; the request generated (``automatic``: the generation is
   the release) and delivered on the fake SMTP leg; the anchor rewound three
   days past the first cadence point; ``POST /mail/reminders`` enqueues the
   first reminder — the letter stored through the records registry, the outbox
   row carrying ``not_before`` = the +3d instant, one ``reminder_enqueued``
   entry on the hash chain — and ``POST /mail/send`` delivers it: the wire
   message is the stored letter, threaded on the request's own ``Message-ID``.
2. **a reply ends the chain.** The same shape, but the client's reply — read
   over the IMAP fallback leg, its ``In-Reply-To`` carrying the request's
   recorded ``Message-ID`` — joins the request's thread at ingestion; a sweep
   eleven days out enqueues nothing and names the skip ``answered``.
3. **supersede cancels pending reminders.** A reminder row pending, the
   request regenerated: §5.1.11's flow says a new version supersedes the old,
   and §5.1.12 says a superseded request ends its chain — its pending reminder
   row cancelled in the same transaction. This walk was R7's RED evidence (a
   ``sent`` request was never superseded, so the row stayed ``pending``); the
   founder ruled it a Critical fix for Slice R, and the transition table now
   admits ``SUPERSEDE`` from ``sent`` (2026-09-29) — the walk is green, and
   asserts the supersession end to end.
4. **the gate refusal is named.** Everything complete, then the licence lapses
   to Free (the §4.3 degradation): the sweep's outcome is ``denied`` with the
   gate's own ``tier_capability_missing``, and nothing was written — no
   letter, no outbox row, no audit entry — while the dashboard's
   ``pending_confirmations`` widget names the block on its ungated read.
5. **exhaustion surfaces on the reads.** A full automatic run to +10d with
   deliveries at +3d and +7d: one ``reminders_exhausted`` entry across two
   sweeps, the dashboard showing ``exhausted`` with ``reminders_sent == 2``,
   and the client-actions reply milestone ``blocked`` with
   ``blocked_reason: reminders_exhausted``.

Across the walks: the audit hash chain verifies after every scenario, and one
custody walk plants a marker in the company name (which both letters quote)
and in the app password, then searches every audit entry, every captured log
record and every report response for both — the markers are expected in the
delivered letters and in the ``company_fact_recorded`` entries alone.

**The harness** mirrors ``tests/test_reminders_application.py`` (R4's just-built
sweep harness: the rollback session, the in-memory store, the seeded tenant
and Administrator, the route helpers) rather than inventing a new one — but it
is restated here, not imported, because this family's convention is that no
test module imports another's fixtures or helpers (the phase-gate files do the
same).

**The clock.** Every business fact in these walks enters through a real route;
the one thing no route can do is wait three days. The production modules
deliberately have no clock seam (the sweep takes ``now``; the route passes the
wall clock), so the walks move the *anchor* instead: after the send cycle has
delivered the request letter for real, the sent exchange's acceptance moment
and the outbox row's send moment are rewound to the instant the walk pretends
the letter left. The letter, the request, the gate decisions — everything that
is a fact — was written by the real routes; only the clock reading is moved.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail import imap_client, smtp_client
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.core.config import settings
from app.domain.audit import AuditAction, verify_chain
from app.domain.communications import CommunicationKind, Direction
from app.domain.confirmation import RequestStatus
from app.domain.documents import MESSAGE_CONTENT_TYPE, DocumentKind
from app.domain.mail_send import OutboxKind, OutboxStatus
from app.domain.service_packages import GatingReason
from app.main import create_app
from tests.fake_imap import FakeImap
from tests.fake_smtp import FakeSmtpServer, trusting_context

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

REMINDERS_URL = "/api/v1/mail/reminders"
SEND_URL = "/api/v1/mail/send"
INGEST_URL = "/api/v1/mail/ingest"

MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
APP_PASSWORD = "app-password-marker-never-logged"
NAME_EN = "Bright Lantern Design Limited"
# Planted where the letters quote the company back: every custody search looks
# for this, and the delivered letters prove the search is not vacuous.
NAME_MARKER = "ZED-MARKER-4471"
# Planted in the inbound reply's body: the ingest report must not carry it,
# and the stored raw document proves it really is in the mail.
BODY_MARKER = "ANSWER-MARKER-9931"

# The panel only lists a period when a milestone date falls inside its window,
# so the walk companies' anniversary sits 40 days out: the continuation ask
# lands inside the 30-day lookback and the rest inside the horizon (R4's own
# derivation).
ANNIVERSARY = date.today() + timedelta(days=40)
TARGET_DATE = ANNIVERSARY.isoformat()
COVERAGE = {"start": f"{ANNIVERSARY.year - 1}-01-01", "end": f"{ANNIVERSARY.year}-12-31"}

# The shipped default cadence (3, 7, 10) is what these walks count on.
CADENCE = (3, 7, 10)

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": ANNIVERSARY.isoformat(),
    "type": "private",
}


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

    Hostname verification stays on: a scenario that sends over SMTP exercises
    the same checks a deployment does, against the local server instead of a
    real provider (``test_mail_send_scenarios_db.py``'s fixture, restated).
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


def provision(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    """Provision the deployment configuration the entitlement seam reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)


def use_smtp(monkeypatch: pytest.MonkeyPatch, server: FakeSmtpServer) -> None:
    """Provision the SMTP fallback (ADR-0005 path A) against a local server.

    The address is what every client-facing message is written from (scope
    §2.5) and the identity the leg authenticates as — the fake accepts an app
    password only for the address it was built with.
    """
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    monkeypatch.setattr(settings, "smtp_host", "localhost")
    monkeypatch.setattr(settings, "smtp_port", server.port)
    monkeypatch.setattr(settings, "mail_app_password", server.password)


def use_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeImap) -> None:
    """Provision the IMAP read fallback — the other half of path A.

    No Google client id, no connected credential: the corporate-Exchange
    deployment both fakes stand up together, one app password for both legs
    (``test_mail_ingest_imap_scenarios_db.py``'s configuration, restated).
    """
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)


def make_company(client: TestClient, **overrides: object) -> str:
    response = client.post("/api/v1/companies", json={**COMPANY, **overrides})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def name_company(
    client: TestClient, company_id: str, *, name_en: str = NAME_EN, marker: bool = False
) -> None:
    recorded = f"Bright Lantern {NAME_MARKER} Limited" if marker else name_en
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "effective_date": "2026-01-01",
            "name": {"name_en": recorded, "name_zh": None},
        },
    )
    assert response.status_code == 200, response.text


def add_registered_contact(client: TestClient, company_id: str, *, email: str = CONTACT) -> None:
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


def cover(client: TestClient, company_id: str) -> None:
    """An assignment whose package covers the two automations these paths gate."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": ["confirmation_generation", "email_ingestion"],
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


def record_continuation(client: TestClient, company_id: str) -> None:
    """The period's answer and fee — what the release gate wants on record."""
    created = client.post(
        f"/api/v1/companies/{company_id}/service-renewals",
        json={"anniversary_year": ANNIVERSARY.year},
    )
    assert created.status_code == 200, created.text
    decision = client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{ANNIVERSARY.year}/decision",
        json={"decision": "confirmed"},
    )
    assert decision.status_code == 200, decision.text
    payment = client.post(
        f"/api/v1/companies/{company_id}/service-renewals/{ANNIVERSARY.year}/payment",
        json={},
    )
    assert payment.status_code == 200, payment.text


def ready_company(client: TestClient, *, name_marker: bool = False) -> str:
    """A company the generator will draft for, covered and paid."""
    company_id = make_company(client)
    name_company(client, company_id, marker=name_marker)
    add_registered_contact(client, company_id)
    cover(client, company_id)
    record_continuation(client, company_id)
    return company_id


def generate(client: TestClient, company_id: str) -> dict[str, Any]:
    """The gated generation — under the shipped defaults it is the release."""
    response = client.post(
        f"/api/v1/companies/{company_id}/confirmation-requests",
        json={"form_type": "nar1", "target_date": TARGET_DATE},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def rewind_anchor(session: AsyncSession, *, request_id: UUID, sent_at: datetime) -> None:
    """Move the chain's clock anchor: the moment the request's letter left.

    The walks' one clock-travel lever (see the module docstring): the send
    cycle delivered the letter for real, and only the acceptance reading is
    moved — the sent exchange's moment and the outbox row's send moment,
    together, so the record stays coherent.
    """
    await session.execute(
        update(Communication)
        .where(
            Communication.confirmation_request_id == request_id,
            Communication.direction == Direction.OUTBOUND.value,
        )
        .values(received_at=sent_at)
    )
    await session.execute(
        update(MailOutbox)
        .where(
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == OutboxKind.CONFIRMATION_REQUEST.value,
        )
        .values(sent_at=sent_at)
    )
    await session.flush()


async def outbox_rows(
    session: AsyncSession, *, request_id: UUID, kind: OutboxKind
) -> list[MailOutbox]:
    # populate_existing: the walks re-read rows a route just changed (the
    # sender's cycle runs in the same session) and the identity map would
    # otherwise answer with the pre-change instance.
    rows = await session.scalars(
        select(MailOutbox)
        .where(
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == kind.value,
        )
        .order_by(MailOutbox.created_at, MailOutbox.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def request_exchange(session: AsyncSession, request_id: UUID) -> Communication:
    """The request's own outbound row — the chain's anchor and thread id."""
    row = await session.scalar(
        select(Communication)
        .where(
            Communication.confirmation_request_id == request_id,
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.CONFIRMATION_REQUEST.value,
        )
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def reminder_exchange(session: AsyncSession, request_id: UUID) -> Communication:
    row = await session.scalar(
        select(Communication)
        .where(
            Communication.confirmation_request_id == request_id,
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.REMINDER.value,
        )
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def document_count(session: AsyncSession) -> int:
    return len((await session.scalars(select(Document))).all())


def reply_raw(*, in_reply_to: str, body: str = BODY_MARKER, sender: str = CONTACT) -> bytes:
    """A client reply whose thread header carries the request's Message-ID."""
    return (
        f"From: CHAN TAI MAN <{sender}>\r\n"
        f"To: {MAILBOX}\r\n"
        f"Subject: Re: annual return confirmation\r\n"
        f"Message-ID: <{uuid4()}@client.example>\r\n"
        f"Date: Tue, 23 Sep 2026 09:31:00 +0800\r\n"
        f"In-Reply-To: {in_reply_to}\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode()


def _message_text(raw: bytes) -> str:
    """The plain-text body of a stored or transmitted message, decoded."""
    body = message_from_bytes(raw, policy=default_policy).get_body(preferencelist=("plain",))
    return "" if body is None else str(body.get_content())


# --- 1. request sent → +3d reminder enqueued and delivered -----------------


async def test_a_sent_request_grows_a_reminder_at_the_first_cadence_point(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch, "standard")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client)
    request = generate(client, company_id)
    assert request["status"] == RequestStatus.QUEUED.value

    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)

        sent = client.post(SEND_URL)
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] == 1
        exchange = await request_exchange(session, UUID(request["id"]))
        assert exchange.kind == CommunicationKind.CONFIRMATION_REQUEST.value
        assert exchange.mailbox_message_id is not None  # the thread anchor
        assert exchange.raw_document_id == UUID(request["document_id"])

        # The walk's clock travel: the letter left four days ago, so the
        # first cadence point (shipped default: +3d) has passed.
        anchor = datetime.now(UTC) - timedelta(days=4)
        await rewind_anchor(session, request_id=UUID(request["id"]), sent_at=anchor)

        sweep = client.post(REMINDERS_URL)
        assert sweep.status_code == 200, sweep.text
        body = sweep.json()
        assert body["examined"] == 1
        assert (body["enqueued"], body["held"]) == (1, 0)
        assert (body["denied"], body["exhausted"], body["skipped"]) == (0, 0, 0)
        (outcome,) = body["outcomes"]
        assert outcome["confirmation_request_id"] == request["id"]
        assert outcome["outcome"] == "enqueued"
        assert outcome["cadence_index"] == 0
        assert outcome["held"] is False

        # The row the report describes: a reminder, released with the queue,
        # not_before exactly the +3d instant, naming its own letter.
        (row,) = await outbox_rows(
            session, request_id=UUID(request["id"]), kind=OutboxKind.REMINDER
        )
        assert row.status == OutboxStatus.PENDING.value
        assert row.destination == "client"
        assert row.not_before == anchor + timedelta(days=CADENCE[0])
        assert row.letter_document_id is not None
        assert row.letter_document_id != UUID(request["document_id"])

        # The letter through the records registry: a generated message/rfc822
        # document, retrievable through the documents read, threaded on the
        # request, the founder-approved bilingual copy.
        letter = await session.get(Document, row.letter_document_id)
        assert letter is not None
        assert letter.content_type == MESSAGE_CONTENT_TYPE
        listed = client.get(f"/api/v1/companies/{company_id}/documents")
        assert listed.status_code == 200, listed.text
        entry = next(item for item in listed.json() if item["id"] == str(letter.id))
        assert entry["kind"] == DocumentKind.GENERATED_DRAFT.value
        assert entry["content_type"] == MESSAGE_CONTENT_TYPE
        download = client.get(f"/api/v1/companies/{company_id}/documents/{letter.id}/download")
        assert download.status_code == 200, download.text
        assert download.json()["url"].startswith("https://storage.invalid/")
        stored = store.objects[letter.storage_key]
        parsed_letter = message_from_bytes(stored, policy=default_policy)
        assert "Reminder: confirmation request" in str(parsed_letter["Subject"])
        assert parsed_letter.get("In-Reply-To") == exchange.mailbox_message_id
        assert parsed_letter.get("References") == exchange.mailbox_message_id
        text = _message_text(stored)
        assert "We wrote to you on" in text
        assert "本公司早於" in text

        # One enqueue entry, on the same facts the contract names — and the
        # hash chain verifies.
        entries = await audit_entries(session, tenant.id)
        enqueued = [e for e in entries if e.action == AuditAction.REMINDER_ENQUEUED.value]
        (entry_log,) = enqueued
        assert entry_log.company_id == UUID(company_id)
        assert entry_log.entity_id == UUID(request["id"])
        assert entry_log.details["cadence_index"] == 0
        assert entry_log.details["held"] is False
        assert entry_log.details["not_before"] == (anchor + timedelta(days=CADENCE[0])).isoformat()
        assert entry_log.details["outbox_id"] == str(row.id)
        assert entry_log.details["document_id"] == str(letter.id)
        assert verify_chain(entries) == []

        # Draining the queue delivers the reminder: the wire carried the
        # stored letter byte for byte, to the request's recipient, with the
        # request's Message-ID as its thread parent.
        drained = client.post(SEND_URL)
        assert drained.status_code == 200, drained.text
        assert drained.json()["sent"] == 1

    assert len(server.messages) == 2
    reminder_wire = server.messages[1]
    assert reminder_wire.recipients == [CONTACT]
    letter = await session.get(Document, (await outbox_rows(
        session, request_id=UUID(request["id"]), kind=OutboxKind.REMINDER
    ))[0].letter_document_id)
    assert letter is not None
    assert reminder_wire.data == store.objects[letter.storage_key]
    parsed_wire = message_from_bytes(reminder_wire.data, policy=default_policy)
    assert parsed_wire.get("In-Reply-To") == exchange.mailbox_message_id
    assert parsed_wire.get("References") == exchange.mailbox_message_id
    # The sender recorded the reminder's own outbound row: its letter, the
    # identity the wire carried, and the same provider thread fields the
    # request has — this leg provides none, and both rows say so alike.
    reminder = await reminder_exchange(session, UUID(request["id"]))
    assert reminder.mailbox_message_id == str(parsed_wire["Message-ID"])
    assert reminder.raw_document_id == letter.id
    assert reminder.thread_id == exchange.thread_id
    (request_row,) = await outbox_rows(
        session, request_id=UUID(request["id"]), kind=OutboxKind.CONFIRMATION_REQUEST
    )
    (reminder_row,) = await outbox_rows(
        session, request_id=UUID(request["id"]), kind=OutboxKind.REMINDER
    )
    assert request_row.provider_message_id is None
    assert reminder_row.provider_message_id is None
    assert request_row.status == OutboxStatus.SENT.value
    assert reminder_row.status == OutboxStatus.SENT.value
    assert verify_chain(await audit_entries(session, tenant.id)) == []


# --- 2. a reply ends the chain ----------------------------------------------


async def test_a_reply_on_the_requests_thread_ends_the_chain(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    provision(monkeypatch, "standard")
    use_imap(monkeypatch, fake)
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client)
    request = generate(client, company_id)

    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        sent = client.post(SEND_URL)
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] == 1

        # Eleven days out the chain would be spent — unless a reply ends it.
        anchor = datetime.now(UTC) - timedelta(days=11)
        await rewind_anchor(session, request_id=UUID(request["id"]), sent_at=anchor)
        exchange = await request_exchange(session, UUID(request["id"]))

        # The client's reply rides the request's Message-ID back, over the
        # IMAP fallback leg — no provider thread exists on this leg, so the
        # header is the whole of the join.
        fake.add_message(
            raw=reply_raw(in_reply_to=exchange.mailbox_message_id or ""),
            received_at=datetime.now(UTC),
        )
        ingested = client.post(INGEST_URL)

    assert ingested.status_code == 200, ingested.text
    ingest_body = ingested.json()
    assert ingest_body["examined"] == 1
    assert ingest_body["ingested"] == 1
    (outcome,) = ingest_body["outcomes"]
    assert outcome["outcome"] == "ingested"
    assert outcome["company_id"] == company_id
    # The response describes mail; it never carries any of it.
    assert BODY_MARKER not in ingested.text

    # The inbound Communication names the request it answers.
    reply_row = await session.scalar(
        select(Communication).where(
            Communication.direction == Direction.INBOUND.value,
            Communication.kind == CommunicationKind.REPLY.value,
        )
    )
    assert reply_row is not None
    assert reply_row.confirmation_request_id == UUID(request["id"])
    assert reply_row.sender_address == CONTACT
    raw_document = await session.get(Document, reply_row.raw_document_id)
    assert raw_document is not None
    assert BODY_MARKER.encode() in store.objects[raw_document.storage_key]

    # The sweep at +10d: nothing enqueued, the skip named "answered".
    sweep = client.post(REMINDERS_URL)
    assert sweep.status_code == 200, sweep.text
    sweep_body = sweep.json()
    assert sweep_body["examined"] == 1
    assert sweep_body["enqueued"] == 0
    (sweep_outcome,) = sweep_body["outcomes"]
    assert sweep_outcome["confirmation_request_id"] == request["id"]
    assert sweep_outcome["outcome"] == "skipped"
    assert sweep_outcome["skip_reason"] == "answered"
    reminder_rows = await outbox_rows(
        session, request_id=UUID(request["id"]), kind=OutboxKind.REMINDER
    )
    assert reminder_rows == []
    entries = await audit_entries(session, tenant.id)
    assert [e for e in entries if e.action == AuditAction.REMINDER_ENQUEUED.value] == []
    assert BODY_MARKER not in sweep.text
    assert verify_chain(entries) == []


# --- 3. supersede cancels pending reminders ---------------------------------


async def test_regenerating_the_request_cancels_its_pending_reminder(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch, "standard")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client)
    first = generate(client, company_id)

    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        sent = client.post(SEND_URL)
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] == 1
        anchor = datetime.now(UTC) - timedelta(days=4)
        await rewind_anchor(session, request_id=UUID(first["id"]), sent_at=anchor)

        # The sweep enqueues the +3d reminder; the walk does not drain it, so
        # the row pends — the shape §5.1.12's supersede sentence is about.
        sweep = client.post(REMINDERS_URL)
        assert sweep.status_code == 200, sweep.text
        assert sweep.json()["enqueued"] == 1
        (pending_row,) = await outbox_rows(
            session, request_id=UUID(first["id"]), kind=OutboxKind.REMINDER
        )
        assert pending_row.status == OutboxStatus.PENDING.value

        # §5.1.11's flow: a new version supersedes the old. §5.1.12: a
        # superseded request ends its chain with it — its pending reminder
        # row cancelled in the same transaction.
        second = generate(client, company_id)
        assert second["id"] != first["id"]
        assert second["version_no"] == 2

        superseded = await session.scalar(
            select(ConfirmationRequest)
            .where(ConfirmationRequest.id == UUID(first["id"]))
            .execution_options(populate_existing=True)
        )
        assert superseded is not None
        # §5.1.12 as ruled 2026-09-29 (the Critical fix this walk was built to
        # prove): a regeneration supersedes a sent request, and its chain —
        # the pending reminder row included — ends in the same transaction.
        assert superseded.status == RequestStatus.SUPERSEDED.value
        row = await session.scalar(
            select(MailOutbox)
            .where(MailOutbox.id == pending_row.id)
            .execution_options(populate_existing=True)
        )
        assert row is not None
        assert row.status == OutboxStatus.CANCELLED.value

        # A sweep now selects nothing for the old request: it is no longer
        # ``sent``, and no reminder for it is ever enqueued again.
        follow_up = client.post(REMINDERS_URL)
        assert follow_up.status_code == 200, follow_up.text
        assert all(
            not (
                item["confirmation_request_id"] == first["id"]
                and item["outcome"] == "enqueued"
            )
            for item in follow_up.json()["outcomes"]
        )
        assert len(
            await outbox_rows(session, request_id=UUID(first["id"]), kind=OutboxKind.REMINDER)
        ) == 1
        verify = await audit_entries(session, tenant.id)
        assert verify_chain(verify) == []

    # Neither the cancelled reminder nor the new version's letter left: the
    # walk drained nothing after the regeneration.
    assert len(server.messages) == 1


# --- 4. the gate refusal is named, and writes nothing ------------------------


async def test_the_gate_refusal_is_named_and_writes_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch, "standard")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client)
    request = generate(client, company_id)

    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        sent = client.post(SEND_URL)
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] == 1
        anchor = datetime.now(UTC) - timedelta(days=4)
        await rewind_anchor(session, request_id=UUID(request["id"]), sent_at=anchor)

        # The licence lapses to Free (§4.3's degradation) before the cycle.
        provision(monkeypatch, "free")
        documents_before = await document_count(session)
        sweep = client.post(REMINDERS_URL)

    assert sweep.status_code == 200, sweep.text
    body = sweep.json()
    assert body["examined"] == 1
    assert (body["denied"], body["enqueued"]) == (1, 0)
    (outcome,) = body["outcomes"]
    assert outcome["outcome"] == "denied"
    assert outcome["reason"] == GatingReason.TIER_CAPABILITY_MISSING.value
    # The gate's standing semantics: a denial writes nothing at all.
    reminder_rows = await outbox_rows(
        session, request_id=UUID(request["id"]), kind=OutboxKind.REMINDER
    )
    assert reminder_rows == []
    assert await document_count(session) == documents_before
    entries = await audit_entries(session, tenant.id)
    assert [e for e in entries if e.action == AuditAction.REMINDER_ENQUEUED.value] == []
    assert len(server.messages) == 1  # the request; no reminder ever left

    # The widget is an ungated read (ADR-0012): the block is named, not
    # implied — the automation the cadence describes is what Free tier lacks.
    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    widget = dashboard.json()["pending_confirmations"]
    (company_entry,) = widget["companies"]
    assert company_entry["company_id"] == company_id
    (entry,) = company_entry["requests"]
    assert entry["confirmation_request_id"] == request["id"]
    assert entry["automation_blocked"] == GatingReason.TIER_CAPABILITY_MISSING.value
    assert entry["exhausted"] is False
    assert entry["reminders_sent"] == 0
    assert dashboard.json()["service_gaps"] != []
    assert verify_chain(entries) == []


# --- 5. exhaustion is recorded once and surfaces on the reads ----------------


async def test_exhaustion_is_recorded_once_and_surfaces_on_the_reads(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch, "standard")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client)
    request = generate(client, company_id)

    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        sent = client.post(SEND_URL)
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] == 1
        # Eleven days out: the +3d and +7d slots are owed and the chain is
        # past its final cadence point — exhaustion is knowable from the
        # first sweep, before the ladder has delivered anything.
        anchor = datetime.now(UTC) - timedelta(days=11)
        await rewind_anchor(session, request_id=UUID(request["id"]), sent_at=anchor)

        first = client.post(REMINDERS_URL)
        assert first.status_code == 200, first.text
        assert first.json()["enqueued"] == 1
        assert client.post(SEND_URL).json()["sent"] == 1
        second = client.post(REMINDERS_URL)
        assert second.status_code == 200, second.text
        assert second.json()["enqueued"] == 1
        assert client.post(SEND_URL).json()["sent"] == 1

    assert len(server.messages) == 3  # the request and the +3d and +7d reminders
    entries = await audit_entries(session, tenant.id)
    exhausted = [e for e in entries if e.action == AuditAction.REMINDERS_EXHAUSTED.value]
    (exhausted_entry,) = exhausted  # exactly one, across the two sweeps
    assert exhausted_entry.company_id == UUID(company_id)
    assert exhausted_entry.entity_id == UUID(request["id"])
    assert exhausted_entry.details["reminders_sent"] == 0  # known at first sight
    assert exhausted_entry.details["days_unanswered"] == 11
    assert verify_chain(entries) == []

    # The dashboard's derived fields — asserted, not the +10d slot's enqueue,
    # which the walk deliberately never pressed.
    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    widget = dashboard.json()["pending_confirmations"]
    (company_entry,) = widget["companies"]
    (entry,) = company_entry["requests"]
    assert entry["confirmation_request_id"] == request["id"]
    assert entry["days_elapsed"] == 11
    assert entry["reminders_sent"] == 2
    assert entry["cadence_total"] == len(CADENCE)
    assert entry["exhausted"] is True
    assert entry["automation_blocked"] is None
    assert datetime.fromisoformat(entry["next_reminder_at"]) == anchor + timedelta(days=CADENCE[2])

    # The panel: the reply milestone is blocked, and names the leg.
    actions = client.get("/api/v1/client-actions")
    assert actions.status_code == 200, actions.text
    (period,) = actions.json()["periods"]
    assert period["anniversary_year"] == ANNIVERSARY.year
    reply = next(m for m in period["milestones"] if m["kind"] == "client_reply")
    assert reply["state"] == "blocked"
    assert reply["blocked_reason"] == "reminders_exhausted"
    assert verify_chain(await audit_entries(session, tenant.id)) == []


# --- The custody claim, asserted rather than promised ------------------------


async def test_the_walk_touches_no_subject_body_or_credential(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The standing PII assertion for the whole engine, in one full walk.

    Two markers are planted: the company name carries one (both letters quote
    it back to the client) and the app password is the other (a credential
    under ADR-0014, which must live in the environment and nowhere else).
    Every audit entry, every captured log record and every report response the
    walk produced is searched for both: the name is expected in exactly one
    kind of entry (``company_fact_recorded``, the master-data write path) and
    in the delivered letters; the password is expected nowhere at all.
    """
    caplog.set_level(logging.DEBUG)
    provision(monkeypatch, "standard")
    tenant, admin = await seed_tenant_with_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = ready_company(client, name_marker=True)
    request = generate(client, company_id)

    reports: list[str] = []
    with FakeSmtpServer(username=MAILBOX, password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        reports.append(client.post(SEND_URL).text)
        anchor = datetime.now(UTC) - timedelta(days=4)
        await rewind_anchor(session, request_id=UUID(request["id"]), sent_at=anchor)
        reports.append(client.post(REMINDERS_URL).text)
        reports.append(client.post(SEND_URL).text)

    assert len(server.messages) == 2
    # The search is not looking in the wrong place: both letters really
    # carry the name marker — the request letter quotes it into its body (it
    # asks the client to confirm the name), the reminder into its subject.
    for captured in server.messages:
        assert NAME_MARKER.encode() in captured.data

    entries = await audit_entries(session, tenant.id)
    carriers = {entry.action for entry in entries if NAME_MARKER in json.dumps(entry.details)}
    assert carriers == {AuditAction.COMPANY_FACT_RECORDED.value}
    for entry in entries:
        assert APP_PASSWORD not in json.dumps(entry.details)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert APP_PASSWORD not in logged
    assert NAME_MARKER not in logged

    # A report is a place a letter could leak: the sweep and send cycles answer
    # in ids, counts and bounded codes — never a subject line, never a body.
    reports_text = "\n".join(reports)
    assert APP_PASSWORD not in reports_text
    assert NAME_MARKER not in reports_text
    assert "Reminder: confirmation request" not in reports_text
    assert "We wrote to you on" not in reports_text
    listed = client.get(f"/api/v1/companies/{company_id}/communications")
    assert listed.status_code == 200, listed.text
    assert NAME_MARKER not in listed.text
    assert APP_PASSWORD not in listed.text
    assert verify_chain(entries) == []
