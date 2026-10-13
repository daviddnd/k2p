"""DB-backed scenarios for the reply-parser schema and pipeline (slice M5b
tasks 2-3; opt-in; needs a migrated PostgreSQL).

Migration ``0029_reply_parsing`` against a real database — the schema the
parser pipeline (Task 3) and the reconfirmer (Task 4) build on:

1. **the migration's own round trip** — downgrade drops ``reply_parses``
   whole and the two assessment columns with it, narrows the outbox ``kind``
   CHECK back, and upgrade restores all three exactly;
2. **the widened outbox CHECK** — a ``reconfirmation`` row is insertable (the
   reconfirmer's kind rides the same outbox), a bogus kind is refused however
   the write arrived, and after the downgrade the same ``reconfirmation``
   insert is refused again — the narrowing round-trips, not just the name;
3. **the ``ReplyParse`` row** — the schema's six statuses all insert, anything
   outside is refused, and a full insert/select round-trips the parsed
   ``confirmations[]``, the proposed ``event_ids``, the nullable request
   join, language and routing;
4. **the resolver's two assessment columns** — ``resolved`` / ``ambiguous`` /
   ``no_match`` and NULL round-trip, with no CHECK: the three-case vocabulary
   is the resolver domain's, and the schema does not restate it.

The Task 3 section drives the application parser end to end with the gateway
seam faked (``ai_gateway.infer`` replaced — no suite talks to a provider):
one ``infer`` call per reply; the typed-error mapping (pre-flight refusal
writes nothing; unavailability writes nothing beyond the gateway's own
booking; grammar-invalid output lands one ``staff_review`` assessment and
nothing else); the §5.1 gate denial reported per message without aborting
the cycle; the language fill; the parse row, the assessment and the proposed
events landing on one session; grammar-incomplete proposals routing to
clarification with no event created; the withdrawal and correction linkage;
and the custody search — planted reply markers appear in no audit entry, log
line, or report.

Task 5 adds the reconfirmation exchange over the parse: a reply joining the
reconfirmation's request thread fires §4.1's system ``client_reply`` — every
event of the company standing ``client_reconfirming`` returns to ``pending``
on the same session, the reply named as the move's source — while an
unsolicited reply (no thread joined) leaves the event where it is; and a
withdrawal reply withdraws the very event that was out for reconfirmation,
``client_reconfirming → withdrawn`` with no ``client_reply`` behind it.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (with ``0029_reply_parsing`` applied). Every test runs inside a
transaction that is rolled back, so the review database stays clean.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Company, Tenant
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.reply_parsing import ReplyParse
from app.adapters.persistence.service_packages import ServiceAssignment
from app.application import ai_gateway, change_events, reply_parsing, service_packages
from app.core.config import settings
from app.domain.ai_gateway import TaskType
from app.domain.change_events import (
    ChangeEventAction,
    ChangeEventRelation,
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
)
from app.domain.rbac import ActorContext, Role
from app.domain.reply_parsing import EarlierEvent
from tests.fake_scanning import FakeScanner

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")


def _load_revision() -> object:
    """The 0029 revision module, importable without the versions dir being a package."""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0029_reply_parsing.py"
    )
    spec = importlib.util.spec_from_file_location("revision_0029_reply_parsing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION = _load_revision()

INSERT_RECONFIRMATION = text(
    "INSERT INTO mail_outbox (id, tenant_id, company_id, confirmation_request_id,"
    " destination, status, kind)"
    " VALUES (:id, :tenant, :company, :request, 'client', 'pending', 'reconfirmation')"
)

SIX_STATUSES = (
    "confirmed_no_change",
    "changes_reported",
    "correction",
    "withdrawal",
    "unclear",
    "off_topic",
)


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


async def seed_company(session: AsyncSession) -> tuple[Tenant, Company]:
    """A tenant and one company — the F1 anchor every row below pins to,
    named so a parse whose decision is a letter can compose (Task 4's tail)."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    company = Company(
        tenant_id=tenant.id,
        cr_number="1234567",
        br_number="12345678",
        date_of_incorporation="2010-03-01",
        anniversary_date="2026-03-01",
        type="private",
    )
    session.add(company)
    await session.flush()
    session.add(
        CompanyNameRecord(
            tenant_id=tenant.id,
            company_id=company.id,
            effective_date=date(2015, 1, 1),
            source_type="manual",
            name_en="Kairos Trading Limited",
            name_zh=None,
            business_name=None,
        )
    )
    await session.flush()
    return tenant, company


async def seed_document(session: AsyncSession, tenant_id: UUID, company_id: UUID) -> Document:
    document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="client_reply",
        origin="email_attachment",
        retention_class="client_communication",
        sha256="a" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
    )
    session.add(document)
    await session.flush()
    return document


async def seed_request(
    session: AsyncSession, tenant_id: UUID, company_id: UUID, document_id: UUID
) -> ConfirmationRequest:
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=date(2026, 3, 1),
        reply_by=date(2026, 1, 15),
        status="sent",
        version_no=1,
        document_id=document_id,
        checklist=[{"fact_id": "officers.0.name_en", "question": "Is the director unchanged?"}],
        recipients=[{"person_id": None, "address": "director@example.hk"}],
    )
    session.add(request)
    await session.flush()
    return request


async def seed_communication(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    document_id: UUID,
    request_id: UUID,
) -> Communication:
    communication = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction="inbound",
        channel="email",
        kind="reply",
        thread_id=f"thread-{uuid4()}",
        sender_address="director@example.hk",
        confirmation_request_id=request_id,
        received_at=datetime.now(UTC),
        raw_document_id=document_id,
    )
    session.add(communication)
    await session.flush()
    return communication


# --- The migration's own round trip ----------------------------------------


async def test_migration_upgrade_downgrade_round_trip(session: AsyncSession) -> None:
    """Downgrade drops the parse's business home and the resolver columns and
    narrows the outbox CHECK; upgrade restores the full shape — and the
    ``reconfirmation`` insert the wide CHECK admits is refused again the
    moment the schema narrows, so the CHECK itself round-trips, not its name.
    The expected failure runs on a savepoint: a refused statement would abort
    the outer transaction the round trip still needs."""

    def _shape(
        sync_connection: object,
    ) -> tuple[set[str], set[str], set[str], set[str]]:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        assessment_columns = {
            column["name"] for column in inspector.get_columns("extraction_assessments")
        }
        parse_columns = (
            {column["name"] for column in inspector.get_columns("reply_parses")}
            if "reply_parses" in tables
            else set()
        )
        parse_checks = (
            {check["name"] for check in inspector.get_check_constraints("reply_parses")}
            if "reply_parses" in tables
            else set()
        )
        return tables, assessment_columns, parse_columns, parse_checks

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION, direction)()

        return _drive

    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)

    connection = await session.connection()
    await connection.run_sync(_run("downgrade"))
    tables, assessment_columns, _, _ = await connection.run_sync(_shape)
    assert "reply_parses" not in tables
    assert "raw_model_resolution" not in assessment_columns
    assert "enforced_resolution" not in assessment_columns
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_RECONFIRMATION,
                {"id": uuid4(), "tenant": tenant.id, "company": company.id, "request": request.id},
            )

    await connection.run_sync(_run("upgrade"))
    tables, assessment_columns, parse_columns, parse_checks = await connection.run_sync(_shape)
    assert "reply_parses" in tables
    assert "raw_model_resolution" in assessment_columns
    assert "enforced_resolution" in assessment_columns
    assert {
        "id",
        "tenant_id",
        "company_id",
        "communication_id",
        "confirmation_request_id",
        "status",
        "language",
        "routing",
        "confirmations",
        "event_ids",
        "created_at",
    } <= parse_columns
    assert parse_checks == {
        "reply_parses_status_check",
        "reply_parses_language_check",
        "reply_parses_routing_check",
    }
    # The widened CHECK is live again: the same insert the narrow schema
    # refused now lands.
    await session.execute(
        INSERT_RECONFIRMATION,
        {"id": uuid4(), "tenant": tenant.id, "company": company.id, "request": request.id},
    )
    await session.flush()


# --- The widened outbox CHECK ---------------------------------------------------


async def test_outbox_kind_check_admits_reconfirmation_and_refuses_bogus_kind(
    session: AsyncSession,
) -> None:
    """The reconfirmer's row rides the same outbox: ``reconfirmation`` inserts,
    a kind outside the closed vocabulary is refused by the schema itself."""
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)

    reconfirmation = MailOutbox(
        tenant_id=tenant.id,
        company_id=company.id,
        confirmation_request_id=request.id,
        destination="client",
        status="pending",
        kind="reconfirmation",
        letter_document_id=document.id,
    )
    session.add(reconfirmation)
    await session.flush()

    bogus = MailOutbox(
        tenant_id=tenant.id,
        company_id=company.id,
        confirmation_request_id=request.id,
        destination="client",
        status="pending",
        kind="newsletter",
    )
    session.add(bogus)
    with pytest.raises(IntegrityError):
        await session.flush()


# --- The ReplyParse row -----------------------------------------------------------


@pytest.mark.parametrize("status", SIX_STATUSES)
async def test_reply_parse_status_vocabulary_inserts(session: AsyncSession, status: str) -> None:
    """Every status the reply-parse grammar names is a row the schema admits."""
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication = await seed_communication(
        session, tenant.id, company.id, document.id, request.id
    )
    session.add(
        ReplyParse(
            tenant_id=tenant.id,
            company_id=company.id,
            communication_id=communication.id,
            status=status,
            confirmations=[],
            event_ids=[],
        )
    )
    await session.flush()


async def test_reply_parse_status_vocabulary_refuses_unknown(session: AsyncSession) -> None:
    """The status vocabulary is closed at the schema, not only in the domain."""
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication = await seed_communication(
        session, tenant.id, company.id, document.id, request.id
    )
    session.add(
        ReplyParse(
            tenant_id=tenant.id,
            company_id=company.id,
            communication_id=communication.id,
            status="confirmed-no-change",
            confirmations=[],
            event_ids=[],
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_reply_parse_insert_select_round_trip(session: AsyncSession) -> None:
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication = await seed_communication(
        session, tenant.id, company.id, document.id, request.id
    )

    proposed_event_id = uuid4()
    parse = ReplyParse(
        tenant_id=tenant.id,
        company_id=company.id,
        communication_id=communication.id,
        confirmation_request_id=request.id,
        status="changes_reported",
        language="zh-hk",
        routing="client_clarification",
        confirmations=[
            {
                "fact_id": "officers.0.name_en",
                "answer": "confirmed",
                "evidence_span": "董事維持不變",
                "uncertain": False,
            }
        ],
        event_ids=[str(proposed_event_id)],
    )
    session.add(parse)
    await session.flush()

    stored = (await session.scalars(select(ReplyParse).where(ReplyParse.id == parse.id))).one()
    assert stored.tenant_id == tenant.id
    assert stored.company_id == company.id
    assert stored.communication_id == communication.id
    assert stored.confirmation_request_id == request.id
    assert stored.status == "changes_reported"
    assert stored.language == "zh-hk"
    assert stored.routing == "client_clarification"
    assert stored.confirmations == parse.confirmations
    assert stored.event_ids == [str(proposed_event_id)]
    assert stored.created_at is not None

    # The nullable legs: an off-topic reply with no request joined, no
    # language, no routing, and nothing confirmed or proposed.
    unsolicited = ReplyParse(
        tenant_id=tenant.id,
        company_id=company.id,
        communication_id=communication.id,
        status="off_topic",
        confirmations=[],
        event_ids=[],
    )
    session.add(unsolicited)
    await session.flush()
    stored_unsolicited = (
        await session.scalars(select(ReplyParse).where(ReplyParse.id == unsolicited.id))
    ).one()
    assert stored_unsolicited.confirmation_request_id is None
    assert stored_unsolicited.language is None
    assert stored_unsolicited.routing is None
    assert stored_unsolicited.confirmations == []
    assert stored_unsolicited.event_ids == []


# --- The resolver's two assessment columns ----------------------------------------


async def test_assessment_resolution_columns_round_trip_the_three_resolver_cases(
    session: AsyncSession,
) -> None:
    """The model fallback's pair — what the model answered, what the rule
    enforced — carries the resolver's three cases and NULL (the deterministic
    rule's runs leave both unset), with no CHECK: the three-case vocabulary
    is the resolver domain's own, and the schema does not restate it."""
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication = await seed_communication(
        session, tenant.id, company.id, document.id, request.id
    )

    def assessment(resolution: str | None, enforced: str | None) -> ExtractionAssessment:
        return ExtractionAssessment(
            tenant_id=tenant.id,
            subject_type="communication",
            subject_id=communication.id,
            component="multi_company_resolver",
            model="reply-resolver/v1",
            field_scores={"communication.company": 0.7},
            signal_log={"resolution": "model_fallback"},
            raw_model_resolution=resolution,
            enforced_resolution=enforced,
        )

    session.add(assessment("resolved", "resolved"))
    session.add(assessment("resolved", "ambiguous"))
    session.add(assessment(None, "no_match"))
    session.add(assessment(None, None))
    await session.flush()

    stored = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_id == communication.id
            )
        )
    ).all()
    pairs = {(row.raw_model_resolution, row.enforced_resolution) for row in stored}
    assert pairs == {
        ("resolved", "resolved"),
        ("resolved", "ambiguous"),
        (None, "no_match"),
        (None, None),
    }


# --- Task 3: the application parser -----------------------------------------


AS_OF = date(2026, 6, 30)
COVERAGE = {"coverage_start": date(2026, 1, 1), "coverage_end": date(2026, 12, 31)}
# A marker planted in the reply body that no evidence span quotes: the
# custody search hunts it outside its homes (the stored document, the
# reply_parses/assessment rows), and a natural-person name planted the same
# way — the strongest leak signal.
PLANTED_MARKER = "ZXCVPLANTED99A"
PLANTED_PERSON = "Seedonly Personq3z"

REPLY_BODY = (
    "All confirmed, the director is unchanged. Also please appoint "
    "Wong New Director as a director effective 1 February 2026. "
    f"Reference {PLANTED_MARKER}. Send any paperwork to {PLANTED_PERSON}."
)


def raw_message(body: str) -> bytes:
    """A minimal stored reply: one text/plain body, nothing else."""
    return (
        "From: director@example.hk\r\n"
        "To: secretary@mailbox.example.hk\r\n"
        "Subject: Re: Annual return confirmation\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}"
    ).encode()


def parse_result(**overrides: object) -> dict:
    """A grammar-valid scripted model answer; spans are verbatim reply text."""
    base: dict = {
        "status": "changes_reported",
        "language": "en",
        "confirmations": [
            {
                "fact_id": "officers.0.name_en",
                "answer": "confirmed",
                "evidence_span": "the director is unchanged",
                "uncertain": False,
            }
        ],
        "change_events": [
            {
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
                "evidence_span": (
                    "appoint Wong New Director as a director effective 1 February 2026"
                ),
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ],
        "multi_company_signals": {"company_mentions": []},
    }
    base.update(overrides)
    return base


def withdrawal_result() -> dict:
    """A withdrawal parse referring to the seeded earlier appointment."""
    return parse_result(
        status="withdrawal",
        change_events=[
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": "Chan Tai Man",
                    "name_en": "Chan Tai Man",
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": "withdraw Chan Tai Man",
                    "uncertain": False,
                },
                "effective_date": None,
                "effective_date_text": "",
                "evidence_span": "withdraw Chan Tai Man",
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ],
    )


class FakeStore:
    """The DocumentStore seam: serves the planted raw message bytes, records
    what the landing stores (the reconfirmer's letter, Task 4's tail)."""

    def __init__(self, bodies: Mapping[str, bytes]) -> None:
        self.objects: dict[str, bytes] = dict(bodies)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        raise AssertionError("the parse cycle never signs URLs")


class FakeInfer:
    """The replaced gateway seam: records the call, then answers or raises."""

    def __init__(
        self,
        result: object = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._result = parse_result() if result is None else result
        self._error = error

    async def infer(self, session: AsyncSession, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return ai_gateway.InferenceResult(
            result=self._result,
            provider="stub-provider",
            model_id="stub-model",
            prompt_name="reply_parser",
            prompt_version=1,
        )


@pytest.fixture(autouse=True)
def licensed_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse scenarios run on a licensed tier, like every gating scenario —
    and with a mailbox address, since a parse whose decision is a letter
    composes one through the reconfirmer (Task 4's tail)."""
    monkeypatch.setattr(settings, "license_tier", "standard")
    monkeypatch.setattr(settings, "mail_address", "ai-filings@tcsp.example")


@pytest.fixture
def fake_infer(monkeypatch: pytest.MonkeyPatch) -> FakeInfer:
    seam = FakeInfer()
    monkeypatch.setattr(ai_gateway, "infer", seam.infer)
    return seam


async def cover(
    session: AsyncSession, tenant: Tenant, company_id: UUID, *, services: list[str]
) -> None:
    """A package and an active assignment — the §5.1 gate's commercial legs."""
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,  # service-level fixture: system actor
        name=f"pkg-{uuid4()}",
        included_services=services,
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


async def seed_reply(
    session: AsyncSession,
    tenant: Tenant,
    company: Company,
    *,
    body: str = REPLY_BODY,
    request: ConfirmationRequest | None = None,
    direction: str = "inbound",
    received_at: datetime | None = None,
    bodies: dict[str, bytes] | None = None,
) -> tuple[Communication, FakeStore]:
    """A stored raw reply document (scan-clean) and its Communication row.

    ``bodies`` collects the raw bytes so one FakeStore can serve a whole
    cycle's documents; a per-seed store is built when it is omitted."""
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
        direction=direction,
        channel="email",
        kind="reply" if direction == "inbound" else "confirmation_request",
        thread_id=f"thread-{uuid4()}",
        sender_address="director@example.hk",
        confirmation_request_id=request.id if request is not None else None,
        received_at=received_at if received_at is not None else datetime.now(UTC),
        raw_document_id=document.id,
    )
    session.add(communication)
    await session.flush()
    raw = raw_message(body)
    if bodies is not None:
        bodies[document.storage_key] = raw
        return communication, FakeStore(bodies)
    return communication, FakeStore({document.storage_key: raw})


async def seed_earlier_event(
    session: AsyncSession, tenant: Tenant, company: Company
) -> ChangeEvent:
    """One pending director appointment the withdrawal/correction refer to."""
    view = await change_events.create(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
        effective_date=date(2026, 1, 15),
        payload={
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "Chan Tai Man",
                    "appointment_date": "2026-01-15",
                }
            }
        },
        as_of=AS_OF,
    )
    return view.current


async def parse_one(
    session: AsyncSession,
    store: FakeStore,
    tenant: Tenant,
    communication_id: UUID,
) -> reply_parsing.ParseReport:
    return await reply_parsing.parse_reply(
        session,
        store=store,
        scanner=FakeScanner(),
        tenant_id=tenant.id,
        communication_id=communication_id,
        as_of=AS_OF,
    )


async def test_parse_reply_lands_parse_assessment_events_and_language(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """The happy path end to end: one gateway call; the detected language on
    the Communication; the parse row, the pinned assessment and the proposed
    event all land on the same session; the parse's audit entry carries
    coordinates and counts only.

    The routing is ``staff_review``, and that is the happy path's own answer
    under ruling R-D: the reply proposes appointing ``Wong New Director``, a
    person this company's appointments do not carry (the fixture seeds none),
    so a run whose fields score 1.0 is capped one band below auto-accept and
    the assessment's log names the person who did it. Everything about the
    landing is unaffected — the cap decides who looks, not what is recorded.
    """
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication, store = await seed_reply(session, tenant, company, request=request)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "parsed"
    assert report.status == "changes_reported"
    # R-D: the proposed director is absent from the company's appointments.
    assert report.routing == "staff_review"
    assert len(report.event_ids) == 1

    # One deliberate gateway call: the right task, the prompt-binding
    # language, and the minimized field set.
    (call,) = fake_infer.calls
    assert call["tenant_id"] == tenant.id
    assert call["company_id"] == company.id
    assert call["task_type"] is TaskType.REPLY_PARSING
    assert call["language"] == "en"
    content = call["content"]
    assert set(content) == {"reply_text", "role_reference", "officer_name"}
    assert "the director is unchanged" in content["reply_text"]
    role_reference = json.loads(content["role_reference"])
    assert role_reference["checklist"] == [
        {"fact_id": "officers.0.name_en", "question": "Is the director unchanged?"}
    ]

    # The detected language lands on the Communication.
    stored_communication = (
        await session.scalars(select(Communication).where(Communication.id == communication.id))
    ).one()
    assert stored_communication.language == "en"

    # The parse's business home.
    (parse_row,) = (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == communication.id)
        )
    ).all()
    assert parse_row.company_id == company.id
    assert parse_row.confirmation_request_id == request.id
    assert parse_row.status == "changes_reported"
    assert parse_row.language == "en"
    assert parse_row.routing == "staff_review"
    assert parse_row.confirmations == parse_result()["confirmations"]
    assert parse_row.event_ids == [str(report.event_ids[0])]

    # The proposed event landed through the change-event write path.
    (event,) = (
        await session.scalars(select(ChangeEvent).where(ChangeEvent.id == report.event_ids[0]))
    ).all()
    assert event.tenant_id == tenant.id
    assert event.company_id == company.id
    assert event.version_no == 1
    assert event.type == ChangeEventType.DIRECTOR_APPOINTMENT.value
    assert event.status == ChangeEventStatus.PENDING.value
    assert event.source_type == "communication"
    assert event.source_id == communication.id
    assert event.effective_date == date(2026, 2, 1)
    assert event.effective_date_text == "1 February 2026"
    assert event.payload == {
        "appointment": {
            "officer": {
                "role": "director",
                "name_en": "Wong New Director",
                "appointment_date": "2026-02-01",
            }
        }
    }

    # The diagnostic home: the §2.7 assessment with the pinned model.
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == communication.id,
            )
        )
    ).all()
    assert assessment.component == "reply_parser"
    assert assessment.model == "stub-model/reply_parser@1"
    assert assessment.field_scores == {
        "confirmations.officers.0.name_en": 1.0,
        "change_events.0": 1.0,
    }
    assert assessment.overall_score is not None and float(assessment.overall_score) == 1.0
    assert assessment.routing == "staff_review"
    assert assessment.raw_model_resolution is None
    assert assessment.enforced_resolution is None
    # The parsed proposals are diagnostics for the reconfirmer's letter.
    assert assessment.signal_log["change_events"] == parse_result()["change_events"]
    # And ruling R-D's cap says why the flawless run still waits for staff.
    assert assessment.signal_log["unregistered_person_names"] == ["Wong New Director"]

    # The audit chain: the event's entry, the one reply_parsed entry, and the
    # reconfirmer's letter entries (Task 4's tail) — coordinates and counts,
    # never spans or bodies. (The gate-seeding package wrote its own entry
    # before the parser ran; the parser's entries are the company's.)
    entries = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id, AuditLog.company_id == company.id)
            .order_by(AuditLog.seq)
        )
    ).all()
    assert [entry.action for entry in entries] == [
        "change_event_recorded",
        "reply_parsed",
        "document_registered",
        "reconfirmation_enqueued",
    ]
    (parse_entry,) = [entry for entry in entries if entry.action == "reply_parsed"]
    assert parse_entry.company_id == company.id
    assert parse_entry.entity_type == "communication"
    assert parse_entry.entity_id == communication.id
    assert parse_entry.details == {
        "parse_id": str(parse_row.id),
        "status": "changes_reported",
        "routing": "staff_review",
        "language": "en",
        "confirmation_count": 1,
        "event_count": 1,
        "withdrawn_count": 0,
    }
    # The tail's letter: a reconfirmation row pends on the same session, with
    # the stored .eml as the letter's only home.
    (letter_row,) = (
        await session.scalars(
            select(MailOutbox).where(
                MailOutbox.tenant_id == tenant.id,
                MailOutbox.kind == "reconfirmation",
            )
        )
    ).all()
    assert letter_row.status == "pending"
    assert letter_row.confirmation_request_id == request.id
    assert letter_row.letter_document_id is not None
    (enqueued_entry,) = [
        entry for entry in entries if entry.action == "reconfirmation_enqueued"
    ]
    assert enqueued_entry.details["parse_id"] == str(parse_row.id)
    assert enqueued_entry.details["request_id"] == str(request.id)
    assert enqueued_entry.details["decision"] == "reconfirmation_letter"
    assert "Wong New Director" not in str(enqueued_entry.details)


async def test_parse_reply_custody_search(
    session: AsyncSession,
    fake_infer: FakeInfer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Planted reply markers appear in no audit entry, log line, or report —
    while the quoted evidence span sits in the parse row, its designed home
    (and the letter the tail enqueues quotes only spans — never the planted
    markers, which no span carries)."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication, store = await seed_reply(session, tenant, company, request=request)

    with caplog.at_level(logging.INFO):
        report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "parsed"
    stored = " ".join(
        str(piece)
        for row in (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant.id)
            )
        ).all()
        for piece in (row.details, row.llm_call, row.action, row.entity_type)
    )
    assert PLANTED_MARKER not in stored
    assert PLANTED_PERSON not in stored
    assert PLANTED_MARKER not in caplog.text
    assert PLANTED_PERSON not in caplog.text
    assert PLANTED_MARKER not in str(report)
    assert PLANTED_PERSON not in str(report)
    # The quoted span lives in the parse row — the business home the custody
    # rule protects everywhere else.
    (parse_row,) = (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == communication.id)
        )
    ).all()
    assert parse_row.confirmations[0]["evidence_span"] == "the director is unchanged"


async def test_parse_reply_pre_flight_refusal_writes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway pre-flight refusal (e.g. ai_disabled) parses nothing, writes
    nothing, and leaves the reply for staff — the caller carries the reason."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    communication, store = await seed_reply(session, tenant, company)
    fake = FakeInfer(error=ai_gateway.InferenceRefused("ai_disabled"))
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "refused:ai_disabled"
    assert report.parse_id is None
    assert report.event_ids == ()
    assert (
        await session.scalars(select(ReplyParse).where(ReplyParse.tenant_id == tenant.id))
    ).all() == []
    assert (
        await session.scalars(
            select(ExtractionAssessment).where(ExtractionAssessment.tenant_id == tenant.id)
        )
    ).all() == []
    assert (
        await session.scalars(select(ChangeEvent).where(ChangeEvent.tenant_id == tenant.id))
    ).all() == []
    # The gate-seeding package's own entry stands; the parser wrote nothing
    # for this company — no call happened, so nothing was audited either.
    assert (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.company_id == company.id
            )
        )
    ).all() == []
    stored = (
        await session.scalars(select(Communication).where(Communication.id == communication.id))
    ).one()
    assert stored.language is None


async def test_parse_reply_unavailable_writes_nothing_beyond_the_gateway(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt is the gateway's record to book; the parser adds
    nothing of its own."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    communication, store = await seed_reply(session, tenant, company)
    fake = FakeInfer(error=ai_gateway.InferenceUnavailable("unreachable"))
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "unavailable"
    assert (
        await session.scalars(select(ReplyParse).where(ReplyParse.tenant_id == tenant.id))
    ).all() == []
    assert (
        await session.scalars(
            select(ExtractionAssessment).where(ExtractionAssessment.tenant_id == tenant.id)
        )
    ).all() == []
    assert (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.company_id == company.id
            )
        )
    ).all() == []


async def test_parse_reply_grammar_incomplete_event_routes_clarification_and_creates_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-guessing rule: a cessation with no effective date never becomes
    an event — the parse routes to client_clarification and the client is
    asked, not guessed at (the tail's clarification letter quotes the span —
    a reply answering the request is seeded so the letter can compose)."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication, store = await seed_reply(session, tenant, company, request=request)
    incomplete = parse_result(
        change_events=[
            {
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
                "effective_date_text": "sometime next year",
                "evidence_span": "retire Wong New Director",
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ]
    )
    fake = FakeInfer(result=incomplete)
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "parsed"
    assert report.routing == "client_clarification"
    assert report.event_ids == ()
    assert (
        await session.scalars(select(ChangeEvent).where(ChangeEvent.tenant_id == tenant.id))
    ).all() == []
    (parse_row,) = (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == communication.id)
        )
    ).all()
    assert parse_row.routing == "client_clarification"
    assert parse_row.event_ids == []
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == communication.id,
            )
        )
    ).all()
    assert assessment.routing == "client_clarification"
    # The unlanded proposal is diagnostic data for the clarification letter.
    assert assessment.signal_log["change_events"] == incomplete["change_events"]


async def test_parse_reply_invalid_output_goes_to_staff_review_with_nothing_else(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model output outside the closed grammar is never trusted: one
    staff_review assessment, no parse row (there is no valid status), no
    events, no language fill."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    communication, store = await seed_reply(session, tenant, company)
    broken = parse_result()
    del broken["multi_company_signals"]
    fake = FakeInfer(result=broken)
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "invalid"
    assert report.status is None
    assert report.routing == "staff_review"
    assert (
        await session.scalars(select(ReplyParse).where(ReplyParse.tenant_id == tenant.id))
    ).all() == []
    assert (
        await session.scalars(select(ChangeEvent).where(ChangeEvent.tenant_id == tenant.id))
    ).all() == []
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == communication.id,
            )
        )
    ).all()
    assert assessment.component == "reply_parser"
    assert assessment.model == "stub-model/reply_parser@1"
    assert assessment.routing == "staff_review"
    assert assessment.overall_score is None
    assert assessment.signal_log["violations"]
    (entry,) = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.company_id == company.id
            )
        )
    ).all()
    assert entry.action == "reply_parsed"
    assert entry.details["routing"] == "staff_review"
    assert entry.details["event_count"] == 0
    stored = (
        await session.scalars(select(Communication).where(Communication.id == communication.id))
    ).one()
    assert stored.language is None


async def test_parse_reply_withdrawal_moves_the_matched_event(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A withdrawal parse moves the matched earlier event to ``withdrawn``
    through the change-event executor, with the reply recorded as the source
    of the withdrawal (and the tail's letter answers the request the reply
    joined)."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    earlier = await seed_earlier_event(session, tenant, company)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication, store = await seed_reply(
        session, tenant, company, body="Please withdraw Chan Tai Man.", request=request
    )
    fake = FakeInfer(result=withdrawal_result())
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "parsed"
    assert report.withdrawn_event_ids == (earlier.id,)
    assert report.event_ids == ()
    moved = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == earlier.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert moved.status == ChangeEventStatus.WITHDRAWN.value
    # The withdrawal's record: the transition entry names the reply.
    transition = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).one()
    assert transition.details["action"] == ChangeEventAction.CLIENT_WITHDRAW.value
    assert transition.details["before"] == ChangeEventStatus.PENDING.value
    assert transition.details["after"] == ChangeEventStatus.WITHDRAWN.value
    assert transition.details["source_communication_id"] == str(communication.id)
    # The parser's own entry carries the withdrawn coordinate.
    (parse_entry,) = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id, AuditLog.action == "reply_parsed"
            )
        )
    ).all()
    assert parse_entry.details["withdrawn_count"] == 1
    assert parse_entry.details["event_count"] == 0


async def test_parse_reply_correction_links_the_new_event(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction parse creates a new event carrying ``relation: corrects``
    linked to the matched earlier event (and the tail's letter answers the
    request the reply joined)."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    earlier = await seed_earlier_event(session, tenant, company)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    communication, store = await seed_reply(
        session,
        tenant,
        company,
        body="Correction: Chan Tai Man was appointed on 1 March 2026.",
        request=request,
    )
    correction = parse_result(
        status="correction",
        change_events=[
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": "Chan Tai Man",
                    "name_en": "Chan Tai Man",
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": "Chan Tai Man",
                    "uncertain": False,
                },
                "effective_date": "2026-03-01",
                "effective_date_text": "1 March 2026",
                "evidence_span": "Correction: Chan Tai Man was appointed on 1 March 2026",
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ],
    )
    fake = FakeInfer(result=correction)
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "parsed"
    assert len(report.event_ids) == 1
    (event,) = (
        await session.scalars(select(ChangeEvent).where(ChangeEvent.id == report.event_ids[0]))
    ).all()
    assert event.status == ChangeEventStatus.PENDING.value
    assert event.source_type == "communication"
    assert event.source_id == communication.id
    assert event.related_event_id == earlier.id
    assert event.relation == ChangeEventRelation.CORRECTS.value
    assert event.payload == {
        "appointment": {
            "officer": {
                "role": "director",
                "name_en": "Chan Tai Man",
                "appointment_date": "2026-03-01",
            }
        }
    }
    # The corrected original stands untouched.
    original = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == earlier.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert original.status == ChangeEventStatus.PENDING.value


async def test_parse_reply_unknown_communication_is_not_found(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    with pytest.raises(reply_parsing.ReplyNotFoundError):
        await reply_parsing.parse_reply(
            session,
            store=FakeStore({}),
            scanner=FakeScanner(),
            tenant_id=tenant.id,
            communication_id=uuid4(),
            as_of=AS_OF,
        )
    assert fake_infer.calls == []


async def test_the_section_5_1_gate_denial_reports_and_parses_nothing(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """A company whose package does not cover AI reply parsing is denied per
    message: the GatingReason is reported, no gateway call is made, nothing
    is written."""
    tenant, company = await seed_company(session)  # no assignment at all
    communication, store = await seed_reply(session, tenant, company)

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "denied:no_assignment"
    assert fake_infer.calls == []
    assert (
        await session.scalars(select(ReplyParse).where(ReplyParse.tenant_id == tenant.id))
    ).all() == []
    assert (
        await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant.id))
    ).all() == []


async def test_the_cycle_skips_non_replies_and_parsed_replies(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """The single-message entry reports skips without writing; a second parse
    of the same reply is refused by the already-parsed check."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    outbound, outbound_store = await seed_reply(
        session, tenant, company, request=request, direction="outbound"
    )
    reply, store = await seed_reply(session, tenant, company, request=request)

    outbound_report = await parse_one(session, outbound_store, tenant, outbound.id)
    assert outbound_report.outcome == "skipped:not_a_reply"

    first = await parse_one(session, store, tenant, reply.id)
    assert first.outcome == "parsed"
    second = await parse_one(session, store, tenant, reply.id)
    assert second.outcome == "skipped:already_parsed"
    assert len(fake_infer.calls) == 1


async def test_the_cycle_never_parses_a_confirmation(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """A registered channel's confirming reply is not a client reply (ADR-0019
    §1, ruling R5): the cycle's listing leaves ``attribution_confirmation``
    rows out entirely, so the words are never read by a model and no parse row
    is written — a later cycle still reports nothing for it."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    bodies: dict[str, bytes] = {}
    confirmation, _ = await seed_reply(
        session, tenant, company, body="Confirm", bodies=bodies
    )
    confirmation.kind = "attribution_confirmation"
    await session.flush()
    reply, _ = await seed_reply(session, tenant, company, body="All unchanged.", bodies=bodies)
    store = FakeStore(bodies)

    first = await reply_parsing.run_cycle(
        session, store=store, scanner=FakeScanner(), tenant_id=tenant.id, as_of=AS_OF
    )

    assert first.examined == 1
    assert [item.communication_id for item in first.outcomes] == [reply.id]
    assert len(fake_infer.calls) == 1
    assert (
        await session.scalars(
            select(ReplyParse).where(ReplyParse.communication_id == confirmation.id)
        )
    ).all() == []

    second = await reply_parsing.run_cycle(
        session, store=store, scanner=FakeScanner(), tenant_id=tenant.id, as_of=AS_OF
    )

    assert second.examined == 0
    assert second.outcomes == ()
    assert len(fake_infer.calls) == 1


async def test_parse_reply_skips_a_body_the_reader_cannot_read(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """An HTML-only message reads as no text at all: no model call is made —
    asked to structure silence, the model could only hallucinate."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    html_only = (
        b"From: director@example.hk\r\n"
        b"To: secretary@mailbox.example.hk\r\n"
        b"Subject: Re: Annual return confirmation\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body>All fine.</body></html>"
    )
    communication, store = await seed_reply(session, tenant, company, body="unused")
    document = await session.scalar(
        select(Document).where(Document.id == communication.raw_document_id)
    )
    assert document is not None
    store.objects[document.storage_key] = html_only

    report = await parse_one(session, store, tenant, communication.id)

    assert report.outcome == "skipped:no_reply_text"
    assert fake_infer.calls == []
    assert (
        await session.scalars(select(ReplyParse).where(ReplyParse.tenant_id == tenant.id))
    ).all() == []


async def test_the_cycle_parses_every_unparsed_reply_oldest_first(
    session: AsyncSession, fake_infer: FakeInfer
) -> None:
    """One cycle: every ingested inbound reply that has no parse row, oldest
    receipt first, each message's outcome reported and a denial never
    aborting the cycle."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    denied_company = Company(
        tenant_id=tenant.id,
        cr_number="7654321",
        br_number="87654321",
        date_of_incorporation="2012-05-05",
        anniversary_date="2026-05-05",
        type="private",
    )
    session.add(denied_company)
    await session.flush()
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    bodies: dict[str, bytes] = {}
    first, _ = await seed_reply(
        session,
        tenant,
        company,
        body="First reply arrives.",
        request=request,
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
        bodies=bodies,
    )
    second, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Second reply arrives.",
        request=request,
        received_at=datetime(2026, 6, 2, tzinfo=UTC),
        bodies=bodies,
    )
    denied_reply, _ = await seed_reply(
        session,
        tenant,
        denied_company,
        received_at=datetime(2026, 6, 3, tzinfo=UTC),
        bodies=bodies,
    )

    report = await reply_parsing.run_cycle(
        session, store=FakeStore(bodies), scanner=FakeScanner(), tenant_id=tenant.id, as_of=AS_OF
    )

    assert report.examined == 3
    assert report.parsed == 2
    outcomes = {item.communication_id: item.outcome for item in report.outcomes}
    assert outcomes == {
        first.id: "parsed",
        second.id: "parsed",
        denied_reply.id: "denied:no_assignment",
    }
    # Oldest first, and the order of the reports matches it.
    assert [item.communication_id for item in report.outcomes] == [
        first.id,
        second.id,
        denied_reply.id,
    ]
    assert len(fake_infer.calls) == 2


# --- Task 5: the reconfirmation exchange over the parse ------------------------


ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


async def out_with_the_client(
    session: AsyncSession,
    store: FakeStore,
    tenant: Tenant,
    company: Company,
    request: ConfirmationRequest,
) -> tuple[Communication, ChangeEvent]:
    """The state task 5's trigger reads: a reply on the request's thread
    proposed an event, and staff pressed ``send_reconfirmation`` — the event
    stands ``client_reconfirming`` with its letter on the outbox."""
    first, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Please appoint Chan Tai Man.",
        request=request,
        bodies={},
    )
    event = (
        await change_events.create(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
            effective_date=date(2026, 1, 15),
            payload={
                "appointment": {
                    "officer": {
                        "role": "director",
                        "name_en": "Chan Tai Man",
                        "appointment_date": "2026-01-15",
                    }
                }
            },
            as_of=AS_OF,
            source_type=ChangeEventSourceType.COMMUNICATION,
            source_id=first.id,
        )
    ).current
    moved = await change_events.transition(
        session,
        tenant_id=tenant.id,
        context=ADMIN_CONTEXT,
        event_id=event.id,
        action=ChangeEventAction.SEND_RECONFIRMATION,
        as_of=AS_OF,
        store=store,
        scanner=FakeScanner(),
    )
    assert moved.current.status == ChangeEventStatus.CLIENT_RECONFIRMING.value
    return first, event


async def test_a_reply_joining_the_reconfirmation_thread_returns_the_event_to_pending(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.1's system row, wired: a reply on the reconfirmation's request
    thread parses, and every event of the company that stood
    ``client_reconfirming`` returns to ``pending`` on the same session — it
    reappears in the review queue — with the reply named as the move's source.
    The parse itself lands beside it: one reply, one new assessment, one
    ``client_reply`` entry per reconfirming event."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    _, event = await out_with_the_client(session, FakeStore({}), tenant, company, request)
    second, store = await seed_reply(
        session,
        tenant,
        company,
        body="Confirmed, no changes to report.",
        request=request,
    )
    fake = FakeInfer(
        result=parse_result(status="confirmed_no_change", confirmations=[], change_events=[])
    )
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, second.id)

    assert report.outcome == "parsed"
    moved = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == event.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert moved.status == ChangeEventStatus.PENDING.value
    transitions = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    (reply_entry,) = [
        entry for entry in transitions if entry.details["action"] == "client_reply"
    ]
    assert reply_entry.details["before"] == ChangeEventStatus.CLIENT_RECONFIRMING.value
    assert reply_entry.details["after"] == ChangeEventStatus.PENDING.value
    assert reply_entry.details["source_communication_id"] == str(second.id)
    assert reply_entry.actor_user_id is None  # the system's row
    # The thread's own entry (the re-ask) stands beside it, untouched.
    (reask,) = [
        entry
        for entry in transitions
        if entry.details["action"] == "send_reconfirmation"
    ]
    assert reask.details["before"] == ChangeEventStatus.PENDING.value
    assert reask.details["after"] == ChangeEventStatus.CLIENT_RECONFIRMING.value


async def test_an_unsolicited_reply_leaves_the_reconfirming_event_where_it_is(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger is the thread join, not any parsed reply: a reply that
    joined no request is not answering the reconfirmation, and the event stays
    ``client_reconfirming`` — staff's ``mark_under_review`` leg is the way in."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    _, event = await out_with_the_client(session, FakeStore({}), tenant, company, request)
    second, store = await seed_reply(
        session, tenant, company, body="Confirmed, nothing changed."
    )
    fake = FakeInfer(
        result=parse_result(status="confirmed_no_change", confirmations=[], change_events=[])
    )
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, second.id)

    assert report.outcome == "parsed"
    unmoved = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == event.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert unmoved.status == ChangeEventStatus.CLIENT_RECONFIRMING.value
    transitions = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    assert [entry for entry in transitions if entry.details["action"] == "client_reply"] == []


async def test_a_withdrawal_reply_withdraws_the_event_that_was_out_for_reconfirmation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The external-contact leg end to end through the reconfirmation
    exchange: the event staff asked about is answered by a withdrawal — the
    reply moves the matched event ``client_reconfirming → withdrawn`` with the
    reply as its source, and no ``client_reply`` fires behind it (the
    withdrawal already took the event out of the reconfirming set)."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    _, event = await out_with_the_client(session, FakeStore({}), tenant, company, request)
    second, store = await seed_reply(
        session,
        tenant,
        company,
        body="Please withdraw Chan Tai Man.",
        request=request,
    )
    fake = FakeInfer(result=withdrawal_result())
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await parse_one(session, store, tenant, second.id)

    assert report.outcome == "parsed"
    assert report.withdrawn_event_ids == (event.id,)
    moved = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == event.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert moved.status == ChangeEventStatus.WITHDRAWN.value
    entries = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    assert [(entry.details["action"], entry.details["after"]) for entry in entries] == [
        ("send_reconfirmation", "client_reconfirming"),
        ("client_withdraw", "withdrawn"),
    ]
    withdrawal = entries[-1]
    assert withdrawal.details["source_communication_id"] == str(second.id)


async def test_a_racing_staff_move_during_the_trigger_is_contained_and_the_cycle_finishes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger's contemplated race, contained: an event read as
    ``client_reconfirming`` leaves the status before the move's lock (a staff
    move commits in between) — the reply has nothing to return for it, that
    message still reports ``parsed``, and the messages behind it in the cycle
    still parse: one message's race is data, never a cycle-aborting 500.

    The race is simulated deterministically: the event really moves, and the
    trigger's read is patched to the stale answer the race would have
    produced — the move itself re-locks and re-reads (its guard refuses),
    which is the real code the containment answers.
    """
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    first, event = await out_with_the_client(session, FakeStore({}), tenant, company, request)
    # The reply that proposed the event was parsed back then: the cycle must
    # not pick it up (its raw bytes live in a discarded store).
    session.add(
        ReplyParse(
            tenant_id=tenant.id,
            company_id=company.id,
            communication_id=first.id,
            status="changes_reported",
            confirmations=[],
            event_ids=[str(event.id)],
        )
    )
    await session.flush()
    # The staff move that wins the race: the event leaves
    # ``client_reconfirming`` before the answering reply's parse lands.
    moved = await change_events.transition(
        session,
        tenant_id=tenant.id,
        context=ADMIN_CONTEXT,
        event_id=event.id,
        action=ChangeEventAction.MARK_UNDER_REVIEW,
        as_of=AS_OF,
    )
    assert moved.current.status == ChangeEventStatus.UNDER_REVIEW.value
    # The stale read the race would have produced: the trigger's list still
    # names the event as it stood when the thread read it.
    stale = ChangeEvent(
        id=event.id,
        version_no=1,
        tenant_id=tenant.id,
        company_id=company.id,
        type=event.type,
        status=ChangeEventStatus.CLIENT_RECONFIRMING.value,
        effective_date=event.effective_date,
    )

    async def stale_reconfirming(*args: object, **kwargs: object) -> list[ChangeEvent]:
        return [stale]

    monkeypatch.setattr(change_events, "reconfirming_events", stale_reconfirming)
    bodies: dict[str, bytes] = {}
    racing, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Confirmed, no changes to report.",
        request=request,
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
        bodies=bodies,
    )
    behind, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Also confirmed, nothing further.",
        request=request,
        received_at=datetime(2026, 6, 2, tzinfo=UTC),
        bodies=bodies,
    )
    fake = FakeInfer(
        result=parse_result(status="confirmed_no_change", confirmations=[], change_events=[])
    )
    monkeypatch.setattr(ai_gateway, "infer", fake.infer)

    report = await reply_parsing.run_cycle(
        session,
        store=FakeStore(bodies),
        scanner=FakeScanner(),
        tenant_id=tenant.id,
        as_of=AS_OF,
    )

    assert report.examined == 2
    assert report.parsed == 2
    assert [item.communication_id for item in report.outcomes] == [racing.id, behind.id]
    assert {item.communication_id: item.outcome for item in report.outcomes} == {
        racing.id: "parsed",
        behind.id: "parsed",
    }
    # The race changed nothing: the event stands where staff moved it, and no
    # client_reply entry fired.
    current = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == event.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert current.status == ChangeEventStatus.UNDER_REVIEW.value
    transitions = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    assert [entry.details["action"] for entry in transitions] == [
        "send_reconfirmation",
        "mark_under_review",
    ]


async def test_a_racing_staff_move_during_the_withdrawal_is_contained_and_the_cycle_finishes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withdrawal leg's contemplated race, contained the same way the
    ``client_reply`` trigger's is: the matcher reads unlocked, and a staff
    move taking the matched event out of the withdrawable set between that
    read and the move's lock makes the executor's guard refuse — which must
    be this message's ``unlanded`` reason and ``client_clarification``
    routing, never an exception out of the cycle: the reply behind the raced
    one still parses.

    The race is simulated deterministically: the event really moves (staff
    delete, committed on this session), and the matcher's projection is
    patched to the stale answer the race would have produced — the executor
    itself re-locks and re-reads (its guard refuses), which is the real code
    the containment answers.
    """
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    earlier = await seed_earlier_event(session, tenant, company)
    # The staff move that wins the race: the matched event leaves the
    # withdrawable set before the withdrawal's move takes its lock.
    deleted = await change_events.transition(
        session,
        tenant_id=tenant.id,
        context=ADMIN_CONTEXT,
        event_id=earlier.id,
        action=ChangeEventAction.DELETE,
        as_of=AS_OF,
    )
    assert deleted.current.status == ChangeEventStatus.DELETED.value
    # The stale projection the race would have produced: the matcher still
    # names the event as it stood when the projection read it.
    stale = [
        EarlierEvent(
            id=earlier.id,
            event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
            status=ChangeEventStatus.PENDING,
            party_names=("Chan Tai Man",),
        )
    ]

    async def stale_earlier(*args: object, **kwargs: object) -> list[EarlierEvent]:
        return stale

    monkeypatch.setattr(reply_parsing, "_earlier_events", stale_earlier)
    bodies: dict[str, bytes] = {}
    racing, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Please withdraw Chan Tai Man.",
        request=request,
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
        bodies=bodies,
    )
    behind, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Confirmed, no changes to report.",
        request=request,
        received_at=datetime(2026, 6, 2, tzinfo=UTC),
        bodies=bodies,
    )
    fake = FakeInfer(
        result=parse_result(status="confirmed_no_change", confirmations=[], change_events=[])
    )
    withdrawal_infer = FakeInfer(result=withdrawal_result())

    def scripted_infer(session_arg: AsyncSession, **kwargs: object) -> object:
        # The raced reply proposes the withdrawal; the one behind confirms.
        content = kwargs["content"]
        assert isinstance(content, Mapping)
        if "withdraw" in content["reply_text"]:
            return withdrawal_infer.infer(session_arg, **kwargs)
        return fake.infer(session_arg, **kwargs)

    monkeypatch.setattr(ai_gateway, "infer", scripted_infer)

    report = await reply_parsing.run_cycle(
        session,
        store=FakeStore(bodies),
        scanner=FakeScanner(),
        tenant_id=tenant.id,
        as_of=AS_OF,
    )

    assert report.examined == 2
    assert report.parsed == 2
    outcomes = {item.communication_id: item.outcome for item in report.outcomes}
    assert outcomes == {racing.id: "parsed", behind.id: "parsed"}
    raced = {item.communication_id: item for item in report.outcomes}[racing.id]
    # The refusal bent this message to clarification and landed nothing…
    assert raced.routing == "client_clarification"
    assert raced.withdrawn_event_ids == ()
    assert raced.event_ids == ()
    # …while the event stands exactly where staff moved it, no withdrawal
    # fired, and the messages behind still parsed.
    current = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == earlier.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert current.status == ChangeEventStatus.DELETED.value
    transitions = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    assert [entry.details["action"] for entry in transitions] == ["delete"]
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == racing.id,
            )
        )
    ).all()
    assert assessment.signal_log["unlanded_proposals"][0]["reason"] == "match_moved"


async def test_a_withdrawal_with_an_impossible_date_bends_to_clarification_and_withdraws_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withdrawal leg consults the one date home too: a withdrawal that
    STATES a calendar-impossible effective date is asked, never filed — the
    event is not withdrawn, the routing bends to ``client_clarification``
    (the letter's restatement never meets the impossible date), and the
    messages behind it in the cycle still parse. A withdrawal that states
    nothing needs nothing: the reference judgment waives the filing legs."""
    tenant, company = await seed_company(session)
    await cover(session, tenant, company.id, services=["ai_reply_parsing"])
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    earlier = await seed_earlier_event(session, tenant, company)
    bodies: dict[str, bytes] = {}
    racing, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Please withdraw Chan Tai Man.",
        request=request,
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
        bodies=bodies,
    )
    behind, _ = await seed_reply(
        session,
        tenant,
        company,
        body="Confirmed, no changes to report.",
        request=request,
        received_at=datetime(2026, 6, 2, tzinfo=UTC),
        bodies=bodies,
    )
    impossible = withdrawal_result()
    impossible["change_events"][0]["effective_date"] = "2026-02-30"
    impossible["change_events"][0]["effective_date_text"] = "30 February 2026"
    withdrawal_infer = FakeInfer(result=impossible)
    fake = FakeInfer(
        result=parse_result(status="confirmed_no_change", confirmations=[], change_events=[])
    )

    def scripted_infer(session_arg: AsyncSession, **kwargs: object) -> object:
        content = kwargs["content"]
        assert isinstance(content, Mapping)
        if "withdraw" in content["reply_text"]:
            return withdrawal_infer.infer(session_arg, **kwargs)
        return fake.infer(session_arg, **kwargs)

    monkeypatch.setattr(ai_gateway, "infer", scripted_infer)

    report = await reply_parsing.run_cycle(
        session,
        store=FakeStore(bodies),
        scanner=FakeScanner(),
        tenant_id=tenant.id,
        as_of=AS_OF,
    )

    assert report.examined == 2
    assert report.parsed == 2
    outcomes = {item.communication_id: item for item in report.outcomes}
    raced = outcomes[racing.id]
    assert raced.outcome == "parsed"
    assert raced.routing == "client_clarification"
    assert raced.withdrawn_event_ids == ()
    assert raced.event_ids == ()
    assert outcomes[behind.id].outcome == "parsed"
    # The event stands exactly where it stood: nothing was withdrawn, no
    # withdrawal fired.
    current = (
        await session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == earlier.id, ChangeEvent.version_no == 1)
        )
    ).one()
    assert current.status == ChangeEventStatus.PENDING.value
    transitions = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == "change_event_transition_recorded",
            )
        )
    ).all()
    assert transitions == []
    (assessment,) = (
        await session.scalars(
            select(ExtractionAssessment).where(
                ExtractionAssessment.subject_type == "communication",
                ExtractionAssessment.subject_id == racing.id,
            )
        )
    ).all()
    assert assessment.signal_log["unlanded_proposals"][0]["reason"] == "grammar_incomplete"


# --- The reads Task 6 serves ---------------------------------------------------

async def test_the_parse_reads_take_the_newest_row_per_communication(
    session: AsyncSession,
) -> None:
    """``latest_parse`` and ``latest_parses`` against a real database: re-parses
    append rows, and the reads take the newest — the DISTINCT ON the list read
    uses is SQL worth executing, not just compiling."""
    tenant, company = await seed_company(session)
    document = await seed_document(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id, document.id)
    first = await seed_communication(session, tenant.id, company.id, document.id, request.id)
    second_document = await seed_document(session, tenant.id, company.id)
    second = await seed_communication(
        session, tenant.id, company.id, second_document.id, request.id
    )
    older = ReplyParse(
        tenant_id=tenant.id,
        company_id=company.id,
        communication_id=first.id,
        confirmation_request_id=request.id,
        status="changes_reported",
        language="en",
        routing="staff_review",
        confirmations=[],
        event_ids=[],
        created_at=datetime(2026, 9, 23, 1, 0, tzinfo=UTC),
    )
    newer = ReplyParse(
        tenant_id=tenant.id,
        company_id=company.id,
        communication_id=first.id,
        confirmation_request_id=request.id,
        status="confirmed_no_change",
        language="en",
        routing="auto_accept",
        confirmations=[{"fact_id": "officers.0.name_en", "answer": "confirmed"}],
        event_ids=[],
        created_at=datetime(2026, 9, 23, 1, 5, tzinfo=UTC),
    )
    session.add_all([older, newer])
    await session.flush()

    latest = await reply_parsing.latest_parse(
        session, tenant_id=tenant.id, company_id=company.id, communication_id=first.id
    )
    assert latest.id == newer.id
    assert latest.status == "confirmed_no_change"

    per_communication = await reply_parsing.latest_parses(
        session, tenant_id=tenant.id, company_id=company.id
    )
    assert list(per_communication) == [first.id]
    assert per_communication[first.id].id == newer.id

    # The reply no cycle has parsed: one named 404, not a crash.
    with pytest.raises(reply_parsing.ParseNotFoundError):
        await reply_parsing.latest_parse(
            session, tenant_id=tenant.id, company_id=company.id, communication_id=second.id
        )
    assert second.id not in per_communication

    # Cross-company and cross-tenant ids resolve to one answer.
    with pytest.raises(reply_parsing.CommunicationNotFoundError):
        await reply_parsing.latest_parse(
            session, tenant_id=tenant.id, company_id=uuid4(), communication_id=first.id
        )
    with pytest.raises(reply_parsing.CommunicationNotFoundError):
        await reply_parsing.latest_parse(
            session, tenant_id=uuid4(), company_id=company.id, communication_id=first.id
        )


# --- Task 3 review fix: overlapping cycles serialize per tenant -------------


async def test_overlapping_cycles_serialize_per_tenant_and_never_double_land() -> None:
    """Two cycles on one tenant overlap: the second waits on the first's
    transaction-scoped advisory lock, then re-derives from the winner's
    committed rows.

    Proven with two real connections against the ephemeral database, no
    commits anywhere (the audit chain is append-only, so this scenario —
    alone in the file — cannot be allowed to commit, and does not need to):
    connection A holds the tenant's lock the way a mid-flight cycle would;
    a cycle on connection B blocks until A's transaction ends, then reads
    afresh. The ordering is what makes double-landing impossible — the lock
    precedes the un-parsed listing, so a cycle that waited always re-derives
    from the winner's committed parse rows and lists nothing; and a reply met
    twice reports ``skipped:already_parsed`` with no duplicate events (the
    single-message half of the guarantee is pinned by
    ``test_the_cycle_skips_non_replies_and_parsed_replies``). No rows are
    seeded at all: a made-up tenant id keys the lock, and the waiting cycle
    simply finds no communications for it.
    """
    tenant_id = uuid4()

    async with engine.connect() as conn_a:
        await conn_a.begin()
        # A stands in for a cycle mid-flight: same key, same lock, held.
        await conn_a.execute(
            select(func.pg_advisory_xact_lock(reply_parsing._parse_lock_key(tenant_id)))
        )
        async with engine.connect() as conn_b:
            await conn_b.begin()
            async with AsyncSession(bind=conn_b, expire_on_commit=False) as session_b:
                blocked = asyncio.create_task(
                    reply_parsing.run_cycle(
                        session_b,
                        store=FakeStore({}),
                        scanner=FakeScanner(),
                        tenant_id=tenant_id,
                        as_of=AS_OF,
                    )
                )
                await asyncio.sleep(0.5)
                # The cycle is waiting on A's lock, not reading or parsing —
                # without the lock it would have finished in milliseconds.
                assert not blocked.done()
                # Release inside B's contexts: awaiting the blocked task
                # needs session_b's connection, which cannot close first.
                await conn_a.rollback()
                report_b = await asyncio.wait_for(blocked, timeout=10)
                # The wait over, the cycle read afresh and found nothing for
                # this made-up tenant — nothing examined, nothing proposed,
                # no error: waiting is serialization, not failure.
                assert report_b.examined == 0
                assert report_b.parsed == 0
            await conn_b.rollback()
