"""DB-backed inbound-mail ingestion scenarios (opt-in; needs a migrated
PostgreSQL).

The Phase 3 ingestion slice end to end against a real database, a faithful
fake of Google (``tests/fake_google.py``, no socket) and an in-memory scanner
(``tests/fake_scanning.py``): one message becoming a stored raw document, its
attachments, and a ``Communication`` row; the ADR-0009 ingestion order with
each verdict it can end in; idempotency by provider message id; the §5.1 gate
refusing a company; the multi-company resolver attributing a message the
subject or body names a company of, and §2.5's quarantine record for the
messages it cannot attribute; the §2.5 review surface — the list staff triage
from (the open queue by default, either closed state on request), the retry
that re-examines one provider message id through that same order and closes the
row it can now attribute, and the dismiss that closes one staff have decided
needs no further action; the resolver's decision as a report carries it (the
case, the tier, the score, the routing band and the candidate ids — never the
text that matched); and the retro-scan that closes the interim posture.

The custody claims are asserted rather than implied: no audit entry and no API
response carries a message body, an attachment's bytes, its filename, or the
text a resolver candidate matched on — the searches look for markers planted in
the mail, and the stored raw document (or the §2.7 signal log, for the matched
text) proves the search is not merely looking in the wrong place.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import base64
import json
import os
import threading
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.gmail_client import GmailClient
from app.adapters.persistence.attribution import set_status
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_ingestion import MailIngestionState
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import (
    AttributionCase,
    SplitParent,
    SplitWorkItem,
    Tenant,
    User,
)
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.quarantine import QuarantinedMessage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import attribution, mail
from app.core.config import settings
from app.domain import communications as communications_domain
from app.domain.attribution import (
    AttributionAction,
    AttributionAskKind,
    AttributionCaseStatus,
    ask_subject,
    registration_ask_letter,
)
from app.domain.audit import AuditAction
from app.domain.communications import IngestOutcome
from app.domain.documents import DocumentKind, DocumentOrigin, ScanStatus
from app.domain.mail_send import OutboxStatus
from app.domain.quarantine import QuarantineReason, QuarantineStatus
from app.domain.service_packages import GatingReason
from app.main import create_app
from tests.fake_google import FakeGoogle, consent_challenge
from tests.fake_scanning import CLEAN, FakeScanner, failed, infected

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

INGEST_URL = "/api/v1/mail/ingest"
QUARANTINE_URL = "/api/v1/mail/quarantine"
SCAN_PENDING_URL = "/api/v1/documents/scan-pending"
CLIENT_ID = "the-deployment-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "the-deployment-client-secret"
MAILBOX = "ai-filings@tcsp.example"
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
# Planted in the mail itself: every test that asserts "no body travelled"
# searches for these, and the stored raw document proves they really are there.
BODY_MARKER = "ANSWER-MARKER-9931"
ATTACHMENT_MARKER = b"%PDF-1.7 ATTACHMENT-MARKER-4471"
# Planted inside a *registered company name* the resolver matches, so the text
# the rule decided on really is this string: the §2.7 signal log holds it as
# the candidate's matched text, which is what makes the "the matched text did
# not travel" assertions able to fail.
MATCHED_TEXT_MARKER = "ANSWER-MARKER-7734"
PDF = b"%PDF-1.7 a filing"
ZIP = b"PK\x03\x04 a zip the registry does not accept"


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


def provision(monkeypatch: pytest.MonkeyPatch, tier: str, *, expires_at: datetime | None = None):
    """Provision the deployment configuration the entitlement adapter reads."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", expires_at)


def configure(monkeypatch: pytest.MonkeyPatch, *, address: str = MAILBOX) -> None:
    """Provision the deployment configuration the mailbox flow reads."""
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "mail_address", address)


def use_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
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
    associate(client, company_id, person_id)
    return person_id


def associate(client: TestClient, company_id: str, person_id: str) -> None:
    response = client.post(
        f"/api/v1/companies/{company_id}/contacts", json={"person_id": person_id}
    )
    # Idempotent: 201 for a new association, 200 for one that already exists.
    assert response.status_code in (200, 201), response.text


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


async def ingest_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    tier: str = "standard",
    covered: bool = True,
    register_contact: bool = True,
) -> tuple[TestClient, UUID, str]:
    """A tenant, a connected mailbox, and a company ready for ingestion."""
    tenant, admin = await seed_tenant_with_admin(session)
    configure(monkeypatch)
    provision(monkeypatch, tier)
    use_google(monkeypatch, google)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    company_id = make_company(client)
    if register_contact:
        add_registered_contact(client, company_id, email=CONTACT)
    if covered:
        cover(client, company_id)
    return client, tenant.id, company_id


async def multi_company_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    tier: str = "standard",
) -> tuple[TestClient, UUID, str, str]:
    """A tenant, a connected mailbox, and one contact registered for two
    companies — the case the resolver decides (scope §2.5)."""
    tenant, admin = await seed_tenant_with_admin(session)
    configure(monkeypatch)
    provision(monkeypatch, tier)
    use_google(monkeypatch, google)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    first = make_company(client)
    second = make_company(client, br_number="87654321", cr_number="7654321")
    person_id = add_registered_contact(client, first, email=CONTACT)
    associate(client, second, person_id)
    cover(client, first)
    cover(client, second)
    return client, tenant.id, first, second


def name_company(
    client: TestClient,
    company_id: str,
    *,
    name_en: str | None = None,
    name_zh: str | None = None,
    effective_date: str = "2026-01-01",
) -> None:
    """Record a registered name through the effective-dated fact path."""
    response = client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "effective_date": effective_date,
            "name": {"name_en": name_en, "name_zh": name_zh},
        },
    )
    assert response.status_code == 200, response.text


def message_with_attachment(
    google: FakeGoogle,
    *,
    attachments: list[tuple[str, str, bytes]] | None = None,
    sender: str = CONTACT,
    subject: str | None = None,
    body: str | None = None,
) -> str:
    return google.with_message(
        subject=(
            subject
            if subject is not None
            else "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"
        ),
        sender=f"CHAN TAI MAN <{sender}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        body=body if body is not None else BODY_MARKER,
        attachments=attachments
        if attachments is not None
        else [("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def actions_of(session: AsyncSession, tenant_id: UUID) -> list[str]:
    return [entry.action for entry in await audit_entries(session, tenant_id)]


async def stored_documents(session: AsyncSession) -> list[Document]:
    return list((await session.scalars(select(Document))).all())


def document_of_kind(rows: Sequence[Document], kind: DocumentKind) -> Document:
    matching = [row for row in rows if row.kind == kind.value]
    assert len(matching) == 1, f"expected one {kind.value} document, found {len(matching)}"
    return matching[0]


async def stored_communications(session: AsyncSession) -> list[Communication]:
    return list((await session.scalars(select(Communication))).all())


async def stored_inbound_communications(session: AsyncSession) -> list[Communication]:
    """The inbound half of the communications table.

    A test that seeds a stored outbound exchange (the thread evidence) has a
    row in this table already; the ingestion cycle's own rows are the inbound
    ones, so "the cycle recorded nothing" is this read.
    """
    return list(
        (
            await session.scalars(
                select(Communication).where(Communication.direction == "inbound")
            )
        ).all()
    )


async def stored_quarantines(session: AsyncSession) -> list[QuarantinedMessage]:
    return list(
        (
            await session.scalars(
                select(QuarantinedMessage).order_by(QuarantinedMessage.mailbox_message_id)
            )
        ).all()
    )


async def stored_cases(session: AsyncSession) -> list[AttributionCase]:
    return list(
        (
            await session.scalars(
                select(AttributionCase).order_by(AttributionCase.created_at, AttributionCase.id)
            )
        ).all()
    )


async def stored_outbox(session: AsyncSession) -> list[MailOutbox]:
    """The tenant's outbox rows — the queue the ask rides."""
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
    mailbox_message_id: str | None = None,
    thread_id: str | None = None,
    confirmation_request_id: UUID | None = None,
) -> Communication:
    """One stored outbound exchange, as the sending slice would have left it.

    These rows are the thread evidence the attribution decision reads (ruling
    R1): the provider ``thread_id`` a reply rides, and the ``mailbox_message_id``
    its ``In-Reply-To``/``References`` carries back. The stored letter itself
    is what ``raw_document_id`` points at, exactly as the real outbound row's
    does.
    """
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
        thread_id=thread_id,
        sender_address=MAILBOX,
        recipient_addresses=CONTACT,
        confirmation_request_id=confirmation_request_id,
        received_at=datetime.now(UTC),
        raw_document_id=document.id,
    )
    session.add(row)
    await session.flush()
    return row


async def stored_assessments(session: AsyncSession) -> list[ExtractionAssessment]:
    return list(
        (
            await session.scalars(
                select(ExtractionAssessment).order_by(ExtractionAssessment.created_at)
            )
        ).all()
    )


def upload(client: TestClient, company_id: str, query: str = "kind=prior_filing&form=NAR1") -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/documents?{query}",
        content=PDF,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def pending_document_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> tuple[TestClient, dict, UUID]:
    """A deployment holding one document that a failed scan left ``pending``."""
    tenant, admin = await seed_tenant_with_admin(session)
    provision(monkeypatch, "standard")
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = make_company(client)
    scanner_seam.default = failed("unavailable:ConnectionRefusedError")
    uploaded = upload(client, company_id)
    assert uploaded["scan_status"] == ScanStatus.PENDING.value
    return client, uploaded, tenant.id


# --- The whole cycle ------------------------------------------------------


async def test_a_message_becomes_documents_and_a_communication_row(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google)
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 1
    assert body["ingested"] == 1
    assert body["skipped_known"] == 0
    assert body["refused"] == 0
    outcome = body["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["mailbox_message_id"] == message_id
    assert outcome["company_id"] == company_id
    assert outcome["scan_status"] == ScanStatus.CLEAN.value
    assert len(outcome["attachments"]) == 1
    assert outcome["attachments"][0]["disposition"] == "stored"
    assert outcome["attachments"][0]["scan_status"] == "clean"
    assert outcome["attachments"][0]["reason"] is None
    # The response describes mail; it never carries any of it.
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text

    # The raw message is stored exactly as the provider hands it over, and the
    # attachment beside it, both through the single ingestion path.
    rows = await stored_documents(session)
    assert len(rows) == 2
    raw_row = document_of_kind(rows, DocumentKind.CLIENT_REPLY)
    attachment_row = document_of_kind(rows, DocumentKind.CLIENT_DOCUMENT)
    assert raw_row.origin == DocumentOrigin.EMAIL_ATTACHMENT.value
    assert raw_row.content_type == "message/rfc822"
    assert raw_row.scan_status == ScanStatus.CLEAN.value
    assert raw_row.retention_class == "client_communication"
    # The raw message is not a PDF, so its key must not say it is.
    assert raw_row.storage_key.endswith(".eml")
    assert store.objects[raw_row.storage_key] == google.raw_of(message_id)
    assert attachment_row.origin == DocumentOrigin.EMAIL_ATTACHMENT.value
    assert attachment_row.content_type == "application/pdf"
    assert attachment_row.retention_class == "client_communication"
    assert store.objects[attachment_row.storage_key] == ATTACHMENT_MARKER
    assert raw_row.company_id == attachment_row.company_id == UUID(company_id)
    assert outcome["raw_document_id"] == str(raw_row.id)
    assert outcome["attachments"][0]["document_id"] == str(attachment_row.id)

    # The Communication row carries the fields this slice can fill honestly.
    record = (await stored_communications(session))[0]
    assert record.direction == "inbound"
    assert record.channel == "email"
    assert record.mailbox_message_id == message_id
    assert record.thread_id == f"thread-{message_id}"
    assert record.sender_address == CONTACT
    assert record.language is None
    assert record.raw_document_id == raw_row.id
    assert str(record.id) == outcome["communication_id"]
    assert record.company_id == raw_row.company_id

    # Every ingestion is audited — message id, document ids, scan outcomes —
    # and the mail itself is not: the markers are in the stored raw document
    # and nowhere in the audit chain.
    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.DOCUMENT_REGISTERED.value) == 2
    assert actions[-1] == AuditAction.MAIL_MESSAGE_INGESTED.value
    ingested = entries[-1]
    assert ingested.details["mailbox_message_id"] == message_id
    assert ingested.details["thread_id"] == f"thread-{message_id}"
    assert ingested.details["raw_document_id"] == str(raw_row.id)
    assert ingested.details["raw_scan_status"] == "clean"
    assert ingested.details["attachments"] == [
        {
            "document_id": str(attachment_row.id),
            "disposition": "stored",
            "scan_status": "clean",
            "reason": None,
        }
    ]
    assert ingested.company_id == UUID(company_id)
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text

    # The cursor is the provider's receipt time of the newest message examined,
    # and the next read asks only for what follows it.
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at is not None
    assert body["cursor_received_at"] is not None
    assert google.list_queries == [None]

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    # The overlap window re-reads the newest messages by design; the message id
    # is what keeps the replay from duplicating anything.
    assert second.json()["examined"] == 1
    assert second.json()["skipped_known"] == 1
    assert second.json()["ingested"] == 0
    assert google.list_queries[-1] is not None
    assert google.list_queries[-1].startswith("after:")


async def test_the_same_message_is_never_ingested_twice(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google)
    client, tenant_id, _ = await ingest_deployment(session, store, monkeypatch, google)
    assert client.post(INGEST_URL).json()["ingested"] == 1
    raw_fetches = list(google.raw_fetches)

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    body = second.json()
    # The overlap window brings the message back — and the message id says it
    # is already here, so nothing is fetched, scanned, or written again.
    assert body["examined"] == 1
    assert body["skipped_known"] == 1
    assert body["ingested"] == 0
    assert body["outcomes"][0]["mailbox_message_id"] == message_id
    assert body["outcomes"][0]["outcome"] == IngestOutcome.SKIPPED_KNOWN.value
    assert google.raw_fetches == raw_fetches
    assert len(await stored_documents(session)) == 2
    assert len(await stored_communications(session)) == 1
    assert (await actions_of(session, tenant_id)).count(
        AuditAction.MAIL_MESSAGE_INGESTED.value
    ) == 1


# --- The ingestion order and fail-closed behaviour ------------------------


async def test_a_scanner_that_cannot_answer_leaves_everything_quarantined(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)
    scanner_seam.default = failed("unavailable:ConnectionRefusedError")

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    # The message is still recorded — it arrived, and the deployment knows it —
    # but nothing is clean, so nothing is usable.
    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["scan_status"] == ScanStatus.PENDING.value
    assert [item["scan_status"] for item in outcome["attachments"]] == ["pending"]
    rows = await stored_documents(session)
    assert {row.scan_status for row in rows} == {ScanStatus.PENDING.value}
    # Both files were scanned — the raw message, then the attachment — before
    # anything was written.
    assert scanner_seam.scanned == [google.raw_of(outcome["mailbox_message_id"]), ATTACHMENT_MARKER]
    # And neither is served or processed.
    for row in rows:
        download = client.get(f"/api/v1/companies/{row.company_id}/documents/{row.id}/download")
        assert download.status_code == 409
        assert download.json()["detail"] == "document_not_available: pending"


async def test_an_infected_attachment_is_stored_as_infected_and_never_served(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    client, tenant_id, _ = await ingest_deployment(session, store, monkeypatch, google)
    scanner_seam.by_content[ATTACHMENT_MARKER] = infected("Eicar-Test-Signature")

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["scan_status"] == ScanStatus.CLEAN.value  # the message body is clean
    assert outcome["attachments"][0]["scan_status"] == ScanStatus.INFECTED.value
    rows = await stored_documents(session)
    assert {row.scan_status for row in rows} == {
        ScanStatus.CLEAN.value,
        ScanStatus.INFECTED.value,
    }
    infected_row = document_of_kind(rows, DocumentKind.CLIENT_DOCUMENT)
    assert infected_row.scan_status == ScanStatus.INFECTED.value
    # Stored as the record of what arrived — and served to nobody.
    assert store.objects[infected_row.storage_key] == ATTACHMENT_MARKER
    download = client.get(
        f"/api/v1/companies/{infected_row.company_id}/documents/{infected_row.id}/download"
    )
    assert download.status_code == 409
    assert download.json()["detail"] == "document_not_available: infected"
    # The detection is audited with ClamAV's own signature, on the document's
    # registration entry.
    entries = await audit_entries(session, tenant_id)
    registration = next(entry for entry in entries if entry.entity_id == infected_row.id)
    assert registration.action == AuditAction.DOCUMENT_REGISTERED.value
    assert registration.details["scan_status"] == "infected"
    assert registration.details["scan_signature"] == "Eicar-Test-Signature"


async def test_an_attachment_outside_the_registry_types_is_refused_and_reported(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        attachments=[("records.zip", "application/zip", ZIP), ("ok.pdf", "application/pdf", PDF)],
    )
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert [item["disposition"] for item in outcome["attachments"]] == ["refused", "stored"]
    assert outcome["attachments"][0]["reason"] == "unsupported_content_type"
    assert outcome["attachments"][0]["document_id"] is None
    rows = await stored_documents(session)
    assert {row.content_type for row in rows} == {"message/rfc822", "application/pdf"}


async def test_attachments_beyond_the_per_message_cap_are_refused_and_reported(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        attachments=[
            ("one.pdf", "application/pdf", PDF),
            ("two.pdf", "application/pdf", PDF),
            ("three.pdf", "application/pdf", PDF),
        ],
    )
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)
    # The cap exists to bound a MIME body that could hold thousands of parts;
    # a message over it is still ingested, and the excess is reported rather
    # than dropped silently.
    monkeypatch.setattr(communications_domain, "MAX_ATTACHMENTS_PER_MESSAGE", 2)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert [item["disposition"] for item in outcome["attachments"]] == [
        "stored",
        "stored",
        "refused",
    ]
    assert outcome["attachments"][2]["reason"] == "too_many_attachments"
    assert outcome["attachments"][2]["document_id"] is None
    assert len(await stored_documents(session)) == 3  # the raw message and two attachments


async def test_a_message_that_is_not_a_message_fails_closed(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="A reply",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        raw=b"not a message at all",
    )
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.FAILED.value
    assert outcome["reason"] == "invalid_message:content_type_mismatch"
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []


# --- Attribution and the gate --------------------------------------------


async def test_a_sender_with_no_registered_company_is_quarantined_for_staff_review(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope §2.5: an unregistered address is quarantined for staff review."""
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, _ = await ingest_deployment(session, store, monkeypatch, google)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == "unregistered_sender"
    # Nothing is attributed — no company, no document, no Communication row —
    # but the message is no longer invisible: §2.5's quarantine record is what
    # staff work from.
    assert outcome["company_id"] is None
    assert outcome["communication_id"] is None
    assert outcome["quarantine_id"] is not None
    # The resolver never ran — the sender reaches no company at all, so there
    # were no candidates to decide between. ``null`` is that fact, not a gap.
    assert outcome["resolution"] is None
    rows = await stored_quarantines(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == UUID(outcome["quarantine_id"])
    assert row.tenant_id == tenant_id
    assert row.mailbox_message_id == message_id
    assert row.thread_id == f"thread-{message_id}"
    assert row.sender_address == "stranger@elsewhere.example"
    # The provider's own internalDate, not a clock of ours (that is the value
    # the Communication row and the ingestion cursor are built from).
    assert row.received_at == google.received_base
    assert row.reason == QuarantineReason.UNREGISTERED_SENDER.value
    assert row.status == QuarantineStatus.PENDING.value
    assert row.resolved_at is None
    assert row.resolved_communication_id is None
    # An unregistered sender's message is never read: there is nothing to read
    # it for, so its bytes are not fetched at all — let alone stored.
    assert google.raw_fetches == []
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []
    # The quarantine is audited; the mail is not.
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_QUARANTINED.value
    assert entries[-1].entity_type == "quarantined_message"
    assert entries[-1].entity_id == row.id
    assert entries[-1].company_id is None
    assert entries[-1].details["mailbox_message_id"] == message_id
    assert entries[-1].details["thread_id"] == f"thread-{message_id}"
    assert entries[-1].details["sender_address"] == "stranger@elsewhere.example"
    assert entries[-1].details["reason"] == "unregistered_sender"
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text


async def test_a_multi_company_reply_is_attributed_by_its_subject(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client serving two companies: the subject names the one it is about.

    The subject is the product's own confirmation-request one — the format
    scope §2.5 fixes ("every AI message must clearly identify the relevant
    company by name and CR number") — so this is the ordinary reply shape.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google)  # subject carries CR 1234567
    client, tenant_id, first, second = await multi_company_deployment(
        session, store, monkeypatch, google
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["reason"] is None
    assert outcome["company_id"] == first
    assert outcome["quarantine_id"] is None
    # A message the rule decided reports what it saw: the case, the tier that
    # decided it, the score, the band it routed to, and the candidate ids.
    assert outcome["resolution"] == {
        "case": "resolved",
        "evidence_tier": "cr_exact",
        "score": 1.0,
        "routing": "auto_accept",
        "candidate_company_ids": [first],
    }
    assert await stored_quarantines(session) == []
    records = await stored_communications(session)
    assert len(records) == 1
    assert str(records[0].company_id) == first
    assert records[0].company_id != UUID(second)
    # The resolver's decision is on the message's own audit entry — one entry
    # per message, and no second one for the assessment.
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_INGESTED.value
    assert entries[-1].details["resolution"] == {
        "case": "resolved",
        "evidence": "cr_exact",
        "score": 1.0,
        "routing": "auto_accept",
        "candidates": [first],
    }
    assert (await actions_of(session, tenant_id)).count(
        AuditAction.MAIL_MESSAGE_QUARANTINED.value
    ) == 0
    # §2.7: the decision carries an assessment, keyed to the Communication row
    # it produced — the type names that table, and the id resolves in it.
    assessments = await stored_assessments(session)
    assert len(assessments) == 1
    assessment = assessments[0]
    assert assessment.subject_type == "communication"
    assert assessment.subject_id == records[0].id
    assert await session.get(Communication, assessment.subject_id) is not None
    assert assessment.component == "multi_company_resolver"
    assert assessment.model == "reply-resolver/v1"
    assert float(assessment.overall_score) == 1.0
    assert assessment.routing == "auto_accept"
    assert assessment.field_scores == {"communication.company": 1.0}
    assert assessment.signal_log["resolution"] == "resolved"
    assert assessment.signal_log["candidates"] == {first: ["cr_exact"]}
    # The mail itself still does not travel — not in the response, and not in
    # the audit chain this path writes both entries and an assessment to.
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text
    assert google.raw_fetches == [message_id]  # read once, to decide and to store


async def test_a_multi_company_reply_that_names_no_company_is_quarantined(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several companies and nothing in the message to choose between them.

    The reason follows the sender: this address is registered, so the remedy
    is to work out which company the message is about — not to register it.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        # The marker really is in this message's body — this is the path that
        # reads a body and then refuses the message, so the "it did not
        # travel" assertions below have to be able to fail. The sentence
        # names no company, so the outcome is the same either way.
        body=f"Please see the attached signed form. {BODY_MARKER}",
    )
    client, tenant_id, _, _ = await multi_company_deployment(session, store, monkeypatch, google)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.AMBIGUOUS_SENDER.value
    assert outcome["reason"] == "multiple_companies"
    assert outcome["company_id"] is None
    assert outcome["quarantine_id"] is not None
    rows = await stored_quarantines(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == QuarantineReason.MULTIPLE_COMPANIES.value
    assert row.status == QuarantineStatus.PENDING.value
    assert row.mailbox_message_id == message_id
    assert row.sender_address == CONTACT
    # The body is read to decide (that is what the decision needs) and the
    # message is neither stored nor recorded.
    assert google.raw_fetches == [message_id]
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_QUARANTINED.value
    assert entries[-1].entity_id == row.id
    assert entries[-1].details["reason"] == "multiple_companies"
    # The §2.7 record of why the rule could not decide: no candidate matched,
    # so it routes to a clarifying question rather than to anything automatic.
    # Its subject type is the table its subject id points into — there is no
    # Communication to point at — and that id resolves to this quarantine row.
    assessments = await stored_assessments(session)
    assert len(assessments) == 1
    assessment = assessments[0]
    assert assessment.subject_type == "quarantine"
    assert assessment.subject_id == row.id
    quarantined = await session.get(QuarantinedMessage, assessment.subject_id)
    assert quarantined is not None
    assert quarantined.mailbox_message_id == message_id
    assert assessment.component == "multi_company_resolver"
    assert assessment.routing == "client_clarification"
    assert float(assessment.overall_score) == 0.0
    assert assessment.signal_log["resolution"] == "no_match"
    assert assessment.signal_log["candidates"] == {}
    assert assessment.signal_log["matched_text"] == {}
    assert BODY_MARKER not in response.text
    assert "signed.pdf" not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text


class SplitTransport:
    """The replaced gateway port for the split's cycle tests: answers the
    spans it was given, whatever the task type (the E2 test reuses it for the
    work item's parse)."""

    def __init__(self, *, envelope: dict) -> None:
        self.calls: list[dict[str, str]] = []
        self._envelope = envelope

    async def complete(self, *, task_type: str, language: str, snippet: str) -> object:
        self.calls.append({"task_type": task_type, "language": language, "snippet": snippet})
        result = _TransportResult(envelope=dict(self._envelope))
        return result


class _TransportResult:
    def __init__(self, *, envelope: dict) -> None:
        self.envelope = envelope


def _split_envelope(result: object) -> dict:
    return {
        "protocol_version": 1,
        "provider": "provider-test",
        "task_type": "split",
        "model_id": "test-model",
        "prompt_name": "split",
        "prompt_version": 1,
        "result": result,
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        "completed_at": "2026-10-09T00:00:00+00:00",
    }


async def split_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    envelope: dict,
    same_name: bool = False,
) -> tuple[TestClient, UUID, str, str, SplitTransport]:
    """Two companies sharing one registered contact, the AI facts the split's
    pre-flight and the call's posture read, and the fake transport. Returns
    the client, the tenant id, the two company ids, and the transport."""
    from app.application import ai_gateway, risk_acknowledgment

    tenant, admin = await seed_tenant_with_admin(session)
    configure(monkeypatch)
    provision(monkeypatch, "standard")
    use_google(monkeypatch, google)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    await risk_acknowledgment.accept(session, tenant_id=tenant.id, actor_user_id=admin.id)
    monkeypatch.setattr(settings, "ai_q13_gate", "residual_risk_accepted")
    monkeypatch.setattr(settings, "license_tier", "enterprise_offline")
    monkeypatch.setattr(settings, "llm_base_url", "https://llm.local/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    first = make_company(client)
    second = make_company(client, br_number="87654321", cr_number="7654321")
    person_id = add_registered_contact(client, first, email=CONTACT)
    associate(client, second, person_id)
    name_company(client, first, name_en=COMPANY_NAME)
    name_company(
        client, second, name_en=COMPANY_NAME if same_name else "Other Harbour Limited"
    )
    cover(client, first, services=["email_ingestion", "ai_reply_parsing"])
    cover(client, second, services=["email_ingestion", "ai_reply_parsing"])
    transport = SplitTransport(envelope=envelope)
    monkeypatch.setattr(ai_gateway, "llm_client", lambda _transport: transport)
    return client, tenant.id, first, second, transport


async def test_a_multi_company_reply_naming_two_companies_splits(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 end to end: a decisive several-match — both CR numbers named — is
    workstream ③'s split, not a refusal. The parent opens, the stub provider's
    division passes the grammar, the two work items carry the per-company
    slices, and nothing is quarantined or ingested whole. (The pre-③ pin this
    test replaces — "a tie is not a decision" — is now the split's premise:
    the tie is divided, not resolved.)"""
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="Both CR 1234567 and CR 7654321 are mentioned in this reply.",
    )
    # The tokenized content (the tokenizer numbers the CR label's values
    # longest-first, then alphabetically): [CR_01] is 1234567, [CR_02] is
    # 7654321.
    spans = [
        {"company": "[CR_01]", "text": "Both CR [CR_01] and "},
        {"company": "[CR_02]", "text": "CR [CR_02] are mentioned in this reply."},
    ]
    client, tenant_id, first, second, transport = await split_deployment(
        session, store, monkeypatch, google, envelope=_split_envelope(result=spans)
    )

    body = client.post(INGEST_URL).json()

    (outcome,) = body["outcomes"]
    assert outcome["outcome"] == IngestOutcome.SPLIT.value
    assert outcome["reason"] is None
    assert outcome["split_parent_id"] is not None
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    assert outcome["resolution"]["case"] == "ambiguous"
    (call,) = transport.calls
    assert call["task_type"] == "split"
    assert "1234567" not in call["snippet"] and "[CR_01]" in call["snippet"]
    rows = await stored_quarantines(session)
    assert rows == []
    assert await stored_communications(session) == []
    parents = list(
        (
            await session.scalars(select(SplitParent).where(SplitParent.tenant_id == tenant_id))
        ).all()
    )
    assert len(parents) == 1
    assert parents[0].status == "split_done"
    items = list(
        (
            await session.scalars(
                select(SplitWorkItem).where(SplitWorkItem.parent_id == parents[0].id)
            )
        ).all()
    )
    assert {item.company_id for item in items} == {UUID(first), UUID(second)}
    for item in items:
        assert item.status == "awaiting_parse"


class _LoopbackLlmServer(ThreadingHTTPServer):
    """The recording half of the stub provider: what the real gateway sent."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], answers: dict[str, Any]) -> None:
        super().__init__(address, _LoopbackLlmHandler)
        self.answers = answers
        self.requests: list[dict[str, Any]] = []


class _LoopbackLlmHandler(BaseHTTPRequestHandler):
    """One OpenAI-compatible endpoint; answers by the task type the local
    provider names in its system message (the real request shape — nothing
    about the gateway is replaced in this test)."""

    def do_POST(self) -> None:
        server = cast("_LoopbackLlmServer", self.server)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            document = json.loads(self.rfile.read(length))
        except ValueError:
            document = None
        server.requests.append(document if isinstance(document, dict) else {})
        if self.path != "/v1/chat/completions" or not isinstance(document, dict):
            self._answer(404, {"error": {"code": "not_found"}})
            return
        messages = document.get("messages")
        system = messages[0].get("content") if isinstance(messages, list) else None
        task_type = str(system).split("'")[1] if system else ""
        result = server.answers.get(task_type)
        if result is None:
            self._answer(400, {"error": {"code": "unknown_task"}})
            return
        self._answer(
            200,
            {
                "id": "chatcmpl-loopback-stub",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": document.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(result)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 34,
                    "total_tokens": 46,
                },
            },
        )

    def _answer(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Bodies are never logged — the request line is not either."""


class LoopbackLlm:
    """The real gateway's stub provider: an OpenAI-compatible server on a
    loopback socket, stdlib only (the local custody leg's ``allow_insecure``
    development override exists for exactly this)."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self._server = _LoopbackLlmServer(("127.0.0.1", 0), answers)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "LoopbackLlm":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests


async def stub_gateway_deployment(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    stub: LoopbackLlm,
) -> tuple[TestClient, UUID, str, str]:
    """``split_deployment``'s twin for the ride-through tests: identical
    tenant, mailbox, companies and coverage, but the gateway is NOT replaced —
    the real compose → transport → grammar seam runs against the loopback
    stub (Enterprise Offline custody, ``allow_insecure`` for the loopback)."""
    from app.application import risk_acknowledgment

    tenant, admin = await seed_tenant_with_admin(session)
    configure(monkeypatch)
    provision(monkeypatch, "standard")
    use_google(monkeypatch, google)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    connect_mailbox(client, google)
    await risk_acknowledgment.accept(session, tenant_id=tenant.id, actor_user_id=admin.id)
    monkeypatch.setattr(settings, "ai_q13_gate", "residual_risk_accepted")
    monkeypatch.setattr(settings, "license_tier", "enterprise_offline")
    monkeypatch.setattr(settings, "llm_base_url", stub.base_url)
    monkeypatch.setattr(settings, "llm_api_key", "loopback-stub-key")
    monkeypatch.setattr(settings, "llm_model", "loopback-stub-model")
    monkeypatch.setattr(settings, "llm_allow_insecure", True)
    first = make_company(client)
    second = make_company(client, br_number="87654321", cr_number="7654321")
    person_id = add_registered_contact(client, first, email=CONTACT)
    associate(client, second, person_id)
    name_company(client, first, name_en=COMPANY_NAME)
    name_company(client, second, name_en="Other Harbour Limited")
    cover(client, first, services=["email_ingestion", "ai_reply_parsing"])
    cover(client, second, services=["email_ingestion", "ai_reply_parsing"])
    return client, tenant.id, first, second


async def test_the_split_items_parse_in_the_same_cycle_with_events_per_company(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1's tail end to end (workstream ③, ruling D7): nothing about the
    gateway is replaced — the real seam rides the loopback stub provider for
    BOTH the division and the two per-item parses. The same parse cycle
    sweeps the awaiting items, each parse lands on its own company, and each
    company's change event rides under the ``split_work_item`` source."""
    from app.adapters.persistence.change_events import ChangeEvent
    from app.application import reply_parsing

    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="Both CR 1234567 and CR 7654321 are mentioned in this reply.",
    )
    # The tokenized content (the tokenizer numbers the CR label's values
    # longest-first): [CR_01] is 1234567, [CR_02] is 7654321.
    spans = [
        {"company": "[CR_01]", "text": "Both CR [CR_01] and "},
        {"company": "[CR_02]", "text": "CR [CR_02] are mentioned in this reply."},
    ]
    parse_result = {
        "status": "changes_reported",
        "language": "en",
        "confirmations": [],
        "change_events": [
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": "Wong Siu Ming",
                    "name_en": "Wong Siu Ming",
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": "Please appoint Ms Wong.",
                    "uncertain": False,
                },
                "effective_date": "2026-05-01",
                "effective_date_text": "1 May 2026",
                "informed_date": None,
                "new_values": None,
                "evidence_span": "Please appoint Ms Wong.",
                "uncertain": False,
                "notes": None,
            }
        ],
        "multi_company_signals": {"company_mentions": []},
    }
    with LoopbackLlm({"split": spans, "reply_parsing": parse_result}) as stub:
        client, tenant_id, first, second = await stub_gateway_deployment(
            session, store, monkeypatch, google, stub=stub
        )

        body = client.post(INGEST_URL).json()
        (outcome,) = body["outcomes"]
        assert outcome["outcome"] == IngestOutcome.SPLIT.value
        assert outcome["split_parent_id"] is not None

        # The division crossed the real wire placeholders-only: the raw CR
        # digits are in no request, the tokens are.
        (split_request,) = [
            request
            for request in stub.requests
            if "K2P inference task 'split'" in str(request["messages"][0]["content"])
        ]
        assert "1234567" not in split_request["messages"][1]["content"]
        assert "[CR_01]" in split_request["messages"][1]["content"]

        report = await reply_parsing.run_cycle(
            session,
            store=store,
            scanner=FakeScanner(),
            tenant_id=tenant_id,
            as_of=date(2026, 9, 23),
        )

        # One split call and two per-company parse calls — the cycle swept
        # both awaiting items through the one parse path.
        assert len(stub.requests) == 3
        assert len(report.outcomes) == 2
        assert all(item_outcome.outcome == "parsed" for item_outcome in report.outcomes)
        items = list(
            (
                await session.scalars(
                    select(SplitWorkItem).where(SplitWorkItem.tenant_id == tenant_id)
                )
            ).all()
        )
        assert {item.company_id for item in items} == {UUID(first), UUID(second)}
        assert all(item.status == "parsed" for item in items)

        # Each company's change event landed on its own company, under the
        # work item's source — the division's whole point.
        events = list(
            (await session.scalars(select(ChangeEvent).where(ChangeEvent.tenant_id == tenant_id)))
            .all()
        )
        assert len(events) == 2
        assert {event.company_id for event in events} == {UUID(first), UUID(second)}
        assert all(event.source_type == "split_work_item" for event in events)
        assert {event.source_id for event in events} == {item.id for item in items}

        # The audit chain: the split's own call is company-less (ruling D10),
        # each parse call names its item's company.
        entries = list(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
                )
            ).all()
        )
        calls = [entry for entry in entries if entry.action == AuditAction.LLM_CALL.value]
        assert len(calls) == 3
        (split_call,) = [entry for entry in calls if entry.llm_call["task_type"] == "split"]
        assert split_call.company_id is None
        assert split_call.entity_type == "split_parent"
        parse_calls = [entry for entry in calls if entry.llm_call["task_type"] == "reply_parsing"]
        assert {entry.company_id for entry in parse_calls} == {UUID(first), UUID(second)}



async def test_a_company_named_in_the_body_resolves_a_multi_company_reply(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered name in the body attributes the message (scope §2.5)."""
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="The return for Harbour View Limited is ready.",
    )
    client, tenant_id, first, second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, first, name_en="Peak Trading Limited")
    name_company(client, second, name_en="Harbour View Limited")

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["company_id"] == second
    assert outcome["quarantine_id"] is None
    assert await stored_quarantines(session) == []
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].details["resolution"]["evidence"] == "name_exact"
    assert entries[-1].details["resolution"]["routing"] == "auto_accept"


async def test_a_name_recorded_after_the_message_arrived_still_resolves_it(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution reads the master data in force *now*, not at receipt.

    The name is recorded two days after the message arrived and before the
    cycle runs (the fake's default receipt is 2026-09-20, the name is
    effective 2026-09-22, and the cycle runs today). That is the staff
    remedy working: a message that could not be attributed when it arrived
    becomes attributable once the company is onboarded and the quarantine is
    retried — a receipt-date rule would leave it permanently unresolvable.
    """
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="The return for Harbour View Limited is ready.",
    )
    client, _, first, second = await multi_company_deployment(session, store, monkeypatch, google)
    name_company(client, first, name_en="Peak Trading Limited")
    name_company(client, second, name_en="Harbour View Limited", effective_date="2026-09-22")
    assert google.received_base.date() < date(2026, 9, 22)  # the name postdates the message

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["company_id"] == second
    assert await stored_quarantines(session) == []


async def test_a_quarantined_message_is_recorded_once_however_often_the_cycle_runs(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider message id is the key, exactly as it is for ingestion."""
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(
        google,
        sender="stranger@elsewhere.example",
        subject="Re: Annual return confirmation",
        body="Who is this?",
    )
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="Please see the attached signed form.",
    )
    client, tenant_id, _, _ = await multi_company_deployment(session, store, monkeypatch, google)

    first = client.post(INGEST_URL).json()
    second = client.post(INGEST_URL).json()

    assert first["refused"] == 2
    assert len(await stored_quarantines(session)) == 2
    assert {row.reason for row in await stored_quarantines(session)} == {
        QuarantineReason.UNREGISTERED_SENDER.value,
        QuarantineReason.MULTIPLE_COMPANIES.value,
    }
    # The second cycle examines the same messages again (the cursor's overlap
    # re-reads them) and finds the same quarantine rows rather than new ones.
    assert {item["quarantine_id"] for item in second["outcomes"]} == {
        item["quarantine_id"] for item in first["outcomes"]
    }
    assert (await actions_of(session, tenant_id)).count(
        AuditAction.MAIL_MESSAGE_QUARANTINED.value
    ) == 2
    assert len(await stored_assessments(session)) == 1  # one per decision record


async def test_the_mailboxs_own_mail_is_not_read_back_as_a_reply(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="[Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender=f"AI Filings <{MAILBOX}>",
        date="Tue, 23 Sep 2026 08:00:00 +0800",
        body="what this deployment sent",
    )
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.SELF_SENT.value
    assert await stored_communications(session) == []
    assert await stored_documents(session) == []


async def test_the_gate_refuses_a_free_deployment_and_nothing_is_ingested(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google, tier="free"
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.DENIED.value
    assert outcome["reason"] == GatingReason.TIER_CAPABILITY_MISSING.value
    assert outcome["company_id"] == company_id
    # Denied means nothing was fetched, scanned, or written — and the denial
    # itself is not an audit event: nothing changed.
    assert google.raw_fetches == []
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []
    actions = await actions_of(session, tenant_id)
    assert AuditAction.MAIL_MESSAGE_INGESTED.value not in actions
    assert AuditAction.DOCUMENT_REGISTERED.value not in actions


async def test_the_gate_refuses_a_company_whose_package_omits_email_ingestion(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    client, _, company_id = await ingest_deployment(
        session, store, monkeypatch, google, covered=False
    )
    # A package that covers something else leaves this company manual-only.
    cover(client, company_id, services=["calendar_sync"])

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.DENIED.value
    assert outcome["reason"] == GatingReason.SERVICE_NOT_INCLUDED.value
    assert await stored_documents(session) == []


async def test_the_gate_refuses_a_company_with_no_service_assignment(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google, covered=False)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.DENIED.value
    assert outcome["reason"] == GatingReason.NO_ASSIGNMENT.value


async def test_the_gate_denies_a_resolved_multi_company_message_and_records_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver decides, and the gate then denies the company it decided.

    Every other denial test sends a single-company message, where the gate is
    reached without the resolver having run. Here the sender is registered
    for two companies and the subject decides which one (CR 1234567 is
    ``first``), and the gate — Free tier denies every company — refuses it
    after the decision. A message that was attributed but not acted on is not
    a quarantine case and has no record to hang a §2.7 assessment on, so the
    cycle must leave nothing at all behind.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google)  # the subject names `first`
    client, tenant_id, first, _ = await multi_company_deployment(
        session, store, monkeypatch, google, tier="free"
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.DENIED.value
    assert outcome["reason"] == GatingReason.TIER_CAPABILITY_MISSING.value
    assert outcome["company_id"] == first  # the resolver decided; the gate denied
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    # The decision is reported even though nothing was written for it: the
    # rule ran, and what it saw is exactly what a reviewer needs to see when a
    # denied message is not the rule's fault.
    assert outcome["resolution"] == {
        "case": "resolved",
        "evidence_tier": "cr_exact",
        "score": 1.0,
        "routing": "auto_accept",
        "candidate_company_ids": [first],
    }
    # The decision needed the message's own words, so the bytes were read —
    # once, and nowhere: read to decide, not retained, never stored.
    assert google.raw_fetches == [message_id]
    assert await stored_quarantines(session) == []
    assert await stored_assessments(session) == []
    assert await stored_communications(session) == []
    assert await stored_documents(session) == []
    assert store.objects == {}
    # No audit entry for the message — and none at all from the cycle, because
    # a denial changed nothing.
    actions = set(await actions_of(session, tenant_id))
    assert AuditAction.MAIL_MESSAGE_INGESTED.value not in actions
    assert AuditAction.MAIL_MESSAGE_QUARANTINED.value not in actions
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert message_id not in audit_text


# --- Thread evidence and the attribution case (ADR-0019 §1) ---------------
#
# A threaded message whose sender is not a registered channel of the chain's
# company opens an attribution case instead of being quarantined: the thread
# binds the company (ruling R1), and the message's own words are not read at
# all before the confirmation (ruling R2). Every test below asserts the
# provider reads it is making, not only the rows it wrote: the fake's
# ``message_fetches``/``raw_fetches`` are the observation that proves the
# awaiting path never fetched the message's bytes.


async def test_a_threaded_message_from_an_unregistered_sender_opens_a_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 §1's 2.1 case: the thread binds the company, the sender is not
    its registered channel, and nothing of the message is read or stored.

    The chain's company is the one the stored outbound exchange's thread
    names. The message is neither quarantined nor ingested — it opens an
    ``awaiting_registered_channel`` case whose ask goes back to the sender,
    and because the ask quotes envelope facts only, the body is **never
    fetched**: the provider observation below is the proof.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<original@client.example>",
        body=f"Please file this. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 1
    assert body["ingested"] == 0
    assert body["skipped_known"] == 0
    # The report's arithmetic has no third bucket: a message that was not
    # ingested is (today) in ``refused``, whatever the awaiting outcome says.
    assert body["refused"] == 1
    (outcome,) = body["outcomes"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["reason"] is None
    assert outcome["company_id"] == company_id  # the chain's company, from the thread
    assert outcome["attribution_case_id"] is not None
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    assert outcome["raw_document_id"] is None

    rows = await stored_cases(session)
    assert len(rows) == 1
    case = rows[0]
    assert str(case.id) == outcome["attribution_case_id"]
    assert case.tenant_id == tenant_id
    assert str(case.company_id) == company_id
    assert case.mailbox_message_id == message_id
    assert case.sender_address == "stranger@elsewhere.example"
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert case.ask_kind == AttributionAskKind.REGISTERED_CHANNEL.value
    assert case.ask_sent_at is None  # opening writes the row; the enqueue stamps it
    assert case.thread_id == f"thread-{message_id}"
    assert case.original_rfc_message_id == "<original@client.example>"
    assert case.resolved_communication_id is None
    assert case.confirmation_communication_id is None
    assert case.closed_at is None
    assert case.dismissed_at is None

    # Metadata only: one provider read, and it was the envelope — the raw
    # message was never fetched, so neither the body nor the attachment could
    # have been read, let alone stored. Nothing was: the only document and the
    # only communication in the tenant are the seeded outbound letter behind
    # the thread evidence — no document and no inbound Communication came from
    # the cycle, and no quarantine row either.
    assert google.message_fetches == [message_id]
    assert google.raw_fetches == []
    assert [row.id for row in await stored_documents(session)] == [letter.raw_document_id]
    assert await stored_inbound_communications(session) == []
    assert await stored_quarantines(session) == []
    assert store.objects == {}

    # The case's opening is audited once, through the single write path — the
    # case id, the sender and the status, never a fragment of the mail.
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.ATTRIBUTION_CASE_OPENED.value
    assert entries[-1].entity_type == "attribution_case"
    assert entries[-1].entity_id == case.id
    assert entries[-1].company_id == UUID(company_id)
    assert entries[-1].actor_user_id is None  # a system decision, not staff
    assert entries[-1].details == {
        "case_id": str(case.id),
        "sender_address": "stranger@elsewhere.example",
        "status": AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value,
    }
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    # The control: the markers really are in the message the provider holds —
    # read straight off the fake's store, never through a fetch — so the
    # searches above are looking at a message that had something to leak. The
    # attachment travels as its base64 body inside the raw message, which is
    # why its marker is checked in that form.
    raw = google.raw_of(message_id)
    assert BODY_MARKER.encode() in raw
    assert base64.b64encode(ATTACHMENT_MARKER) in raw

    # The cursor's overlap re-reads the message — and the provider message id
    # is the idempotency key here too: the second examination finds the case
    # the unique index holds (the insert's refusal is answered, not raised),
    # writes no second row and no second audit entry.
    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    assert second.json()["outcomes"][0]["attribution_case_id"] == str(case.id)
    assert second.json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    )
    assert len(await stored_cases(session)) == 1
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_CASE_OPENED.value
    ) == 1
    assert google.raw_fetches == []


async def test_a_registered_channel_on_copy_is_asked_to_confirm(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boss-on-copy case: a registered channel among the To/Cc addresses.

    The sender is not a registered channel of the chain's company, but one of
    the message's recipients is — so the case awaits that channel's
    confirmation (``awaiting_confirmation``, ask kind ``confirmation``), and
    the message is still never read.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id="<original@client.example>",
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.AWAITING_CONFIRMATION.value
    assert outcome["company_id"] == company_id
    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.ask_kind == AttributionAskKind.CONFIRMATION.value
    assert case.ask_sent_at is None
    assert case.sender_address == "assistant@elsewhere.example"
    assert google.raw_fetches == []
    assert [row.id for row in await stored_documents(session)] == [letter.raw_document_id]
    assert await stored_inbound_communications(session) == []


async def test_a_sender_registered_to_another_company_is_not_a_channel_of_the_chain(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R1: the registration that matters is the chain company's.

    CONTACT is registered against the second company only, and the message's
    ``In-Reply-To`` carries back the first company's stored exchange (the
    header signal, with no provider thread on either side) — so the sender is
    not a registered channel of the chain: the ask flow runs, and the message
    is never auto-attributed to the company the address *is* registered with.
    """
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 7654321] Annual Return Confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<original@client.example>",
        in_reply_to="<request-a@tcsp.example>",
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, first = await ingest_deployment(
        session, store, monkeypatch, google, register_contact=False
    )
    second = make_company(client, br_number="87654321", cr_number="7654321")
    add_registered_contact(client, second, email=CONTACT)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(first),
        mailbox_message_id="<request-a@tcsp.example>",
    )

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["company_id"] == first
    assert outcome["quarantine_id"] is None
    (case,) = await stored_cases(session)
    assert str(case.company_id) == first
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert case.ask_kind == AttributionAskKind.REGISTERED_CHANNEL.value
    assert case.original_rfc_message_id == "<original@client.example>"
    # The message is not read (metadata only), not stored, not quarantined —
    # and not attributed to the company the address is registered with.
    assert google.raw_fetches == []
    assert [row.id for row in await stored_documents(session)] == [letter.raw_document_id]
    assert await stored_inbound_communications(session) == []
    assert await stored_quarantines(session) == []


async def test_a_registered_sender_on_the_chains_thread_is_attributed_to_the_chain(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The address-and-thread agreement fixes an ambiguous sender's company.

    CONTACT reaches two companies, so an address-only rule would need the
    message's own words to choose. The thread already answers: the message
    rides the first company's stored exchange, and the subject — which names
    the *second* company — is never weighed. The message is processed as the
    chain's company and no resolver decision is reported, because none was
    made.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(
        google,
        subject="Re: [Synthetic Harbour Limited — CR 7654321] Annual Return Confirmation",
    )
    client, tenant_id, first, _second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(first),
        thread_id=f"thread-{message_id}",
    )

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["company_id"] == first  # the thread, not the subject (`second`)
    assert outcome["resolution"] is None  # the resolver never ran
    assert outcome["attribution_case_id"] is None
    assert await stored_cases(session) == []
    assert await stored_quarantines(session) == []
    assert await stored_assessments(session) == []
    (record,) = await stored_inbound_communications(session)
    assert str(record.company_id) == first
    assert record.confirmation_request_id is None  # the chain links no request
    assert google.raw_fetches == [message_id]
    # The seeded letter, the raw message and its attachment — and nothing else.
    documents = await stored_documents(session)
    assert len(documents) == 3
    assert letter.raw_document_id in {row.id for row in documents}


async def test_a_thread_that_names_two_companies_binds_neither(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R1's exactly-one rule, at the chain: ambiguity is not a guess.

    The stored thread carries exchanges for two companies, so it names no
    single chain — and the message falls back to today's flow: an unregistered
    sender is quarantined, and no case is opened for a company the evidence
    does not single out.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    second = make_company(client, br_number="87654321", cr_number="7654321")
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(second),
        thread_id=f"thread-{message_id}",
    )

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert outcome["quarantine_id"] is not None
    assert outcome["attribution_case_id"] is None
    assert await stored_cases(session) == []
    assert len(await stored_quarantines(session)) == 1
    assert await stored_inbound_communications(session) == []


async def test_a_message_with_no_thread_evidence_keeps_todays_flow(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No thread of its own: the chain does not exist, stored exchanges or not.

    The tenant has outbound rows, but this message's provider thread and
    headers name none of them, so the address-only decision stands — an
    unregistered sender is quarantined exactly as before, with no case.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id="thread-somewhere-else",
        mailbox_message_id="<request-elsewhere@tcsp.example>",
    )

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert outcome["attribution_case_id"] is None
    assert await stored_cases(session) == []
    assert len(await stored_quarantines(session)) == 1
    # The message itself was only ever read as an envelope, as every refusal
    # on this path is.
    assert google.message_fetches == [message_id]
    assert google.raw_fetches == []


async def test_a_retry_opens_the_case_for_a_message_the_cycle_quarantined(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §2.5 retry re-examines through the same order — including a message
    whose thread now has a chain.

    The first cycle quarantined the sender as unregistered; an outbound
    exchange on the message's thread appeared since (a request went out, or
    arrived into the fake's mailbox), so the retry meets the chain and opens
    the case. The quarantine row stays pending — the ask flow is not the
    retry's remedy to close — and the outcome names the new case.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    assert refused["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    quarantine_id = str(refused["quarantine_id"])
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == QuarantineStatus.PENDING.value
    outcome = body["outcome"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["company_id"] == company_id
    (case,) = await stored_cases(session)
    assert outcome["attribution_case_id"] == str(case.id)
    assert case.mailbox_message_id == message_id
    assert google.raw_fetches == []
    assert [row.id for row in await stored_documents(session)] == [letter.raw_document_id]
    assert await stored_inbound_communications(session) == []


async def test_open_case_refuses_an_action_that_opens_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the two asks open cases; ``plain``/``process`` are caller bugs.

    The decision's other two actions mean "run today's flow" and "the chain
    attributed it" — neither has a case to keep — so the seam refuses rather
    than writing a row the decision never chose.
    """
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google)
    _client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )

    with pytest.raises(ValueError, match="open_case is for the two asks"):
        await attribution.open_case(
            session,
            tenant_id=tenant_id,
            company_id=UUID(company_id),
            mailbox_message_id="msg-not-asked",
            sender_address=CONTACT,
            action=AttributionAction.PROCESS,
        )

    assert await stored_cases(session) == []


async def test_the_cycle_enqueues_the_ask_for_a_threaded_unregistered_sender(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 §1's ask, end to end through the cycle: the case opens *and*
    its ask rides the outbox — the bilingual letter stored as the records
    registry's ``generated_draft``, the request-less ``attribution_ask`` row
    pending and send-on-release, the case stamped, one
    ``attribution_ask_enqueued`` entry — while the unregistered original's
    body is still never fetched (ruling R2)."""
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<original@client.example>",
        body=f"Please file this. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    (case,) = await stored_cases(session)
    assert case.ask_sent_at is not None
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value

    # The ask's row: request-less, its case in the request's place, and
    # send-on-release (only the mode-free pending index paces it).
    (row,) = await stored_outbox(session)
    assert row.kind == "attribution_ask"
    assert row.attribution_case_id == case.id
    assert row.confirmation_request_id is None
    assert row.status == "pending"
    assert row.not_before is None
    assert row.letter_document_id is not None
    document = await session.get(Document, row.letter_document_id)
    assert document is not None
    assert document.kind == "generated_draft"
    assert document.origin == "generated"
    assert document.retention_class == "client_communication"
    assert document.content_type == "message/rfc822"
    assert document.company_id == UUID(company_id)

    # The letter: from the deployment's mailbox, back to the sender who is not
    # a registered channel, threaded on the original's RFC Message-ID, naming
    # the company by name and number in both language halves (scope §7).
    raw = store.objects[document.storage_key]
    message = message_from_bytes(raw, policy=default_policy)
    assert str(message["From"]) == MAILBOX
    assert str(message["To"]) == "stranger@elsewhere.example"
    assert str(message["In-Reply-To"]) == "<original@client.example>"
    assert str(message["References"]) == "<original@client.example>"
    assert str(message["Subject"]) == ask_subject(
        company_name=COMPANY_NAME,
        cr_number="1234567",
        br_number="12345678",
        kind=AttributionAskKind.REGISTERED_CHANNEL,
    )
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    # The stored .eml is the transmitted form (policy.SMTP's CRLF endings);
    # the domain draft is written with plain newlines.
    assert body.get_content() == registration_ask_letter(
        company_name=COMPANY_NAME, cr_number="1234567", br_number="12345678"
    ).replace("\n", "\r\n")

    # Ruling R2 still holds: the ask quotes envelope facts, so the original's
    # bytes were never fetched — and the only documents are the seeded letter
    # and the ask's own.
    assert google.raw_fetches == []
    assert {row.id for row in await stored_documents(session)} == {
        letter.raw_document_id,
        document.id,
    }
    assert await stored_inbound_communications(session) == []

    entries = await audit_entries(session, tenant_id)
    assert [entry.action for entry in entries][-3:] == [
        AuditAction.ATTRIBUTION_CASE_OPENED.value,
        AuditAction.DOCUMENT_REGISTERED.value,
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value,
    ]
    enqueued = entries[-1]
    assert enqueued.entity_type == "attribution_case"
    assert enqueued.entity_id == case.id
    assert enqueued.actor_user_id is None
    assert enqueued.details["case_id"] == str(case.id)
    assert enqueued.details["ask_kind"] == "registered_channel"
    assert enqueued.details["outbox_id"] == str(row.id)
    assert enqueued.details["met_pending_row"] is False
    assert BODY_MARKER not in response.text
    assert BODY_MARKER not in str(enqueued.details)

    # The cursor's overlap re-reads the message: the case stands, the ask is
    # already out (the suppression read answers with its own case), and
    # neither a second row nor a second enqueue entry appears.
    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    assert len(await stored_cases(session)) == 1
    assert len(await stored_outbox(session)) == 1
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ) == 1
    assert google.raw_fetches == []


async def test_a_second_message_on_an_asked_thread_opens_its_case_without_an_ask(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R3's single-ask: two messages on one Gmail conversation from the
    unregistered sender — the first opens its case and asks, the second opens
    its own case and gets no ask. One row, one enqueue entry, two open cases
    (one suppressed, visible to staff with ``ask_sent_at`` NULL)."""
    google = FakeGoogle(address=MAILBOX)
    first = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<first@client.example>",
        body=f"Please file this. {BODY_MARKER}",
    )
    second = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:40:00 +0800",
        message_id="<second@client.example>",
        body=f"And this too. {BODY_MARKER}",
    )
    # A Gmail conversation shares one thread id; the fake names a message's
    # thread after itself, so the second reply is put on the first's thread
    # here — the provider's own grouping, which the cycle reads verbatim.
    google.messages[-1]["threadId"] = f"thread-{first}"
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{first}",
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
    assert suppressed[0].mailbox_message_id == second
    (row,) = await stored_outbox(session)
    assert row.attribution_case_id == asked[0].id
    assert google.raw_fetches == []


async def test_the_cycle_keeps_the_ask_off_for_auto_submitted_and_null_return_path(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R4's anti-abuse guards, through the real envelope readers: a
    message that announces itself automated (``Auto-Submitted``) and one whose
    ``Return-Path`` carries no address (``<>``, where bounces go) each open
    their case and get no ask — no letter stored, no row, nothing stamped."""
    google = FakeGoogle(address=MAILBOX)
    automated = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Mailbot <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<auto@client.example>",
        auto_submitted="auto-replied",
        body=BODY_MARKER,
    )
    bounced = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:32:00 +0800",
        message_id="<bounce@client.example>",
        return_path="<>",
        body=BODY_MARKER,
    )
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    first_letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{automated}",
    )
    second_letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{bounced}",
    )

    outcomes = client.post(INGEST_URL).json()["outcomes"]

    assert [item["outcome"] for item in outcomes] == [
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
    ]
    cases = await stored_cases(session)
    assert len(cases) == 2
    assert all(case.ask_sent_at is None for case in cases)
    assert await stored_outbox(session) == []
    assert {row.id for row in await stored_documents(session)} == {
        first_letter.raw_document_id,
        second_letter.raw_document_id,
    }
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ) == 0
    assert google.raw_fetches == []


async def test_an_overlap_re_read_of_a_settled_case_asks_nothing(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settled case is not reopened by the cursor's overlap: the re-read
    meets the same provider message id, ``open_case`` returns the row as it
    stands, and no second ask, letter, stamp or enqueue entry appears."""
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<original@client.example>",
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        thread_id=f"thread-{message_id}",
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    )
    (case,) = await stored_cases(session)
    assert case.ask_sent_at is not None
    stamped_at = case.ask_sent_at
    (ask_row,) = await stored_outbox(session)
    documents = {row.id for row in await stored_documents(session)}

    await set_status(
        session,
        tenant_id=tenant_id,
        case_id=case.id,
        status=AttributionCaseStatus.RESOLVED,
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    assert second.json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    )
    assert [row.id for row in await stored_cases(session)] == [case.id]
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.ask_sent_at == stamped_at
    assert [row.id for row in await stored_outbox(session)] == [ask_row.id]
    assert {row.id for row in await stored_documents(session)} == documents
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ) == 1
    assert google.raw_fetches == []


# A ``Message-ID`` token longer than the anchor column can hold
# (``attribution_cases.original_rfc_message_id`` is ``String(320)``): no case
# may be written under it, and the cycle must not be refused by its insert.
OVER_LONG_RFC_ID = "<" + "a" * 400 + "@client.example>"


async def test_an_over_long_message_id_is_never_used_as_the_case_anchor(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token the anchor column cannot hold is not an anchor.

    ``attribution_cases.original_rfc_message_id`` is 320 characters wide; a
    longer ``Message-ID`` would refuse the case's insert with a ``DataError``
    the cycle does not answer — the transaction would abort and the cursor
    would never advance over the message, so the mailbox would be stuck on it
    for ever. The token is bounded away at the envelope (the provider thread
    is this message's other anchor), the case opens with no RFC anchor, and
    the cycle completes over the message.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id=OVER_LONG_RFC_ID,
        in_reply_to="<request-a@tcsp.example>",
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id="<request-a@tcsp.example>",
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    (outcome,) = response.json()["outcomes"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    (case,) = await stored_cases(session)
    assert case.thread_id == f"thread-{message_id}"
    assert case.original_rfc_message_id is None
    assert case.mailbox_message_id == message_id
    # The cycle finished over the message rather than aborting on it: the
    # cursor is the message's own receipt time, and the overlap's re-read is
    # the ordinary id check.
    state = await session.get(MailIngestionState, tenant_id)
    assert state is not None
    assert state.cursor_received_at == google.received_base
    second = client.post(INGEST_URL)
    assert second.status_code == 200, second.text
    assert len(await stored_cases(session)) == 1


# --- The confirmation act and the original (ADR-0019 §1; rulings R5-R7) ----
#
# The confirming reply, the original it authorises and the closure a
# registered channel's own message brings to a 2.1 case. This is where the
# unregistered sender's bytes are read for the first time (ruling R2's
# "before the confirmation" ends here), so every scenario asserts what the
# provider was asked for — which message, in which format — beside the rows
# and entries the cycle wrote.

CONFIRM_SUBJECT = "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"
OUTBOUND_RFC_ID = "<request-a@tcsp.example>"
ORIGINAL_RFC_ID = "<original@client.example>"
CONFIRM_RFC_ID = "<confirmation@client.example>"
FOLLOW_UP_RFC_ID = "<follow-up@client.example>"


def outcome_of(report: dict, message_id: str) -> dict:
    """A cycle report's outcome for one provider message id."""
    matches = [item for item in report["outcomes"] if item["mailbox_message_id"] == message_id]
    assert len(matches) == 1, f"expected one outcome for {message_id}, found {len(matches)}"
    return matches[0]


async def entries_of(
    session: AsyncSession, tenant_id: UUID, action: AuditAction
) -> list[AuditLog]:
    """The tenant's audit entries for one action, in chain order."""
    return [
        entry for entry in await audit_entries(session, tenant_id) if entry.action == action.value
    ]


async def communication_of(session: AsyncSession, mailbox_message_id: str) -> Communication:
    """One Communication by the provider message id that keys it."""
    row = await session.scalar(
        select(Communication).where(Communication.mailbox_message_id == mailbox_message_id)
    )
    assert row is not None, f"no Communication for {mailbox_message_id}"
    return row


async def test_a_confirmation_processes_the_original_and_records_the_act(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5/R6 end to end: the registered channel's ``確認`` on the thread stores
    the confirming exchange, authorises the original — ingested for the first
    time, with its raw document and attachment — resolves the case to that
    exchange and cancels the ask the case queued.

    The confirming message itself is stored as ``attribution_confirmation``,
    its outcome is ``attribution_confirmed``, and the overlap's replay is a
    no-op: the provider message ids are the idempotency keys throughout.
    """
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )

    first = client.post(INGEST_URL)

    assert first.status_code == 200, first.text
    assert outcome_of(first.json(), original)["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.ask_sent_at is not None
    assert case.confirmation_communication_id is None
    (ask_row,) = await stored_outbox(session)
    # Ruling R2 through this point: the assistant's bytes were never fetched.
    assert google.raw_fetches == []

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="確認",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    act = outcome_of(second.json(), confirmation)
    assert act["outcome"] == IngestOutcome.ATTRIBUTION_CONFIRMED.value
    assert act["reason"] is None
    assert act["company_id"] == company_id
    assert act["communication_id"] is not None
    assert act["quarantine_id"] is None
    assert act["attribution_case_id"] is None
    assert act["scan_status"] == ScanStatus.CLEAN.value
    # The original's own re-examination (the cursor's overlap) still reported
    # the await: the case was resolved later in this same cycle.
    assert outcome_of(second.json(), original)["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )

    # The confirming exchange: stored under the normal rules, recorded as the
    # authorising act's message, and threaded where the case's anchor is.
    confirming = await communication_of(session, confirmation)
    assert str(confirming.id) == str(act["communication_id"])
    assert confirming.kind == "attribution_confirmation"
    assert confirming.direction == "inbound"
    assert confirming.sender_address == CONTACT
    assert confirming.company_id == UUID(company_id)
    confirming_document = await session.get(Document, confirming.raw_document_id)
    assert confirming_document is not None
    assert confirming_document.kind == DocumentKind.CLIENT_REPLY.value
    assert confirming_document.scan_status == ScanStatus.CLEAN.value
    assert store.objects[confirming_document.storage_key] == google.raw_of(confirmation)

    # The original, processed for the first time: its own exchange, its raw
    # document and its attachment, all in the chain's company.
    processed = await communication_of(session, original)
    assert processed.kind == "reply"
    assert processed.sender_address == "assistant@elsewhere.example"
    assert processed.company_id == UUID(company_id)
    processed_document = await session.get(Document, processed.raw_document_id)
    assert processed_document is not None
    assert store.objects[processed_document.storage_key] == google.raw_of(original)
    assert BODY_MARKER.encode() in store.objects[processed_document.storage_key]

    # The case: resolved to the original's exchange, the act's message pinned
    # to it, and the ask it queued cancelled (a resolved case is never asked).
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.resolved_communication_id == processed.id
    assert case.confirmation_communication_id == confirming.id
    cancelled = await session.get(MailOutbox, ask_row.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_RESOLVE

    # One entry names the act: the company, the stored confirming exchange and
    # the case it resolved (with the exchange the original became).
    (entry,) = await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)
    assert entry.company_id == UUID(company_id)
    assert entry.actor_user_id is None
    assert entry.details == {
        "company_id": company_id,
        "confirmation_communication_id": str(confirming.id),
        "cases": [{"case_id": str(case.id), "communication_id": str(processed.id)}],
    }
    # The ingestion tail's own entry still records the confirming message's
    # landing; the act's entry is the authorisation on top of it.
    ingested_ids = [
        entry.entity_id
        for entry in await entries_of(session, tenant_id, AuditAction.MAIL_MESSAGE_INGESTED)
    ]
    assert ingested_ids == [confirming.id, processed.id]
    assert BODY_MARKER not in second.text
    assert "ATTACHMENT-MARKER" not in second.text

    # The provider reads: the confirmation's bytes for the grammar, the
    # original's for its tail — and both exactly once.
    assert google.raw_fetches == [confirmation, original]
    documents = await stored_documents(session)
    assert {row.id for row in documents} >= {
        letter.raw_document_id,
        ask_row.letter_document_id,
        processed_document.id,
        confirming_document.id,
    }
    attachments = [row for row in documents if row.kind == DocumentKind.CLIENT_DOCUMENT.value]
    assert len(attachments) == 1
    assert attachments[0].company_id == UUID(company_id)

    # The replay: both message ids are recorded, so the overlap examines them
    # and writes nothing — one confirming exchange, one resolved case, one
    # act entry, no second fetch.
    third = client.post(INGEST_URL)

    assert third.status_code == 200, third.text
    assert [item["outcome"] for item in third.json()["outcomes"]] == [
        IngestOutcome.SKIPPED_KNOWN.value,
        IngestOutcome.SKIPPED_KNOWN.value,
    ]
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.resolved_communication_id == processed.id
    assert case.confirmation_communication_id == confirming.id
    assert len(await stored_communications(session)) == 3  # letter + two inbound
    assert len(await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)) == 1
    assert google.raw_fetches == [confirmation, original]


async def test_a_confirmation_carrying_a_signature_still_authorises_the_original(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirm grammar reads the first non-empty *line* of the body.

    A real reply carries its signature below the instruction — ``Confirm`` on
    its own line, the sign-off beneath it — and the act is recognised from the
    line, not from a body whose newlines were collapsed into one run of
    prose. The original is processed and the case resolves.
    """
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="Confirm\n\nBest regards,\nAlice Chan, Director",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    act = outcome_of(second.json(), confirmation)
    assert act["outcome"] == IngestOutcome.ATTRIBUTION_CONFIRMED.value
    assert act["company_id"] == company_id
    assert case.status == AttributionCaseStatus.RESOLVED.value
    processed = await communication_of(session, original)
    assert case.resolved_communication_id == processed.id
    assert processed.kind == "reply"


async def test_a_confirmation_below_blanks_and_quoted_history_still_authorises(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leading blank lines and a quoted history below do not hide the act.

    The reply's body opens with blank lines, then the bare ``Confirm``, then
    the quoted exchange it answers. The first non-empty line is the act, so
    the original is processed — a read that collapsed the newlines would see
    one long line that is not the grammar.
    """
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body=(
            "\n\nConfirm\n\n"
            "On Tue, 23 Sep 2026 at 09:31, AI Filings <ai-filings@tcsp.example> wrote:\n"
            "> Please confirm the message we received on your behalf.\n"
        ),
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    act = outcome_of(second.json(), confirmation)
    assert act["outcome"] == IngestOutcome.ATTRIBUTION_CONFIRMED.value
    assert case.status == AttributionCaseStatus.RESOLVED.value
    processed = await communication_of(session, original)
    assert case.resolved_communication_id == processed.id


async def test_a_confirm_buried_in_prose_does_not_authorise_the_original(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5's grammar is one bare first line: a reply that merely mentions the
    word is processed as an ordinary registered-channel message, and the
    original stays where it was — unanswered, never silently authorised."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)

    hedged = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="Please confirm the attached.\n\n確認",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    report = outcome_of(second.json(), hedged)
    assert report["outcome"] == IngestOutcome.INGESTED.value
    assert report["attribution_case_id"] is None
    # The message is an ordinary reply of the registered channel's; the case
    # it rode is untouched and the original was never read.
    recorded = await communication_of(session, hedged)
    assert recorded.kind == "reply"
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.confirmation_communication_id is None
    assert (
        await session.scalar(
            select(Communication).where(Communication.mailbox_message_id == original)
        )
    ) is None
    assert await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED) == []
    assert google.raw_fetches == [hedged]


async def test_a_confirmation_from_an_unregistered_address_does_not_authorise(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5's first half is authority, not words: the same ``確認`` from an
    address that is no registered channel of the chain's company opens its own
    case instead, and the original is not processed."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)

    stranger = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="確認",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    report = outcome_of(second.json(), stranger)
    assert report["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert report["attribution_case_id"] is not None
    cases = await stored_cases(session)
    assert len(cases) == 2
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.confirmation_communication_id is None
    assert (
        await session.scalar(
            select(Communication).where(Communication.mailbox_message_id == original)
        )
    ) is None
    assert await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED) == []
    # The 2.1 path still fetches nothing: the stranger's message only decided.
    assert google.raw_fetches == []


async def test_a_registered_channels_message_closes_the_registration_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R7: a registered channel's own message on the thread is the
    remedy the 2.1 ask asked for — the case closes, its queued ask is
    cancelled, one ``attribution_case_closed`` entry names it — and the
    message itself is processed as any registered channel's message is."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    )
    (case,) = await stored_cases(session)
    (ask_row,) = await stored_outbox(session)

    follow_up = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=FOLLOW_UP_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body=f"Here are the details. {BODY_MARKER}",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    report = outcome_of(second.json(), follow_up)
    assert report["outcome"] == IngestOutcome.INGESTED.value
    assert report["company_id"] == company_id
    recorded = await communication_of(session, follow_up)
    assert recorded.kind == "reply"
    assert recorded.company_id == UUID(company_id)

    assert case.status == AttributionCaseStatus.CLOSED.value
    assert case.closed_at is not None
    assert case.resolved_communication_id is None
    cancelled = await session.get(MailOutbox, ask_row.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_CLOSE
    (entry,) = await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CASE_CLOSED)
    assert entry.company_id == UUID(company_id)
    assert entry.actor_user_id is None
    assert entry.details == {"case_ids": [str(case.id)]}
    assert await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED) == []
    # The remedy is the follow-up itself: the parked original is closed, not
    # processed — it is never read and never recorded.
    assert (
        await session.scalar(
            select(Communication).where(Communication.mailbox_message_id == original)
        )
    ) is None
    # The follow-up is not a confirmation: nothing was read for the grammar,
    # and the message's own tail read it once.
    assert google.raw_fetches == [follow_up]

    # A second registered-channel message on the thread closes nothing more:
    # the case has left the awaits.
    another = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:20:00 +0800",
        to=MAILBOX,
        message_id="<another@client.example>",
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body=f"One more detail. {BODY_MARKER}",
    )
    third = client.post(INGEST_URL)

    assert third.status_code == 200, third.text
    assert outcome_of(third.json(), another)["outcome"] == IngestOutcome.INGESTED.value
    assert len(await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CASE_CLOSED)) == 1
    assert case.status == AttributionCaseStatus.CLOSED.value


async def test_the_gate_denial_leaves_the_case_awaiting_and_the_act_recorded(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §5.1 gate applies to storing the confirming reply and to processing
    the original, each on its own: denied, nothing is stored and the case stays
    awaiting for staff — while the act itself is still recorded, and its
    outcome still says what the message was."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google, covered=False
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    letter = await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_CONFIRMATION.value
    )
    (case,) = await stored_cases(session)
    assert await stored_outbox(session) == []  # the ask is gated too

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="確認",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    act = outcome_of(second.json(), confirmation)
    assert act["outcome"] == IngestOutcome.ATTRIBUTION_CONFIRMED.value
    assert act["reason"] == GatingReason.NO_ASSIGNMENT.value
    assert act["communication_id"] is None
    assert act["raw_document_id"] is None
    assert act["company_id"] == company_id

    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.confirmation_communication_id is None
    assert case.resolved_communication_id is None
    assert await stored_inbound_communications(session) == []
    assert (
        await session.scalar(
            select(Communication).where(Communication.mailbox_message_id == original)
        )
    ) is None
    assert {row.id for row in await stored_documents(session)} == {letter.raw_document_id}
    (entry,) = await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)
    assert entry.details == {
        "company_id": company_id,
        "confirmation_communication_id": None,
        "cases": [{"case_id": str(case.id), "communication_id": None}],
    }
    assert google.raw_fetches == [confirmation]


async def test_a_denied_confirmation_leaves_the_21_case_awaiting_for_staff(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling M2 (over R7's ordering): the confirming act has *attempted* every
    open awaiting case on the thread, so the 2.1 closure stays off its path —
    a case whose processing the gate denied stays awaiting for staff, where
    T7's processing route would refuse to look at a closed one and the parked
    original would be stranded."""
    google = FakeGoogle(address=MAILBOX)
    first_original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id="<first@client.example>",
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    second_original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:40:00 +0800",
        to=MAILBOX,
        message_id="<second@client.example>",
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"And this one too. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(
        session, store, monkeypatch, google, covered=False
    )
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )

    opened = client.post(INGEST_URL).json()["outcomes"]

    assert [item["outcome"] for item in opened] == [
        IngestOutcome.AWAITING_CONFIRMATION.value,
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
    ]
    cases = await stored_cases(session)
    assert {case.status for case in cases} == {
        AttributionCaseStatus.AWAITING_CONFIRMATION.value,
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value,
    }

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to="<second@client.example>",
        references=f"{OUTBOUND_RFC_ID} <first@client.example> <second@client.example>",
        body="確認",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    act = outcome_of(second.json(), confirmation)
    assert act["outcome"] == IngestOutcome.ATTRIBUTION_CONFIRMED.value
    assert act["reason"] == GatingReason.NO_ASSIGNMENT.value

    # Both cases were attempted and the gate denied both: both stay exactly
    # where the act found them, and no closure entry exists — the 2.1 case was
    # not closed out from under the staff who can still process it.
    assert {case.status for case in cases} == {
        AttributionCaseStatus.AWAITING_CONFIRMATION.value,
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value,
    }
    assert all(case.closed_at is None for case in cases)
    assert all(case.resolved_communication_id is None for case in cases)
    assert await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CASE_CLOSED) == []
    (entry,) = await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)
    assert entry.details["cases"] == [
        {"case_id": str(case.id), "communication_id": None} for case in cases
    ]
    assert await stored_inbound_communications(session) == []
    assert (
        await session.scalar(
            select(Communication).where(
                Communication.mailbox_message_id.in_([first_original, second_original])
            )
        )
    ) is None


async def test_a_confirmation_authorises_every_open_case_on_the_thread(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5's authorisation is the anchor, not one case: two parked originals on
    the thread — one waiting for a confirmation, one waiting for a registered
    channel — are both processed by the one confirmation, each resolved to its
    own exchange, and one act entry names both."""
    google = FakeGoogle(address=MAILBOX)
    first_original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id="<first@client.example>",
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    second_original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:40:00 +0800",
        to=MAILBOX,
        message_id="<second@client.example>",
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"And this one too. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )

    outcomes = client.post(INGEST_URL).json()["outcomes"]

    assert [item["outcome"] for item in outcomes] == [
        IngestOutcome.AWAITING_CONFIRMATION.value,
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value,
    ]
    cases = await stored_cases(session)
    assert {case.status for case in cases} == {
        AttributionCaseStatus.AWAITING_CONFIRMATION.value,
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value,
    }

    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 10:05:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to="<second@client.example>",
        references=f"{OUTBOUND_RFC_ID} <first@client.example> <second@client.example>",
        body="Confirm",
    )

    second = client.post(INGEST_URL)

    assert second.status_code == 200, second.text
    assert outcome_of(second.json(), confirmation)["outcome"] == (
        IngestOutcome.ATTRIBUTION_CONFIRMED.value
    )
    assert {case.status for case in cases} == {AttributionCaseStatus.RESOLVED.value}
    first_processed = await communication_of(session, first_original)
    second_processed = await communication_of(session, second_original)
    assert {case.resolved_communication_id for case in cases} == {
        first_processed.id,
        second_processed.id,
    }
    (entry,) = await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)
    assert entry.details["cases"] == [
        {"case_id": str(case.id), "communication_id": str(case.resolved_communication_id)}
        for case in cases
    ]


async def test_one_cycle_settles_the_case_it_opened_in_the_same_window(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst needs no second read: the cycle examines the parked original,
    opens its case and queues its ask, then meets the registered channel's
    confirmation behind it — and settles everything on the row it wrote
    moments earlier in the same transaction (flush, not commit)."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Assistant <assistant@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        cc=f"CHAN TAI MAN <{CONTACT}>",
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
    )
    confirmation = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:40:00 +0800",
        to=MAILBOX,
        message_id=CONFIRM_RFC_ID,
        in_reply_to=ORIGINAL_RFC_ID,
        references=f"{OUTBOUND_RFC_ID} {ORIGINAL_RFC_ID}",
        body="確認",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )

    report = client.post(INGEST_URL).json()

    assert [item["outcome"] for item in report["outcomes"]] == [
        IngestOutcome.AWAITING_CONFIRMATION.value,
        IngestOutcome.ATTRIBUTION_CONFIRMED.value,
    ]
    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.confirmation_communication_id is not None
    assert case.resolved_communication_id is not None
    assert await communication_of(session, original) is not None
    (ask_row,) = await stored_outbox(session)
    assert ask_row.status == "cancelled"
    assert ask_row.last_error == attribution.ASK_CANCELLED_ON_RESOLVE
    assert len(await entries_of(session, tenant_id, AuditAction.ATTRIBUTION_CONFIRMED)) == 1
    assert google.raw_fetches == [confirmation, original]


async def test_a_staff_pressed_processing_acts_as_the_pressing_member(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``process_case`` carries the actor a forced processing was pressed by
    (the case surface's route): the original's documents and its ingest entry
    name that member rather than the system actor a cycle runs as."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject=CONFIRM_SUBJECT,
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to=MAILBOX,
        message_id=ORIGINAL_RFC_ID,
        in_reply_to=OUTBOUND_RFC_ID,
        body=f"Please file this. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    name_company(client, company_id, name_en=COMPANY_NAME)
    await seed_outbound(
        session,
        tenant_id=tenant_id,
        company_id=UUID(company_id),
        mailbox_message_id=OUTBOUND_RFC_ID,
    )
    assert client.post(INGEST_URL).json()["outcomes"][0]["outcome"] == (
        IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    )
    (case,) = await stored_cases(session)
    admin = await session.scalar(select(User).where(User.tenant_id == tenant_id))
    assert admin is not None

    report = await attribution.process_case(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant_id,
        case=case,
        actor_user_id=admin.id,
        now=datetime.now(UTC),
    )

    assert report.resolved is True
    assert report.communication_id is not None
    entries = await audit_entries(session, tenant_id)
    ingested = [
        entry for entry in entries if entry.action == AuditAction.MAIL_MESSAGE_INGESTED.value
    ]
    assert [entry.entity_id for entry in ingested] == [report.communication_id]
    assert ingested[0].actor_user_id == admin.id
    registered = [
        entry for entry in entries if entry.action == AuditAction.DOCUMENT_REGISTERED.value
    ]
    actors = [entry.actor_user_id for entry in registered]
    # The raw message and its attachment are this press's; the ask letter the
    # first cycle registered is the system's, and is the only other entry.
    assert actors.count(admin.id) == 2
    assert actors.count(None) == 1
    assert (await communication_of(session, original)).id == report.communication_id


# --- The resolver's decision, as a report carries it ---------------------


async def test_a_quarantined_multi_company_message_reports_the_rules_decision(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the rule could not name a company, as the reviewer reads it.

    The §2.7 assessment is the record; this is the same decision on the wire —
    the case it reached, the evidence tier, the score, the band it routes to,
    and the candidates it considered — so staff pressing retry are told what
    the rule saw, not only what it could not do.
    """
    google = FakeGoogle(address=MAILBOX)
    # A body naming nothing decisive: ``no_match`` — the quarantine road,
    # unchanged by workstream ③ (the decisive several-match beside it is the
    # split, asserted in
    # ``test_a_multi_company_reply_naming_two_companies_splits``).
    message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body=f"Please see the attached signed form. {BODY_MARKER}",
    )
    client, tenant_id, _first, _second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    expected = {
        "case": "no_match",
        "evidence_tier": None,
        "score": 0.0,
        "routing": "client_clarification",
        "candidate_company_ids": [],
    }

    refused = client.post(INGEST_URL).json()["outcomes"][0]

    assert refused["outcome"] == IngestOutcome.AMBIGUOUS_SENDER.value
    assert refused["resolution"] == expected
    quarantine_id = str(refused["quarantine_id"])

    retried = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert retried.status_code == 200, retried.text
    # The retry re-runs the decision through the cycle's own order, so it
    # reports the same answer for the same message.
    assert retried.json()["outcome"]["resolution"] == expected
    # And the audit entry the refusal wrote carries the same projection: one
    # code path builds both, so they cannot disagree about what the rule saw.
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_MESSAGE_QUARANTINED.value
    assert entries[-1].details["resolution"] == {
        "case": "no_match",
        "evidence": None,
        "score": 0.0,
        "routing": "client_clarification",
        "candidates": [],
    }


async def test_a_resolved_message_whose_bytes_are_not_a_message_still_reports_the_decision(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field says whether the resolver ran, not whether anything landed.

    The subject the *provider* returned names the company, so the rule decides
    on it — and the message's own bytes turn out not to be a message, which the
    document path refuses. The decision is still reported: it was made, and a
    report that dropped it would leave the reader guessing why a message with a
    company was not ingested.
    """
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="[Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        raw=b"not a message at all",
    )
    client, _, first, _ = await multi_company_deployment(session, store, monkeypatch, google)

    outcome = client.post(INGEST_URL).json()["outcomes"][0]

    assert outcome["outcome"] == IngestOutcome.FAILED.value
    assert outcome["reason"] == "invalid_message:content_type_mismatch"
    assert outcome["company_id"] == first
    assert outcome["resolution"] == {
        "case": "resolved",
        "evidence_tier": "cr_exact",
        "score": 1.0,
        "routing": "auto_accept",
        "candidate_company_ids": [first],
    }


async def test_the_matched_text_never_travels_in_a_report(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place a matched fragment lives is the §2.7 signal log.

    Both names under test are ones the rule really matched — a decisive
    several-match, so workstream ③'s split runs — and the assertions below
    can fail: any surface that carried matched text, at any level, would put
    a candidate's words in the cycle report, the wire snippet, or the parent's
    audit entries. Both are searched, not only the marked one, because which
    candidate comes first is the resolver's ordering and a leak does not have
    to be the first one. The split path writes no assessment at all (the
    parent is the record), so the custody bar is higher than the quarantine
    road's: the matched text lives only in the message the provider served
    and the WORM raw the parent stores in the customer's own records registry.
    """
    first_name, second_name = "Peak Trading Limited", f"{MATCHED_TEXT_MARKER} Limited"
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body=f"The return for {first_name} and {second_name} is ready.",
    )
    # The tokenizer numbers the COMPANY label's values longest-first:
    # [COMPANY_01] is the marked name, [COMPANY_02] the plain one.
    spans = [
        {"company": "[COMPANY_02]", "text": "The return for [COMPANY_02] and "},
        {"company": "[COMPANY_01]", "text": "[COMPANY_01] is ready."},
    ]
    client, tenant_id, first, second, _transport = await split_deployment(
        session, store, monkeypatch, google, envelope=_split_envelope(result=spans)
    )
    name_company(client, first, name_en=first_name)
    name_company(client, second, name_en=second_name)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    outcome = response.json()["outcomes"][0]
    assert outcome["outcome"] == IngestOutcome.SPLIT.value
    assert outcome["resolution"]["case"] == "ambiguous"
    # The split path writes no assessment: the parent is the record.
    assert await stored_assessments(session) == []
    # The wire snippet was tokenized: no candidate's name crossed the gateway.
    assert len(google.raw_fetches) == 1

    decisions = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.entity_type == "split_parent"
    ]
    assert decisions != []
    # Neither report, the wire, nor any parent entry carries matched text.
    for matched in (first_name, second_name):
        assert matched not in response.text
        assert matched not in str([entry.details for entry in decisions])
    # The controls: the text really is in the message the provider served, and
    # the rule really did match on it — so the searches above are looking at a
    # decision that had the text to leak.
    assert f"for {first_name} and {second_name} is ready".encode() in google.raw_of(message_id)
    assert outcome["resolution"]["candidate_company_ids"] == sorted([first, second])


# --- The §2.5 review surface ---------------------------------------------


async def test_the_review_list_shows_what_is_waiting_for_staff(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope §2.5's "quarantined for staff review", as the staff read it."""
    google = FakeGoogle(address=MAILBOX)
    older = message_with_attachment(
        google,
        sender="stranger@elsewhere.example",
        subject="Re: Annual return confirmation",
        body=f"Who is this? {BODY_MARKER}",
    )
    newer = message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="Please see the attached signed form.",
    )
    client, tenant_id, _, _ = await multi_company_deployment(session, store, monkeypatch, google)
    assert client.post(INGEST_URL).json()["refused"] == 2
    actions_before = await actions_of(session, tenant_id)

    # Another tenant's row, built directly: this deployment is single-tenant
    # (ADR-0002) and reads only its own.
    other = Tenant(name=f"tenant-{uuid4()}")
    session.add(other)
    await session.flush()
    session.add(
        QuarantinedMessage(
            tenant_id=other.id,
            mailbox_message_id="msg-elsewhere",
            thread_id=None,
            sender_address="someone@elsewhere.example",
            received_at=google.received_base,
            reason=QuarantineReason.UNREGISTERED_SENDER.value,
            status=QuarantineStatus.PENDING.value,
        )
    )
    await session.flush()

    response = client.get(QUARANTINE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    # Newest receipt first — the message that arrived last is the one staff
    # most likely just saw arrive — and the other tenant's row is not here.
    assert [item["mailbox_message_id"] for item in body] == [newer, older]
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
    assert body[0]["thread_id"] == f"thread-{newer}"
    assert body[0]["sender_address"] == CONTACT
    assert datetime.fromisoformat(body[0]["received_at"]) == google.received_base + timedelta(
        minutes=1
    )
    assert body[0]["reason"] == QuarantineReason.MULTIPLE_COMPANIES.value
    assert body[0]["status"] == QuarantineStatus.PENDING.value
    assert body[0]["resolved_at"] is None
    assert body[0]["communication_id"] is None
    assert body[1]["mailbox_message_id"] == older
    assert body[1]["sender_address"] == "stranger@elsewhere.example"
    assert datetime.fromisoformat(body[1]["received_at"]) == google.received_base
    assert body[1]["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    # Reading the review changes nothing (no audit entry, no row touched).
    assert await actions_of(session, tenant_id) == actions_before
    # And the list carries no mail: the markers really are in these messages.
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text


async def test_a_retry_ingests_a_quarantined_message_once_the_sender_is_registered(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry is the staff remedy reaching a message the cycle refused.

    The cursor only moves forward (contract §5.1.7), so a later cycle's window
    never re-examines a refused message: registering the sender's address is
    half of the remedy, and this is the other half.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    assert refused["reason"] == "unregistered_sender"
    quarantine_id = str(refused["quarantine_id"])

    add_registered_contact(client, company_id, email="stranger@elsewhere.example")

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quarantine_id"] == quarantine_id
    assert body["status"] == QuarantineStatus.RESOLVED.value
    assert body["resolved_at"] is not None
    outcome = body["outcome"]
    assert outcome["outcome"] == IngestOutcome.INGESTED.value
    assert outcome["company_id"] == company_id
    assert body["communication_id"] == outcome["communication_id"]
    # The row now points at the record the message became, and the message was
    # ingested like any other: the raw message and its attachment are stored.
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.RESOLVED.value
    assert row.resolved_at is not None
    assert str(row.resolved_communication_id) == outcome["communication_id"]
    records = await stored_communications(session)
    assert len(records) == 1
    assert str(records[0].id) == outcome["communication_id"]
    assert str(records[0].company_id) == company_id
    assert len(await stored_documents(session)) == 2
    # The retry is audited: the ingestion's own entry for the message, then the
    # resolution of the row it closes.
    entries = await audit_entries(session, tenant_id)
    assert [entry.action for entry in entries[-2:]] == [
        AuditAction.MAIL_MESSAGE_INGESTED.value,
        AuditAction.MAIL_QUARANTINE_RESOLVED.value,
    ]
    resolution = entries[-1]
    assert resolution.entity_type == "quarantined_message"
    assert resolution.entity_id == UUID(quarantine_id)
    assert resolution.company_id == UUID(company_id)
    assert resolution.actor_user_id is not None  # the administrator who retried
    assert resolution.details["mailbox_message_id"] == message_id
    assert resolution.details["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert resolution.details["communication_id"] == outcome["communication_id"]
    # Nothing is left to retry, and the retry reads no message: the row's status
    # answers first.
    fetches = list(google.raw_fetches)
    again = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")
    assert again.status_code == 409
    assert google.raw_fetches == fetches
    assert len(await audit_entries(session, tenant_id)) == len(entries)
    # The review no longer lists it: the queue is what is still open, and this
    # row is closed. The closed rows are still readable on request, with the
    # record the message became.
    assert client.get(QUARANTINE_URL).json() == []
    listed = client.get(f"{QUARANTINE_URL}?status=all").json()
    assert listed[0]["status"] == QuarantineStatus.RESOLVED.value
    assert listed[0]["communication_id"] == outcome["communication_id"]
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text


async def test_a_retry_closes_a_row_whose_message_a_cycle_already_ingested(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending row is not a lie: a message already in the flow is resolved.

    The staff remedy can reach the message through the cycle's own trigger
    rather than the retry (an administrator registers the address and runs the
    ingest button), and the cursor's overlap does re-read it. The cycle moves
    no quarantine row — the review surface owns them — so the retry is where
    the row catches up with the message it was waiting for.
    """
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    quarantine_id = str(refused["quarantine_id"])

    add_registered_contact(client, company_id, email="stranger@elsewhere.example")
    assert client.post(INGEST_URL).json()["ingested"] == 1
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.PENDING.value  # the cycle moved nothing
    record = (await stored_communications(session))[0]

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    # The message is already recorded, so this attempt ingested nothing — and
    # the row points at the record it became.
    assert body["outcome"]["outcome"] == IngestOutcome.SKIPPED_KNOWN.value
    assert body["status"] == QuarantineStatus.RESOLVED.value
    assert body["resolved_at"] is not None
    assert body["communication_id"] == str(record.id)
    assert str(row.resolved_communication_id) == str(record.id)
    assert len(await stored_communications(session)) == 1
    assert len(await stored_documents(session)) == 2
    entries = await audit_entries(session, tenant_id)
    assert entries[-1].action == AuditAction.MAIL_QUARANTINE_RESOLVED.value
    assert entries[-1].entity_id == UUID(quarantine_id)
    assert entries[-1].details["communication_id"] == str(record.id)
    # The company comes from the record the message became (a ``skipped_known``
    # outcome carries none of its own), so the entry is still attributable in
    # the per-company compliance report.
    assert entries[-1].company_id == UUID(company_id)


async def test_the_retry_read_of_the_row_takes_the_lock(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping retries must not both close the same row.

    A retry spends a metadata read, a possible raw fetch, a scan and an
    object-store write between reading its row and moving it, so the read is
    where a second request has to be stopped: it takes the row lock, and the
    request that lost the race then reads ``resolved`` and is refused (409)
    instead of appending a second ``mail_quarantine_resolved`` entry to the
    append-only chain.

    The harness cannot stage that overlap: every test runs inside one
    transaction that is rolled back, two async sessions cannot share it, and a
    committed fixture row would stay in the review database. So the mechanism
    is pinned where it can be — the statement the route really emits — rather
    than by a test that would pass whatever the service did.
    """
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google, sender="stranger@elsewhere.example")
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    quarantine_id = str(refused["quarantine_id"])
    add_registered_contact(client, company_id, email="stranger@elsewhere.example")

    statements: list[str] = []

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry").status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    locking = [
        statement
        for statement in statements
        if "quarantined_messages" in statement and "FOR UPDATE" in statement
    ]
    assert locking, statements
    # The row it locked is the one row this retry closed, once.
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.RESOLVED.value
    assert (await actions_of(session, tenant_id)).count(
        AuditAction.MAIL_QUARANTINE_RESOLVED.value
    ) == 1


async def test_a_retry_that_still_cannot_attribute_reports_the_current_reason(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second attempt is a second decision, and it says so.

    The row keeps the reason it was quarantined with — that is history — while
    the attempt reports the reason the rule reaches *now*: here the sender's
    address was registered for two companies in the meantime, so the remedy
    staff need is the other one.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = message_with_attachment(
        google,
        sender="stranger@elsewhere.example",
        subject="Re: Annual return confirmation",
        body=f"Please see the attached signed form. {BODY_MARKER}",
    )
    client, tenant_id, first, second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    assert refused["reason"] == "unregistered_sender"
    quarantine_id = str(refused["quarantine_id"])

    person_id = add_registered_contact(client, first, email="stranger@elsewhere.example")
    associate(client, second, person_id)

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == QuarantineStatus.PENDING.value
    assert body["resolved_at"] is None
    assert body["communication_id"] is None
    assert body["outcome"]["outcome"] == IngestOutcome.AMBIGUOUS_SENDER.value
    assert body["outcome"]["reason"] == QuarantineReason.MULTIPLE_COMPANIES.value
    assert body["outcome"]["quarantine_id"] == quarantine_id
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.reason == QuarantineReason.UNREGISTERED_SENDER.value
    assert row.status == QuarantineStatus.PENDING.value
    assert row.resolved_at is None
    # The decision needs the message's words, so this attempt read them — and
    # stores none of it.
    assert google.raw_fetches == [message_id]
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []
    assert len(await stored_quarantines(session)) == 1
    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.MAIL_MESSAGE_QUARANTINED.value) == 1
    assert AuditAction.MAIL_QUARANTINE_RESOLVED.value not in actions
    # Nothing decided on this attempt is written: the §2.7 assessment and the
    # quarantine entry belong to the refusal that created the row (the
    # one-entry-per-message rule), and the first refusal here ran no resolver.
    assert await stored_assessments(session) == []
    assert BODY_MARKER not in response.text
    assert "ATTACHMENT-MARKER" not in response.text
    assert "signed.pdf" not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert "signed.pdf" not in audit_text


async def test_a_retry_of_an_unknown_or_cross_tenant_row_is_not_found(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google, sender="stranger@elsewhere.example")
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    quarantine_id = str(refused["quarantine_id"])
    fetches = list(google.raw_fetches)

    other = Tenant(name=f"tenant-{uuid4()}")
    session.add(other)
    await session.flush()
    foreign = QuarantinedMessage(
        tenant_id=other.id,
        mailbox_message_id="msg-elsewhere",
        thread_id=None,
        sender_address="someone@elsewhere.example",
        received_at=google.received_base,
        reason=QuarantineReason.UNREGISTERED_SENDER.value,
        status=QuarantineStatus.PENDING.value,
    )
    session.add(foreign)
    await session.flush()

    unknown = client.post(f"{QUARANTINE_URL}/{uuid4()}/retry")
    cross_tenant = client.post(f"{QUARANTINE_URL}/{foreign.id}/retry")

    # One answer for both: whether an id exists in another tenant is not this
    # deployment's to reveal.
    assert unknown.status_code == 404
    assert cross_tenant.status_code == 404
    # Neither attempt reached the mailbox, and neither row was touched.
    assert google.raw_fetches == fetches
    own = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert own is not None
    assert own.status == QuarantineStatus.PENDING.value
    assert foreign.status == QuarantineStatus.PENDING.value


# --- The dismiss ---------------------------------------------------------


async def deployment_with_a_quarantined_message(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    google: FakeGoogle,
    *,
    sender: str = "stranger@elsewhere.example",
) -> tuple[TestClient, UUID, str, str]:
    """A deployment, its company, and one message waiting in quarantine.

    The sender reaches no company, so the cycle refuses the message and leaves
    the §2.5 row whose id this returns; ``company_id`` is the company the
    sender can be registered against, which is the retry's remedy.
    """
    message_with_attachment(google, sender=sender)
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)
    refused = client.post(INGEST_URL).json()["outcomes"][0]
    assert refused["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    return client, tenant_id, company_id, str(refused["quarantine_id"])


async def test_a_dismiss_closes_a_pending_row_and_audits_it(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client already deleted the mail: the row must be closable.

    A retry of a message the provider no longer has answers 502 for ever, so
    without a dismiss the row would stay in the queue for good.

    The route has no mailbox access by construction — it takes no store and no
    scanner and its service never calls the mail client — so "it read no mail"
    is not a claim a test here could falsify. What it *can* check is that the
    dismissal wrote nothing to the records registry: no stored file and no
    `Communication` appears, because a dismissal is a statement about the row.
    """
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, _, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == quarantine_id
    assert body["status"] == QuarantineStatus.DISMISSED.value
    assert body["dismissed_at"] is not None
    assert body["resolved_at"] is None
    assert body["communication_id"] is None
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.DISMISSED.value
    assert row.dismissed_at is not None
    assert row.resolved_at is None
    assert row.resolved_communication_id is None
    # And nothing else about the row moved: the dismissal closes it, it does
    # not rewrite why the message was quarantined or when it arrived.
    assert row.sender_address == "stranger@elsewhere.example"
    assert row.received_at == google.received_base
    assert row.reason == QuarantineReason.UNREGISTERED_SENDER.value
    # The dismissal is audited, once, against the row — with the reason it was
    # quarantined and never the message.
    entries = await audit_entries(session, tenant_id)
    assert [entry.action for entry in entries].count(
        AuditAction.MAIL_QUARANTINE_DISMISSED.value
    ) == 1
    dismissed = entries[-1]
    assert dismissed.action == AuditAction.MAIL_QUARANTINE_DISMISSED.value
    assert dismissed.entity_type == "quarantined_message"
    assert dismissed.entity_id == UUID(quarantine_id)
    assert dismissed.company_id is None
    assert dismissed.actor_user_id is not None  # the administrator who dismissed
    assert dismissed.details["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert BODY_MARKER not in response.text
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    # Nothing was stored: the row is the record of a staff decision, and the
    # message — which this deployment never even read — stays in the mailbox.
    assert await stored_documents(session) == []
    assert await stored_communications(session) == []


async def test_a_second_dismiss_of_the_same_row_is_refused(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, _, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )
    assert client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss").status_code == 200

    again = client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss")

    assert again.status_code == 409
    assert again.json()["detail"] == "already_dismissed"
    # One dismissal, one entry: the append-only chain is not rewritten by a
    # second click.
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.MAIL_QUARANTINE_DISMISSED.value
    ) == 1
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.DISMISSED.value


async def test_a_dismiss_of_a_resolved_row_is_refused(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two terminal states are distinct answers, and a resolved row is not
    dismissed over the record it became."""
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, company_id, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )
    add_registered_contact(client, company_id, email="stranger@elsewhere.example")
    resolved = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == QuarantineStatus.RESOLVED.value

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss")

    assert response.status_code == 409
    assert response.json()["detail"] == "already_resolved"
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.RESOLVED.value
    assert row.dismissed_at is None
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.MAIL_QUARANTINE_DISMISSED.value
    ) == 0


async def test_a_retry_of_a_dismissed_row_is_refused(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dismissal is final for the retry too, and it reads no mail."""
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, company_id, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )
    assert client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss").status_code == 200
    add_registered_contact(client, company_id, email="stranger@elsewhere.example")
    fetches = list(google.raw_fetches)
    entries = await audit_entries(session, tenant_id)

    response = client.post(f"{QUARANTINE_URL}/{quarantine_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "already_dismissed"
    # The remedy was applied and refused: the row's status answers before the
    # mailbox is touched, and nothing is written.
    assert google.raw_fetches == fetches
    assert len(await audit_entries(session, tenant_id)) == len(entries)
    assert await stored_communications(session) == []
    assert await stored_documents(session) == []


async def test_a_dismiss_of_an_unknown_or_cross_tenant_row_is_not_found(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, _, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )
    other = Tenant(name=f"tenant-{uuid4()}")
    session.add(other)
    await session.flush()
    foreign = QuarantinedMessage(
        tenant_id=other.id,
        mailbox_message_id="msg-elsewhere",
        thread_id=None,
        sender_address="someone@elsewhere.example",
        received_at=google.received_base,
        reason=QuarantineReason.UNREGISTERED_SENDER.value,
        status=QuarantineStatus.PENDING.value,
    )
    session.add(foreign)
    await session.flush()

    unknown = client.post(f"{QUARANTINE_URL}/{uuid4()}/dismiss")
    cross_tenant = client.post(f"{QUARANTINE_URL}/{foreign.id}/dismiss")

    # One answer for both: whether an id exists in another tenant is not this
    # deployment's to reveal.
    assert unknown.status_code == 404
    assert cross_tenant.status_code == 404
    # Neither row moved, and nothing was audited.
    own = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert own is not None
    assert own.status == QuarantineStatus.PENDING.value
    assert own.dismissed_at is None
    assert foreign.status == QuarantineStatus.PENDING.value
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.MAIL_QUARANTINE_DISMISSED.value
    ) == 0


async def test_the_dismiss_read_of_the_row_takes_the_lock(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping dismissals must not both close the same row.

    Same reason as the retry's own lock: without it both requests read
    ``pending`` and both would append a dismissal to the append-only chain —
    a duplicated fact in the record. The harness cannot stage the overlap (one
    transaction per test, rolled back), so the mechanism is pinned where it can
    be — the statement the route really emits.
    """
    google = FakeGoogle(address=MAILBOX)
    client, tenant_id, _, quarantine_id = await deployment_with_a_quarantined_message(
        session, store, monkeypatch, google
    )

    statements: list[str] = []

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert client.post(f"{QUARANTINE_URL}/{quarantine_id}/dismiss").status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    locking = [
        statement
        for statement in statements
        if "quarantined_messages" in statement and "FOR UPDATE" in statement
    ]
    assert locking, statements
    # The row it locked is the one row this dismissal closed, once.
    row = await session.get(QuarantinedMessage, UUID(quarantine_id))
    assert row is not None
    assert row.status == QuarantineStatus.DISMISSED.value
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.MAIL_QUARANTINE_DISMISSED.value
    ) == 1


# --- The review list is a queue ------------------------------------------


async def test_the_review_list_is_the_open_queue_unless_asked_otherwise(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three refusals, two of them closed by staff, and the four reads.

    The list is the queue staff work from, so it shows what is still waiting
    and nothing else; the closed rows are not lost — they are one query
    parameter away, which is what a reviewer checking history needs.
    """
    google = FakeGoogle(address=MAILBOX)
    unregistered = message_with_attachment(
        google,
        sender="stranger@elsewhere.example",
        subject="Re: Annual return confirmation",
        body="Who is this?",
    )
    ambiguous = message_with_attachment(
        google,
        subject="Re: Annual return confirmation",
        body="Please see the attached signed form.",
    )
    waiting = message_with_attachment(
        google,
        sender="someone-else@elsewhere.example",
        subject="Re: Annual return confirmation",
        body="Who is this?",
    )
    client, _, first, _ = await multi_company_deployment(session, store, monkeypatch, google)
    assert client.post(INGEST_URL).json()["refused"] == 3
    rows = await stored_quarantines(session)
    by_message = {row.mailbox_message_id: row for row in rows}
    assert set(by_message) == {unregistered, ambiguous, waiting}

    # One closed the staff way (the remedy reached the message), one dismissed.
    add_registered_contact(client, first, email="stranger@elsewhere.example")
    resolved = client.post(
        f"{QUARANTINE_URL}/{by_message[unregistered].id}/retry"
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == QuarantineStatus.RESOLVED.value
    dismissed = client.post(f"{QUARANTINE_URL}/{by_message[ambiguous].id}/dismiss")
    assert dismissed.status_code == 200, dismissed.text

    default = client.get(QUARANTINE_URL)
    pending = client.get(f"{QUARANTINE_URL}?status=pending")
    resolved_rows = client.get(f"{QUARANTINE_URL}?status=resolved")
    dismissed_rows = client.get(f"{QUARANTINE_URL}?status=dismissed")
    everything = client.get(f"{QUARANTINE_URL}?status=all")

    assert default.status_code == 200, default.text
    assert [item["mailbox_message_id"] for item in default.json()] == [waiting]
    assert default.json() == pending.json()
    assert default.json()[0]["status"] == QuarantineStatus.PENDING.value
    assert [item["mailbox_message_id"] for item in resolved_rows.json()] == [unregistered]
    assert resolved_rows.json()[0]["status"] == QuarantineStatus.RESOLVED.value
    assert [item["mailbox_message_id"] for item in dismissed_rows.json()] == [ambiguous]
    assert dismissed_rows.json()[0]["status"] == QuarantineStatus.DISMISSED.value
    assert dismissed_rows.json()[0]["dismissed_at"] is not None
    # Everything, newest receipt first — the closed rows included.
    assert [item["mailbox_message_id"] for item in everything.json()] == [
        waiting,
        ambiguous,
        unregistered,
    ]


async def test_an_unknown_status_filter_is_refused(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_with_attachment(google, sender="stranger@elsewhere.example")
    client, _, _ = await ingest_deployment(session, store, monkeypatch, google)
    assert client.post(INGEST_URL).json()["refused"] == 1

    response = client.get(f"{QUARANTINE_URL}?status=triage_later")

    # A closed vocabulary, refused rather than answered: a filter nobody
    # understands must not read as "nothing is waiting".
    assert response.status_code == 422
    assert (await stored_quarantines(session))[0].status == QuarantineStatus.PENDING.value


# --- The retro-scan that closes the interim posture ----------------------


async def test_the_retro_scan_flips_a_pending_document_clean_and_audits_it(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    client, uploaded, tenant_id = await pending_document_deployment(
        session, store, monkeypatch, scanner_seam
    )
    scanner_seam.default = CLEAN

    response = client.post(SCAN_PENDING_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned"] == 1
    assert body["clean"] == 1
    assert body["failed"] == 0
    assert body["remaining"] == 0
    assert body["outcomes"][0]["document_id"] == uploaded["id"]
    assert body["outcomes"][0]["scan_status"] == ScanStatus.CLEAN.value
    document = await session.get(Document, UUID(uploaded["id"]))
    assert document is not None
    assert document.scan_status == ScanStatus.CLEAN.value
    scanned = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action == AuditAction.DOCUMENT_SCANNED.value
    ]
    assert len(scanned) == 1
    assert scanned[0].entity_id == document.id
    assert scanned[0].details["previous_scan_status"] == ScanStatus.PENDING.value
    assert scanned[0].details["scan_status"] == ScanStatus.CLEAN.value
    # And it is servable now.
    download = client.get(
        f"/api/v1/companies/{document.company_id}/documents/{document.id}/download"
    )
    assert download.status_code == 200


async def test_the_retro_scan_marks_an_infected_document_infected_and_keeps_it_unserved(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    client, uploaded, tenant_id = await pending_document_deployment(
        session, store, monkeypatch, scanner_seam
    )
    scanner_seam.default = infected("Eicar-Test-Signature")

    body = client.post(SCAN_PENDING_URL).json()

    assert body["infected"] == 1
    assert body["outcomes"][0]["signature"] == "Eicar-Test-Signature"
    document = await session.get(Document, UUID(uploaded["id"]))
    assert document is not None
    assert document.scan_status == ScanStatus.INFECTED.value
    download = client.get(
        f"/api/v1/companies/{document.company_id}/documents/{document.id}/download"
    )
    assert download.status_code == 409
    assert download.json()["detail"] == "document_not_available: infected"
    assert (await actions_of(session, tenant_id)).count(AuditAction.DOCUMENT_SCANNED.value) == 1


async def test_the_retro_scan_bounds_each_request_and_reports_what_is_left(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    client, uploaded, _ = await pending_document_deployment(
        session, store, monkeypatch, scanner_seam
    )
    upload(client, str(uploaded["company_id"]), "kind=prior_filing&form=ND2A")
    scanner_seam.default = CLEAN

    first = client.post(f"{SCAN_PENDING_URL}?limit=1").json()

    assert first["scanned"] == 1
    assert first["remaining"] == 1
    second = client.post(SCAN_PENDING_URL).json()
    assert second["scanned"] == 1
    assert second["clean"] == 1
    assert second["remaining"] == 0


async def test_the_retro_scan_leaves_a_document_pending_when_the_scanner_fails_again(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    scanner_seam: FakeScanner,
) -> None:
    client, uploaded, tenant_id = await pending_document_deployment(
        session, store, monkeypatch, scanner_seam
    )

    body = client.post(SCAN_PENDING_URL).json()

    assert body["failed"] == 1
    assert body["clean"] == 0
    assert body["remaining"] == 1  # still part of the backlog
    document = await session.get(Document, UUID(uploaded["id"]))
    assert document is not None
    assert document.scan_status == ScanStatus.PENDING.value
    scanned = [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action == AuditAction.DOCUMENT_SCANNED.value
    ]
    assert scanned[0].details["scan_reason"] == "unavailable:ConnectionRefusedError"


# --- The clarification loop's plain path (workstream ②, T4) -------------------


def planted_message(
    google: FakeGoogle,
    *,
    subject: str,
    sender: str,
    body: str,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """One unthreaded inbound message with the thread headers a test names —
    an absent ``message_id`` stages the anchor-less case the loop refuses."""
    return google.with_message(
        subject=subject,
        sender=sender,
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        body=body,
    )


async def test_an_unregistered_senders_first_contact_opens_a_company_less_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-class: the first-contact ask replaces the ``UNATTRIBUTED_SENDER``
    quarantine — on a **Free** deployment too (ruling D3: the company-less
    ask consults no automation gate, so ungated core quarantine handling is
    not regressed by the replacement).

    The case opens company-less, anchored on the original's own RFC
    ``Message-ID``; nothing of the message is read beyond the envelope (the
    ask quotes envelope facts only) and no quarantine row exists. The ask
    itself now rides the outbox (migration ``0035``): the letter registers
    tenant-scoped, the case's ``ask_sent_at`` is stamped, and the cycle's
    outcome carries the new ``awaiting_registered_channel`` shape with the
    case id and no company.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = planted_message(
        google,
        subject="Company setup enquiry",
        sender="Prospect <prospect@elsewhere.example>",
        body=f"Please quote for a new company. {BODY_MARKER}",
        message_id="<cold-contact@elsewhere.example>",
    )
    client, tenant_id, _company_id = await ingest_deployment(
        session, store, monkeypatch, google, tier="free", covered=False
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    assert response.json()["examined"] == 1
    assert response.json()["ingested"] == 0
    (outcome,) = response.json()["outcomes"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["reason"] is None
    assert outcome["company_id"] is None  # no chain, no company — the case is company-less
    assert outcome["attribution_case_id"] is not None
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    assert outcome["raw_document_id"] is None

    rows = await stored_cases(session)
    assert len(rows) == 1
    case = rows[0]
    assert str(case.id) == outcome["attribution_case_id"]
    assert case.tenant_id == tenant_id
    assert case.company_id is None
    assert case.mailbox_message_id == message_id
    assert case.sender_address == "prospect@elsewhere.example"
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert case.ask_kind == AttributionAskKind.REGISTERED_CHANNEL.value
    assert case.ask_sent_at is not None  # the ask left with the cycle
    assert case.original_rfc_message_id == "<cold-contact@elsewhere.example>"
    assert case.thread_id == f"thread-{message_id}"

    # The ask Letter B is queued company-less: the outbox row names the case,
    # the stored letter is tenant-scoped under ``_correspondence``, and no
    # Communication exists yet — delivery is the send cycle's separate step.
    (ask_row,) = await stored_outbox(session)
    assert ask_row.company_id is None
    assert str(ask_row.attribution_case_id) == str(case.id)
    (letter,) = await stored_documents(session)
    assert letter.company_id is None
    assert letter.storage_key.startswith("_correspondence/")

    # Metadata only for the *message*: the envelope was read, the body never
    # fetched, and no quarantine row or Communication exists for it.
    assert google.message_fetches == [message_id]
    assert google.raw_fetches == []
    assert await stored_quarantines(session) == []
    assert await stored_communications(session) == []

    # The opening is audited once, through the single write path, and it
    # writes the company column NULL (ruling D10).
    entries = await audit_entries(session, tenant_id)
    opened_entry = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_OPENED.value
    )
    assert opened_entry.company_id is None
    assert opened_entry.details == {
        "case_id": str(case.id),
        "sender_address": "prospect@elsewhere.example",
        "status": AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value,
    }

    # The overlap re-examination meets the same case: one row, one entry.
    second = client.post(INGEST_URL)
    assert second.status_code == 200, second.text
    assert second.json()["outcomes"][0]["attribution_case_id"] == str(case.id)
    assert len(await stored_cases(session)) == 1
    assert [entry.action for entry in await audit_entries(session, tenant_id)].count(
        AuditAction.ATTRIBUTION_CASE_OPENED.value
    ) == 1


async def test_a_first_contact_without_its_own_message_id_is_quarantined_as_today(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anchor-less rule extends verbatim: a cold message whose own RFC
    ``Message-ID`` is absent opens no case — no anchor could ever be found
    again — and lands in §2.5's quarantine exactly as today."""
    google = FakeGoogle(address=MAILBOX)
    planted_message(
        google,
        subject="Company setup enquiry",
        sender="Prospect <prospect@elsewhere.example>",
        body=f"Please quote for a new company. {BODY_MARKER}",
    )
    client, _tenant_id, _company_id = await ingest_deployment(session, store, monkeypatch, google)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    (outcome,) = response.json()["outcomes"]
    assert outcome["outcome"] == IngestOutcome.UNATTRIBUTED_SENDER.value
    assert outcome["reason"] == QuarantineReason.UNREGISTERED_SENDER.value
    assert outcome["attribution_case_id"] is None
    assert outcome["quarantine_id"] is not None
    assert await stored_cases(session) == []


async def test_a_multi_company_message_the_resolver_cannot_match_opens_a_clarification_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-class: the sender is registered for two companies and the resolver
    matches neither — with the anchor present, the which-company ask replaces
    the ``AMBIGUOUS_SENDER`` quarantine, and the resolver's verdict rides the
    outcome the way a refusal's does.

    The body is read to decide (that is what the multi-company decision
    needs); the message is neither stored nor recorded, and the case opens
    company-less with the new status and kind.
    """
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<ambiguous-original@client.example>",
        body=f"Please see the attached signed form. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    client, tenant_id, _first, _second = await multi_company_deployment(
        session, store, monkeypatch, google
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    (outcome,) = response.json()["outcomes"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_CLARIFICATION.value
    assert outcome["reason"] is None
    assert outcome["company_id"] is None
    assert outcome["attribution_case_id"] is not None
    assert outcome["quarantine_id"] is None
    assert outcome["communication_id"] is None
    # The resolver ran and could not decide; the summary says so, without the
    # text that matched (nothing did).
    assert outcome["resolution"]["case"] == "no_match"
    assert outcome["resolution"]["routing"] == "client_clarification"

    rows = await stored_cases(session)
    assert len(rows) == 1
    case = rows[0]
    assert str(case.id) == outcome["attribution_case_id"]
    assert case.company_id is None
    assert case.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value
    assert case.ask_kind == AttributionAskKind.CLARIFICATION.value
    assert case.ask_sent_at is not None  # Letter A left with the cycle
    assert case.original_rfc_message_id == "<ambiguous-original@client.example>"
    assert case.sender_address == CONTACT

    # Read to decide, not stored: one raw fetch (the resolver's), and no
    # Communication or quarantine row from this message. The one stored
    # document is the ask's own letter, tenant-scoped — the deployment's
    # writing, not a copy of the mail.
    assert google.raw_fetches == [message_id]
    (letter,) = await stored_documents(session)
    assert letter.company_id is None
    assert letter.storage_key.startswith("_correspondence/")
    assert await stored_communications(session) == []
    assert await stored_quarantines(session) == []
    entries = await audit_entries(session, tenant_id)
    opened_entry = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_OPENED.value
    )
    assert opened_entry.company_id is None
    audit_text = await session.scalar(
        text("SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    )
    assert audit_text is not None
    assert BODY_MARKER not in audit_text
    assert "ATTACHMENT-MARKER" not in audit_text
    assert BODY_MARKER not in response.text


async def test_a_clarification_answer_naming_one_company_resolves_the_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer grammar end to end: the ambiguous original opens the case in
    the same cycle; the reply on the anchor, from the same sender, whose first
    line names exactly one company resolves it — the case takes the company,
    the parked original is ingested for it (the tail re-ran), the answer
    itself lands as ordinary mail for the company it named, and one
    ``attribution_case_resolved`` entry names the act."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<ambiguous-original@client.example>",
        body=f"Please see the attached signed form. {BODY_MARKER}",
        attachments=[("signed.pdf", "application/pdf", ATTACHMENT_MARKER)],
    )
    answer = google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:41:00 +0800",
        message_id="<the-answer@client.example>",
        in_reply_to="<ambiguous-original@client.example>",
        body=f"{COMPANY_NAME}\nPlease proceed with the filing. {BODY_MARKER}",
    )
    client, tenant_id, first, _second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, first, name_en=COMPANY_NAME)
    name_company(client, _second, name_en="Other Harbour Limited")

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["examined"] == 2
    # The count is per outcome: the parked original's ingestion folds into the
    # answer's examination (①'s confirmation precedent) — one ingested
    # outcome, two stored exchanges below.
    assert body["ingested"] == 1
    opened, answered = body["outcomes"]
    assert opened["outcome"] == IngestOutcome.AWAITING_CLARIFICATION.value
    assert opened["attribution_case_id"] is not None
    # The answer resolved the case and then landed as ordinary mail for the
    # company it named: its own outcome is the ingestion's.
    assert answered["outcome"] == IngestOutcome.INGESTED.value
    assert answered["company_id"] == first
    assert answered["communication_id"] is not None

    rows = await stored_cases(session)
    assert len(rows) == 1
    case = rows[0]
    assert str(case.id) == opened["attribution_case_id"]
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert str(case.company_id) == first
    assert case.resolved_communication_id is not None

    # Both messages are stored records for the named company: the parked
    # original (the tail re-ran for it) and the answer itself.
    communications = await stored_inbound_communications(session)
    assert len(communications) == 2
    by_message = {row.mailbox_message_id: row for row in communications}
    original_row = by_message[original]
    answer_row = by_message[answer]
    assert original_row.company_id == UUID(first)
    assert answer_row.company_id == UUID(first)
    assert case.resolved_communication_id == original_row.id

    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.ATTRIBUTION_CASE_OPENED.value) == 1
    assert actions.count(AuditAction.MAIL_MESSAGE_INGESTED.value) == 2
    resolved = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_RESOLVED.value
    )
    assert resolved.company_id == UUID(first)
    assert resolved.details["case_id"] == str(case.id)


async def test_a_clarification_answer_with_cr_digits_resolves_the_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digit-sequence half of the grammar: the CR number on the answer's
    first line names the company as surely as its full name does."""
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<ambiguous-original@client.example>",
        body=f"Please see the attached signed form. {BODY_MARKER}",
    )
    google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:41:00 +0800",
        message_id="<the-answer@client.example>",
        in_reply_to="<ambiguous-original@client.example>",
        body="CR 7654321\nPlease proceed.",
    )
    client, _tenant_id, first, second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, first, name_en=COMPANY_NAME)
    name_company(client, second, name_en="Other Harbour Limited", effective_date="2026-01-02")

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    answered = response.json()["outcomes"][1]
    assert answered["outcome"] == IngestOutcome.INGESTED.value
    assert answered["company_id"] == second
    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert str(case.company_id) == second


async def test_an_unresolvable_clarification_answer_drives_the_bounded_reask(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D7, message-driven: a later unresolvable message from the same
    sender on the anchor drives the re-ask — no worker, no timer. The grammar
    rejects the line, the case stays awaiting and company-less, and the
    re-ask itself now rides the outbox (migration ``0035``): the outcome
    carries no reason because the enqueue succeeded; nothing of the answer
    message is stored."""
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<ambiguous-original@client.example>",
        body=f"Please see the attached signed form. {BODY_MARKER}",
    )
    answer = google.with_message(
        subject="Re: Annual return confirmation",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:51:00 +0800",
        message_id="<not-an-answer@client.example>",
        in_reply_to="<ambiguous-original@client.example>",
        body=f"Any update on this? {BODY_MARKER}",
    )
    client, tenant_id, _first, _second = await multi_company_deployment(
        session, store, monkeypatch, google
    )
    name_company(client, _first, name_en=COMPANY_NAME)
    name_company(client, _second, name_en="Other Harbour Limited")

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    opened, reasked = response.json()["outcomes"]
    assert opened["outcome"] == IngestOutcome.AWAITING_CLARIFICATION.value
    assert reasked["outcome"] == IngestOutcome.AWAITING_CLARIFICATION.value
    assert reasked["reason"] is None  # the re-ask enqueued
    assert reasked["attribution_case_id"] == opened["attribution_case_id"]
    assert reasked["company_id"] is None
    assert reasked["communication_id"] is None

    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value
    assert case.company_id is None
    assert case.ask_sent_at is not None  # the re-ask left with the cycle
    # The grammar fetch read the answer's first line; the answer message was
    # not stored, and the re-ask queued exactly one company-less ask row.
    assert answer in google.raw_fetches
    communications = await stored_inbound_communications(session)
    assert [row.mailbox_message_id for row in communications] == []
    (ask_row,) = await stored_outbox(session)
    assert ask_row.company_id is None
    assert str(ask_row.attribution_case_id) == str(case.id)
    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.ATTRIBUTION_CASE_OPENED.value) == 1
    assert actions.count(AuditAction.ATTRIBUTION_ASK_ENQUEUED.value) == 2  # ask + re-ask


async def test_a_registered_channels_message_on_the_anchor_closes_the_first_contact_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D8: any message on the anchor whose sender is a registered
    channel of any company is the remedy a first-contact case asked for. The
    case closes — its queued ask (live since migration ``0035``) cancelled
    with the bounded reason — one ``attribution_case_closed`` entry with
    ``company_id = NULL`` names it, and the closing message is **not
    consumed**: it follows its own plain path and lands as ordinary mail for
    its company."""
    google = FakeGoogle(address=MAILBOX)
    google.with_message(
        subject="Director appointment",
        sender="Stranger <stranger@elsewhere.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<cold-appointment@client.example>",
        body=f"Please appoint Ms Wong. {BODY_MARKER}",
    )
    remedy = google.with_message(
        subject="Re: Director appointment",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:41:00 +0800",
        message_id="<the-remedy@client.example>",
        in_reply_to="<cold-appointment@client.example>",
        body=f"I authorised this. {BODY_MARKER}",
    )
    client, tenant_id, company_id = await ingest_deployment(session, store, monkeypatch, google)

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    opened, closing = response.json()["outcomes"]
    assert opened["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert opened["attribution_case_id"] is not None
    # The closing message's own outcome: ordinary registered-sender mail, with
    # no case attached to it.
    assert closing["outcome"] == IngestOutcome.INGESTED.value
    assert closing["company_id"] == company_id
    assert closing["attribution_case_id"] is None

    (case,) = await stored_cases(session)
    assert str(case.id) == opened["attribution_case_id"]
    assert case.status == AttributionCaseStatus.CLOSED.value
    assert case.closed_at is not None
    assert case.company_id is None  # the closure stamps no company (D10)

    entries = await audit_entries(session, tenant_id)
    actions = [entry.action for entry in entries]
    assert actions.count(AuditAction.ATTRIBUTION_CASE_OPENED.value) == 1
    assert actions.count(AuditAction.MAIL_MESSAGE_INGESTED.value) == 1
    assert actions.count(AuditAction.ATTRIBUTION_ASK_ENQUEUED.value) == 1
    closed_entry = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_CLOSED.value
    )
    assert closed_entry.company_id is None
    assert closed_entry.details == {"case_ids": [str(case.id)]}
    # The queued ask is cancelled with the bounded reason — the letter must
    # not reach the client after the remedy arrived.
    (ask_row,) = await stored_outbox(session)
    assert ask_row.status == OutboxStatus.CANCELLED.value
    assert ask_row.last_error == attribution.ASK_CANCELLED_ON_CLOSE
    communications = await stored_inbound_communications(session)
    assert [row.mailbox_message_id for row in communications] == [remedy]


async def test_a_clarification_answer_naming_several_hands_the_original_to_the_split(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2 end to end: the which-company answer naming several companies —
    two of the sender's companies share the registered name the line carries —
    closes the clarification case with the multi-company handoff named in its
    audit, and the parked original hands to the split. The division itself
    cannot complete (the original the answer refers to names no company, so
    the span grammar has nothing to divide it against) and the parent parks
    ``failed`` with the violation named — staff's case, never guessed. The
    handoff is what this path proves; the split's happy road is pinned at the
    application seam (``test_split_message_creates_the_parent_items_and_the_audits``)."""
    google = FakeGoogle(address=MAILBOX)
    original = google.with_message(
        subject="Annual filings",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        message_id="<ambiguous-original@client.example>",
        body="Please handle the enclosed filings.",
    )
    answer = google.with_message(
        subject="Re: Annual filings",
        sender=f"CHAN TAI MAN <{CONTACT}>",
        date="Tue, 23 Sep 2026 09:41:00 +0800",
        message_id="<the-answer@client.example>",
        in_reply_to="<ambiguous-original@client.example>",
        body=COMPANY_NAME,
    )
    client, tenant_id, first, second, _transport = await split_deployment(
        session, store, monkeypatch, google, envelope=_split_envelope(result=[]), same_name=True
    )

    response = client.post(INGEST_URL)

    assert response.status_code == 200, response.text
    opened, answered = response.json()["outcomes"]
    assert opened["outcome"] == IngestOutcome.AWAITING_CLARIFICATION.value
    assert answered["outcome"] == IngestOutcome.SPLIT.value
    assert answered["attribution_case_id"] == opened["attribution_case_id"]
    assert answered["split_parent_id"] is not None
    assert answered["communication_id"] is None

    (case,) = await stored_cases(session)
    assert case.status == AttributionCaseStatus.CLOSED.value
    assert case.company_id is None  # the handoff stamps no company
    entries = await audit_entries(session, tenant_id)
    closed = next(
        entry for entry in entries if entry.action == AuditAction.ATTRIBUTION_CASE_CLOSED.value
    )
    assert closed.details["handoff"] == "multi_company_split"
    assert sorted(closed.details["matched_company_ids"]) == sorted(
        [str(UUID(first)), str(UUID(second))]
    )
    (parent,) = list(
        (
            await session.scalars(select(SplitParent).where(SplitParent.tenant_id == tenant_id))
        ).all()
    )
    assert parent.status == "failed"
    assert parent.mailbox_message_id == original
    assert answer != original
    failed = next(entry for entry in entries if entry.action == "split_parent_failed")
    assert failed.details["violations"] == ["coverage_mismatch"]


async def test_a_retry_of_a_pre_loop_quarantine_row_opens_the_company_less_case(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review surface's retry meets the clarification loop too: a row the
    pre-② cycle wrote (an unregistered sender, quarantined before the
    first-touch ask existed) holds a message that carries its own RFC
    ``Message-ID`` — so the retry's re-examination opens the company-less
    first-contact case instead of re-refusing.

    The row stays ``pending``: opening the case is not the retry's remedy to
    close — the case surface is where the message is worked now — and the
    outcome names the new case, company-less, with nothing of the message
    itself read or stored; its ask (live since migration ``0035``) rides the
    outbox the retry's cycle enqueues."""
    google = FakeGoogle(address=MAILBOX)
    message_id = planted_message(
        google,
        subject="Company setup enquiry",
        sender="Prospect <prospect@elsewhere.example>",
        body=f"Please quote for a new company. {BODY_MARKER}",
        message_id="<cold-quoted@elsewhere.example>",
    )
    client, tenant_id, _company_id = await ingest_deployment(session, store, monkeypatch, google)
    session.add(
        QuarantinedMessage(
            tenant_id=tenant_id,
            mailbox_message_id=message_id,
            thread_id=f"thread-{message_id}",
            sender_address="prospect@elsewhere.example",
            received_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
            reason=QuarantineReason.UNREGISTERED_SENDER.value,
            status=QuarantineStatus.PENDING.value,
        )
    )
    await session.flush()
    (row,) = await stored_quarantines(session)

    response = client.post(f"{QUARANTINE_URL}/{row.id}/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == QuarantineStatus.PENDING.value
    outcome = body["outcome"]
    assert outcome["outcome"] == IngestOutcome.AWAITING_REGISTERED_CHANNEL.value
    assert outcome["company_id"] is None
    assert outcome["attribution_case_id"] is not None
    assert outcome["quarantine_id"] is None
    (case,) = await stored_cases(session)
    assert str(case.id) == outcome["attribution_case_id"]
    assert case.company_id is None
    assert case.original_rfc_message_id == "<cold-quoted@elsewhere.example>"
    assert case.ask_sent_at is not None  # the ask left with the retry's cycle
    assert len(await stored_outbox(session)) == 1  # the company-less ask row
    # Metadata only for the message: no raw fetch, no Communication, no
    # second quarantine row.
    assert google.raw_fetches == []
    assert await stored_inbound_communications(session) == []
    assert len(await stored_quarantines(session)) == 1
    opened = await audit_entries(session, tenant_id)
    assert any(
        entry.action == AuditAction.ATTRIBUTION_CASE_OPENED.value for entry in opened
    )
