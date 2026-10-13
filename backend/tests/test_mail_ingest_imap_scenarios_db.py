"""DB-backed inbound ingestion over the IMAP fallback (opt-in; needs a migrated
PostgreSQL).

ADR-0005's fallback end to end, against a real database, a faithful fake IMAP
server (``tests/fake_imap.py``, no socket) and an in-memory scanner: a
corporate-Exchange deployment — **no Google client, no connected credential,
only ``AICS_IMAP_*`` and the app password** — runs the same cycle the provider
leg runs and gets the same records out of it. What is asserted here is what
differs by leg and what must not: the ``Message-ID`` identity a re-fetch
recognizes, the UID fallback for a message that carries none, the mailbox's
own ``INTERNALDATE`` as the cursor, the body-read discipline (an unregistered
sender's body is never fetched), the custody claim for the app password
(searched for across every table, the audit chain and the captured logs, with
a control row proving the search finds things), and the §2.5 retry reading one
message back over the same leg.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import logging
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail import imap_client
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.base import Base
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_ingestion import MailIngestionState
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import AttributionCase, Tenant, User
from app.adapters.persistence.quarantine import QuarantinedMessage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.core.config import settings
from app.domain.attribution import AttributionAskKind, AttributionCaseStatus
from app.domain.audit import AuditAction
from app.domain.communications import IngestOutcome
from app.domain.documents import DocumentKind, DocumentOrigin, ScanStatus
from app.domain.quarantine import QuarantineReason, QuarantineStatus
from app.main import create_app
from tests.fake_imap import FakeImap

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

INGEST_URL = "/api/v1/mail/ingest"
QUARANTINE_URL = "/api/v1/mail/quarantine"
MAILBOX = "ai-filings@tcsp.example"
# Planted in the deployment's configuration, never in the database: a
# credential under ADR-0014 custody, and the marker the custody search looks
# for. The fake refuses any other password, so a test cannot pass by accident.
APP_PASSWORD = "imap-app-password-marker-never-stored"
CONTACT = "director@client.example"
COVERAGE = {"start": "2026-01-01", "end": "2026-12-31"}
COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}
# The name the ask letters identify the company by (scope §7: name and number
# in both language halves), recorded through the effective-dated fact path.
COMPANY_NAME = "Synthetic Harbour Limited"
# The outbound exchange the two unregistered messages below reply to: the
# chain evidence both legs read (on this leg it is the *only* evidence — the
# provider thread id is always ``None``).
REQUEST_MESSAGE_ID = "<annual-return-request@tcsp.example>"
# Planted in the mail itself: every test that asserts "no body travelled"
# searches for these, and the stored raw document proves they really are there.
BODY_MARKER = "ANSWER-MARKER-9931"
ATTACHMENT_MARKER = b"%PDF-1.7 ATTACHMENT-MARKER-4471"
FIXED_MESSAGE_ID = "<20260920.0931.7@ai-filings.tcsp.example>"
# A header that claims the deployment's own fallback namespace, and one longer
# than the narrowest column that carries an identity (128 characters): both
# are staged as raw headers, because the point is what the *wire* carries.
CRAFTED_MESSAGE_ID = "imap-uid:9999:7"
OVER_LONG_MESSAGE_ID = "<" + "a" * 400 + "@client.example>"
# The fake's clock, and the receipt time the cursor must carry.
RECEIVED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


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


def configure_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeImap) -> None:
    """The whole of this deployment's mail configuration — path A, read half.

    No Google client id, no client secret, no connected credential: the
    fallback is configured entirely by the environment, and the password lands
    where ADR-0014 puts it.
    """
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)


async def mail_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeImap,
    *,
    tier: str = "standard",
    covered: bool = True,
    register_contact: bool = True,
) -> tuple[TestClient, UUID, str]:
    """A tenant, an IMAP-configured deployment, and a company ready to ingest."""
    tenant, admin = await seed_tenant_with_admin(session)
    provision(monkeypatch, tier)
    configure_imap(monkeypatch, fake)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = make_company(client)
    if register_contact:
        add_registered_contact(client, company_id, email=CONTACT)
    if covered:
        cover(client, company_id)
    return client, tenant.id, company_id


def make_company(client: TestClient, **overrides: object) -> str:
    response = client.post("/api/v1/companies", json={**COMPANY, **overrides})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def add_registered_contact(client: TestClient, company_id: str, *, email: str) -> str:
    """A person with an email channel, associated with the company."""
    person = client.post("/api/v1/persons", json={"name_en": "CHAN TAI MAN"})
    assert person.status_code == 201, person.text
    person_id = str(person.json()["id"])
    channel = client.post(
        f"/api/v1/persons/{person_id}/contact-channels",
        json={"channel_type": "email", "identifier": email},
    )
    assert channel.status_code == 201, channel.text
    response = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    assert response.status_code in (200, 201), response.text
    return person_id


def cover(client: TestClient, company_id: str, *, services: Sequence[str] | None = None) -> None:
    """Assign a package covering ``services`` through the real endpoints."""
    package = client.post(
        "/api/v1/service-packages",
        json={
            "name": f"pkg-{uuid4()}",
            "included_services": list(services) if services is not None else ["email_ingestion"],
            "covered_forms": ["NAR1", "ND2A"],
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


def name_company(
    client: TestClient, company_id: str, *, name_en: str = COMPANY_NAME
) -> None:
    """Record a registered name through the effective-dated fact path.

    The ask's composition identifies the company by name and number (scope
    §7), and refuses when no name is in force.
    """
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2026-01-01", "name": {"name_en": name_en, "name_zh": None}},
    )
    assert response.status_code == 200, response.text


def reply(
    fake: FakeImap,
    *,
    sender: str = f"CHAN TAI MAN <{CONTACT}>",
    subject: str = "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
    body: str = BODY_MARKER,
    message_id: str | None = FIXED_MESSAGE_ID,
    received_at: datetime | None = RECEIVED_AT,
    raw: bytes | None = None,
) -> int:
    """One client reply in the mailbox, as a UID."""
    return fake.add_message(
        subject=subject,
        sender=sender,
        body=body,
        message_id=message_id,
        received_at=received_at or RECEIVED_AT,
        raw=raw,
    )


def attachment_message(fake: FakeImap, **kwargs: object) -> int:
    """One reply with a PDF attachment — the stored-document path."""
    message = EmailMessage()
    message["Message-ID"] = kwargs.pop("message_id", FIXED_MESSAGE_ID)
    message["From"] = kwargs.pop("sender", f"CHAN TAI MAN <{CONTACT}>")
    message["To"] = MAILBOX
    message["Date"] = "Tue, 23 Sep 2026 09:31:00 +0800"
    message["Subject"] = kwargs.pop(
        "subject", "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"
    )
    message.set_content(kwargs.pop("body", BODY_MARKER))
    message.add_attachment(
        ATTACHMENT_MARKER, maintype="application", subtype="pdf", filename="signed.pdf"
    )
    return fake.add_message(
        raw=message.as_bytes(policy=policy.SMTP),
        received_at=kwargs.pop("received_at", RECEIVED_AT),
    )


def chain_message(
    fake: FakeImap,
    *,
    message_id: str,
    in_reply_to: str,
    references: str,
    sender: str = "Stranger <stranger@elsewhere.example>",
    received_at: datetime,
) -> int:
    """One unregistered sender's message on a References chain, staged raw.

    The fake's own builder carries no thread headers (and this leg has no
    provider thread id at all), so the chain evidence is written into the
    bytes here — exactly what a corporate mailbox would hand over.
    """
    message = EmailMessage(policy=policy.SMTP)
    message["Message-ID"] = message_id
    message["From"] = sender
    message["To"] = MAILBOX
    message["Date"] = "Tue, 23 Sep 2026 09:31:00 +0800"
    message["Subject"] = (
        "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"
    )
    message["In-Reply-To"] = in_reply_to
    message["References"] = references
    message.set_content(BODY_MARKER)
    return fake.add_message(raw=message.as_bytes(), received_at=received_at)


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def rows_containing(session: AsyncSession, needle: str) -> list[str]:
    """Every table holding a row whose text form contains ``needle``.

    ``table::text`` renders the whole row, so this searches all columns of all
    tables — the claim is "no table in this schema", not "the tables this
    slice happens to know about" (``test_mail_scenarios_db.py``'s own search).
    """
    found: list[str] = []
    for table in Base.metadata.sorted_tables:
        query = text(f'SELECT count(*) FROM "{table.name}" WHERE "{table.name}"::text LIKE :needle')
        count = await session.scalar(query, {"needle": f"%{needle}%"})
        if count:
            found.append(f"{table.name} ({count})")
    return found


async def stored_documents(session: AsyncSession) -> list[Document]:
    return list((await session.scalars(select(Document))).all())


async def stored_communications(session: AsyncSession) -> list[Communication]:
    return list((await session.scalars(select(Communication))).all())


async def stored_quarantines(session: AsyncSession) -> list[QuarantinedMessage]:
    return list((await session.scalars(select(QuarantinedMessage))).all())


async def stored_cases(session: AsyncSession) -> list[AttributionCase]:
    return list(
        (
            await session.scalars(
                select(AttributionCase).order_by(AttributionCase.created_at, AttributionCase.id)
            )
        ).all()
    )


async def stored_outbox(session: AsyncSession) -> list[MailOutbox]:
    """Every outbox row — the queue the ask rides."""
    return list(
        (
            await session.scalars(
                select(MailOutbox).order_by(MailOutbox.created_at, MailOutbox.id)
            )
        ).all()
    )


async def seed_outbound(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    mailbox_message_id: str,
) -> Communication:
    """One stored outbound exchange — the chain evidence the messages below
    reply to (the sending slice's own row shape)."""
    document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind=DocumentKind.GENERATED_DRAFT.value,
        origin=DocumentOrigin.GENERATED.value,
        retention_class="client_communication",
        sha256="c" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=64,
        content_type="message/rfc822",
        scan_status=ScanStatus.CLEAN.value,
    )
    session.add(document)
    await session.flush()
    row = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction="outbound",
        channel="email",
        kind="confirmation_request",
        mailbox_message_id=mailbox_message_id,
        thread_id=None,
        sender_address=MAILBOX,
        recipient_addresses=CONTACT,
        confirmation_request_id=None,
        received_at=datetime.now(UTC),
        raw_document_id=document.id,
    )
    session.add(row)
    await session.flush()
    return row


def full_fetches(fake: FakeImap) -> list[int]:
    """The messages whose *whole* body this deployment fetched."""
    return [uid for uid, kind in fake.fetches if kind == "full"]


# --- The whole cycle -----------------------------------------------------


async def test_a_message_read_over_imap_becomes_documents_and_a_communication_row(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = attachment_message(fake)
    client, tenant_id, company_id = await mail_deployment(session, store, monkeypatch, fake)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 1
    assert body["ingested"] == 1
    assert body["skipped_known"] == 0
    assert body["refused"] == 0
    # The window is the mailbox's own: a first read asks for everything and the
    # bound decides how far back it reaches.
    assert body["query"] == "ALL"
    outcome = body["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    # The identity is the message's own Message-ID — the same value the SMTP
    # send leg stamps — not an IMAP UID.
    assert outcome["mailbox_message_id"] == FIXED_MESSAGE_ID
    assert outcome["company_id"] == company_id
    assert outcome["scan_status"] == ScanStatus.CLEAN.value
    assert [item["disposition"] for item in outcome["attachments"]] == ["stored"]
    # The response describes mail; it never carries any of it.
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text

    # The stored raw message is byte-identical to what the mailbox holds.
    rows = await stored_documents(session)
    assert len(rows) == 2
    raw_row = next(row for row in rows if row.kind == DocumentKind.CLIENT_REPLY.value)
    attachment_row = next(row for row in rows if row.kind == DocumentKind.CLIENT_DOCUMENT.value)
    assert raw_row.origin == DocumentOrigin.EMAIL_ATTACHMENT.value
    assert raw_row.content_type == "message/rfc822"
    assert raw_row.retention_class == "client_communication"
    assert store.objects[raw_row.storage_key] == fake.raw_of(uid)
    assert store.objects[attachment_row.storage_key] == ATTACHMENT_MARKER

    # The Communication row: the message's own id, and — IMAP has no thread of
    # its own — no thread, with the mailbox's INTERNALDATE as the receipt time.
    record = (await stored_communications(session))[0]
    assert record.direction == "inbound"
    assert record.channel == "email"
    assert record.mailbox_message_id == FIXED_MESSAGE_ID
    assert record.thread_id is None
    assert record.sender_address == CONTACT
    assert record.received_at == RECEIVED_AT
    assert record.raw_document_id == raw_row.id

    # The cursor is that same INTERNALDATE, and the audit entry carries ids and
    # outcomes — never a body.
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == RECEIVED_AT
    assert datetime.fromisoformat(body["cursor_received_at"]) == RECEIVED_AT
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_INGESTED.value
    assert entries[-1].details["mailbox_message_id"] == FIXED_MESSAGE_ID
    assert entries[-1].details["thread_id"] is None
    assert entries[-1].details["raw_scan_status"] == "clean"
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text


async def test_the_same_message_is_never_ingested_twice_over_imap(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    reply(fake)
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)
    assert client.post(INGEST_URL).json()["ingested"] == 1
    fetches = list(fake.fetches)

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    body = second.json()
    # The overlap window brings the message back — the day-granular search
    # reads its envelope again — and the Message-ID says it is already here,
    # so nothing is fetched, scanned or written for it a second time.
    assert body["examined"] == 1
    assert body["skipped_known"] == 1
    assert body["ingested"] == 0
    assert body["outcomes"][0]["mailbox_message_id"] == FIXED_MESSAGE_ID
    assert body["outcomes"][0]["outcome"] == IngestOutcome.SKIPPED_KNOWN.value
    assert body["query"] is not None and body["query"].startswith("SINCE ")
    # The window itself re-read the envelope — that is what identifying the
    # message costs on this leg — and the message id then stopped the cycle
    # there: no body was fetched, nothing was scanned, and nothing was written.
    assert [kind for _, kind in fake.fetches[len(fetches) :]] == ["headers"]
    assert full_fetches(fake) == [1]
    assert len(await stored_documents(session)) == 1
    assert len(await stored_communications(session)) == 1
    entries = await audit_entries(session, tenant_id)
    assert [entry.action for entry in entries].count(
        AuditAction.MAIL_MESSAGE_INGESTED.value
    ) == 1


async def test_a_message_without_a_message_id_is_recorded_under_its_uid_fallback(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = reply(fake, raw=b"From: CHAN TAI MAN <director@client.example>\r\n\r\nno id here\r\n")
    client, _, _ = await mail_deployment(session, store, monkeypatch, fake)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    first = client.post(INGEST_URL).json()

    assert first["ingested"] == 1
    assert first["outcomes"][0]["mailbox_message_id"] == identity
    record = (await stored_communications(session))[0]
    assert record.mailbox_message_id == identity
    assert record.sender_address == CONTACT

    # Deterministic, so a re-fetch recognizes it: the fallback id is the
    # idempotency key exactly as a Message-ID is.
    second = client.post(INGEST_URL).json()
    assert second["skipped_known"] == 1
    assert full_fetches(fake) == [uid]  # read once, for the one ingestion


async def test_a_message_id_claiming_the_fallback_namespace_is_recorded_under_its_uid(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header cannot make a message look like this deployment's own UID id.

    A crafted ``Message-ID: imap-uid:<n>:<m>`` is the one header value the
    client would read back as a UID reference — this message's, a different
    one's, or (another incarnation's UIDVALIDITY) nothing at all, which would
    stall the cycle on the same message for ever. The rule refuses the
    namespace instead: the message is recorded under its own UID, and the
    cycle keeps its cursor moving.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = reply(
        fake,
        raw=(
            f"Message-ID: {CRAFTED_MESSAGE_ID}\r\n"
            f"From: CHAN TAI MAN <{CONTACT}>\r\n"
            "\r\n"
        ).encode(),
    )
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    first = client.post(INGEST_URL).json()

    assert first["ingested"] == 1
    assert first["outcomes"][0]["mailbox_message_id"] == identity
    record = (await stored_communications(session))[0]
    assert record.mailbox_message_id == identity
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == fake.messages[uid].received_at

    # And the window recognizes it next time: the id is the message's own, so
    # the replay is the ordinary id check rather than a not-found refusal.
    second = client.post(INGEST_URL).json()
    assert second["skipped_known"] == 1
    assert await stored_communications(session) == [record]


async def test_an_over_long_message_id_is_ingested_under_its_uid(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id no column could hold is not an identity.

    ``communications.mailbox_message_id`` allows 320 characters and
    ``quarantined_messages`` 128; a longer header would be refused by one of
    them and abort the cycle on the same message for ever. The message is
    recorded under its own UID instead, which is bounded whatever a sender
    writes.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = reply(
        fake,
        raw=(
            f"Message-ID: {OVER_LONG_MESSAGE_ID}\r\n"
            f"From: CHAN TAI MAN <{CONTACT}>\r\n"
            "\r\n"
        ).encode(),
    )
    client, _, _ = await mail_deployment(session, store, monkeypatch, fake)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingested"] == 1
    assert body["outcomes"][0]["mailbox_message_id"] == identity
    record = (await stored_communications(session))[0]
    assert record.mailbox_message_id == identity
    assert OVER_LONG_MESSAGE_ID not in response.text


async def test_an_over_long_message_id_quarantines_under_its_uid_and_the_cycle_advances(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quarantine column's width is what the identity bound exists for.

    An unattributable message is written to ``quarantined_messages``, whose
    ``mailbox_message_id`` is 128 characters: a longer header would raise a
    DataError inside the cycle, the transaction would roll back and the same
    message would be re-listed every cycle — a mailbox that never ingests
    again. Under the UID identity the row is written, the cycle returns, and
    the cursor advances.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = reply(
        fake,
        sender="stranger@elsewhere.example",
        raw=(
            f"Message-ID: {OVER_LONG_MESSAGE_ID}\r\n"
            "From: stranger@elsewhere.example\r\n"
            "\r\n"
        ).encode(),
    )
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert outcome["mailbox_message_id"] == identity
    rows = await stored_quarantines(session)
    assert len(rows) == 1
    assert rows[0].mailbox_message_id == identity
    assert len(rows[0].mailbox_message_id) <= 128
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == fake.messages[uid].received_at

    # The next cycle re-examines the boundary message and finds its row — one
    # row, one entry, no stall and no duplicate.
    second = client.post(INGEST_URL).json()
    assert second["refused"] == 1
    assert second["outcomes"][0]["quarantine_id"] == outcome["quarantine_id"]
    assert len(await stored_quarantines(session)) == 1


async def test_an_unregistered_senders_body_is_never_fetched_over_imap(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workstream ②'s B-class over the IMAP leg: an unregistered sender whose
    message carries its own RFC ``Message-ID`` opens the company-less
    first-contact case instead of the §2.5 quarantine — and the body is still
    never read at all, because the ask quotes envelope facts only (ruling R2
    holds on both legs). The case is the record; there is no quarantine row,
    no document and no Communication for the message."""
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = reply(
        fake,
        sender="stranger@elsewhere.example",
        message_id="<stranger@elsewhere.example>",
    )
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["reason"] is None
    assert outcome["attribution_case_id"] is not None
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    # There is no company to decide between and no answer to read, so the
    # message's body is never read at all — the rule both legs hold.
    assert full_fetches(fake) == []
    assert uid not in [fetched for fetched, kind in fake.fetches if kind == "full"]
    assert await stored_quarantines(session) == []
    rows = await stored_cases(session)
    assert len(rows) == 1
    case = rows[0]
    assert str(case.id) == outcome["attribution_case_id"]
    assert case.company_id is None  # the IMAP leg's case is company-less too
    assert case.mailbox_message_id == "<stranger@elsewhere.example>"
    assert case.sender_address == "stranger@elsewhere.example"
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert case.ask_kind == AttributionAskKind.REGISTERED_CHANNEL.value
    assert case.original_rfc_message_id == "<stranger@elsewhere.example>"
    assert case.thread_id is None  # the IMAP leg carries no provider thread id
    # The one stored document is the ask's own letter — the deployment's
    # writing, tenant-scoped (migration 0035), never a copy of the mail; the
    # message's body itself was never fetched.
    (letter,) = await stored_documents(session)
    assert letter.company_id is None
    assert letter.storage_key.startswith("_correspondence/")
    assert await stored_communications(session) == []
    entries = await audit_entries(session, tenant_id)
    opened = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_OPENED.value
    )
    assert opened.company_id is None  # ruling D10
    assert opened.details["sender_address"] == "stranger@elsewhere.example"
    assert entries[-1].action == AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    assert entries[-1].company_id is None
    assert BODY_MARKER not in response.text


async def test_a_second_message_on_the_references_chain_is_suppressed_over_imap(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IMAP leg's single-ask: its provider thread id is always ``None``,
    so the only thing that can stop a second unregistered message's ask is the
    first case's ``Message-ID`` — which the second message carries back in
    ``In-Reply-To``/``References``. The suppression read matches it in the
    ancestor set: the second case opens and asks nothing."""
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    chain_message(
        fake,
        message_id="<m1@client.example>",
        in_reply_to=REQUEST_MESSAGE_ID,
        references=REQUEST_MESSAGE_ID,
        received_at=RECEIVED_AT,
    )
    chain_message(
        fake,
        message_id="<m2@client.example>",
        in_reply_to="<m1@client.example>",
        references=f"{REQUEST_MESSAGE_ID} <m1@client.example>",
        received_at=RECEIVED_AT + timedelta(minutes=10),
    )
    client, tenant_id, company_id = await mail_deployment(session, store, monkeypatch, fake)
    name_company(client, company_id)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=REQUEST_MESSAGE_ID,
    )

    outcomes = client.post(INGEST_URL).json()["outcomes"]

    assert [item["outcome"] for item in outcomes] == [
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
    ]
    cases = await stored_cases(session)
    assert len(cases) == 2
    asked = [case for case in cases if case.ask_sent_at is not None]
    suppressed = [case for case in cases if case.ask_sent_at is None]
    assert len(asked) == 1
    assert len(suppressed) == 1
    # No provider thread id anywhere on this leg: the chain is the ids alone.
    assert asked[0].thread_id is None
    assert asked[0].original_rfc_message_id == "<m1@client.example>"
    assert suppressed[0].thread_id is None
    assert suppressed[0].original_rfc_message_id == "<m2@client.example>"
    (row,) = await stored_outbox(session)
    assert row.kind == "attribution_ask"
    assert row.attribution_case_id == asked[0].id
    assert row.not_before is None
    assert {item.id for item in await stored_documents(session)} == {
        letter.raw_document_id,
        row.letter_document_id,
    }
    assert await stored_communications(session) == [letter]
    # Ruling R2 still holds on this leg: neither unregistered original's body
    # was fetched, so only the deployment's own letter is in the registry.
    assert full_fetches(fake) == []
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ) == 1

    # The cursor's overlap re-reads both: the first case answers with its own
    # id, the second with the first's — still one ask, one entry, one letter.
    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    assert len(await stored_cases(session)) == 2
    assert [item.id for item in await stored_outbox(session)] == [row.id]
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ) == 1


async def test_a_message_with_no_usable_anchor_is_quarantined_not_cased_over_imap(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both anchors unusable: the message is refused, not parked.

    On this leg the provider thread is always ``None``, so the message's own
    ``Message-ID`` is the only anchor a case could carry — and here the
    header is absent, leaving the ``In-Reply-To`` chain evidence to bind the
    company. A case with neither anchor could never be found again by the
    confirmation or closure reads: every overlap re-read would ask again and
    store another letter. The message therefore falls back to the §2.5
    refusal (``unregistered_sender``), which is staff-visible — one
    quarantine row, no case, no ask, and the cycle advances over it.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "Stranger <stranger@elsewhere.example>"
    message["To"] = MAILBOX
    message["Date"] = "Tue, 23 Sep 2026 09:31:00 +0800"
    message["Subject"] = "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"
    message["In-Reply-To"] = REQUEST_MESSAGE_ID
    message["References"] = REQUEST_MESSAGE_ID
    message.set_content(BODY_MARKER)
    fake.add_message(raw=message.as_bytes(), received_at=RECEIVED_AT)
    client, tenant_id, company_id = await mail_deployment(session, store, monkeypatch, fake)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=REQUEST_MESSAGE_ID,
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert outcome["quarantine_id"] is not None
    assert outcome["attribution_case_id"] is None
    rows = await stored_quarantines(session)
    assert len(rows) == 1
    assert rows[0].reason == QuarantineReason.UNREGISTERED_SENDER.value
    assert await stored_cases(session) == []
    assert await stored_outbox(session) == []
    assert await stored_communications(session) == [letter]
    # Nothing of the message was read: the refusal is decided from the envelope.
    assert full_fetches(fake) == []
    # The cycle finished over the message: the cursor is its own receipt time.
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == RECEIVED_AT


async def test_the_mailboxs_own_mail_is_not_read_back_as_a_reply_over_imap(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The self-sent rule compares the sender against the leg's own address — on
    # this leg the account the app password authenticates as.
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    reply(fake, sender=f"AI Filings <{MAILBOX}>", message_id="<sent-by-us@tcsp.example>")
    client, _, _ = await mail_deployment(session, store, monkeypatch, fake)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.SELF_SENT.value
    assert await stored_communications(session) == []
    assert await stored_documents(session) == []


async def test_the_window_continues_where_the_last_cycle_stopped(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window larger than the bound is finished on later cycles, not skipped.

    The cycle takes the window's **oldest** messages, so the cursor advances
    only over what it finished; what is left is still newer than the cursor
    when the next cycle asks, and the bound only decides how many cycles the
    window takes.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    spaced = timedelta(minutes=10)
    reply(fake, message_id="<one@client.example>", received_at=RECEIVED_AT)
    reply(fake, message_id="<two@client.example>", received_at=RECEIVED_AT + spaced)
    third = reply(fake, message_id="<three@client.example>", received_at=RECEIVED_AT + 2 * spaced)
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)

    opening = client.post(INGEST_URL).json()

    # The first read is unbounded by a cursor: one cycle with a wide enough
    # bound takes the whole mailbox, oldest first.
    assert [item["mailbox_message_id"] for item in opening["outcomes"]] == [
        "<one@client.example>",
        "<two@client.example>",
        "<three@client.example>",
    ]
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == fake.messages[third].received_at

    # Two more arrive, well outside the cursor's overlap band, and the bound is
    # two: each cycle finishes the boundary message the inclusive edge lists
    # again and one new one, so the window takes two cycles rather than losing
    # anything.
    monkeypatch.setattr(settings, "mail_ingest_max_messages", 2)
    fourth = reply(fake, message_id="<four@client.example>", received_at=RECEIVED_AT + 3 * spaced)
    reply(fake, message_id="<five@client.example>", received_at=RECEIVED_AT + 4 * spaced)

    second_cycle = client.post(INGEST_URL).json()

    assert [
        (item["mailbox_message_id"], item["outcome"]) for item in second_cycle["outcomes"]
    ] == [
        ("<three@client.example>", IngestOutcome.SKIPPED_KNOWN.value),
        ("<four@client.example>", IngestOutcome.INGESTED.value),
    ]
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == fake.messages[fourth].received_at

    third_cycle = client.post(INGEST_URL).json()

    assert [item["mailbox_message_id"] for item in third_cycle["outcomes"]] == [
        "<four@client.example>",
        "<five@client.example>",
    ]
    assert third_cycle["ingested"] == 1
    # Nothing was skipped: every message ended up recorded, in order.
    assert [record.mailbox_message_id for record in await stored_communications(session)] == [
        "<one@client.example>",
        "<two@client.example>",
        "<three@client.example>",
        "<four@client.example>",
        "<five@client.example>",
    ]


async def test_the_first_reads_bound_decides_how_far_back_the_deployment_reaches(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is the only thing that decides a first read's reach.

    Three messages are already in the mailbox when it is connected and the
    bound is two. The first cycle takes the newest, and the one it did not
    reach is below the new cursor — the window only ever opens at the cursor,
    so nothing reaches back for it. That is the same limit the provider leg
    states (contract §5.1.7): the bound bounds the reach, and the remedy for a
    mailbox with history is a larger ``AICS_MAIL_INGEST_MAX_MESSAGES``.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    reply(fake, message_id="<one@client.example>", received_at=RECEIVED_AT)
    reply(fake, message_id="<two@client.example>", received_at=RECEIVED_AT + timedelta(minutes=1))
    reply(fake, message_id="<three@client.example>", received_at=RECEIVED_AT + timedelta(minutes=2))
    client, _, _ = await mail_deployment(session, store, monkeypatch, fake)
    monkeypatch.setattr(settings, "mail_ingest_max_messages", 2)

    opening = client.post(INGEST_URL).json()

    assert [item["mailbox_message_id"] for item in opening["outcomes"]] == [
        "<two@client.example>",
        "<three@client.example>",
    ]

    later = client.post(INGEST_URL).json()

    # The window opens at the cursor, and the boundary message is listed again
    # (and skipped): the message below the cursor is not.
    assert [item["mailbox_message_id"] for item in later["outcomes"]] == [
        "<three@client.example>"
    ]
    assert later["outcomes"][0]["outcome"] == IngestOutcome.SKIPPED_KNOWN.value
    assert [record.mailbox_message_id for record in await stored_communications(session)] == [
        "<two@client.example>",
        "<three@client.example>",
    ]


async def test_a_quarantined_message_retries_over_the_imap_leg(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §2.5 staff remedy reaches the fallback's messages too.

    The quarantined message is staged **anchor-less** (no ``Message-ID``
    header): workstream ②'s first-contact case needs the original's own RFC
    id to exist, so this one lands in the quarantine the way it always did —
    recorded under the UID fallback identity, because an absent header is not
    an identity (``app.domain.mail.message_identity``). The retry acquires
    its own reader through the same rule the cycle uses, so a message
    quarantined on this leg is re-examined on this leg — the cursor only
    moves forward, and nothing else would ever look at it again.
    """
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    uid = attachment_message(fake, sender="stranger@elsewhere.example", message_id=None)
    client, tenant_id, company_id = await mail_deployment(session, store, monkeypatch, fake)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    assert refused["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert refused["attribution_case_id"] is None  # no anchor, no case
    quarantine_id = str(refused["quarantine_id"])

    add_registered_contact(client, company_id, email="stranger@elsewhere.example")
    searches = len(fake.searches)

    resolved = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == QuarantineStatus.RESOLVED.value
    assert body["outcome"]["outcome"] == IngestOutcome.INGESTED.value
    # The UID fallback identity: the header was absent, so the message is
    # recorded under its own UID namespaced by the mailbox's UIDVALIDITY.
    assert body["outcome"]["mailbox_message_id"] == f"imap-uid:{fake.uidvalidity}:{uid}"
    assert body["communication_id"] == body["outcome"]["communication_id"]
    assert len(await stored_communications(session)) == 1
    assert len(await stored_documents(session)) == 2
    # The retry resolved the UID identity directly, by its own fetch — no
    # Message-ID search exists for a message that never carried one.
    assert fake.searches[searches:] == []
    assert full_fetches(fake) == [uid]
    entries = await audit_entries(session, tenant_id)
    assert [entry.action for entry in entries[-2:]] == [
        AuditAction.MAIL_MESSAGE_INGESTED.value,
        AuditAction.MAIL_QUARANTINE_RESOLVED.value,
    ]


# --- Custody, and the schema's own backstops ------------------------------


async def test_the_app_password_and_the_mail_reach_no_entry_and_no_log(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The custody claim, asserted rather than promised.

    Two markers are planted: the app password (a credential under ADR-0014,
    which must live in the environment and nowhere else) and the body of the
    message that arrived (which is stored, and is what the deployment is for).
    Every table of the schema, every audit entry and every captured log record
    is searched for both. The body is expected in exactly two places — the
    ``communications`` row and the stored object, neither of which this search
    reads — and in no audit entry, no log line and no response; the password is
    expected nowhere at all. The tenant row proves the table search finds
    things, so "no table" is not "the search was looking in the wrong place".
    """
    caplog.set_level(logging.DEBUG)
    fake = FakeImap(username=MAILBOX, password=APP_PASSWORD)
    reply(fake)
    client, tenant_id, _ = await mail_deployment(session, store, monkeypatch, fake)
    tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None

    response = client.post(INGEST_URL)
    assert response.status_code == 200, response.text
    # The search is non-vacuous: the message really did arrive and really was
    # read — its bytes are in the store this deployment wrote.
    assert BODY_MARKER.encode() in b"".join(store.objects.values())

    assert await rows_containing(session, tenant.name) == ["tenants (1)"]
    assert await rows_containing(session, APP_PASSWORD) == []
    assert BODY_MARKER not in response.text
    assert APP_PASSWORD not in response.text
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert APP_PASSWORD not in logged
    assert BODY_MARKER not in logged
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_INGESTED.value
    assert APP_PASSWORD not in str(entries[-1].details)
    assert BODY_MARKER not in str(entries[-1].details)
    # And the mailbox itself was left alone: read-only, no flag, nothing moved.
    assert fake.seen == set()
    assert fake.writes == []
    assert sorted(fake.messages) == [1]
