"""DB-backed scenarios for the reconfirmer's sending half (slice M5b task 4;
opt-in; needs a migrated PostgreSQL).

The reconfirmer rides the M2 machinery and nothing else: the deterministic
bilingual letter (``app.domain.reconfirmation``) is composed, stored through
the records registry, and released through the same outbox the request and the
reminder use — the client destination's ``SendMode`` decides whether the row
goes out at once or waits held for a staff release, exactly the reminder
posture. What these scenarios prove, in the task brief's order:

1. **stored and released under ``automatic``** — the letter lands in the
   records registry (``generated_draft``, retention ``client_communication``,
   the request's recipients, its thread anchor in ``In-Reply-To``/``References``,
   the company named by name and CR number) and the outbox row pends with
   ``not_before NULL`` (send-on-release);
2. **held under ``review_required``, released on staff action** — the row
   parks on ``HELD_NOT_BEFORE``, no delivery cycle touches it, the staff
   release (the reminder release's own path, widened) moves it to the press
   instant, and the accepted SMTP delivery records the outbound
   ``Communication`` with ``kind=reconfirmation`` in the request's thread;
3. **the audit shapes** — ``reconfirmation_enqueued``/``reconfirmation_released``
   carry ids, counts, modes and instants — never a span, a quote, or a body;
4. **nothing sends when there is nothing to ask** — a clean no-change parse
   (and a staff-review parse) writes no document and no outbox row;
5. **the parse cycle's tail** — one parsed reply whose decision is a letter
   enqueues it on the same session, and a letter failure is that message's
   outcome, never an abort: the cycle finishes the messages behind it.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (with ``0029_reply_parsing`` applied). Every test runs inside a
transaction that is rolled back, so the review database stays clean.
"""

# These tests pin the letter's bilingual copy verbatim — the Traditional-
# Chinese strings carry fullwidth CJK punctuation, which ruff's RUF001 reads
# as ambiguous look-alikes. The rule guards code, not a letter a client reads
# (the test_reconfirmation_domain.py precedent).
# ruff: noqa: RUF001

import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import default as default_policy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail import smtp_client
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.reply_parsing import ReplyParse
from app.adapters.persistence.service_packages import ServiceAssignment
from app.adapters.persistence.tenant_config import TenantConfig
from app.application import (
    ai_gateway,
    mail_send,
    outbox,
    reconfirmation,
    reply_parsing,
    service_packages,
)
from app.core.config import settings
from app.domain.mail_send import HELD_NOT_BEFORE, OutboxKind, OutboxStatus
from app.domain.milestones import default_milestones
from app.domain.naming_templates import DEFAULT_DOCUMENT_TEMPLATE, DEFAULT_GENERATED_TEMPLATE
from app.domain.reconfirmation import ReconfirmationDecision
from tests.fake_scanning import FakeScanner
from tests.fake_smtp import FakeSmtpServer, trusting_context

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

MAILBOX = "ai-filings@tcsp.example"
APP_PASSWORD = "app-password-marker-never-logged"
CONTACT = "director@example.hk"
COMPANY_NAME = "Kairos Trading Limited"
CR_NUMBER = "1234567"
BR_NUMBER = "12345678"
ANCHOR_MESSAGE_ID = "<annual-return-request@tcsp.example>"
AT = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 6, 30)
COVERAGE = {"coverage_start": date(2026, 1, 1), "coverage_end": date(2026, 12, 31)}

CHECKLIST = [
    {"fact_id": "officers.0.name_en", "question": "Is the director unchanged? / 董事有否變更？"}
]
RECIPIENTS = [{"person_id": None, "address": CONTACT}]
CONFIRMATIONS = [
    {
        "fact_id": "officers.0.name_en",
        "answer": "confirmed",
        "evidence_span": "the director is unchanged",
        "uncertain": False,
    }
]

# Planted in the unlanded proposal's evidence span: the letter quotes it (the
# stored .eml is designed to carry the client's words), and the custody search
# hunts it everywhere a letter must not leak — audit entries, log lines, the
# report.
SPAN_MARKER = "SPANMARKER-77-Q"

QUESTION_BILINGUAL = "Is the director unchanged? / 董事有否變更？"


def appointment_event() -> dict:
    """One complete director-appointment proposal (lands in the review queue)."""
    return {
        "type": "director_appointment",
        "party": {
            "name_as_written": "Wong New Director",
            "name_en": "Wong New Director",
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "Wong New Director",
            "uncertain": False,
        },
        "effective_date": "2026-02-01",
        "effective_date_text": "1 February 2026",
        "evidence_span": "appoint Wong New Director as a director effective 1 February 2026",
        "uncertain": False,
        "informed_date": None,
        "new_values": None,
        "notes": None,
    }


def unclear_cessation_event() -> dict:
    """A cessation the client gave no effective date for — never lands, asks."""
    return {
        "type": "director_cessation",
        "party": {
            "name_as_written": "Wong New Director",
            "name_en": "Wong New Director",
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "Wong New Director",
            "uncertain": False,
        },
        "effective_date": None,
        "effective_date_text": f"next month {SPAN_MARKER}",
        "evidence_span": f"retire Wong New Director next month {SPAN_MARKER}",
        "uncertain": False,
        "informed_date": None,
        "new_values": None,
        "notes": None,
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


class FakeInfer:
    """The replaced gateway seam: answers every call with the scripted parse."""

    def __init__(self, result: object = None) -> None:
        self.calls: list[dict] = []
        self._result = result if result is not None else grammar_valid_changes()

    async def infer(self, session: AsyncSession, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return ai_gateway.InferenceResult(
            result=self._result,
            provider="stub-provider",
            model_id="stub-model",
            prompt_name="reply_parser",
            prompt_version=1,
        )


def grammar_valid_changes() -> dict:
    """A grammar-valid scripted model answer: one confirmation, one proposal."""
    return {
        "status": "changes_reported",
        "language": "en",
        "confirmations": CONFIRMATIONS,
        "change_events": [appointment_event()],
        "multi_company_signals": {"company_mentions": []},
    }


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration every letter scenario assumes: a licensed deployment
    and a mailbox address to write from (the send itself is only exercised by
    the delivery scenario, against the fake SMTP server)."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "license_expires_at", None)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)


@pytest.fixture(autouse=True)
def tls_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the SMTP leg trust ``tests/fake_smtp.py``'s own certificate."""
    monkeypatch.setattr(smtp_client, "tls_context", trusting_context)


def use_smtp(monkeypatch: pytest.MonkeyPatch, server: FakeSmtpServer) -> None:
    """Provision the SMTP fallback against the loopback fake."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    monkeypatch.setattr(settings, "smtp_host", "localhost")
    monkeypatch.setattr(settings, "smtp_port", server.port)
    monkeypatch.setattr(settings, "mail_app_password", server.password)


# --- Seeds --------------------------------------------------------------------


async def seed_company(session: AsyncSession, *, named: bool = True) -> tuple[Tenant, Company]:
    """A tenant and one company — with the name record the letter reads."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    company = Company(
        tenant_id=tenant.id,
        cr_number=CR_NUMBER,
        br_number=BR_NUMBER,
        date_of_incorporation="2010-03-01",
        anniversary_date="2026-03-01",
        type="private",
    )
    session.add(company)
    await session.flush()
    if named:
        session.add(
            CompanyNameRecord(
                tenant_id=tenant.id,
                company_id=company.id,
                effective_date=date(2015, 1, 1),
                source_type="manual",
                name_en=COMPANY_NAME,
                name_zh=None,
                business_name=None,
            )
        )
        await session.flush()
    return tenant, company


async def seed_request(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> ConfirmationRequest:
    """The sent request the reply answered and the letter answers back."""
    document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="c" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(document)
    await session.flush()
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=date(2026, 3, 1),
        reply_by=date(2026, 2, 1),
        status="sent",
        version_no=1,
        document_id=document.id,
        checklist=CHECKLIST,
        recipients=RECIPIENTS,
    )
    session.add(request)
    await session.flush()
    return request


async def seed_anchor(
    session: AsyncSession, tenant_id: UUID, company_id: UUID, request_id: UUID
) -> MailOutbox:
    """The request's own accepted send — the thread anchor the letter replies on."""
    row = MailOutbox(
        tenant_id=tenant_id,
        company_id=company_id,
        confirmation_request_id=request_id,
        destination="client",
        status="sent",
        kind="confirmation_request",
        message_id=ANCHOR_MESSAGE_ID,
        sent_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def seed_parse(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID | None,
    *,
    status: str,
    routing: str | None,
    confirmations: list,
    event_ids: list,
) -> tuple[ReplyParse, Communication]:
    """The parse row the reply parser landed, and the reply it parsed."""
    document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="client_reply",
        origin="email_attachment",
        retention_class="client_communication",
        sha256="d" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(document)
    await session.flush()
    communication = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction="inbound",
        channel="email",
        kind="reply",
        thread_id=f"thread-{uuid4()}",
        sender_address=CONTACT,
        confirmation_request_id=request_id,
        received_at=datetime(2026, 6, 29, tzinfo=UTC),
        raw_document_id=document.id,
    )
    session.add(communication)
    await session.flush()
    parse = ReplyParse(
        tenant_id=tenant_id,
        company_id=company_id,
        communication_id=communication.id,
        confirmation_request_id=request_id,
        status=status,
        language="en",
        routing=routing,
        confirmations=confirmations,
        event_ids=event_ids,
    )
    session.add(parse)
    await session.flush()
    return parse, communication


async def hold_client_sends(session: AsyncSession, tenant_id: UUID) -> None:
    """A tenant configuration whose client destination waits for a person."""
    session.add(
        TenantConfig(
            tenant_id=tenant_id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones=default_milestones().as_dict(),
            send_modes={"client": "review_required", "government": "review_required"},
        )
    )
    await session.flush()


async def cover(session: AsyncSession, tenant: Tenant, company_id: UUID) -> None:
    """A package and an active assignment — the §5.1 gate's commercial legs."""
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=["ai_reply_parsing"],
        covered_forms=["NAR1", "ND2A"],
    )
    session.add(
        ServiceAssignment(
            company_id=company_id,
            package_id=package.id,
            tenant_id=tenant.id,
            **COVERAGE,
            status="active",
        )
    )
    await session.flush()


async def company_audit(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.company_id == company_id)
        .order_by(AuditLog.seq)
    )
    return list(rows.all())


async def outbox_rows(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id, MailOutbox.company_id == company_id
        )
    )
    return list(rows.all())


async def reconfirmation_rows(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> list[MailOutbox]:
    """The company's reconfirmation rows — the letter's own queue surface."""
    return [
        row
        for row in await outbox_rows(session, tenant_id, company_id)
        if row.kind == "reconfirmation"
    ]


async def enqueue(
    session: AsyncSession,
    store: FakeStore,
    scanner: FakeScanner,
    tenant: Tenant,
    parse: ReplyParse,
    communication: Communication,
    request: ConfirmationRequest | None,
    *,
    parsed_events: list | None = None,
    unlanded_events: list | None = None,
) -> reconfirmation.LetterReport:
    return await reconfirmation.enqueue_letter(
        session,
        store,
        scanner,
        tenant_id=tenant.id,
        parse_row=parse,
        communication=communication,
        request=request,
        parsed_events=[] if parsed_events is None else parsed_events,
        unlanded_events=[] if unlanded_events is None else unlanded_events,
        actor_user_id=None,
        now=AT,
    )


# --- Stored and released under automatic ---------------------------------------


async def test_the_letter_is_stored_and_released_under_automatic(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """The automatic client mode: the letter is the stored .eml (records
    registry, retention ``client_communication``), composed on the request's
    thread and naming the company, and the outbox row pends send-on-release."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    await seed_anchor(session, tenant.id, company.id, request.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )

    report = await enqueue(
        session,
        store,
        scanner_seam,
        tenant,
        parse,
        communication,
        request,
        parsed_events=[appointment_event()],
    )

    assert report.decision is ReconfirmationDecision.RECONFIRMATION_LETTER
    assert report.held is False
    assert report.outbox_id is not None
    assert report.document_id is not None

    # The records-registry home: the letter prose lives here and nowhere else.
    (document,) = (
        await session.scalars(select(Document).where(Document.id == report.document_id))
    ).all()
    assert document.kind == "generated_draft"
    assert document.origin == "generated"
    assert document.retention_class == "client_communication"
    assert document.content_type == "message/rfc822"
    assert document.scan_status == "clean"
    assert document.company_id == company.id
    raw = store.objects[document.storage_key]

    # The outbox row rides the same queue, naming its own stored letter.
    (row,) = await reconfirmation_rows(session, tenant.id, company.id)
    assert row.id == report.outbox_id
    assert row.kind == "reconfirmation"
    assert row.status == OutboxStatus.PENDING.value
    assert row.destination == "client"
    assert row.confirmation_request_id == request.id
    assert row.letter_document_id == document.id
    assert row.not_before is None  # send-on-release, the automatic posture
    assert row.created_by is None  # the parse cycle is the system actor

    # Send-on-release: a delivery cycle at the enqueue instant owes it.
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    assert [job.id for job in due] == [row.id]

    # The message itself: threaded on the request, addressed to the request's
    # own recipients, identifying the company by name and CR number.
    message = message_from_bytes(raw, policy=default_policy)
    assert str(message["From"]) == MAILBOX
    assert str(message["To"]) == CONTACT
    assert str(message["Message-ID"]).endswith("@tcsp.example>")
    assert str(message["In-Reply-To"]) == ANCHOR_MESSAGE_ID
    assert str(message["References"]) == ANCHOR_MESSAGE_ID
    assert (
        str(message["Subject"])
        == f"[{COMPANY_NAME} — CR {CR_NUMBER}] "
        "Reconfirmation — please confirm the changes you reported"
    )
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert f"Thank you for your reply regarding {COMPANY_NAME} (CR Number {CR_NUMBER})" in text
    assert f"（公司註冊編號{CR_NUMBER}）" in text
    assert QUESTION_BILINGUAL in text
    assert "Confirmed / 確認無誤" in text
    assert "Appointment of a director / 委任董事" in text
    assert "Wong New Director" in text
    assert "2026-02-01" in text

    # The audit chain: the letter's entry, coordinates only. (The registry's
    # own document_registered entry is the seam's, asserted by its shape.)
    entries = await company_audit(session, tenant.id, company.id)
    actions = [entry.action for entry in entries]
    assert actions == ["document_registered", "reconfirmation_enqueued"]
    (registered, enqueued) = entries
    assert registered.entity_type == "document"
    assert enqueued.entity_type == "confirmation_request"
    assert enqueued.entity_id == request.id
    assert enqueued.actor_user_id is None
    assert enqueued.details == {
        "parse_id": str(parse.id),
        "communication_id": str(communication.id),
        "request_id": str(request.id),
        "decision": "reconfirmation_letter",
        "confirmation_count": 1,
        "change_count": 1,
        "clarification_count": 0,
        "send_mode": "automatic",
        "held": False,
        "not_before": None,
        "destination": "client",
        "outbox_id": str(row.id),
        "document_id": str(document.id),
        "met_pending_row": False,
    }
    assert SPAN_MARKER not in str(enqueued.details)


# --- Held under review_required, released on staff action -----------------------


async def test_review_required_holds_the_letter_until_the_staff_release_delivers_it(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review_required client mode: the row parks on HELD_NOT_BEFORE and no
    delivery cycle touches it; the staff release — the reminder release's own
    path — moves it to the press instant, and the accepted delivery records
    the outbound Communication with kind=reconfirmation on the request."""
    tenant, company = await seed_company(session)
    await hold_client_sends(session, tenant.id)
    request = await seed_request(session, tenant.id, company.id)
    await seed_anchor(session, tenant.id, company.id, request.id)
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )

    report = await enqueue(
        session,
        store,
        scanner_seam,
        tenant,
        parse,
        communication,
        request,
        parsed_events=[appointment_event()],
    )

    assert report.held is True
    (row,) = await reconfirmation_rows(session, tenant.id, company.id)
    assert row.not_before == HELD_NOT_BEFORE
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    assert due == []  # owed but not due: nothing may leave before a person

    entries = await company_audit(session, tenant.id, company.id)
    (enqueued,) = [entry for entry in entries if entry.action == "reconfirmation_enqueued"]
    assert enqueued.details["held"] is True
    assert enqueued.details["send_mode"] == "review_required"
    assert enqueued.details["met_pending_row"] is False
    assert datetime.fromisoformat(enqueued.details["not_before"]) == HELD_NOT_BEFORE

    # The staff release: the send route's press posture for held follow-ups.
    released = await outbox.release_reminders(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        request_id=request.id,
        actor_user_id=admin.id,
        now=AT,
    )
    assert [job.id for job in released] == [row.id]
    assert row.not_before == AT
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    assert [job.id for job in due] == [row.id]

    (released_entry,) = [
        entry for entry in await company_audit(session, tenant.id, company.id)
        if entry.action == "reconfirmation_released"
    ]
    assert released_entry.entity_type == "confirmation_request"
    assert released_entry.entity_id == request.id
    assert released_entry.actor_user_id == admin.id
    assert released_entry.details == {
        "request_id": str(request.id),
        "outbox_id": str(row.id),
        "released_by": "staff",
        "released_at": AT.isoformat(),
    }

    # The delivery the release owed: same cycle, same legs, same reconcile.
    with FakeSmtpServer(password=APP_PASSWORD) as server:
        use_smtp(monkeypatch, server)
        outcome = await mail_send.deliver_one(session, store, job=row, now=AT)

    assert outcome.disposition is mail_send.SendDisposition.SENT
    assert len(server.messages) == 1
    assert server.messages[0].recipients == [CONTACT]

    # The exchange record: kind=reconfirmation, in the request's thread.
    (exchange,) = (
        await session.scalars(
            select(Communication).where(
                Communication.tenant_id == tenant.id,
                Communication.company_id == company.id,
                Communication.direction == "outbound",
            )
        )
    ).all()
    assert exchange.direction == "outbound"
    assert exchange.channel == "email"
    assert exchange.kind == "reconfirmation"
    assert exchange.confirmation_request_id == request.id
    assert exchange.sender_address == MAILBOX
    assert exchange.recipient_addresses == CONTACT
    assert exchange.raw_document_id == report.document_id
    assert exchange.mailbox_message_id is not None  # the SMTP leg's identity
    assert exchange.thread_id is None  # SMTP has no provider thread

    stored = (
        await session.scalars(select(Document).where(Document.id == report.document_id))
    ).one()
    assert server.messages[0].data == store.objects[stored.storage_key]

    await session.flush()
    (sent_row,) = await reconfirmation_rows(session, tenant.id, company.id)
    assert sent_row.status == OutboxStatus.SENT.value
    assert sent_row.sent_at == AT
    assert sent_row.communication_id == exchange.id
    assert sent_row.message_id == exchange.mailbox_message_id
    # The request the letter answers is already sent: no status move, no gate.
    assert sent_row.last_error is None
    assert sent_row.blocked_reason is None

    actions = [
        entry.action for entry in await company_audit(session, tenant.id, company.id)
    ]
    assert actions == [
        "document_registered",
        "reconfirmation_enqueued",
        "reconfirmation_released",
        "mail_message_sent",
    ]


async def test_a_second_enqueue_of_one_request_meets_the_pending_index(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """One pending reconfirmation row per request (the §5.1.12 widened index):
    a concurrent second enqueue re-reads the winner's row and reports
    fresh-vs-met, the reminder rule made explicit."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    letter_document = Document(
        tenant_id=tenant.id,
        company_id=company.id,
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="e" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(letter_document)
    await session.flush()

    first = await outbox.enqueue_reconfirmation(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        request_id=request.id,
        letter_document_id=letter_document.id,
        not_before=None,
    )
    second = await outbox.enqueue_reconfirmation(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        request_id=request.id,
        letter_document_id=letter_document.id,
        not_before=HELD_NOT_BEFORE,
    )

    assert first.created is True
    assert second.created is False  # the met answer: the winner's row stands
    assert second.row.id == first.row.id
    assert second.row.not_before is None  # the winner's release posture stands
    assert (
        await session.scalars(
            select(MailOutbox).where(
                MailOutbox.confirmation_request_id == request.id,
                MailOutbox.kind == OutboxKind.RECONFIRMATION.value,
            )
        )
    ).all() == [first.row]


async def test_a_second_letter_for_one_request_reports_the_met_pending_row(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """Two replies for one request parsed while the first letter still pends —
    a wide window under ``review_required``, where letters wait for staff.
    The widened index re-reads the pending row, and both the audit entry and
    the report name the letter the row will actually deliver: never pairing
    the fresh, undelivered document with the pre-existing row. The fresh
    document stands, registered, as the record of what the second parse
    asked."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    first_parse, first_communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )
    second_parse, second_communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )

    first = await enqueue(
        session,
        store,
        scanner_seam,
        tenant,
        first_parse,
        first_communication,
        request,
        parsed_events=[appointment_event()],
    )
    second = await enqueue(
        session,
        store,
        scanner_seam,
        tenant,
        second_parse,
        second_communication,
        request,
        parsed_events=[appointment_event()],
    )

    assert first.met_pending_row is False
    assert second.met_pending_row is True
    assert second.outbox_id == first.outbox_id  # the one pending row stands
    assert second.document_id is None  # no new letter enters the queue
    (row,) = await reconfirmation_rows(session, tenant.id, company.id)
    assert row.letter_document_id == first.document_id

    # The stored record of the second ask exists; the row still names the
    # first letter as what the client will receive.
    stored = (
        await session.scalars(
            select(Document).where(
                Document.tenant_id == tenant.id,
                Document.company_id == company.id,
                Document.origin == "generated",
            )
        )
    ).all()
    assert len(stored) == 3  # the request's letter plus one stored per parse
    assert first.document_id in {document.id for document in stored}
    assert row.letter_document_id in {document.id for document in stored}

    (fresh_entry, met_entry) = [
        entry
        for entry in await company_audit(session, tenant.id, company.id)
        if entry.action == "reconfirmation_enqueued"
    ]
    assert fresh_entry.details["met_pending_row"] is False
    assert fresh_entry.details["document_id"] == str(first.document_id)
    assert met_entry.details["parse_id"] == str(second_parse.id)
    assert met_entry.details["met_pending_row"] is True
    assert met_entry.details["outbox_id"] == str(first.outbox_id)
    # The honest pairing: the entry names the letter the pending row delivers,
    # not the fresh document the row will never carry.
    second_document_id = {document.id for document in stored} - {
        request.document_id,
        first.document_id,
    }
    assert row.letter_document_id == first.document_id
    assert row.letter_document_id not in second_document_id
    assert met_entry.details["document_id"] == str(first.document_id)
    assert met_entry.details["document_id"] not in {
        str(document_id) for document_id in second_document_id
    }


# --- The clarification letter and custody ----------------------------------------


async def test_the_clarification_letter_quotes_the_unlanded_proposal(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """A grammar-incomplete proposal never lands — it becomes a per-field
    question quoting the client's own words, and the landed part of the parse
    is restated beside it. The quote reaches only its designed home (the
    stored letter); the audit entry carries counts, never the span."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    await seed_anchor(session, tenant.id, company.id, request.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="client_clarification",
        confirmations=[],
        event_ids=[],
    )
    unlanded = [{"index": 0, **unclear_cessation_event(), "reason": "grammar_incomplete"}]

    report = await enqueue(
        session,
        store,
        scanner_seam,
        tenant,
        parse,
        communication,
        request,
        parsed_events=[unclear_cessation_event()],
        unlanded_events=unlanded,
    )

    assert report.decision is ReconfirmationDecision.CLARIFICATION_LETTER
    (document,) = (
        await session.scalars(select(Document).where(Document.id == report.document_id))
    ).all()
    message = message_from_bytes(store.objects[document.storage_key], policy=default_policy)
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    # The client's own words, quoted back — the letter is where a quote lives.
    assert "Clarifications we need / 需要澄清之處：" in text
    assert "change_events[0].effective_date" in text
    assert f"「retire Wong New Director next month {SPAN_MARKER}」" in text
    # No change restated: the proposal never landed.
    assert "Changes you reported" not in text

    (enqueued,) = [
        entry
        for entry in await company_audit(session, tenant.id, company.id)
        if entry.action == "reconfirmation_enqueued"
    ]
    assert enqueued.details["decision"] == "clarification_letter"
    assert enqueued.details["change_count"] == 0
    assert enqueued.details["clarification_count"] == 1
    assert SPAN_MARKER not in str(enqueued.details)


async def test_the_custody_search_finds_the_span_nowhere_else(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The planted span marker appears in no audit entry, no log line, and no
    report — while the stored letter proves the search is not vacuous."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    await seed_anchor(session, tenant.id, company.id, request.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="client_clarification",
        confirmations=[],
        event_ids=[],
    )
    with caplog.at_level(logging.INFO):
        report = await enqueue(
            session,
            store,
            scanner_seam,
            tenant,
            parse,
            communication,
            request,
            parsed_events=[unclear_cessation_event()],
            unlanded_events=[
                {"index": 0, **unclear_cessation_event(), "reason": "grammar_incomplete"}
            ],
        )

    (document,) = (
        await session.scalars(select(Document).where(Document.id == report.document_id))
    ).all()
    message = message_from_bytes(store.objects[document.storage_key], policy=default_policy)
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert SPAN_MARKER in body.get_content()
    stored = " ".join(
        str(piece)
        for row in (
            await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant.id))
        ).all()
        for piece in (row.details, row.llm_call, row.action, row.entity_type)
    )
    assert SPAN_MARKER not in stored
    assert SPAN_MARKER not in caplog.text
    assert SPAN_MARKER not in str(report)


# --- No letter cases --------------------------------------------------------------


async def test_nothing_to_ask_writes_nothing(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """R6: a clean confirmed_no_change — no events, no uncertainties — asks
    nothing: no document, no outbox row, no reconfirmation audit entry."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="confirmed_no_change",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[],
    )

    report = await enqueue(
        session, store, scanner_seam, tenant, parse, communication, request
    )

    assert report.decision is ReconfirmationDecision.NO_LETTER
    assert report.outbox_id is None
    assert report.document_id is None
    assert report.held is None
    assert await outbox_rows(session, tenant.id, company.id) == []
    # The seeds hold the request's stored letter and the reply's raw document;
    # a no-letter decision stores nothing of its own.
    letter_documents = (
        await session.scalars(
            select(Document).where(
                Document.tenant_id == tenant.id,
                Document.company_id == company.id,
                Document.origin == "generated",
            )
        )
    ).all()
    assert len(letter_documents) == 1  # the request's own stored letter only
    assert [
        entry.action
        for entry in await company_audit(session, tenant.id, company.id)
        if entry.action.startswith("reconfirmation_")
    ] == []


async def test_a_staff_review_parse_writes_no_letter(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """A wholly unclear reply with a parse row is staff's to read — never a
    client letter."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="unclear",
        routing=None,
        confirmations=[],
        event_ids=[],
    )

    report = await enqueue(
        session, store, scanner_seam, tenant, parse, communication, request
    )

    assert report.decision is ReconfirmationDecision.STAFF_REVIEW
    assert report.outbox_id is None
    assert await outbox_rows(session, tenant.id, company.id) == []


# --- Fail-closed refusals ----------------------------------------------------------


async def test_a_letter_without_a_joined_request_refuses(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """The outbox's request reference is NOT NULL and the letter needs the
    request's recipients, thread and checklist: an unsolicited reply whose
    decision is a letter is a typed refusal, never a guessed recipient."""
    tenant, company = await seed_company(session)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        None,
        status="changes_reported",
        routing="auto_accept",
        confirmations=[],
        event_ids=[str(uuid4())],
    )

    with pytest.raises(reconfirmation.ReconfirmationError) as excinfo:
        await enqueue(
            session,
            store,
            scanner_seam,
            tenant,
            parse,
            communication,
            None,
            parsed_events=[appointment_event()],
        )

    assert excinfo.value.reason == "no_confirmation_request"
    assert await outbox_rows(session, tenant.id, company.id) == []


async def test_an_unreadable_send_mode_block_fails_the_letter_closed(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """The mode that decides release-vs-hold must be the tenant's own: a block
    nobody can read refuses the letter, never defaults to sending."""
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={"document": "{Bogus}", "generated": "{Nope}"},
            milestones=default_milestones().as_dict(),
            send_modes={"client": "sideways", "government": "review_required"},
        )
    )
    await session.flush()
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )

    with pytest.raises(reconfirmation.ReconfirmationError) as excinfo:
        await enqueue(
            session,
            store,
            scanner_seam,
            tenant,
            parse,
            communication,
            request,
            parsed_events=[appointment_event()],
        )

    assert excinfo.value.reason == "tenant_config_invalid"
    assert await outbox_rows(session, tenant.id, company.id) == []


async def test_no_configured_mailbox_refuses_the_letter(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The From address is what identifies the TCSP's letter (and stamps the
    Message-ID's domain): without one there is no letter to compose."""
    monkeypatch.setattr(settings, "mail_address", " ")
    tenant, company = await seed_company(session)
    request = await seed_request(session, tenant.id, company.id)
    parse, communication = await seed_parse(
        session,
        tenant.id,
        company.id,
        request.id,
        status="changes_reported",
        routing="auto_accept",
        confirmations=CONFIRMATIONS,
        event_ids=[str(uuid4())],
    )

    with pytest.raises(reconfirmation.ReconfirmationError) as excinfo:
        await enqueue(
            session,
            store,
            scanner_seam,
            tenant,
            parse,
            communication,
            request,
            parsed_events=[appointment_event()],
        )

    assert excinfo.value.reason == "mail_not_configured"
    assert await outbox_rows(session, tenant.id, company.id) == []


# --- The parse cycle's tail ---------------------------------------------------------


REPLY_BODY = (
    "All confirmed, the director is unchanged. Also please appoint "
    "Wong New Director as a director effective 1 February 2026."
)


def raw_message(body: str) -> bytes:
    """A minimal stored reply: one text/plain body, nothing else."""
    return (
        "From: director@example.hk\r\n"
        "To: ai-filings@tcsp.example\r\n"
        "Subject: Re: Annual return confirmation\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}"
    ).encode()


async def seed_reply(
    session: AsyncSession,
    tenant: Tenant,
    company: Company,
    store: FakeStore,
    *,
    body: str = REPLY_BODY,
    request: ConfirmationRequest | None = None,
) -> Communication:
    """A stored raw reply document (scan-clean) and its Communication row."""
    document = Document(
        tenant_id=tenant.id,
        company_id=company.id,
        kind="client_reply",
        origin="email_attachment",
        retention_class="client_communication",
        sha256="b" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=len(body),
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(document)
    await session.flush()
    communication = Communication(
        tenant_id=tenant.id,
        company_id=company.id,
        direction="inbound",
        channel="email",
        kind="reply",
        thread_id=f"thread-{uuid4()}",
        sender_address=CONTACT,
        confirmation_request_id=request.id if request is not None else None,
        received_at=datetime(2026, 6, 29, tzinfo=UTC),
        raw_document_id=document.id,
    )
    session.add(communication)
    await session.flush()
    store.objects[document.storage_key] = raw_message(body)
    return communication


async def parse_one(
    session: AsyncSession,
    store: FakeStore,
    scanner: FakeScanner,
    tenant: Tenant,
    communication_id: UUID,
) -> reply_parsing.ParseReport:
    return await reply_parsing.parse_reply(
        session,
        store=store,
        scanner=scanner,
        tenant_id=tenant.id,
        communication_id=communication_id,
        as_of=AS_OF,
    )


async def test_the_parse_cycle_lands_the_letter_on_the_same_session(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tail wiring end to end: one parsed reply whose decision is a letter
    stores the letter and pends the outbox row through the parse cycle's own
    session — the report's outcome stays ``parsed`` and the audit chain names
    every fact in order."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id)
    request = await seed_request(session, tenant.id, company.id)
    communication = await seed_reply(session, tenant, company, store, request=request)
    fake = FakeInfer()
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, scanner_seam, tenant, communication.id)

    assert report.outcome == "parsed"
    assert report.status == "changes_reported"
    assert len(fake.calls) == 1

    (parse_row,) = (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == communication.id)
        )
    ).all()
    (row,) = await outbox_rows(session, tenant.id, company.id)
    assert row.kind == "reconfirmation"
    assert row.status == OutboxStatus.PENDING.value
    (document,) = (
        await session.scalars(select(Document).where(Document.id == row.letter_document_id))
    ).all()
    assert document.company_id == company.id
    message = message_from_bytes(store.objects[document.storage_key], policy=default_policy)
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert "Wong New Director" in body.get_content()

    actions = [entry.action for entry in await company_audit(session, tenant.id, company.id)]
    assert actions == [
        "change_event_recorded",
        "reply_parsed",
        "document_registered",
        "reconfirmation_enqueued",
    ]
    (enqueued,) = [
        entry
        for entry in await company_audit(session, tenant.id, company.id)
        if entry.action == "reconfirmation_enqueued"
    ]
    assert enqueued.details["parse_id"] == str(parse_row.id)
    assert enqueued.details["request_id"] == str(request.id)


async def test_a_letter_failure_is_that_message_s_outcome_and_the_cycle_continues(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A letter that cannot compose reports as that message's outcome and
    never aborts the cycle: the company without a name gets
    ``letter_failed:company_name_unavailable``, the named company's reply
    behind it parses and lands its letter on the same cycle."""
    tenant, named = await seed_company(session)
    unnamed = Company(
        tenant_id=tenant.id,
        cr_number="7654321",
        br_number="87654321",
        date_of_incorporation="2012-05-05",
        anniversary_date="2026-05-05",
        type="private",
    )
    session.add(unnamed)
    await session.flush()
    await cover(session, tenant, named.id)
    await cover(session, tenant, unnamed.id)
    named_request = await seed_request(session, tenant.id, named.id)
    await seed_anchor(session, tenant.id, named.id, named_request.id)
    unnamed_request = await seed_request(session, tenant.id, unnamed.id)
    first = await seed_reply(session, tenant, named, store, request=named_request)
    second = await seed_reply(session, tenant, unnamed, store, request=unnamed_request)
    fake = FakeInfer()
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await reply_parsing.run_cycle(
        session,
        store=store,
        scanner=scanner_seam,
        tenant_id=tenant.id,
        as_of=AS_OF,
    )

    assert report.examined == 2
    outcomes = {item.communication_id: item.outcome for item in report.outcomes}
    assert outcomes == {
        first.id: "parsed",
        second.id: "letter_failed:company_name_unavailable",
    }
    # The failure landed the parse and proposed the event; it wrote no letter.
    (parse_row,) = (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == second.id)
        )
    ).all()
    assert parse_row.status == "changes_reported"
    assert len(parse_row.event_ids) == 1
    assert [
        row
        for row in await outbox_rows(session, tenant.id, unnamed.id)
        if row.kind == "reconfirmation"
    ] == []
    # The message behind it: parsed, and its letter is waiting on the queue.
    named_rows = await outbox_rows(session, tenant.id, named.id)
    assert [row.kind for row in named_rows if row.kind == "reconfirmation"] == ["reconfirmation"]


async def test_an_unsolicited_letter_decision_without_a_request_reports_the_refusal(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that joined no request can still parse to a letter decision;
    the refusal is the message's outcome and nothing is guessed."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id)
    communication = await seed_reply(session, tenant, company, store)
    fake = FakeInfer()
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, scanner_seam, tenant, communication.id)

    assert report.outcome == "letter_failed:no_confirmation_request"
    assert report.status == "changes_reported"
    assert await outbox_rows(session, tenant.id, company.id) == []
