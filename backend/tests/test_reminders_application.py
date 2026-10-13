"""DB-backed reminder-sweep scenarios (opt-in; needs a migrated PostgreSQL).

Slice R's core against a real database: the sweep's enqueue, hold and release,
the gate's fail-closed denial, exhaustion (time-bound, recorded once), the
deterministic reply join at ingestion, the outbox corrections the widened
pending index forced (``_pending_job``'s kind leg, ``supersede`` cancelling a
request's reminder rows too), the dashboard's ``pending_confirmations`` widget
and the client-actions panel's ``reminders_exhausted`` block — and, from R5,
the kind-aware delivery of a reminder row (its own letter, the release gate
never consulted), the superseded request's cancel at delivery, and the
Administrator trigger's report and refusal.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.adapters.mail import smtp_client
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.base import Base
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import (
    communications,
    documents,
    mail_ingest,
    mail_send,
    outbox,
    reminders,
    scanning,
    service_renewals,
)
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.communications import CommunicationKind
from app.domain.confirmation import RequestStatus
from app.domain.documents import MESSAGE_CONTENT_TYPE
from app.domain.mail import MessageMetadata
from app.domain.mail_send import HELD_NOT_BEFORE, OutboxKind, OutboxStatus
from app.domain.service_packages import GatingReason
from app.main import create_app
from tests.fake_smtp import FakeSmtpServer, trusting_context

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
NAME_EN = "Bright Lantern Design Limited"

# The panel only lists a period when a milestone date falls inside its window,
# so the test companies' anniversary sits 40 days out: the continuation ask
# lands inside the 30-day lookback and the rest inside the horizon.
ANNIVERSARY = date.today() + timedelta(days=40)
TARGET_DATE = ANNIVERSARY.isoformat()
COVERAGE = {"start": f"{ANNIVERSARY.year - 1}-01-01", "end": f"{ANNIVERSARY.year}-12-31"}

# The shipped default cadence (3, 7, 10) is what these scenarios count on
# unless a test stores its own milestone block.
CADENCE = (3, 7, 10)


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@dataclass
class FakeMailbox:
    """The three reads one examined message needs, with no provider behind them."""

    metadata: dict[str, MessageMetadata]
    raws: dict[str, bytes]
    address: str = MAILBOX

    async def fetch_message_metadata(self, *, message_id: str) -> MessageMetadata:
        return self.metadata[message_id]

    async def fetch_message_raw(self, *, message_id: str) -> bytes:
        return self.raws[message_id]


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
def standard_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard tier and a mailbox address: the sweep's letter has a From."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)


@pytest.fixture
async def actor(session: AsyncSession) -> ActorRef:
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
    return ActorRef(user_id=admin.id, tenant_id=tenant.id)


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


# --- Harness -------------------------------------------------------------


def make_company(client: TestClient, **overrides: object) -> str:
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": "12345678",
            "cr_number": "1234567",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": ANNIVERSARY.isoformat(),
            "type": "private",
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def name_company(client: TestClient, company_id: str, *, name_en: str = NAME_EN) -> None:
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": None}},
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


def covered_company(client: TestClient, *, br_number: str = "12345678") -> str:
    company_id = make_company(client, br_number=br_number)
    name_company(client, company_id)
    add_registered_contact(client, company_id)
    cover(client, company_id)
    return company_id


def letter_bytes(message_id: str) -> bytes:
    """A minimal stored request letter carrying its own Message-ID."""
    return (
        f"From: {MAILBOX}\r\n"
        f"To: {CONTACT}\r\n"
        f"Subject: [Bright Lantern — CR 1234567] Annual Return Confirmation\r\n"
        f"Message-ID: {message_id}\r\n"
        f"\r\n"
        f"Please confirm. / 請確認。\r\n"
    ).encode()


async def make_sent_request(
    session: AsyncSession,
    store: FakeStore,
    *,
    tenant_id: UUID,
    company_id: UUID,
    sent_at: datetime,
    message_id: str,
    thread_id: str | None = None,
    version: int = 1,
    with_exchange: bool = True,
) -> ConfirmationRequest:
    """A request the deployment has already sent: row, letter, exchange, outbox.

    The four facts the sweep derives from — the request's ``sent`` state, the
    outbound ``Communication`` (the chain's anchor), and the sender's recorded
    ``Message-ID`` on the outbox row — assembled the way the send cycle would
    leave them, minus the provider. ``with_exchange=False`` leaves the
    exchange and outbox row off: the row-plus-letter shape of a request whose
    acceptance never recorded, the sweep's ``no_sent_communication`` edge.
    """
    document = await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=company_id,
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=letter_bytes(message_id),
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=version,
    )
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=ANNIVERSARY,
        reply_by=ANNIVERSARY - timedelta(days=20),
        statutory_due=None,
        status="sent",
        version_no=version,
        document_id=document.id,
        checklist=[],
        recipients=[{"person_id": str(uuid4()), "address": CONTACT}],
        created_by=None,
    )
    session.add(request)
    await session.flush()
    if not with_exchange:
        return request
    exchange = await communications.record_outbound_message(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=request.id,
        mailbox_message_id=message_id,
        thread_id=thread_id,
        sender_address=MAILBOX,
        recipient_addresses=CONTACT,
        received_at=sent_at,
        raw_document_id=document.id,
    )
    session.add(
        MailOutbox(
            tenant_id=tenant_id,
            company_id=company_id,
            confirmation_request_id=request.id,
            destination="client",
            status=OutboxStatus.SENT.value,
            attempts=1,
            message_id=message_id,
            provider_message_id=None,
            communication_id=exchange.id,
            created_by=None,
            sent_at=sent_at,
        )
    )
    await session.flush()
    return request


async def reminder_outbound(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    sent_at: datetime,
    document_id: UUID,
    index: int,
) -> None:
    """The sender's record of one accepted reminder (the count the ladder reads)."""
    await communications.record_outbound_message(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=request_id,
        mailbox_message_id=f"<reminder-{index}@tcsp.example>",
        thread_id=None,
        sender_address=MAILBOX,
        recipient_addresses=CONTACT,
        received_at=sent_at,
        raw_document_id=document_id,
        kind=CommunicationKind.REMINDER,
    )


async def audit_entries(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.scalars(select(AuditLog).where(AuditLog.action == action.value))
    return list(rows.all())


async def outbox_rows(
    session: AsyncSession, *, request_id: UUID, kind: OutboxKind
) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox)
        .where(
            MailOutbox.confirmation_request_id == request_id,
            MailOutbox.kind == kind.value,
        )
        .order_by(MailOutbox.created_at, MailOutbox.id)
    )
    return list(rows.all())


def inbound_raw(
    *, from_address: str = CONTACT, in_reply_to: str | None = None, body: str = "NO CHANGES"
) -> bytes:
    reply = f"In-Reply-To: {in_reply_to}\r\n" if in_reply_to else ""
    return (
        f"From: {from_address}\r\n"
        f"To: {MAILBOX}\r\n"
        f"Subject: Re: confirmation request\r\n"
        f"Message-ID: <{uuid4()}@client.example>\r\n"
        f"{reply}"
        f"\r\n"
        f"{body}\r\n"
    ).encode()


def _message_text(raw: bytes) -> str:
    """The plain-text body of a stored or transmitted message, decoded."""
    body = message_from_bytes(raw, policy=default_policy).get_body(preferencelist=("plain",))
    return "" if body is None else str(body.get_content())


# --- The sweep ------------------------------------------------------------


async def test_sweep_enqueues_a_due_reminder_under_automatic(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-1@tcsp.example>",
    )

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )

    (outcome,) = report.outcomes
    assert outcome.request_id == request.id
    assert outcome.outcome is reminders.SweepOutcome.ENQUEUED
    assert outcome.cadence_index == 0
    assert outcome.held is False
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    assert row.status == OutboxStatus.PENDING.value
    assert row.destination == "client"
    assert row.not_before == sent_at + timedelta(days=CADENCE[0])
    assert row.letter_document_id is not None
    # The stored letter is the bilingual reminder, threaded on the request.
    letter = row.letter_document_id and await session.get(
        documents.Document, row.letter_document_id
    )
    assert letter is not None
    assert letter.content_type == MESSAGE_CONTENT_TYPE
    parsed_letter = message_from_bytes(
        await store.get_bytes(letter.storage_key), policy=default_policy
    )
    assert "Reminder: confirmation request" in str(parsed_letter["Subject"])
    assert parsed_letter.get("In-Reply-To") == "<req-1@tcsp.example>"
    assert parsed_letter.get("References") == "<req-1@tcsp.example>"
    body = parsed_letter.get_content()
    assert "We wrote to you on" in body
    assert "本公司早於" in body
    # One audit entry, on the same facts the contract names.
    (entry,) = await audit_entries(session, AuditAction.REMINDER_ENQUEUED)
    assert entry.company_id == UUID(company_id)
    assert entry.entity_id == request.id
    assert entry.details["cadence_index"] == 0
    assert entry.details["held"] is False
    assert entry.details["not_before"] == (sent_at + timedelta(days=CADENCE[0])).isoformat()


async def test_sweep_holds_under_review_required_and_the_release_unholds(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    current = client.get("/api/v1/tenant-config").json()
    payload = {
        "naming_templates": current["naming_templates"],
        "milestones": current["milestones"],
        "send_modes": {"client": "review_required", "government": "review_required"},
    }
    put = client.put("/api/v1/tenant-config", json=payload)
    assert put.status_code == 200, put.text
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-2@tcsp.example>",
    )

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is reminders.SweepOutcome.ENQUEUED
    assert outcome.held is True
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    assert row.not_before == HELD_NOT_BEFORE

    # The staff release moves the hold to the press instant; a second press
    # finds nothing held and is a no-op.
    press = sent_at + timedelta(days=5)
    released = await outbox.release_reminders(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        actor_user_id=actor.user_id,
        now=press,
    )
    assert [r.id for r in released] == [row.id]
    assert row.not_before == press
    again = await outbox.release_reminders(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        actor_user_id=actor.user_id,
        now=press,
    )
    assert again == ()
    # No audit entry of its own: the enqueue was the audited act.
    assert await audit_entries(session, AuditAction.REMINDER_ENQUEUED) != []
    assert await audit_entries(session, AuditAction.REMINDERS_EXHAUSTED) == []


async def test_a_pending_reminder_row_paces_the_ladder(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=8)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-3@tcsp.example>",
    )
    now = sent_at + timedelta(days=8)
    first = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
    )
    assert first.outcomes[0].outcome is reminders.SweepOutcome.ENQUEUED

    second = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
    )

    (outcome,) = second.outcomes
    assert outcome.outcome is reminders.SweepOutcome.SKIPPED
    assert outcome.skip is reminders.SweepSkip.PENDING
    assert len(await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)) == 1


async def test_gate_denial_writes_nothing(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-4@tcsp.example>",
    )
    document_count = len((await session.scalars(select(documents.Document))).all())
    monkeypatch.setattr(settings, "license_tier", "free")

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is reminders.SweepOutcome.DENIED
    assert outcome.reason == GatingReason.TIER_CAPABILITY_MISSING.value
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []
    assert len((await session.scalars(select(documents.Document))).all()) == document_count
    assert await audit_entries(session, AuditAction.REMINDER_ENQUEUED) == []


async def test_exhaustion_fires_once_and_the_final_slot_still_dispatches(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=11)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-5@tcsp.example>",
    )
    now = sent_at + timedelta(days=11)  # past the final cadence point (10)

    first = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
    )

    # The chain reads exhausted, but the owed slot still dispatched: the
    # boundary is time-bound, not send-bound.
    (outcome,) = first.outcomes
    assert outcome.outcome is reminders.SweepOutcome.ENQUEUED
    (exhausted,) = await audit_entries(session, AuditAction.REMINDERS_EXHAUSTED)
    assert exhausted.entity_id == request.id
    assert exhausted.details["reminders_sent"] == 0
    assert exhausted.details["days_unanswered"] == 11

    second = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
    )
    assert len(await audit_entries(session, AuditAction.REMINDERS_EXHAUSTED)) == 1
    assert second.outcomes[0].outcome is reminders.SweepOutcome.SKIPPED

    # Once every cadence point has an accepted send behind it (the ladder is
    # spent), nothing is owed and the read is exhausted — still one entry.
    row = (await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER))[0]
    row.status = OutboxStatus.SENT.value
    row.sent_at = now
    row.last_error = None
    row.blocked_reason = None
    for index in (1, 2, 3):
        await reminder_outbound(
            session,
            tenant_id=actor.tenant_id,
            company_id=UUID(company_id),
            request_id=request.id,
            sent_at=now,
            document_id=row.letter_document_id,  # type: ignore[arg-type]
            index=index,
        )
    third = await reminders.sweep(
        session, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
    )
    assert third.outcomes[0].outcome is reminders.SweepOutcome.EXHAUSTED
    assert len(await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)) == 1
    assert len(await audit_entries(session, AuditAction.REMINDERS_EXHAUSTED)) == 1


async def test_a_sent_request_without_its_exchange_is_skipped(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-6@tcsp.example>",
        with_exchange=False,
    )

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )

    (outcome,) = report.outcomes
    assert outcome.outcome is reminders.SweepOutcome.SKIPPED
    assert outcome.skip is reminders.SweepSkip.NO_SENT_COMMUNICATION
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []


async def test_an_unreadable_milestone_block_stops_the_sweep_cleanly(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-7@tcsp.example>",
    )
    session.add(
        TenantConfig(
            tenant_id=actor.tenant_id,
            naming_templates={
                "document": "{CompanyCode}-{Form}",
                "generated": "{CompanyCode}-{Form}-{Version}",
            },
            milestones={"reminder_cadence_days": "not-a-list"},
            send_modes={"client": "automatic", "government": "review_required"},
        )
    )
    await session.flush()

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )

    assert report.config_error is not None
    assert report.outcomes == ()
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []


# --- The reply join at ingestion ------------------------------------------


async def test_a_reply_joins_its_request_by_in_reply_to_and_ends_the_chain(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-8@tcsp.example>",
    )
    metadata = MessageMetadata(
        message_id="provider-in-1",
        subject="Re: confirmation request",
        sender=CONTACT,
        date=None,
        received_at=datetime.now(UTC),
        thread_id=None,
        # What the envelope parsers put on the metadata (ADR-0019 §1's
        # metadata-first posture): the join reads these tokens, not the raw
        # bytes — the raw message is fetched later, to be stored.
        in_reply_to=("<req-8@tcsp.example>",),
    )
    mailbox = FakeMailbox(
        metadata={"provider-in-1": metadata},
        raws={"provider-in-1": inbound_raw(in_reply_to="<req-8@tcsp.example>")},
    )

    examined = await mail_ingest.examine_message(
        session,
        store,
        scanning.scanner(),
        at=datetime.now(UTC),
        tenant_id=actor.tenant_id,
        mailbox=mailbox,  # type: ignore[arg-type]
        message_id="provider-in-1",
    )

    assert examined.outcome.outcome is mail_ingest.IngestOutcome.INGESTED
    reply_id = examined.outcome.communication_id
    reply = await session.get(Communication, reply_id)
    assert reply is not None
    assert reply.confirmation_request_id == request.id

    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )
    (outcome,) = report.outcomes
    assert outcome.outcome is reminders.SweepOutcome.SKIPPED
    assert outcome.skip is reminders.SweepSkip.ANSWERED
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []


async def test_ambiguous_headers_join_nothing(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-9@tcsp.example>",
        thread_id="thread-request-a",
    )
    # Another company's sent request, carried on a different thread.
    other_company = make_company(client, br_number="87654321")
    name_company(client, other_company, name_en="Other Company Limited")
    add_registered_contact(client, other_company, email="other@client.example")
    await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(other_company),
        sent_at=sent_at,
        message_id="<req-b@tcsp.example>",
        thread_id="thread-request-b",
    )
    # The inbound message says A in In-Reply-To but rides B's thread: two
    # distinct requests match, and a guessed join would end the wrong chain.
    metadata = MessageMetadata(
        message_id="provider-in-2",
        subject="Re: confirmation request",
        sender=CONTACT,
        date=None,
        received_at=datetime.now(UTC),
        thread_id="thread-request-b",
        in_reply_to=("<req-9@tcsp.example>",),
    )
    mailbox = FakeMailbox(
        metadata={"provider-in-2": metadata},
        raws={"provider-in-2": inbound_raw(in_reply_to="<req-9@tcsp.example>")},
    )

    examined = await mail_ingest.examine_message(
        session,
        store,
        scanning.scanner(),
        at=datetime.now(UTC),
        tenant_id=actor.tenant_id,
        mailbox=mailbox,  # type: ignore[arg-type]
        message_id="provider-in-2",
    )

    assert examined.outcome.outcome is mail_ingest.IngestOutcome.INGESTED
    reply = await session.get(Communication, examined.outcome.communication_id)
    assert reply is not None
    assert reply.confirmation_request_id is None
    assert request.id is not None  # the sweep still sees an unanswered chain


# --- The outbox corrections ------------------------------------------------


async def test_supersede_cancels_the_requests_pending_reminder_rows_too(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    # A queued request whose reminder row pends alongside its send row: the
    # shape the widened index admits (one pending row per request and kind).
    queued = ConfirmationRequest(
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        form_type="nar1",
        target_date=ANNIVERSARY,
        reply_by=ANNIVERSARY - timedelta(days=20),
        statutory_due=None,
        status="queued",
        version_no=1,
        document_id=(
            await documents.register_upload(
                session,
                store,
                tenant_id=actor.tenant_id,
                company_id=UUID(company_id),
                actor_user_id=None,
                kind=documents.DocumentKind.GENERATED_DRAFT,
                origin=documents.DocumentOrigin.GENERATED,
                content_type=MESSAGE_CONTENT_TYPE,
                data=letter_bytes("<req-10@tcsp.example>"),
                scanner=scanning.scanner(),
                form="nar1",
                retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
                version=1,
            )
        ).id,
        checklist=[],
        recipients=[{"person_id": str(uuid4()), "address": CONTACT}],
        created_by=None,
    )
    session.add(queued)
    await session.flush()
    await outbox.release(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=queued.id,
        actor_user_id=actor.user_id,
    )
    letter = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=b"From: ai-filings@tcsp.example\r\nTo: d@x\r\nSubject: reminder\r\n\r\nbody\r\n",
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    reminder_row = await outbox.enqueue_reminder(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=queued.id,
        letter_document_id=letter.id,
        not_before=datetime.now(UTC),
    )
    request_row = (
        await outbox_rows(session, request_id=queued.id, kind=OutboxKind.CONFIRMATION_REQUEST)
    )[0]

    await outbox.supersede(session, request=queued)

    assert queued.status == "superseded"
    assert reminder_row.status == OutboxStatus.CANCELLED.value
    assert request_row.status == OutboxStatus.CANCELLED.value


async def test_release_returns_the_request_row_even_when_a_reminder_pends(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    document = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=letter_bytes("<req-11@tcsp.example>"),
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    request = ConfirmationRequest(
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        form_type="nar1",
        target_date=ANNIVERSARY,
        reply_by=ANNIVERSARY - timedelta(days=20),
        statutory_due=None,
        status="draft",
        version_no=1,
        document_id=document.id,
        checklist=[],
        recipients=[{"person_id": str(uuid4()), "address": CONTACT}],
        created_by=None,
    )
    session.add(request)
    await session.flush()
    released = await outbox.release(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        actor_user_id=actor.user_id,
    )
    assert released.job.kind == OutboxKind.CONFIRMATION_REQUEST.value
    # A reminder row pends beside the request's own row — the shape the
    # widened index admits. The retry's read must still answer with the
    # request's row, never the reminder's.
    letter = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=b"From: ai-filings@tcsp.example\r\nTo: d@x\r\nSubject: reminder\r\n\r\nbody\r\n",
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    await outbox.enqueue_reminder(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        letter_document_id=letter.id,
        not_before=HELD_NOT_BEFORE,
    )

    retry = await outbox.release(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        actor_user_id=actor.user_id,
    )

    assert retry.job.kind == OutboxKind.CONFIRMATION_REQUEST.value


async def test_pending_jobs_honors_not_before(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-12@tcsp.example>",
    )
    letter = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=b"From: ai-filings@tcsp.example\r\nTo: d@x\r\nSubject: reminder\r\n\r\nbody\r\n",
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    await outbox.enqueue_reminder(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        letter_document_id=letter.id,
        not_before=datetime.now(UTC) + timedelta(days=1),
    )

    early = await outbox.pending_jobs(session, tenant_id=actor.tenant_id, now=datetime.now(UTC))
    assert all(job.kind != OutboxKind.REMINDER.value for job in early)
    later = await outbox.pending_jobs(
        session,
        tenant_id=actor.tenant_id,
        now=datetime.now(UTC) + timedelta(days=2),
    )
    assert any(job.id != request.id and job.kind == OutboxKind.REMINDER.value for job in later)


# --- The read surfaces ------------------------------------------------------


async def test_dashboard_pending_confirmations_widget(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-13@tcsp.example>",
    )
    # A reminder the sender already accepted: the count the widget reports.
    row = await outbox.enqueue_reminder(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        letter_document_id=request.document_id,
        not_before=sent_at + timedelta(days=CADENCE[0]),
    )
    row.status = OutboxStatus.SENT.value
    row.sent_at = sent_at + timedelta(days=3)
    row.last_error = None
    row.blocked_reason = None
    await reminder_outbound(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        sent_at=sent_at + timedelta(days=3),
        document_id=row.letter_document_id,  # type: ignore[arg-type]
        index=1,
    )

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    widget = response.json()["pending_confirmations"]
    (company_entry,) = widget["companies"]
    assert company_entry["company_id"] == company_id
    (entry,) = company_entry["requests"]
    assert entry["confirmation_request_id"] == str(request.id)
    assert entry["days_elapsed"] == 4
    assert entry["reminders_sent"] == 1
    assert entry["cadence_total"] == len(CADENCE)
    assert entry["next_reminder_at"] is not None
    assert datetime.fromisoformat(entry["next_reminder_at"]) == sent_at + timedelta(days=CADENCE[1])
    assert entry["exhausted"] is False
    assert entry["automation_blocked"] is None


async def test_dashboard_widget_names_the_gate_that_blocks_the_automation(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-14@tcsp.example>",
    )
    monkeypatch.setattr(settings, "license_tier", "free")

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    widget = response.json()["pending_confirmations"]
    (company_entry,) = widget["companies"]
    (entry,) = company_entry["requests"]
    assert entry["automation_blocked"] == GatingReason.TIER_CAPABILITY_MISSING.value
    # The widget is an ungated read: the service_gaps flag carries the same
    # reason, and the data is present either way.
    assert response.json()["service_gaps"] != []


async def test_client_actions_reply_milestone_blocks_reminders_exhausted(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    # Past the final cadence point (10 days), no reply: the chain is spent.
    sent_at = datetime.combine(date.today() - timedelta(days=11), datetime.min.time(), tzinfo=UTC)
    await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-15@tcsp.example>",
    )

    response = client.get("/api/v1/client-actions")

    assert response.status_code == 200, response.text
    (period,) = response.json()["periods"]
    assert period["anniversary_year"] == ANNIVERSARY.year
    reply = next(
        milestone for milestone in period["milestones"] if milestone["kind"] == "client_reply"
    )
    assert reply["state"] == "blocked"
    assert reply["blocked_reason"] == "reminders_exhausted"


async def test_client_actions_reply_milestone_reads_by_its_date_before_exhaustion(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    # Sent four days ago: a reminder is due but the chain is nowhere near spent.
    sent_at = datetime.combine(date.today() - timedelta(days=4), datetime.min.time(), tzinfo=UTC)
    await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-16@tcsp.example>",
    )

    response = client.get("/api/v1/client-actions")

    assert response.status_code == 200, response.text
    (period,) = response.json()["periods"]
    reply = next(
        milestone for milestone in period["milestones"] if milestone["kind"] == "client_reply"
    )
    assert reply["state"] in {"pending", "due", "overdue"}
    assert reply["blocked_reason"] is None


async def test_client_actions_reply_milestone_done_once_a_reply_joins_the_thread(
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
) -> None:
    """The M5b done signal, closing the M3b panel gap: a reply ``Communication``
    on the request's thread (the §5.1.12 join) completes the reply milestone.

    Done outranks everything the date or the chain can say: this request's
    reminder chain is spent (sent eleven days ago, past the final cadence
    point of the shipped (3, 7, 10)), so before the reply the milestone reads
    ``blocked`` with ``reminders_exhausted`` — and one inbound row carrying
    the request's id turns it ``done``, with no block reason at all. The
    reply's arrival is the fact; what the reply then needs (a parse, staff
    review) is not the panel's business.
    """
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.combine(date.today() - timedelta(days=11), datetime.min.time(), tzinfo=UTC)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-17@tcsp.example>",
    )

    def reply_state() -> dict[str, object]:
        response = client.get("/api/v1/client-actions")
        assert response.status_code == 200, response.text
        (period,) = response.json()["periods"]
        milestone = next(
            item for item in period["milestones"] if item["kind"] == "client_reply"
        )
        return milestone

    assert reply_state()["state"] == "blocked"
    assert reply_state()["blocked_reason"] == "reminders_exhausted"

    reply_document = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.CLIENT_REPLY,
        origin=documents.DocumentOrigin.EMAIL_ATTACHMENT,
        content_type=MESSAGE_CONTENT_TYPE,
        data=b"From: director@brightlantern.example.hk\r\n"
        b"Subject: Re: Confirmation request\r\n\r\nAll confirmed, no changes.\r\n",
        scanner=scanning.scanner(),
    )
    await communications.record_inbound_message(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id="<reply-17@client.example>",
        thread_id=None,
        sender_address=CONTACT,
        received_at=datetime.now(UTC),
        raw_document_id=reply_document.id,
        confirmation_request_id=request.id,
    )

    milestone = reply_state()
    assert milestone["state"] == "done"
    assert milestone["blocked_reason"] is None


# --- The delivery of a reminder row (R5) -------------------------------------


async def test_a_reminder_row_delivers_its_own_letter_and_never_the_release_gate(
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1.12's "two gates, kept apart", at delivery: a reminder row goes out
    on the letter it names, without consulting the release gate — and this
    period would refuse a request row, which is exactly why the reminder must
    not ask."""
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-20@tcsp.example>",
    )
    report = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )
    assert report.outcomes[0].outcome is reminders.SweepOutcome.ENQUEUED
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    letter = await session.get(documents.Document, row.letter_document_id)
    assert letter is not None

    # The gate the request's own letter meets refuses the period: no
    # continuation is recorded for it. The reminder row never consults it.
    request_letter = await session.get(documents.Document, request.document_id)
    assert request_letter is not None
    refusal = await service_renewals.release_refusal(
        session, tenant_id=actor.tenant_id, document=request_letter, as_of=date.today()
    )
    assert refusal is not None

    with FakeSmtpServer(password="app-password") as server:
        monkeypatch.setattr(smtp_client, "tls_context", trusting_context)
        monkeypatch.setattr(settings, "smtp_host", "localhost")
        monkeypatch.setattr(settings, "smtp_port", server.port)
        monkeypatch.setattr(settings, "mail_app_password", server.password)
        outcome = await mail_send.deliver_one(session, store, job=row)
        await session.commit()

    assert outcome.disposition is mail_send.SendDisposition.SENT
    assert outcome.message_id is not None
    assert row.status == OutboxStatus.SENT.value
    # The chain's request is untouched by its reminder's delivery.
    assert request.status == RequestStatus.SENT.value
    # The outbound exchange is a reminder of the row's own letter, not a
    # second confirmation request.
    exchanges = list(
        (
            await session.scalars(
                select(Communication).where(
                    Communication.confirmation_request_id == request.id,
                    Communication.direction == "outbound",
                )
            )
        ).all()
    )
    (reminder_exchange,) = [
        item for item in exchanges if item.kind == CommunicationKind.REMINDER.value
    ]
    assert reminder_exchange.raw_document_id == row.letter_document_id
    assert reminder_exchange.raw_document_id != request.document_id
    assert len([item for item in exchanges if item.kind == "confirmation_request"]) == 1
    # The wire carried the stored reminder letter, not the request's.
    (sent_message,) = server.messages
    sent = message_from_bytes(sent_message.data, policy=default_policy)
    assert "Reminder: confirmation request" in str(sent["Subject"])
    assert sent.get("In-Reply-To") == "<req-20@tcsp.example>"
    assert "We wrote to you on" in _message_text(sent_message.data)
    # One send entry naming the row's own document — and no refusal entry,
    # because the release gate was never consulted.
    (entry,) = await audit_entries(session, AuditAction.MAIL_MESSAGE_SENT)
    assert entry.details["confirmation_request_id"] == str(request.id)
    assert entry.details["outbox_id"] == str(row.id)
    assert entry.details["document_id"] == str(row.letter_document_id)
    assert await audit_entries(session, AuditAction.DOCUMENT_RELEASE_REFUSED) == []


async def test_a_superseded_request_cancels_its_reminder_row_at_delivery(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    """The defence behind ``supersede`` at the sender: whatever the row says,
    a request that is no longer ``sent`` ends the chain, and the row closes
    with the ``request_superseded`` vocabulary."""
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-21@tcsp.example>",
    )
    letter = await documents.register_upload(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        actor_user_id=None,
        kind=documents.DocumentKind.GENERATED_DRAFT,
        origin=documents.DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=b"From: ai-filings@tcsp.example\r\nTo: d@x\r\nSubject: reminder\r\n\r\nbody\r\n",
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    row = await outbox.enqueue_reminder(
        session,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        request_id=request.id,
        letter_document_id=letter.id,
        not_before=datetime.now(UTC) - timedelta(hours=1),
    )
    # A regeneration won the race after the sweep enqueued: the request left
    # the ``sent`` state without this row being cancelled in the same commit.
    request.status = RequestStatus.SUPERSEDED.value
    await session.flush()

    outcome = await mail_send.deliver_one(session, store, job=row)

    assert outcome.disposition is mail_send.SendDisposition.CANCELLED
    assert outcome.reason == mail_send.REASON_REQUEST_SUPERSEDED
    assert row.status == OutboxStatus.CANCELLED.value
    assert row.last_error == mail_send.REASON_REQUEST_SUPERSEDED


# --- The administrator trigger (R5) ------------------------------------------


REMINDERS_URL = "/api/v1/mail/reminders"


async def test_the_reminders_route_reports_an_automatic_enqueue(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-22@tcsp.example>",
    )

    response = client.post(REMINDERS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 1
    assert (body["enqueued"], body["held"], body["denied"], body["exhausted"]) == (1, 0, 0, 0)
    assert body["skipped"] == 0
    (outcome,) = body["outcomes"]
    assert outcome["confirmation_request_id"] == str(request.id)
    assert outcome["company_id"] == company_id
    assert outcome["outcome"] == "enqueued"
    assert outcome["cadence_index"] == 0
    assert outcome["held"] is False
    # The row the report describes is real and released with the queue.
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    assert row.status == OutboxStatus.PENDING.value
    assert row.not_before == sent_at + timedelta(days=CADENCE[0])


async def test_the_reminders_route_reports_held_rows_under_review_required(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    current = client.get("/api/v1/tenant-config").json()
    put = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
            "send_modes": {"client": "review_required", "government": "review_required"},
        },
    )
    assert put.status_code == 200, put.text
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-23@tcsp.example>",
    )

    response = client.post(REMINDERS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["enqueued"], body["held"]) == (1, 1)
    (outcome,) = body["outcomes"]
    assert outcome["held"] is True
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    assert row.not_before == HELD_NOT_BEFORE


async def test_the_reminders_route_names_the_gate_that_denies(
    session: AsyncSession, store: FakeStore, actor: ActorRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep denial is a report entry carrying the gate's own reason — named,
    not raised — and nothing is written."""
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-24@tcsp.example>",
    )
    monkeypatch.setattr(settings, "license_tier", "free")

    response = client.post(REMINDERS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["denied"], body["enqueued"]) == (1, 0)
    (outcome,) = body["outcomes"]
    assert outcome["outcome"] == "denied"
    assert outcome["reason"] == GatingReason.TIER_CAPABILITY_MISSING.value
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []


async def test_the_reminders_route_answers_tenant_config_invalid(
    session: AsyncSession, store: FakeStore, actor: ActorRef
) -> None:
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-25@tcsp.example>",
    )
    session.add(
        TenantConfig(
            tenant_id=actor.tenant_id,
            naming_templates={
                "document": "{CompanyCode}-{Form}",
                "generated": "{CompanyCode}-{Form}-{Version}",
            },
            milestones={"reminder_cadence_days": "not-a-list"},
            send_modes={"client": "automatic", "government": "review_required"},
        )
    )
    await session.flush()

    response = client.post(REMINDERS_URL)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("tenant_config_invalid: ")
    assert "reminder_cadence_days" in detail
    # Nothing was written: the cycle refused before it started.
    assert await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER) == []


# --- Concurrency: two presses, two cycles -----------------------------------

# The review database's cleanliness comes from the rollback harness above, but
# a concurrency test needs one session to COMMIT — the other side can only
# pass a lock and prove the serialization once the winner's transaction is
# durable. That session's rows are removed here, after the test's own session
# has closed (declared before ``session`` in each test's signature so this
# teardown runs last).
ALL_TABLES = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)


@pytest.fixture
async def purge_committed_rows() -> AsyncIterator[None]:
    yield
    async with engine.connect() as connection:
        # Fail loudly after a few seconds rather than hang forever: a leaked
        # lock here means a racing session was not closed, and that is the
        # bug the test should report, not mask.
        await connection.execute(text("SET lock_timeout = '5s'"))
        await connection.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        await connection.commit()


async def test_two_interleaved_presses_release_a_held_reminder_once(
    purge_committed_rows: None,
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sent-press path is race-safe: press 2 waits on press 1's lock.

    A double-click across a slow delivery must not release the held row twice
    — a duplicated client letter is the trust incident ADR-0011 names
    (§5.1.11's own vocabulary). Press 1 holds the row lock in its open
    transaction while press 2 reads; without the lock press 2 reads the row
    still held and releases it too. With it, press 2 blocks, re-reads after
    press 1 commits, finds the row released, and answers ``409 already_sent``
    through the route's existing "nothing held" leg.
    """
    client = make_client(session, store, actor)
    current = client.get("/api/v1/tenant-config").json()
    put = client.put(
        "/api/v1/tenant-config",
        json={
            "naming_templates": current["naming_templates"],
            "milestones": current["milestones"],
            "send_modes": {"client": "review_required", "government": "review_required"},
        },
    )
    assert put.status_code == 200, put.text
    company_id = covered_company(client)
    sent_at = datetime.now(UTC) - timedelta(days=4)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-press-a@tcsp.example>",
    )
    sweep = await reminders.sweep(
        session,
        store,
        scanning.scanner(),
        tenant_id=actor.tenant_id,
        now=sent_at + timedelta(days=4),
    )
    assert sweep.outcomes[0].held is True
    press = sent_at + timedelta(days=5)
    # The racing connections can only see durable rows: commit the fixture
    # transaction itself (a session commit would release only its savepoint),
    # and let the purge fixture remove the rows after the test.
    connection = await session.connection()
    await connection.commit()

    # Gate press 2's read of the held row so the interleave is deterministic:
    # press 2 must reach its select while press 1's transaction is still open
    # (press 1 read the row held; without the lock press 2 reads it held too).
    row_touched = asyncio.Event()
    row_read = asyncio.Event()
    original_followups = outbox._pending_followups

    async def gated_pending_followups(
        session: AsyncSession, **kwargs: object
    ) -> list[MailOutbox]:
        row_touched.set()
        rows = await original_followups(session, **kwargs)
        row_read.set()
        return rows

    monkeypatch.setattr(outbox, "_pending_followups", gated_pending_followups)

    async def press_b() -> tuple[MailOutbox, ...]:
        async with engine.connect() as conn_b:
            await conn_b.begin()
            async with AsyncSession(bind=conn_b, expire_on_commit=False) as session_b:
                return await outbox.release_reminders(
                    session_b,
                    tenant_id=actor.tenant_id,
                    company_id=UUID(company_id),
                    request_id=request.id,
                    actor_user_id=actor.user_id,
                    now=press,
                )

    # Press 1 on its own connection: it takes the row lock and holds its
    # transaction open across press 2, the way a slow delivery does.
    conn_a = await engine.connect()
    try:
        await conn_a.begin()
        async with AsyncSession(bind=conn_a, expire_on_commit=False) as session_a:
            released_a = await outbox.release_reminders(
                session_a,
                tenant_id=actor.tenant_id,
                company_id=UUID(company_id),
                request_id=request.id,
                actor_user_id=actor.user_id,
                now=press,
            )
        assert len(released_a) == 1
        row_touched.clear()
        row_read.clear()

        task_b = asyncio.create_task(press_b())
        await asyncio.wait_for(row_touched.wait(), timeout=2.0)
        # Without the lock press 2's read finishes while press 1 is still
        # open — the double release. With it, press 2 is blocked inside the
        # lock and the read only happens after the commit, so the wait times
        # out (the fixed behavior).
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(row_read.wait(), timeout=2.0)
        await conn_a.commit()
        released_b = await task_b
    finally:
        # Closing rolls back the open transaction and releases the lock even
        # when an assertion above fails — a left-open lock would deadlock the
        # cleanup truncate.
        await conn_a.close()

    assert released_b == ()
    # One move, visible and durable: the row reads the press instant.
    (row,) = await outbox_rows(session, request_id=request.id, kind=OutboxKind.REMINDER)
    assert row.not_before == press


async def test_overlapping_cycles_serialize_one_enqueue_and_one_exhaustion(
    purge_committed_rows: None,
    session: AsyncSession,
    store: FakeStore,
    actor: ActorRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping cycles decide as one: a single enqueue, a single entry.

    Without serialization both cycles read the same facts (no pending row, no
    exhaustion recorded), both store a letter, and the loser's outbox insert
    loses the widened index to the winner — after which it still writes a
    ``reminder_enqueued`` entry pairing the WINNER's outbox row with its OWN
    orphan document, and a second ``reminders_exhausted``. With the per-tenant
    advisory lock the second cycle blocks before any read and re-derives the
    facts from the winner's committed transaction.
    """
    client = make_client(session, store, actor)
    company_id = covered_company(client)
    # Past the final cadence point: the owed slot dispatches AND exhaustion
    # records in the same cycle — the two facts a doubled cycle doubled.
    sent_at = datetime.now(UTC) - timedelta(days=11)
    request = await make_sent_request(
        session,
        store,
        tenant_id=actor.tenant_id,
        company_id=UUID(company_id),
        sent_at=sent_at,
        message_id="<req-cycle-a@tcsp.example>",
    )
    now = sent_at + timedelta(days=11)
    # The racing connections can only see durable rows: commit the fixture
    # transaction itself (a session commit would release only its savepoint),
    # and let the purge fixture remove the rows after the test.
    connection = await session.connection()
    await connection.commit()

    # Gate cycle B between its fact-reads and its enqueue so the interleave is
    # deterministic: B must read before A's commit (the stale facts), then
    # enqueue after it (the document-key and outbox-index losses). The gate is
    # the per-candidate automation check — the last await before any write.
    # Under the lock B never reaches it — it is blocked before any read — so
    # the first wait timing out IS the fixed behavior.
    b_past_reads = asyncio.Event()
    a_durable = asyncio.Event()
    original_check = reminders.service_packages.check_automation

    async def gated_check(*args: object, **kwargs: object) -> object:
        b_past_reads.set()
        await a_durable.wait()
        return await original_check(*args, **kwargs)

    # Cycle A on its own connection, committed only after cycle B has read —
    # the interleave the finding describes.
    conn_a = await engine.connect()
    try:
        await conn_a.begin()
        async with AsyncSession(bind=conn_a, expire_on_commit=False) as session_a:
            report_a = await reminders.sweep(
                session_a, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
            )
        assert report_a.outcomes[0].outcome is reminders.SweepOutcome.ENQUEUED
        monkeypatch.setattr(reminders.service_packages, "check_automation", gated_check)

        async def cycle_b() -> tuple[reminders.SweepReport, AsyncSession, AsyncConnection]:
            conn_b = await engine.connect()
            await conn_b.begin()
            session_b = AsyncSession(bind=conn_b, expire_on_commit=False)
            try:
                report = await reminders.sweep(
                    session_b, store, scanning.scanner(), tenant_id=actor.tenant_id, now=now
                )
            except BaseException:
                # A failed cycle must not leak its connection — an open
                # transaction here would deadlock the cleanup truncate.
                await session_b.close()
                await conn_b.close()
                raise
            return report, session_b, conn_b

        task_b = asyncio.create_task(cycle_b())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(b_past_reads.wait(), timeout=2.0)
        await conn_a.commit()
        a_durable.set()
        report_b, session_b, conn_b = await task_b
    finally:
        # Closing rolls back the open transaction and releases the advisory
        # lock even when an assertion above fails.
        await conn_a.close()

    try:
        (outcome_b,) = report_b.outcomes
        assert outcome_b.outcome is reminders.SweepOutcome.SKIPPED
        assert outcome_b.skip is reminders.SweepSkip.PENDING
        # Exactly one enqueue, and its audit entry names a real pair: the stored
        # document the row will deliver, and the row itself. Read through cycle B's
        # own session: an unserialized loser commits nothing but still writes its
        # mismatched entry and its second exhaustion inside its transaction, and
        # those are the facts that must not exist.
        (row,) = await outbox_rows(session_b, request_id=request.id, kind=OutboxKind.REMINDER)
        enqueued_entries = await audit_entries(session_b, AuditAction.REMINDER_ENQUEUED)
        assert len(enqueued_entries) == 1
        (enqueued,) = enqueued_entries
        assert enqueued.details["outbox_id"] == str(row.id)
        assert enqueued.details["document_id"] == str(row.letter_document_id)
        exhausted_entries = await audit_entries(session_b, AuditAction.REMINDERS_EXHAUSTED)
        assert len(exhausted_entries) == 1
        assert exhausted_entries[0].entity_id == request.id
    finally:
        # Closing rolls back whatever an unserialized loser wrote — session
        # first, then the connection itself: the session was bound to an
        # externally-begun transaction, which only the connection's close
        # releases (and its read locks with it).
        await session_b.close()
        await conn_b.close()
