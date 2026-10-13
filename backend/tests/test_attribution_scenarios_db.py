"""DB-backed scenarios for the attribution case's table and persistence
(slice T3; opt-in; needs a migrated PostgreSQL).

ADR-0019 §1's registration/confirmation flow stands on one new table
(``attribution_cases``, migration ``0033``) and one widened outbox; the
clarification loop (workstream ②, rulings D1/D2) widens both
(``company_id`` NULL, the ``awaiting_clarification`` status and the
``clarification`` ask kind, migration ``0034``). What these scenarios prove,
in the task brief's order:

1. **the case round-trips** — ``create_case`` writes the row the ingestion
   cycle opens (tenant, company, both thread anchors, the sender, the ask kind
   and the awaiting status), and ``get_case`` reads it back tenant-scoped;
2. **one case per original message** — ``uq_attribution_cases_message_id_per_tenant``
   refuses a second row for the same provider message id, ever;
3. **the closed vocabularies bite** — the status and ask-kind CHECKs admit
   exactly ``AttributionCaseStatus`` / ``AttributionAskKind`` (pinned verbatim
   against the domain enums) and refuse anything else, and each closure stamp
   pairs with the status that names it (the ``0016``/``0017`` style);
4. **the request-less ask** — ``mail_outbox`` accepts a pending
   ``attribution_ask`` row with ``confirmation_request_id`` NULL and
   ``attribution_case_id`` set (the M2 shape, unchanged, still inserts), and
   ``uq_mail_outbox_pending_per_ask`` admits exactly one pending ask per case
   while a delivered one frees the slot;
5. **the reads the flow will make** — ``open_cases_for_thread`` matches either
   anchor (the provider thread id, or any RFC ``Message-ID`` in the caller's
   ancestor set), ``latest_ask_for_thread`` reports only an open asked case,
   ``count_asks_since`` counts the hour's asks, and ``set_ask_sent`` /
   ``set_status`` write what the application layer will write;
6. **the ask's enqueue** (ADR-0019 §1's ruling R4) — a case's
   ask is composed from envelope facts, stored through the records registry
   (``generated_draft``, retention ``client_communication``) and queued as a
   request-less ``attribution_ask`` row (send-on-release, never held for
   staff); the recipients are addressed in the form the tenant's channel rows
   store; a case that has left the two awaits, the suppression read (over the
   thread id **or** the ancestor id set, which is the IMAP leg's only
   evidence), the §5.1 gate, the anti-abuse headers and the hourly cap each
   keep an ask off while the case stays open; and two overlapping enqueues for
   one case write one row;
7. **the migration round-trips** — ``0033``'s downgrade removes the table and
   the outbox column and narrows both kind CHECKs; the upgrade restores the
   full shape and the widened vocabulary is live again;
8. **the clarification-loop migration round-trips** (②, ruling D2) —
   ``0034``'s upgrade drops the two ``company_id`` NOT NULLs, widens the
   status and ask-kind CHECKs (and the stamp-pairing CHECK the new status
   joins), and re-leads the thread index; company-less case and ask rows
   round-trip, the composite FK accepts a real pair and refuses another
   tenant's exactly as before, and the downgrade refuses what the narrower
   schema cannot hold;
9. **the lifecycle and the staff surface** (T6/T7) — the confirming act and
   the 2.1 closure, the processing of the original over the mailbox and the
   idempotent road, and the surface's own three entry points: the list's
   ``awaiting`` over both open states, the dismissal and the forced
   processing (their cancels of the queued ask, their audit entries, and the
   typed refusals for a settled case and a foreign id);
10. **the clarification loop's application units** (② T3) — the company-less
    open (both classes, the NULL-company audit), the tenant-scoped anchor
    reads, the two-ask bound and the split hourly pools, the company-less
    enqueue's guard set stopping at the records registry's honest refusal,
    the A-class answer's stamp-and-process resolution, the B-class closure's
    company-less filter, and the staff surface's D9 refusal before the
    mailbox is touched;
11. **the registry widening's own round trip** (0035, the wall's removal) —
    the two NOT NULL drops, the company-less letter and its delivered
    exchange inserting under the widened schema, the non-null company keeping
    its F1 pinning, and the downgrade refusing what only the wider schema
    admits.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (with ``0036_multi_company_split`` applied — the revisions are built HELD
for the founder's schema sign-off like their predecessors, and the throwaway
test database is migrated by the runner, never a review database). Every test
runs inside a transaction that is rolled back, so the review database stays
clean.
"""

import os
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.mail import imap_client
from app.adapters.persistence.attribution import (
    OPEN_STATUSES,
    count_asks_for_case,
    count_asks_since,
    create_case,
    get_case,
    latest_ask_for_anchor,
    latest_ask_for_thread,
    list_cases,
    open_cases_for_anchor,
    open_cases_for_thread,
    set_ask_sent,
    set_case_company,
    set_status,
)
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.contacts import CompanyExternalContact, ContactChannel, Person
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import AttributionCase, Company, Tenant, User
from app.adapters.persistence.service_packages import ServiceAssignment
from app.application import attribution, documents, outbox, service_packages
from app.core.config import settings
from app.domain.attribution import (
    AttributionAction,
    AttributionAskKind,
    AttributionCaseStatus,
    CompanyReference,
    UnthreadedAction,
    ask_subject,
    clarification_ask_letter,
    companyless_ask_subject,
    confirmation_ask_letter,
    first_contact_ask_letter,
    registration_ask_letter,
)
from app.domain.audit import AuditAction
from app.domain.communications import IngestOutcome
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.mail_send import OutboxKind, OutboxStatus
from tests.fake_imap import FakeImap
from tests.fake_scanning import FakeScanner

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

THREAD = "thread-18cf"
RFC_ID = "<original-message@client.example>"
SENDER = "director@client.example"
ORIGINAL_SUBJECT = "Fwd: appointment of a director"
AT = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)
# The mailbox every client-facing message goes out from, and the name the ask
# letters identify the company by (scope §7: name and number, both halves).
MAILBOX = "ai-filings@tcsp.example"
COMPANY_NAME = "Synthetic Harbour Limited"


def _load_revision_module(filename: str, module_name: str) -> object:
    """One revision module, importable without the versions dir being a package."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_revision() -> object:
    """The 0033 revision module, importable without the versions dir being a package."""
    return _load_revision_module("0033_attribution_cases.py", "revision_0033_attribution_cases")


REVISION = _load_revision()
REVISION_0034 = _load_revision_module(
    "0034_clarification_loop.py", "revision_0034_clarification_loop"
)

# The request-less ask row the widened CHECK admits and the narrow one refuses:
# no confirmation request behind it, the case in its place.
INSERT_REQUESTLESS_ASK = text(
    "INSERT INTO mail_outbox (id, tenant_id, company_id, confirmation_request_id,"
    " attribution_case_id, destination, status, kind)"
    " VALUES (:id, :tenant, :company, NULL, :case, 'client', 'pending', 'attribution_ask')"
)

# The two probes the downgraded schema must refuse: the ask's kind is outside
# the narrow CHECK, and a request-less pending row is outside the restored
# NOT NULL. Written without the dropped column, which no longer exists.
INSERT_ASK_KIND_ON_REQUEST = text(
    "INSERT INTO mail_outbox (id, tenant_id, company_id, confirmation_request_id,"
    " destination, status, kind)"
    " VALUES (:id, :tenant, :company, :request, 'client', 'pending', 'attribution_ask')"
)
INSERT_REQUESTLESS_REQUEST_KIND = text(
    "INSERT INTO mail_outbox (id, tenant_id, company_id, confirmation_request_id,"
    " destination, status, kind)"
    " VALUES (:id, :tenant, :company, NULL, 'client', 'pending', 'confirmation_request')"
)

# The attribution exchange's probe: a kind only the widened vocabulary admits,
# aimed at the narrowed CHECK the same way.
INSERT_ASK_KIND_COMMUNICATION = text(
    "INSERT INTO communications (id, tenant_id, company_id, direction, channel, kind,"
    " received_at, raw_document_id)"
    " VALUES (:id, :tenant, :company, 'inbound', 'email', 'attribution_ask', now(), :document)"
)

# 0034's probes: the company-less case and its ask row (the clarification
# loop's whole shape) — NULL company, the new status and ask kind.
INSERT_COMPANY_LESS_CASE = text(
    "INSERT INTO attribution_cases (id, tenant_id, company_id, mailbox_message_id,"
    " sender_address, ask_kind, status, thread_id, original_rfc_message_id)"
    " VALUES (:id, :tenant, NULL, :message, :sender, 'clarification',"
    " 'awaiting_clarification', :thread, :rfc)"
)
INSERT_COMPANY_LESS_ASK = text(
    "INSERT INTO mail_outbox (id, tenant_id, company_id, confirmation_request_id,"
    " attribution_case_id, destination, status, kind)"
    " VALUES (:id, :tenant, NULL, NULL, :case, 'client', 'pending', 'attribution_ask')"
)
# The FK probes on the widened table: a real (company, tenant) pair accepts,
# another tenant's company under this tenant does not.
INSERT_CASE_FOR_COMPANY = text(
    "INSERT INTO attribution_cases (id, tenant_id, company_id, mailbox_message_id,"
    " sender_address, status)"
    " VALUES (:id, :tenant, :company, :message, :sender, 'awaiting_confirmation')"
)
# The narrowing's three refused shapes: the NULL company the restored NOT
# NULL cannot hold, and the new status / ask kind the restored CHECKs admit
# no value for (written against the 0033 schema, so no ask_kind/anchor
# columns beyond what it holds).
INSERT_CLARIFICATION_STATUS_NARROW = text(
    "INSERT INTO attribution_cases (id, tenant_id, company_id, mailbox_message_id,"
    " sender_address, status)"
    " VALUES (:id, :tenant, :company, :message, :sender, 'awaiting_clarification')"
)
INSERT_CLARIFICATION_KIND_NARROW = text(
    "INSERT INTO attribution_cases (id, tenant_id, company_id, mailbox_message_id,"
    " sender_address, ask_kind, status)"
    " VALUES (:id, :tenant, :company, :message, :sender, 'clarification',"
    " 'awaiting_confirmation')"
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
def store() -> FakeStore:
    return FakeStore()


def licensed(monkeypatch: pytest.MonkeyPatch, *, tier: str = "standard") -> None:
    """The deployment configuration the ask's gate and composition read."""
    monkeypatch.setattr(settings, "license_tier", tier)
    monkeypatch.setattr(settings, "license_expires_at", None)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)


async def seed_company(session: AsyncSession) -> tuple[Tenant, Company]:
    """A tenant and one company — the F1 anchor every row below pins to."""
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
    return tenant, company


async def name_company(
    session: AsyncSession, tenant_id: UUID, company_id: UUID, *, name_en: str = COMPANY_NAME
) -> None:
    """The name record the ask's copy identifies the company by (scope §7)."""
    session.add(
        CompanyNameRecord(
            tenant_id=tenant_id,
            company_id=company_id,
            effective_date=date(2015, 1, 1),
            source_type="manual",
            name_en=name_en,
            name_zh=None,
            business_name=None,
        )
    )
    await session.flush()


async def cover(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    *,
    services: tuple[str, ...] = ("email_ingestion",),
) -> None:
    """The package and active assignment the §5.1 gate's two legs read."""
    package = await service_packages.create_package(
        session,
        tenant_id=tenant_id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=list(services),
        covered_forms=["NAR1", "ND2A"],
    )
    session.add(
        ServiceAssignment(
            tenant_id=tenant_id,
            company_id=company_id,
            package_id=package.id,
            coverage_start=date(2026, 1, 1),
            coverage_end=date(2026, 12, 31),
            status="active",
        )
    )
    await session.flush()


async def seed_channel(
    session: AsyncSession, tenant_id: UUID, company_id: UUID, *, identifier: str
) -> None:
    """One registered email channel, stored in the form a staff member typed.

    Seeded directly (not through ``contacts.add_contact_channel``) because the
    scenario is about the *stored* form: the ask must address the channel the
    way the tenant holds it, not the comparison form the decision carries.
    """
    person = Person(tenant_id=tenant_id, name_en="CHAN TAI MAN", is_external_contact=True)
    session.add(person)
    await session.flush()
    session.add(
        ContactChannel(
            tenant_id=tenant_id,
            person_id=person.id,
            channel_type="email",
            identifier=identifier,
            status="active",
        )
    )
    session.add(
        CompanyExternalContact(
            tenant_id=tenant_id, company_id=company_id, person_id=person.id
        )
    )
    await session.flush()


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return list(rows.all())


def crlf(text: str) -> str:
    """A composed letter as ``policy.SMTP`` serializes it (CRLF line endings).

    The stored ``.eml`` is what the client receives, so the assertions read it
    in its transmitted form; the domain drafts are written with plain newlines.
    """
    return text.replace("\n", "\r\n")


async def outbox_rows(session: AsyncSession, tenant_id: UUID) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox)
        .where(MailOutbox.tenant_id == tenant_id)
        .order_by(MailOutbox.created_at, MailOutbox.id)
    )
    return list(rows.all())


async def seed_document(session: AsyncSession, tenant_id: UUID, company_id: UUID) -> Document:
    """One stored object, for the rows that hang off one (communications)."""
    document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="a" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add(document)
    await session.flush()
    return document


async def seed_communication(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    *,
    kind: str,
    mailbox_message_id: str | None = None,
) -> Communication:
    """One exchange row — the record a resolved case points at."""
    document = await seed_document(session, tenant_id, company_id)
    communication = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction="inbound",
        channel="email",
        kind=kind,
        mailbox_message_id=mailbox_message_id or f"provider-{uuid4()}",
        thread_id=None,
        sender_address=SENDER,
        recipient_addresses=None,
        confirmation_request_id=None,
        language=None,
        received_at=AT,
        raw_document_id=document.id,
    )
    session.add(communication)
    await session.flush()
    return communication


async def seed_request(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> ConfirmationRequest:
    """A sent request — the M2 outbox shape's own anchor (and the table the
    outbox's request leg is a foreign key into)."""
    document = await seed_document(session, tenant_id, company_id)
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=date(2026, 3, 1),
        reply_by=date(2026, 2, 1),
        status="sent",
        version_no=1,
        document_id=document.id,
        checklist=[],
        recipients=[{"person_id": None, "address": SENDER}],
    )
    session.add(request)
    await session.flush()
    return request


async def open_case(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    *,
    mailbox_message_id: str | None = None,
    thread_id: str | None = THREAD,
    rfc_message_id: str | None = RFC_ID,
    status: AttributionCaseStatus = AttributionCaseStatus.AWAITING_CONFIRMATION,
    ask_kind: AttributionAskKind | None = AttributionAskKind.CONFIRMATION,
) -> AttributionCase:
    return await create_case(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox_message_id=mailbox_message_id or f"provider-{uuid4()}",
        sender_address=SENDER,
        status=status,
        thread_id=thread_id,
        original_rfc_message_id=rfc_message_id,
        ask_kind=ask_kind,
    )


# --- 1. The case round-trips --------------------------------------------------


async def test_a_case_round_trips_through_create_and_get(session: AsyncSession) -> None:
    """The row the ingestion cycle opens, read back by id and tenant."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id, mailbox_message_id="provider-1")

    assert case.id is not None
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.ask_kind == AttributionAskKind.CONFIRMATION.value
    assert case.ask_sent_at is None
    assert case.thread_id == THREAD
    assert case.original_rfc_message_id == RFC_ID
    assert case.sender_address == SENDER
    assert case.mailbox_message_id == "provider-1"
    assert case.resolved_communication_id is None
    assert case.confirmation_communication_id is None
    assert case.created_at is not None and case.updated_at is not None

    found = await get_case(session, tenant_id=tenant.id, case_id=case.id)
    assert found is not None and found.id == case.id

    other_tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    assert await get_case(session, tenant_id=other_tenant.id, case_id=case.id) is None
    assert await get_case(session, tenant_id=tenant.id, case_id=uuid4()) is None


async def test_list_cases_filters_by_status_and_stays_in_its_tenant(
    session: AsyncSession,
) -> None:
    """The review list's read: the tenant's rows, optionally one status."""
    tenant, company = await seed_company(session)
    awaiting = await open_case(session, tenant.id, company.id)
    resolved = await open_case(
        session,
        tenant.id,
        company.id,
        status=AttributionCaseStatus.RESOLVED,
        ask_kind=None,
    )

    every = await list_cases(session, tenant_id=tenant.id)
    assert {row.id for row in every} == {awaiting.id, resolved.id}
    only_awaiting = await list_cases(
        session, tenant_id=tenant.id, status=AttributionCaseStatus.AWAITING_CONFIRMATION
    )
    assert [row.id for row in only_awaiting] == [awaiting.id]

    other_tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(other_tenant)
    await session.flush()
    assert await list_cases(session, tenant_id=other_tenant.id) == []


async def test_the_unique_index_refuses_a_second_case_for_the_same_message(
    session: AsyncSession,
) -> None:
    """One case per original message, ever — the idempotency guarantee."""
    tenant, company = await seed_company(session)
    await open_case(session, tenant.id, company.id, mailbox_message_id="provider-dup")

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await open_case(session, tenant.id, company.id, mailbox_message_id="provider-dup")

    # Another tenant may key its own row under the same provider id: the
    # unique index is per tenant, like ``communications``'.
    other_tenant, other_company = await seed_company(session)
    elsewhere = await open_case(
        session, other_tenant.id, other_company.id, mailbox_message_id="provider-dup"
    )
    assert elsewhere.id is not None


# --- 3. The closed vocabularies -----------------------------------------------


async def _check_values(session: AsyncSession, name: str) -> set[str]:
    """The quoted literals of one CHECK constraint, as the database holds it."""

    def _read(sync_connection: object) -> set[str]:
        for check in inspect(sync_connection).get_check_constraints("attribution_cases"):
            if check["name"] == name:
                return set(re.findall(r"'([^']*)'", str(check["sqltext"])))
        raise AssertionError(f"check {name} not found")

    connection = await session.connection()
    return await connection.run_sync(_read)


async def test_the_status_check_is_the_domain_status_vocabulary_verbatim(
    session: AsyncSession,
) -> None:
    """The CHECK's value set is ``AttributionCaseStatus``'s, character for
    character — an enum that grows without its migration turns this red."""
    assert await _check_values(session, "attribution_cases_status_check") == {
        member.value for member in AttributionCaseStatus
    }


async def test_the_ask_kind_check_is_the_domain_ask_kind_vocabulary_verbatim(
    session: AsyncSession,
) -> None:
    assert await _check_values(session, "attribution_cases_ask_kind_check") == {
        member.value for member in AttributionAskKind
    }


async def test_every_open_status_and_ask_kind_inserts(session: AsyncSession) -> None:
    """Each domain value the writers use is admitted by the schema."""
    tenant, company = await seed_company(session)
    for status in (
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        AttributionCaseStatus.AWAITING_CONFIRMATION,
        AttributionCaseStatus.RESOLVED,
    ):
        case = await open_case(session, tenant.id, company.id, status=status, ask_kind=None)
        assert case.status == status.value
    for ask_kind in AttributionAskKind:
        case = await open_case(session, tenant.id, company.id, ask_kind=ask_kind)
        assert case.ask_kind == ask_kind.value


async def test_an_unknown_status_or_ask_kind_is_refused(session: AsyncSession) -> None:
    """A value outside the closed sets never reaches the table."""
    tenant, company = await seed_company(session)

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                AttributionCase(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    mailbox_message_id=f"provider-{uuid4()}",
                    sender_address=SENDER,
                    status="bogus",
                )
            )
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                AttributionCase(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    mailbox_message_id=f"provider-{uuid4()}",
                    sender_address=SENDER,
                    status=AttributionCaseStatus.AWAITING_CONFIRMATION.value,
                    ask_kind="bogus",
                )
            )
            await session.flush()


async def test_a_closure_stamp_pairs_with_the_status_that_names_it(
    session: AsyncSession,
) -> None:
    """``closed`` carries ``closed_at``, ``dismissed`` carries ``dismissed_at``,
    and no other status carries either — the 0016/0017 pairing, enforced by the
    database rather than by the writer's discipline."""
    tenant, company = await seed_company(session)

    def case(status: str) -> AttributionCase:
        return AttributionCase(
            tenant_id=tenant.id,
            company_id=company.id,
            mailbox_message_id=f"provider-{uuid4()}",
            sender_address=SENDER,
            status=status,
        )

    closed = case(AttributionCaseStatus.CLOSED.value)
    closed.closed_at = AT
    session.add(closed)
    await session.flush()

    dismissed = case(AttributionCaseStatus.DISMISSED.value)
    dismissed.dismissed_at = AT
    session.add(dismissed)
    await session.flush()

    # A closed row without its stamp, one with the wrong stamp, and a stamp on
    # an open row: each is refused.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(case(AttributionCaseStatus.CLOSED.value))
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            row = case(AttributionCaseStatus.DISMISSED.value)
            row.closed_at = AT
            session.add(row)
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            row = case(AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value)
            row.closed_at = AT
            session.add(row)
            await session.flush()


# --- 4. The request-less ask ---------------------------------------------------


async def test_the_outbox_admits_a_request_less_pending_attribution_ask(
    session: AsyncSession,
) -> None:
    """The M2 row shape still inserts, and the ask's own row inserts beside it:
    no confirmation request behind it, the case in that column's place."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id)
    document = await seed_document(session, tenant.id, company.id)

    # The shape every stored row has, unchanged by this migration.
    request_row = MailOutbox(
        tenant_id=tenant.id,
        company_id=company.id,
        confirmation_request_id=request.id,
        attribution_case_id=None,
        destination="client",
        status="pending",
        kind=OutboxKind.CONFIRMATION_REQUEST.value,
    )
    session.add(request_row)
    await session.flush()
    assert request_row.attribution_case_id is None

    ask_row = MailOutbox(
        tenant_id=tenant.id,
        company_id=company.id,
        confirmation_request_id=None,
        attribution_case_id=case.id,
        destination="client",
        status="pending",
        kind=OutboxKind.ATTRIBUTION_ASK.value,
        letter_document_id=document.id,
    )
    session.add(ask_row)
    await session.flush()

    assert ask_row.confirmation_request_id is None
    assert ask_row.attribution_case_id == case.id
    assert ask_row.kind == OutboxKind.ATTRIBUTION_ASK.value

    # A cross-tenant case id is uninsertable (the composite FK pins the case
    # to this row's own tenant, the F1 pattern).
    other_tenant, other_company = await seed_company(session)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                MailOutbox(
                    tenant_id=other_tenant.id,
                    company_id=other_company.id,
                    confirmation_request_id=None,
                    attribution_case_id=case.id,
                    destination="client",
                    status="pending",
                    kind=OutboxKind.ATTRIBUTION_ASK.value,
                )
            )
            await session.flush()

    # The refused vocabulary: a kind outside the widened CHECK does not insert.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                MailOutbox(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    confirmation_request_id=None,
                    attribution_case_id=case.id,
                    destination="client",
                    status="pending",
                    kind="bogus",
                )
            )
            await session.flush()


async def test_the_pending_ask_index_admits_one_pending_ask_per_case(
    session: AsyncSession,
) -> None:
    """The ask's idempotency: a second pending ask for the same case is
    refused, and a delivered one frees the slot (the partial predicate)."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id)

    def ask() -> MailOutbox:
        return MailOutbox(
            tenant_id=tenant.id,
            company_id=company.id,
            confirmation_request_id=None,
            attribution_case_id=case.id,
            destination="client",
            status="pending",
            kind=OutboxKind.ATTRIBUTION_ASK.value,
        )

    first = ask()
    session.add(first)
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(ask())
            await session.flush()

    # Send the first one and enqueue the retry: only *pending* rows collide.
    first.status = "sent"
    first.sent_at = AT
    await session.flush()
    second = ask()
    session.add(second)
    await session.flush()

    rows = list(
        (
            await session.scalars(
                select(MailOutbox).where(MailOutbox.attribution_case_id == case.id)
            )
        ).all()
    )
    assert {row.id for row in rows} == {first.id, second.id}


# --- 5. The reads and writes the flow will make --------------------------------


async def test_open_cases_for_thread_matches_either_anchor(session: AsyncSession) -> None:
    """The suppression and processing reads find a case by its provider thread
    id or by any RFC ``Message-ID`` its message carries — either anchor, only
    open cases, only the case's own company."""
    tenant, company = await seed_company(session)
    by_thread = await open_case(session, tenant.id, company.id, rfc_message_id=None)
    by_rfc = await open_case(session, tenant.id, company.id, thread_id=None)
    resolved = await open_case(
        session,
        tenant.id,
        company.id,
        status=AttributionCaseStatus.RESOLVED,
        ask_kind=None,
    )
    other_company = Company(
        tenant_id=tenant.id,
        cr_number="7654321",
        br_number="87654321",
        date_of_incorporation="2011-04-02",
        anniversary_date="2026-04-02",
        type="private",
    )
    session.add(other_company)
    await session.flush()
    other = await open_case(session, tenant.id, other_company.id)

    async def found(*, thread_id: str | None, rfc_message_ids: list[str]) -> set[UUID]:
        rows = await open_cases_for_thread(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            thread_id=thread_id,
            rfc_message_ids=rfc_message_ids,
        )
        return {row.id for row in rows}

    assert await found(thread_id=THREAD, rfc_message_ids=[]) == {by_thread.id}
    assert await found(thread_id=None, rfc_message_ids=[RFC_ID]) == {by_rfc.id}
    assert await found(thread_id=THREAD, rfc_message_ids=[RFC_ID]) == {
        by_thread.id,
        by_rfc.id,
    }
    # The id leg is a set: any token the message carries finds the case …
    assert await found(thread_id=None, rfc_message_ids=["<other@x>", RFC_ID]) == {by_rfc.id}
    # … a blank entry matches no column, and no anchor at all matches nothing.
    assert await found(thread_id=None, rfc_message_ids=["", "  "]) == set()
    assert await found(thread_id="another-thread", rfc_message_ids=["<nothing@x>"]) == set()
    assert await found(thread_id=None, rfc_message_ids=[]) == set()
    # The resolved case is not open; another company's case is not this one's.
    assert resolved.id not in await found(thread_id=THREAD, rfc_message_ids=[RFC_ID])
    assert other.id not in await found(thread_id=THREAD, rfc_message_ids=[RFC_ID])


async def test_latest_ask_for_thread_reports_only_an_open_asked_case(
    session: AsyncSession,
) -> None:
    """R3's suppression read: the newest open case on the anchor whose ask has
    left. An open case whose ask was never sent, a resolved case and a case on
    another anchor all answer ``None`` — an ask may go out."""
    tenant, company = await seed_company(session)
    await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-a", thread_id="thread-unasked"
    )
    asked = await open_case(session, tenant.id, company.id, mailbox_message_id="provider-b")
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)
    later = await open_case(session, tenant.id, company.id, mailbox_message_id="provider-c")
    await set_ask_sent(
        session, tenant_id=tenant.id, case_id=later.id, at=AT + timedelta(minutes=5)
    )
    resolved = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-d",
        status=AttributionCaseStatus.RESOLVED,
        ask_kind=None,
    )
    await set_ask_sent(session, tenant_id=tenant.id, case_id=resolved.id, at=AT)

    latest = await latest_ask_for_thread(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        thread_id=THREAD,
        rfc_message_ids=[],
    )
    assert latest is not None and latest.id == later.id

    # The open case whose ask never left does not suppress its own thread …
    assert (
        await latest_ask_for_thread(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            thread_id="thread-unasked",
            rfc_message_ids=[],
        )
        is None
    )
    # … and neither does an anchor nothing sits on.
    assert (
        await latest_ask_for_thread(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            thread_id="another-thread",
            rfc_message_ids=["<nothing@x>"],
        )
        is None
    )
    # The id leg is a set: the asked case is found from any token its chain
    # carries (the IMAP leg's only evidence), and not from an unrelated one.
    assert (
        await latest_ask_for_thread(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            thread_id=None,
            rfc_message_ids=["<request@tcsp.example>", RFC_ID],
        )
    ) is not None
    assert (
        await latest_ask_for_thread(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            thread_id=None,
            rfc_message_ids=["<request@tcsp.example>", "<unrelated@x>"],
        )
        is None
    )

    # No asked open case at all: the read that lets the ask go out answers None.
    other_tenant, other_company = await seed_company(session)
    await open_case(session, other_tenant.id, other_company.id, ask_kind=None)
    assert (
        await latest_ask_for_thread(
            session,
            tenant_id=other_tenant.id,
            company_id=other_company.id,
            thread_id=THREAD,
            rfc_message_ids=[RFC_ID],
        )
        is None
    )


async def test_count_asks_since_counts_the_window(session: AsyncSession) -> None:
    """The hourly cap's read: asks that left at or after the cutoff, and only
    this tenant's."""
    tenant, company = await seed_company(session)
    within = await open_case(session, tenant.id, company.id)
    boundary = await open_case(session, tenant.id, company.id)
    before = await open_case(session, tenant.id, company.id)
    await set_ask_sent(session, tenant_id=tenant.id, case_id=within.id, at=AT)
    await set_ask_sent(
        session, tenant_id=tenant.id, case_id=boundary.id, at=AT - timedelta(hours=1)
    )
    await set_ask_sent(
        session, tenant_id=tenant.id, case_id=before.id, at=AT - timedelta(hours=1, seconds=1)
    )

    assert await count_asks_since(session, tenant_id=tenant.id, since=AT - timedelta(hours=1)) == 2
    assert await count_asks_since(session, tenant_id=tenant.id, since=AT) == 1
    assert await count_asks_since(session, tenant_id=tenant.id, since=AT + timedelta(hours=1)) == 0

    other_tenant, other_company = await seed_company(session)
    other = await open_case(session, other_tenant.id, other_company.id)
    await set_ask_sent(session, tenant_id=other_tenant.id, case_id=other.id, at=AT)
    assert await count_asks_since(session, tenant_id=tenant.id, since=AT) == 1


async def test_set_status_moves_a_case_and_stamps_its_closure(session: AsyncSession) -> None:
    """The writes the lifecycle will make: resolved with its exchange, closed
    and dismissed with their instants, and a missing closure instant refused
    before the database sees it."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id)
    communication = await seed_communication(
        session, tenant.id, company.id, kind="reply", mailbox_message_id=RFC_ID
    )

    moved = await set_status(
        session,
        tenant_id=tenant.id,
        case_id=case.id,
        status=AttributionCaseStatus.RESOLVED,
        resolved_communication_id=communication.id,
    )
    assert moved is not None
    assert moved.status == AttributionCaseStatus.RESOLVED.value
    assert moved.resolved_communication_id == communication.id

    closed = await open_case(session, tenant.id, company.id)
    closed_row = await set_status(
        session, tenant_id=tenant.id, case_id=closed.id, status=AttributionCaseStatus.CLOSED, at=AT
    )
    assert closed_row is not None
    assert closed_row.status == AttributionCaseStatus.CLOSED.value
    assert closed_row.closed_at == AT

    dismissed = await open_case(session, tenant.id, company.id)
    dismissed_row = await set_status(
        session,
        tenant_id=tenant.id,
        case_id=dismissed.id,
        status=AttributionCaseStatus.DISMISSED,
        at=AT,
    )
    assert dismissed_row is not None and dismissed_row.dismissed_at == AT

    with pytest.raises(ValueError):
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
        )
    with pytest.raises(ValueError):
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=case.id,
            status=AttributionCaseStatus.DISMISSED,
        )
    # An unknown (or another tenant's) id is simply not found.
    assert (
        await set_status(
            session,
            tenant_id=uuid4(),
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
            at=AT,
        )
        is None
    )
    assert (
        await set_ask_sent(session, tenant_id=uuid4(), case_id=case.id, at=AT) is None
    )


async def test_the_two_new_communication_kinds_insert(session: AsyncSession) -> None:
    """The widened ``communications_kind_check`` admits the ask and the
    confirming reply, and still refuses a value outside the vocabulary."""
    tenant, company = await seed_company(session)
    ask = await seed_communication(session, tenant.id, company.id, kind="attribution_ask")
    confirmation = await seed_communication(
        session, tenant.id, company.id, kind="attribution_confirmation"
    )
    assert ask.kind == "attribution_ask"
    assert confirmation.kind == "attribution_confirmation"

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await seed_communication(session, tenant.id, company.id, kind="bogus")


# --- 6. The ask's enqueue (ADR-0019 §1; ruling R4) -----------------------------
#
# The ask's own half: the guard order (suppression, the §5.1 gate, the two
# anti-abuse headers, the hourly cap), the compose-store-enqueue glue and its
# one audit entry, and the outbox row's request-less idempotency. Every
# suppression path leaves the case open with ``ask_sent_at`` NULL — the R3
# posture — and writes no ``attribution_ask_enqueued`` entry.


async def askable_company(session: AsyncSession, **kwargs: object) -> tuple[Tenant, Company]:
    """A tenant and a company the ask's composition and gate admit."""
    tenant, company = await seed_company(session)
    await name_company(session, tenant.id, company.id, **kwargs)
    await cover(session, tenant.id, company.id)
    return tenant, company


async def ask_case(
    session: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    *,
    mailbox_message_id: str,
    thread_id: str | None = THREAD,
    rfc_message_id: str | None = RFC_ID,
    action: AttributionAction = AttributionAction.ASK_REGISTERED_CHANNEL,
) -> AttributionCase:
    case, _created = await attribution.open_case(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox_message_id=mailbox_message_id,
        sender_address=SENDER,
        action=action,
        thread_id=thread_id,
        original_rfc_message_id=rfc_message_id,
    )
    return case


async def enqueue_ask(
    session: AsyncSession,
    store: FakeStore,
    scanner: FakeScanner,
    tenant_id: UUID,
    case: AttributionCase,
    *,
    recipients: tuple[str, ...] = (SENDER,),
    rfc_message_ids: tuple[str, ...] = (),
    original_subject: str = "Re: Annual Return Confirmation",
    auto_submitted: str | None = None,
    return_path: str | None = None,
) -> attribution.AskReport:
    return await attribution.enqueue_ask(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        case=case,
        recipients=recipients,
        rfc_message_ids=rfc_message_ids,
        original_subject=original_subject,
        original_received_at=AT,
        auto_submitted=auto_submitted,
        return_path=return_path,
        now=AT,
    )


async def test_the_registration_ask_is_composed_stored_and_enqueued(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0019 §1's 2.1 ask: the letter is composed from envelope facts alone,
    stored as the records registry's ``generated_draft``, queued as a
    request-less ``attribution_ask`` row and stamped — all one transaction,
    audited by one coordinates-only entry."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    case = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-ask-1"
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

    assert report.enqueued is True
    assert report.reason is None
    assert report.met_pending_row is False
    assert report.outbox_id is not None
    assert report.document_id is not None

    # The records-registry home: the letter prose lives here and nowhere else.
    document = await session.get(Document, report.document_id)
    assert document is not None
    assert document.kind == "generated_draft"
    assert document.origin == "generated"
    assert document.retention_class == "client_communication"
    assert document.content_type == "message/rfc822"
    assert document.scan_status == "clean"
    assert document.company_id == company.id
    raw = store.objects[document.storage_key]

    # The message itself: from the deployment's mailbox, to the sender the
    # decision read, threaded on the original's RFC Message-ID, naming the
    # company by name and number in both language halves (scope §7).
    message = message_from_bytes(raw, policy=default_policy)
    assert str(message["From"]) == MAILBOX
    assert str(message["To"]) == SENDER
    assert str(message["Message-ID"]).endswith("@tcsp.example>")
    assert str(message["In-Reply-To"]) == RFC_ID
    assert str(message["References"]) == RFC_ID
    assert str(message["Subject"]) == ask_subject(
        company_name=COMPANY_NAME,
        cr_number="1234567",
        br_number="12345678",
        kind=AttributionAskKind.REGISTERED_CHANNEL,
    )
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content() == crlf(
        registration_ask_letter(
            company_name=COMPANY_NAME, cr_number="1234567", br_number="12345678"
        )
    )

    # The outbox row: request-less, its case in the request's place, and
    # send-on-release — the ask is a notice, never held for staff.
    row = await session.get(MailOutbox, report.outbox_id)
    assert row is not None
    assert row.kind == OutboxKind.ATTRIBUTION_ASK.value
    assert row.attribution_case_id == case.id
    assert row.confirmation_request_id is None
    assert row.status == OutboxStatus.PENDING.value
    assert row.destination == "client"
    assert row.letter_document_id == document.id
    assert row.not_before is None
    assert row.created_by is None
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=AT)
    assert [job.id for job in due] == [row.id]

    # The stamp the suppression read answers from, in the same transaction.
    assert case.ask_sent_at == AT

    entries = await audit_entries(session, tenant.id)
    assert [entry.action for entry in entries][-3:] == [
        AuditAction.ATTRIBUTION_CASE_OPENED.value,
        AuditAction.DOCUMENT_REGISTERED.value,
        AuditAction.ATTRIBUTION_ASK_ENQUEUED.value,
    ]
    enqueued = entries[-1]
    assert enqueued.entity_type == "attribution_case"
    assert enqueued.entity_id == case.id
    assert enqueued.company_id == company.id
    assert enqueued.actor_user_id is None
    assert enqueued.details == {
        "case_id": str(case.id),
        "ask_kind": AttributionAskKind.REGISTERED_CHANNEL.value,
        "outbox_id": str(row.id),
        "document_id": str(document.id),
        "destination": "client",
        "met_pending_row": False,
    }


async def test_the_confirmation_ask_addresses_the_stored_channel_form(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmation ask's recipients arrive in ``decide``'s comparison
    form; the composed message addresses the form the tenant registered — and
    an address with no stored channel falls back to the comparison form."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    await seed_channel(session, tenant.id, company.id, identifier="Director@Client.Example")
    case = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-ask-2",
        action=AttributionAction.ASK_CONFIRMATION,
    )
    subject = "Re: [Synthetic Harbour Limited — CR 1234567] Annual Return Confirmation"

    report = await enqueue_ask(
        session,
        store,
        scanner_seam,
        tenant.id,
        case,
        recipients=("director@client.example", "assistant@elsewhere.example"),
        original_subject=subject,
    )

    document = await session.get(Document, report.document_id)
    assert document is not None
    message = message_from_bytes(
        store.objects[document.storage_key], policy=default_policy
    )
    assert str(message["To"]) == "Director@Client.Example, assistant@elsewhere.example"
    assert str(message["Subject"]) == ask_subject(
        company_name=COMPANY_NAME,
        cr_number="1234567",
        br_number="12345678",
        kind=AttributionAskKind.CONFIRMATION,
    )
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert body.get_content() == crlf(
        confirmation_ask_letter(
            company_name=COMPANY_NAME,
            cr_number="1234567",
            br_number="12345678",
            sender_address=SENDER,
            original_subject=subject,
            original_received_at=AT,
        )
    )


async def test_a_letter_that_cannot_compose_keeps_the_ask_off(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §7 and the mailbox rule, fail-closed: a company with no name in
    force, and a deployment with no mailbox address, compose no letter — the
    case stays open with ``ask_sent_at`` NULL, no row is written, and the
    report carries the refusal's own bounded code."""
    licensed(monkeypatch)
    tenant, company = await seed_company(session)
    await cover(session, tenant.id, company.id)  # licensed and covered, still no name
    unnamed = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-unnamed"
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, unnamed)

    assert report.enqueued is False
    assert report.reason == "company_name_unavailable"
    assert unnamed.ask_sent_at is None

    # The other refusal: a name in force, but no mailbox to write from.
    monkeypatch.setattr(settings, "mail_address", "")
    named_tenant, named_company = await askable_company(session)
    unmailboxed = await ask_case(
        session,
        named_tenant.id,
        named_company.id,
        mailbox_message_id="provider-unmailboxed",
        thread_id="thread-unmailboxed",
    )

    second = await enqueue_ask(session, store, scanner_seam, named_tenant.id, unmailboxed)

    assert second.enqueued is False
    assert second.reason == "mail_not_configured"
    assert unmailboxed.ask_sent_at is None
    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []


async def test_an_ask_that_already_left_the_thread_suppresses_the_next_one(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3's suppression: an open case on the same anchor whose ask has left
    keeps the next ask off. The new case still opens — visible to staff — with
    ``ask_sent_at`` NULL, no letter stored, no row written, and no enqueue
    entry in the chain."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    asked = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-asked"
    )
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)
    case = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-suppressed",
        rfc_message_id="<second@client.example>",
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

    assert report.enqueued is False
    assert report.reason == attribution.ASK_SUPPRESSED
    assert report.outbox_id is None and report.document_id is None
    assert case.ask_sent_at is None
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []
    actions = [entry.action for entry in await audit_entries(session, tenant.id)]
    assert actions.count(AuditAction.ATTRIBUTION_CASE_OPENED.value) == 2  # both cases
    assert AuditAction.ATTRIBUTION_ASK_ENQUEUED.value not in actions


async def test_an_ancestor_id_suppresses_the_next_ask_on_the_chain(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suppression read's id leg is the ancestor set: on a leg with no
    provider thread id (IMAP), a second message whose
    ``In-Reply-To``/``References`` carry the first case's ``Message-ID`` meets
    that case's ask and asks nothing of its own."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    asked = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-imap-1",
        thread_id=None,
        rfc_message_id="<m1@client.example>",
    )
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)
    case = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-imap-2",
        thread_id=None,
        rfc_message_id="<m2@client.example>",
    )

    report = await enqueue_ask(
        session,
        store,
        scanner_seam,
        tenant.id,
        case,
        rfc_message_ids=("<request@tcsp.example>", "<m1@client.example>"),
    )

    assert report.enqueued is False
    assert report.reason == attribution.ASK_SUPPRESSED
    assert case.ask_sent_at is None
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []
    assert AuditAction.ATTRIBUTION_ASK_ENQUEUED.value not in [
        entry.action for entry in await audit_entries(session, tenant.id)
    ]


async def test_a_terminal_case_is_never_asked_again(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case that has left the two awaits refuses the ask, whichever way it
    settled: the cursor's overlap re-read meets the settled row through
    ``open_case`` and must not reopen it with a fresh ask."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    settlements = (
        (AttributionCaseStatus.RESOLVED, "provider-settled-resolved"),
        (AttributionCaseStatus.CLOSED, "provider-settled-closed"),
        (AttributionCaseStatus.DISMISSED, "provider-settled-dismissed"),
    )

    for index, (status, mailbox_message_id) in enumerate(settlements):
        case = await ask_case(
            session,
            tenant.id,
            company.id,
            mailbox_message_id=mailbox_message_id,
            thread_id=f"thread-settled-{index}",
            rfc_message_id=f"<settled-{index}@client.example>",
        )
        await set_status(session, tenant_id=tenant.id, case_id=case.id, status=status, at=AT)

        report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

        assert report.enqueued is False
        assert report.reason == attribution.ASK_CASE_NOT_OPEN
        assert case.status == status.value
        assert case.ask_sent_at is None

    assert len(await list_cases(session, tenant_id=tenant.id)) == 3
    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []
    assert AuditAction.ATTRIBUTION_ASK_ENQUEUED.value not in [
        entry.action for entry in await audit_entries(session, tenant.id)
    ]


async def test_the_gate_denies_the_ask_and_leaves_the_case_open(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask is gated on ``EMAIL_INGESTION`` for the case's company (the
    review's first ruling): a deployment without the licensed capability
    writes no ask, and the case — already opened by the thread evidence —
    stays open for staff."""
    licensed(monkeypatch, tier="free")
    tenant, company = await askable_company(session)
    case = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-gated"
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

    assert report.enqueued is False
    assert report.reason == attribution.ASK_GATED
    assert case.ask_sent_at is None
    assert case.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []
    actions = [entry.action for entry in await audit_entries(session, tenant.id)]
    assert actions.count(AuditAction.ATTRIBUTION_CASE_OPENED.value) == 1
    assert AuditAction.ATTRIBUTION_ASK_ENQUEUED.value not in actions


async def test_the_anti_abuse_headers_keep_the_ask_off(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling R4's two metadata-only guards: an original that carries
    ``Auto-Submitted`` (any value — the guard leans safe) or a ``Return-Path``
    with no address (``<>``, the bounce/backscatter sender) is not answered
    with an ask, and its case still opens."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)

    # The guard itself, over the metadata the ingestion cycle hands over.
    assert attribution.ask_allowed(auto_submitted=None, return_path=None)
    assert attribution.ask_allowed(
        auto_submitted="", return_path="<bounce@tcsp.example>"
    )
    assert not attribution.ask_allowed(auto_submitted="auto-generated", return_path=None)
    assert not attribution.ask_allowed(auto_submitted="no", return_path=None)
    assert not attribution.ask_allowed(auto_submitted=None, return_path="")
    assert not attribution.ask_allowed(auto_submitted=None, return_path="<>")
    assert not attribution.ask_allowed(auto_submitted=None, return_path="< >")

    headers = (("auto-replied", None), (None, "<>"))
    for index, (auto_submitted, return_path) in enumerate(headers):
        case = await ask_case(
            session,
            tenant.id,
            company.id,
            mailbox_message_id=f"provider-guard-{index}",
            thread_id=f"thread-guard-{index}",
        )
        report = await enqueue_ask(
            session,
            store,
            scanner_seam,
            tenant.id,
            case,
            auto_submitted=auto_submitted,
            return_path=return_path,
        )
        assert report.enqueued is False
        assert report.reason == attribution.ASK_NOT_ALLOWED
        assert case.ask_sent_at is None

    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []


async def test_the_hourly_cap_suppresses_further_asks(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling R4's ceiling: once the window holds ``attribution_ask_hourly_cap``
    asks, further asks are suppressed without failing the cycle — and ``0``
    means the deployment never asks at all."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    asked = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-cap-asked"
    )
    await set_ask_sent(
        session, tenant_id=tenant.id, case_id=asked.id, at=AT - timedelta(minutes=5)
    )
    monkeypatch.setattr(settings, "attribution_ask_hourly_cap", 1)
    case = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-cap-next",
        thread_id="thread-cap-next",
        rfc_message_id="<cap-next@client.example>",
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

    assert report.enqueued is False
    assert report.reason == attribution.ASK_CAP_REACHED
    assert case.ask_sent_at is None

    # ``0`` is "never ask": a tenant with no ask in the window is refused too.
    other_tenant, other_company = await askable_company(session)
    monkeypatch.setattr(settings, "attribution_ask_hourly_cap", 0)
    never = await ask_case(
        session,
        other_tenant.id,
        other_company.id,
        mailbox_message_id="provider-cap-zero",
        thread_id="thread-cap-zero",
        rfc_message_id="<cap-zero@client.example>",
    )
    zero_report = await enqueue_ask(session, store, scanner_seam, other_tenant.id, never)
    assert zero_report.enqueued is False
    assert zero_report.reason == attribution.ASK_CAP_REACHED
    assert never.ask_sent_at is None

    assert store.objects == {}
    assert [row.id for row in await outbox_rows(session, tenant.id)] == []


async def test_a_second_enqueue_for_one_case_meets_the_pending_index(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping enqueues for one case write one row (the partial index
    per case and kind): the loser re-reads the winner's row, which is the
    idempotent answer, and the row keeps its send-on-release posture."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    case = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-race"
    )
    first_document = await seed_document(session, tenant.id, company.id)
    second_document = await seed_document(session, tenant.id, company.id)

    first = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        case_id=case.id,
        letter_document_id=first_document.id,
    )
    second = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        case_id=case.id,
        letter_document_id=second_document.id,
    )

    assert first.created is True
    assert second.created is False
    assert second.row.id == first.row.id
    assert second.row.letter_document_id == first_document.id  # the winner's letter stands
    assert second.row.not_before is None
    assert [row.id for row in await outbox_rows(session, tenant.id)] == [first.row.id]


async def test_a_second_ask_for_one_case_reports_the_met_pending_row(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case that already has a pending ask row (a raced enqueue, or one
    written before its stamp): the enqueue meets the index, and both the
    report and the audit name the winner's letter — never pairing the fresh,
    undelivered document with a row that will not carry it. The fresh letter
    stands registered as the record of what this call composed."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    case = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-met"
    )
    first_document = await seed_document(session, tenant.id, company.id)
    first = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        case_id=case.id,
        letter_document_id=first_document.id,
    )

    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)

    assert report.enqueued is True
    assert report.met_pending_row is True
    assert report.outbox_id == first.row.id
    assert report.document_id is None
    assert case.ask_sent_at == AT
    (row,) = await outbox_rows(session, tenant.id)
    assert row.id == first.row.id
    assert row.letter_document_id == first_document.id
    # The fresh letter is on the registry all the same …
    assert len(store.objects) == 1
    stored = list(
        (
            await session.scalars(
                select(Document).where(Document.company_id == company.id)
            )
        ).all()
    )
    fresh = [document for document in stored if document.id != first_document.id]
    assert len(fresh) == 1
    # … and the entry names the winner's letter, which is what will deliver.
    (enqueued,) = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
    ]
    assert enqueued.details["met_pending_row"] is True
    assert enqueued.details["outbox_id"] == str(first.row.id)
    assert enqueued.details["document_id"] == str(first_document.id)


# --- 8. The confirmation act, the original, and the 2.1 closure --------------
#
# The lifecycle T6 adds, at the application seam: what ``confirm_case``
# records and when it refuses, what ``process_case`` does with a case whose
# original another road already recorded (and with a case that has left the
# awaits), and what ``close_registration_cases`` closes, cancels and audits.
# The end-to-end confirming reply — the stored exchange, the processed
# original, the parse cycle's exclusion — rides the ingestion suite, which
# owns the mailbox harness.


async def test_confirm_case_records_the_act_only_while_a_case_awaits_a_confirmation(
    session: AsyncSession,
) -> None:
    """R5's record: the confirming exchange is pinned to the case when a
    registered channel's reply arrives, and the status stays the await — the
    resolve is ``process_case``'s, so a gate denial can leave the case where it
    is. A 2.1 case and one that has left the awaits are both refused
    unchanged."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id)
    communication = await seed_communication(
        session,
        tenant.id,
        company.id,
        kind="attribution_confirmation",
        mailbox_message_id="provider-confirm",
    )

    applied = await attribution.confirm_case(
        session,
        tenant_id=tenant.id,
        case=case,
        confirmation_communication_id=communication.id,
    )

    assert applied is True
    assert case.confirmation_communication_id == communication.id
    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.resolved_communication_id is None

    registration = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-21",
        status=AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        ask_kind=AttributionAskKind.REGISTERED_CHANNEL,
    )
    assert (
        await attribution.confirm_case(
            session,
            tenant_id=tenant.id,
            case=registration,
            confirmation_communication_id=communication.id,
        )
        is False
    )
    assert registration.confirmation_communication_id is None

    moved = await set_status(
        session,
        tenant_id=tenant.id,
        case_id=case.id,
        status=AttributionCaseStatus.RESOLVED,
        resolved_communication_id=communication.id,
    )
    assert moved is not None
    assert (
        await attribution.confirm_case(
            session,
            tenant_id=tenant.id,
            case=case,
            confirmation_communication_id=communication.id,
        )
        is False
    )
    assert case.confirmation_communication_id == communication.id


async def test_close_registration_cases_closes_the_21_cases_and_cancels_their_asks(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7's closure, idempotently: only the awaiting-registered-channel cases
    close — a case awaiting a confirmation, and one already settled, are left
    alone — each closed case's queued ask is cancelled, and one entry names
    the ids that closed."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    registration = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-21",
        action=AttributionAction.ASK_REGISTERED_CHANNEL,
    )
    report = await enqueue_ask(session, store, scanner_seam, tenant.id, registration)
    assert report.enqueued is True
    (ask_row,) = await outbox_rows(session, tenant.id)
    confirmation = await ask_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-22",
        action=AttributionAction.ASK_CONFIRMATION,
    )
    settled = await open_case(session, tenant.id, company.id, mailbox_message_id="provider-23")
    assert (
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=settled.id,
            status=AttributionCaseStatus.RESOLVED,
        )
        is not None
    )

    closed = await attribution.close_registration_cases(
        session,
        tenant_id=tenant.id,
        cases=[registration, confirmation, settled],
        now=AT,
    )

    assert [row.id for row in closed] == [registration.id]
    assert registration.status == AttributionCaseStatus.CLOSED.value
    assert registration.closed_at == AT
    assert registration.resolved_communication_id is None
    cancelled = await session.get(MailOutbox, ask_row.id)
    assert cancelled is not None
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_CLOSE
    assert confirmation.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert confirmation.closed_at is None
    assert settled.status == AttributionCaseStatus.RESOLVED.value

    (entry,) = [
        item
        for item in await audit_entries(session, tenant.id)
        if item.action == AuditAction.ATTRIBUTION_CASE_CLOSED.value
    ]
    assert entry.company_id == company.id
    assert entry.actor_user_id is None
    assert entry.details == {"case_ids": [str(registration.id)]}

    assert (
        await attribution.close_registration_cases(
            session, tenant_id=tenant.id, cases=[registration], now=AT
        )
        == ()
    )
    assert [item.action for item in await audit_entries(session, tenant.id)].count(
        AuditAction.ATTRIBUTION_CASE_CLOSED.value
    ) == 1
    assert cancelled.status == OutboxStatus.CANCELLED.value


async def test_process_case_resolves_to_the_exchange_the_original_already_became(
    session: AsyncSession,
) -> None:
    """The original was ingested by another road (a §2.5 retry after its sender
    was registered) while its case still awaited: processing the case resolves
    it to that exchange — the provider message id keys one Communication, ever
    — instead of writing a second row for it."""
    tenant, company = await seed_company(session)
    case = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-original",
        thread_id=None,
        rfc_message_id=None,
    )
    communication = await seed_communication(
        session,
        tenant.id,
        company.id,
        kind="reply",
        mailbox_message_id="provider-original",
    )

    report = await attribution.process_case(
        session, FakeStore(), FakeScanner(), tenant_id=tenant.id, case=case, now=AT
    )

    assert report.outcome is IngestOutcome.SKIPPED_KNOWN
    assert report.resolved is True
    assert report.communication_id == communication.id
    assert report.reason is None
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.resolved_communication_id == communication.id
    rows = list(
        (
            await session.scalars(
                select(Communication).where(Communication.tenant_id == tenant.id)
            )
        ).all()
    )
    assert [row.id for row in rows] == [communication.id]


async def test_process_case_leaves_a_settled_case_alone(session: AsyncSession) -> None:
    """A case that has left the awaits is never processed — the answer a
    staff-forced processing route reads when another writer settled the case
    first. No mailbox is reached for, so the refusal stands with none
    configured."""
    tenant, company = await seed_company(session)
    case = await open_case(session, tenant.id, company.id, mailbox_message_id="provider-settled")
    assert (
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
            at=AT,
        )
        is not None
    )

    report = await attribution.process_case(
        session, FakeStore(), FakeScanner(), tenant_id=tenant.id, case=case, now=AT
    )

    assert report.resolved is False
    assert report.outcome is IngestOutcome.SKIPPED_KNOWN
    assert report.reason == attribution.PROCESS_CASE_NOT_OPEN
    assert case.status == AttributionCaseStatus.CLOSED.value
    assert case.resolved_communication_id is None


# --- 9. The staff surface (T7) -------------------------------------------------
#
# The application half the API's routes call: the list's awaiting filter (the
# two open states as one queue), the dismissal (close + cancel the queued ask
# + one entry), and the staff-forced processing (resolve + cancel + the entry
# naming the case, the exchange and the actor). The routes' auth and refusal
# mapping ride the hermetic ``test_api_attribution.py``; the writes and the
# chain are what is proven here.


async def seed_user(session: AsyncSession, tenant_id: UUID) -> User:
    """One staff account — the actor the surface's entries name.

    The audit table's ``(actor_user_id, tenant_id)`` foreign key pins the
    actor to a real user row, so the acting member is seeded rather than a
    bare uuid passed in.
    """
    user = User(
        tenant_id=tenant_id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(user)
    await session.flush()
    return user


async def test_the_surface_list_covers_both_awaits_and_stays_in_its_tenant(
    session: AsyncSession,
) -> None:
    """``awaiting`` is both open statuses at once — the queue staff work from
    — the single-status filters stay single, and every read is the tenant's,
    newest first as the adapter orders."""
    tenant, company = await seed_company(session)
    registration = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-open-1",
        status=AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        ask_kind=AttributionAskKind.REGISTERED_CHANNEL,
    )
    confirmation = await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-open-2"
    )
    resolved = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-resolved",
        status=AttributionCaseStatus.RESOLVED,
        ask_kind=None,
    )
    settled = await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-settled"
    )
    assert (
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=settled.id,
            status=AttributionCaseStatus.DISMISSED,
            at=AT,
        )
        is not None
    )

    awaiting = await attribution.list_cases(
        session, tenant_id=tenant.id, statuses=OPEN_STATUSES
    )
    assert {row.id for row in awaiting} == {registration.id, confirmation.id}
    # The merge keeps the adapter's order: newest first, both statuses mixed.
    timestamps = [row.created_at for row in awaiting]
    assert timestamps == sorted(timestamps, reverse=True)
    assert [
        row.id
        for row in await attribution.list_cases(
            session, tenant_id=tenant.id, statuses=(AttributionCaseStatus.RESOLVED,)
        )
    ] == [resolved.id]
    assert {
        row.id
        for row in await attribution.list_cases(session, tenant_id=tenant.id, statuses=None)
    } == {registration.id, confirmation.id, resolved.id, settled.id}

    other_tenant, other_company = await seed_company(session)
    other_case = await open_case(session, other_tenant.id, other_company.id)
    assert {
        row.id
        for row in await attribution.list_cases(
            session, tenant_id=other_tenant.id, statuses=OPEN_STATUSES
        )
    } == {other_case.id}


async def test_dismiss_case_closes_the_case_cancels_the_ask_and_writes_one_entry(
    session: AsyncSession,
    store: FakeStore,
    scanner_seam: FakeScanner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dismissal is a statement about the row: it moves to ``dismissed``
    with the caller's instant, the queued ask is cancelled before it could
    deliver, and one ``attribution_case_dismissed`` entry — the case id and
    nothing of the mail, the actor on the entry — records who decided."""
    licensed(monkeypatch)
    tenant, company = await askable_company(session)
    actor = await seed_user(session, tenant.id)
    case = await ask_case(
        session, tenant.id, company.id, mailbox_message_id="provider-dismiss"
    )
    report = await enqueue_ask(session, store, scanner_seam, tenant.id, case)
    assert report.enqueued is True
    (ask_row,) = await outbox_rows(session, tenant.id)

    moved = await attribution.dismiss_case(
        session, tenant_id=tenant.id, case_id=case.id, actor_user_id=actor.id, now=AT
    )

    assert moved.status == AttributionCaseStatus.DISMISSED.value
    assert moved.dismissed_at == AT
    assert moved.closed_at is None
    assert moved.ask_sent_at == AT  # the ask's own stamp is history, unchanged
    cancelled = await session.get(MailOutbox, ask_row.id)
    assert cancelled is not None
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_DISMISS
    (entry,) = [
        item
        for item in await audit_entries(session, tenant.id)
        if item.action == AuditAction.ATTRIBUTION_CASE_DISMISSED.value
    ]
    assert entry.actor_user_id == actor.id
    assert entry.company_id == company.id
    assert entry.entity_type == "attribution_case"
    assert entry.entity_id == case.id
    assert entry.details == {"case_id": str(case.id)}


async def test_process_open_case_resolves_cancels_the_ask_and_writes_the_entry(
    session: AsyncSession,
) -> None:
    """A staff-forced processing of a case whose original another road already
    recorded: the case resolves to that exchange, the queued ask is cancelled
    (a settled case is never asked), and one ``attribution_case_resolved``
    entry names the case, the exchange and the acting member."""
    tenant, company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    case = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id="provider-surface-process",
        thread_id=None,
        rfc_message_id=None,
    )
    communication = await seed_communication(
        session,
        tenant.id,
        company.id,
        kind="reply",
        mailbox_message_id="provider-surface-process",
    )
    letter = await seed_document(session, tenant.id, company.id)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        case_id=case.id,
        letter_document_id=letter.id,
    )

    result = await attribution.process_open_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case_id=case.id,
        actor_user_id=actor.id,
        now=AT,
    )

    assert result.report.resolved is True
    assert result.report.reason is None
    assert result.report.communication_id == communication.id
    assert result.case.status == AttributionCaseStatus.RESOLVED.value
    assert result.case.resolved_communication_id == communication.id
    cancelled = await session.get(MailOutbox, enqueued.row.id)
    assert cancelled is not None
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_RESOLVE
    (entry,) = [
        item
        for item in await audit_entries(session, tenant.id)
        if item.action == AuditAction.ATTRIBUTION_CASE_RESOLVED.value
    ]
    assert entry.actor_user_id == actor.id
    assert entry.company_id == company.id
    assert entry.entity_type == "attribution_case"
    assert entry.entity_id == case.id
    assert entry.details == {
        "case_id": str(case.id),
        "communication_id": str(communication.id),
        "actor_user_id": str(actor.id),
    }


async def test_a_case_that_has_left_the_awaits_is_refused_by_both_actions(
    session: AsyncSession,
) -> None:
    """Only an open case can be dismissed or processed: the two actions answer
    the same typed refusal for every terminal state, and a refusal moves
    nothing and writes no second entry."""
    tenant, company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    dismissed_case = await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-settled-a"
    )
    await attribution.dismiss_case(
        session, tenant_id=tenant.id, case_id=dismissed_case.id, actor_user_id=actor.id, now=AT
    )
    resolved_case = await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-settled-b"
    )
    assert (
        await set_status(
            session,
            tenant_id=tenant.id,
            case_id=resolved_case.id,
            status=AttributionCaseStatus.RESOLVED,
        )
        is not None
    )
    before = [entry.action for entry in await audit_entries(session, tenant.id)]

    with pytest.raises(attribution.AttributionCaseNotOpenError):
        await attribution.dismiss_case(
            session,
            tenant_id=tenant.id,
            case_id=dismissed_case.id,
            actor_user_id=actor.id,
            now=AT,
        )
    with pytest.raises(attribution.AttributionCaseNotOpenError):
        await attribution.dismiss_case(
            session,
            tenant_id=tenant.id,
            case_id=resolved_case.id,
            actor_user_id=actor.id,
            now=AT,
        )
    with pytest.raises(attribution.AttributionCaseNotOpenError):
        await attribution.process_open_case(
            session,
            FakeStore(),
            FakeScanner(),
            tenant_id=tenant.id,
            case_id=dismissed_case.id,
            actor_user_id=actor.id,
            now=AT,
        )

    assert dismissed_case.dismissed_at == AT
    assert dismissed_case.status == AttributionCaseStatus.DISMISSED.value
    assert resolved_case.status == AttributionCaseStatus.RESOLVED.value
    assert resolved_case.dismissed_at is None
    assert [entry.action for entry in await audit_entries(session, tenant.id)] == before


async def test_a_foreign_or_unknown_case_id_is_not_found_for_both_actions(
    session: AsyncSession,
) -> None:
    """Another tenant's case id is simply not found — the quarantine
    surface's one-answer rule — and the refused case is left untouched."""
    tenant, _company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    other_tenant, other_company = await seed_company(session)
    foreign = await open_case(
        session, other_tenant.id, other_company.id, mailbox_message_id="provider-foreign"
    )

    with pytest.raises(attribution.AttributionCaseNotFoundError):
        await attribution.dismiss_case(
            session, tenant_id=tenant.id, case_id=foreign.id, actor_user_id=actor.id, now=AT
        )
    with pytest.raises(attribution.AttributionCaseNotFoundError):
        await attribution.process_open_case(
            session,
            FakeStore(),
            FakeScanner(),
            tenant_id=tenant.id,
            case_id=foreign.id,
            actor_user_id=actor.id,
            now=AT,
        )
    with pytest.raises(attribution.AttributionCaseNotFoundError):
        await attribution.dismiss_case(
            session, tenant_id=tenant.id, case_id=uuid4(), actor_user_id=actor.id, now=AT
        )

    assert foreign.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert foreign.dismissed_at is None


async def test_process_open_case_without_a_mailbox_propagates_the_connection_refusal(
    session: AsyncSession,
) -> None:
    """A case whose original was never recorded needs the mailbox, and a
    deployment with none answers ``MailNotConnected`` — the refusal the API
    route maps to its 409 ``not_connected``. The case stays awaiting and the
    chain gains nothing: the refusal is not a processing."""
    from app.application.mail import MailNotConnected

    tenant, company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    case = await open_case(
        session, tenant.id, company.id, mailbox_message_id="provider-no-mailbox"
    )

    with pytest.raises(MailNotConnected):
        await attribution.process_open_case(
            session,
            FakeStore(),
            FakeScanner(),
            tenant_id=tenant.id,
            case_id=case.id,
            actor_user_id=actor.id,
            now=AT,
        )

    assert case.status == AttributionCaseStatus.AWAITING_CONFIRMATION.value
    assert case.resolved_communication_id is None
    assert await audit_entries(session, tenant.id) == []


async def test_process_open_case_ingests_the_original_over_the_mailbox(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forced processing's fresh-ingest road, end to end over the IMAP leg
    (no socket, no provider thread): the original is re-read by its provider
    message id, stored through the records registry, recorded as a
    ``Communication`` for the case's company, and the case resolves with its
    queued ask cancelled and the entry naming the exchange and the actor —
    who rides the ingest's own document and audit rows too."""
    tenant, company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    await name_company(session, tenant.id, company.id)
    await cover(session, tenant.id, company.id)
    licensed(monkeypatch)
    fake = FakeImap(username=MAILBOX, password="imap-app-password-t7")
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    original_id = "<original-just-in-time@client.example>"
    uid = fake.add_message(
        subject="Re: Annual Return Confirmation",
        sender=f"CHAN TAI MAN <{SENDER}>",
        body="The signed transfer is enclosed.",
        message_id=original_id,
        received_at=AT,
    )
    case = await open_case(
        session,
        tenant.id,
        company.id,
        mailbox_message_id=original_id,
        thread_id=None,
        rfc_message_id=None,
    )
    letter = await seed_document(session, tenant.id, company.id)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        case_id=case.id,
        letter_document_id=letter.id,
    )
    store = FakeStore()

    result = await attribution.process_open_case(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        case_id=case.id,
        actor_user_id=actor.id,
        now=AT,
    )

    assert result.report.resolved is True
    assert result.report.outcome is IngestOutcome.INGESTED
    assert result.report.reason is None
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.resolved_communication_id == result.report.communication_id

    # The original is a stored record now: its raw bytes through the records
    # registry, its exchange for the case's own company.
    communication = await session.get(Communication, result.report.communication_id)
    assert communication is not None
    assert communication.company_id == company.id
    assert communication.mailbox_message_id == original_id
    assert communication.sender_address == SENDER
    raw = await session.get(Document, communication.raw_document_id)
    assert raw is not None
    assert raw.kind == "client_reply"
    assert raw.scan_status == "clean"
    assert store.objects[raw.storage_key] == fake.raw_of(uid)

    # The settle: the ask never delivers, and one entry names the act — the
    # actor rides it and the ingest's own entry.
    cancelled = await session.get(MailOutbox, enqueued.row.id)
    assert cancelled is not None
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_RESOLVE
    entries = await audit_entries(session, tenant.id)
    (resolved_entry,) = [
        item
        for item in entries
        if item.action == AuditAction.ATTRIBUTION_CASE_RESOLVED.value
    ]
    assert resolved_entry.actor_user_id == actor.id
    assert resolved_entry.company_id == company.id
    assert resolved_entry.details == {
        "case_id": str(case.id),
        "communication_id": str(result.report.communication_id),
        "actor_user_id": str(actor.id),
    }
    ingested_entry = next(
        item for item in entries if item.action == AuditAction.MAIL_MESSAGE_INGESTED.value
    )
    assert ingested_entry.actor_user_id == actor.id
    assert ingested_entry.details["mailbox_message_id"] == original_id


# --- 7. The migration's own round trip ----------------------------------------


async def test_migration_upgrade_downgrade_round_trip(session: AsyncSession) -> None:
    """The downgrade removes the table and the outbox column, restores the
    NOT NULL request link and narrows the kind vocabularies; the upgrade
    restores the full shape — and the request-less ask and the attribution
    exchange the wide schema admits are refused again the moment the schema
    narrows, so the shape itself round-trips, not its name. The expected
    failures run on savepoints: a refused statement would abort the outer
    transaction the round trip still needs."""

    def _shape(
        sync_connection: object,
    ) -> tuple[set[str], set[str], set[str], int | None]:
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        outbox_columns = {column["name"] for column in inspector.get_columns("mail_outbox")}
        case_checks = (
            {check["name"] for check in inspector.get_check_constraints("attribution_cases")}
            if "attribution_cases" in tables
            else set()
        )
        communication_kind_length = next(
            column["type"].length
            for column in inspector.get_columns("communications")
            if column["name"] == "kind"
        )
        return tables, outbox_columns, case_checks, communication_kind_length

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION, direction)()

        return _drive

    tenant, company = await seed_company(session)
    await open_case(session, tenant.id, company.id)
    request = await seed_request(session, tenant.id, company.id)
    document = await seed_document(session, tenant.id, company.id)

    connection = await session.connection()
    await connection.run_sync(_run("downgrade"))
    tables, outbox_columns, _, kind_length = await connection.run_sync(_shape)
    assert "attribution_cases" not in tables
    assert "attribution_case_id" not in outbox_columns
    assert kind_length == 20
    # The narrow kind CHECK has no shape for the ask's kind…
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_ASK_KIND_ON_REQUEST,
                {"id": uuid4(), "tenant": tenant.id, "company": company.id, "request": request.id},
            )
    # …the attribution exchange is refused the same way…
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_ASK_KIND_COMMUNICATION,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": company.id,
                    "document": document.id,
                },
            )
    # …and the restored NOT NULL has no place for a request-less row.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_REQUESTLESS_REQUEST_KIND,
                {"id": uuid4(), "tenant": tenant.id, "company": company.id},
            )

    await connection.run_sync(_run("upgrade"))
    tables, outbox_columns, case_checks, kind_length = await connection.run_sync(_shape)
    assert "attribution_cases" in tables
    assert "attribution_case_id" in outbox_columns
    assert kind_length == 32
    assert case_checks == {
        "attribution_cases_status_check",
        "attribution_cases_ask_kind_check",
        "attribution_cases_closed_at_check",
    }
    # The widened schema is live again: the request-less ask inserts (with a
    # case the recreated table actually holds)…
    case = await open_case(session, tenant.id, company.id)
    await session.execute(
        INSERT_REQUESTLESS_ASK,
        {"id": uuid4(), "tenant": tenant.id, "company": company.id, "case": case.id},
    )
    # …and the attribution exchange inserts under the widened kind CHECK.
    await session.execute(
        INSERT_ASK_KIND_COMMUNICATION,
        {"id": uuid4(), "tenant": tenant.id, "company": company.id, "document": document.id},
    )
    await session.flush()


# --- 8. The clarification-loop migration's own round trip (②, ruling D2) -------


def _column_nullable(sync_connection: object, table: str, column: str) -> bool:
    for found in inspect(sync_connection).get_columns(table):
        if found["name"] == column:
            return bool(found["nullable"])
    raise AssertionError(f"column {table}.{column} not found")


def _thread_index_columns(sync_connection: object) -> list[str]:
    for index in inspect(sync_connection).get_indexes("attribution_cases"):
        if index["name"] == "ix_attribution_cases_thread":
            return list(index["column_names"])
    raise AssertionError("index ix_attribution_cases_thread not found")


async def test_the_clarification_loop_migration_round_trips(session: AsyncSession) -> None:
    """0034's upgrade drops the two ``company_id`` NOT NULLs, widens the
    status and ask-kind CHECKs (and the stamp-pairing CHECK the new status
    joins), and re-leads the thread index company-less; the company-less
    case and its ask row insert under the widened schema, the composite FK
    accepts a real pair and refuses another tenant's exactly as before, and
    the downgrade restores 0033's shape — refusing the NULL-company rows and
    the new vocabulary the narrower schema cannot hold. The expected
    failures run on savepoints for the same reason as 0033's round trip."""

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION_0034, direction)()

        return _drive

    tenant, company = await seed_company(session)
    _, other_company = await seed_company(session)

    connection = await session.connection()
    await connection.run_sync(_run("upgrade"))

    assert await connection.run_sync(
        lambda c: _column_nullable(c, "attribution_cases", "company_id")
    )
    assert await connection.run_sync(lambda c: _column_nullable(c, "mail_outbox", "company_id"))
    assert await connection.run_sync(_thread_index_columns) == [
        "tenant_id",
        "status",
        "thread_id",
    ]
    assert await _check_values(session, "attribution_cases_status_check") == {
        member.value for member in AttributionCaseStatus
    }
    assert await _check_values(session, "attribution_cases_ask_kind_check") == {
        member.value for member in AttributionAskKind
    }

    # The company-less pair inserts: an awaiting_clarification case with no
    # company, and its ask row with none either.
    case_id = uuid4()
    await session.execute(
        INSERT_COMPANY_LESS_CASE,
        {
            "id": case_id,
            "tenant": tenant.id,
            "message": f"provider-{uuid4()}",
            "sender": SENDER,
            "thread": THREAD,
            "rfc": RFC_ID,
        },
    )
    await session.execute(
        INSERT_COMPANY_LESS_ASK,
        {"id": uuid4(), "tenant": tenant.id, "case": case_id},
    )

    # The composite FK is unchanged for non-null companies: a real pair
    # accepts…
    await session.execute(
        INSERT_CASE_FOR_COMPANY,
        {
            "id": uuid4(),
            "tenant": tenant.id,
            "company": company.id,
            "message": f"provider-{uuid4()}",
            "sender": SENDER,
        },
    )
    # …another tenant's company under this tenant is refused…
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_CASE_FOR_COMPANY,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": other_company.id,
                    "message": f"provider-{uuid4()}",
                    "sender": SENDER,
                },
            )
    # …and an unknown company is refused the same way.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_CASE_FOR_COMPANY,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": uuid4(),
                    "message": f"provider-{uuid4()}",
                    "sender": SENDER,
                },
            )

    # The downgrade cannot run over the company-less rows this schema
    # legitimately holds — the restored NOT NULL has no shape for them — so
    # they leave the savepoint's way first, as 0033's docstring's own remedy
    # prescribes for vocabulary rows.
    await session.execute(text("DELETE FROM mail_outbox WHERE company_id IS NULL"))
    await session.execute(text("DELETE FROM attribution_cases WHERE company_id IS NULL"))
    await connection.run_sync(_run("downgrade"))

    assert not await connection.run_sync(
        lambda c: _column_nullable(c, "attribution_cases", "company_id")
    )
    assert not await connection.run_sync(
        lambda c: _column_nullable(c, "mail_outbox", "company_id")
    )
    assert await connection.run_sync(_thread_index_columns) == [
        "tenant_id",
        "company_id",
        "thread_id",
        "status",
    ]
    # The narrower schema refuses what only the wider one admits: a
    # NULL-company case, the new status, the new ask kind — and a
    # company-less ask row.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_COMPANY_LESS_CASE,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "message": f"provider-{uuid4()}",
                    "sender": SENDER,
                    "thread": THREAD,
                    "rfc": RFC_ID,
                },
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_CLARIFICATION_STATUS_NARROW,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": company.id,
                    "message": f"provider-{uuid4()}",
                    "sender": SENDER,
                },
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_CLARIFICATION_KIND_NARROW,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": company.id,
                    "message": f"provider-{uuid4()}",
                    "sender": SENDER,
                },
            )
    held_case = await open_case(session, tenant.id, company.id)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_COMPANY_LESS_ASK,
                {"id": uuid4(), "tenant": tenant.id, "case": held_case.id},
            )

    # The upgrade restores the full shape, and the company-less pair is
    # live again.
    await connection.run_sync(_run("upgrade"))
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "attribution_cases", "company_id")
    )
    assert await connection.run_sync(lambda c: _column_nullable(c, "mail_outbox", "company_id"))
    assert await connection.run_sync(_thread_index_columns) == [
        "tenant_id",
        "status",
        "thread_id",
    ]
    await session.execute(
        INSERT_COMPANY_LESS_CASE,
        {
            "id": case_id,
            "tenant": tenant.id,
            "message": f"provider-{uuid4()}",
            "sender": SENDER,
            "thread": THREAD,
            "rfc": RFC_ID,
        },
    )
    await session.execute(
        INSERT_COMPANY_LESS_ASK,
        {"id": uuid4(), "tenant": tenant.id, "case": case_id},
    )
    await session.flush()


# --- 10. The clarification loop's application units (②, T3) -------------------


async def open_company_less_case(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    mailbox_message_id: str | None = None,
    thread_id: str | None = THREAD,
    rfc_message_id: str | None = RFC_ID,
    status: AttributionCaseStatus = AttributionCaseStatus.AWAITING_CLARIFICATION,
    sender: str = SENDER,
) -> AttributionCase:
    """The application-layer open for the two company-less classes (ruling D4)."""
    action = {
        AttributionCaseStatus.AWAITING_CLARIFICATION: UnthreadedAction.ASK_CLARIFICATION,
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL: UnthreadedAction.ASK_FIRST_CONTACT,
    }[status]
    case, _created = await attribution.open_case(
        session,
        tenant_id=tenant_id,
        company_id=None,
        mailbox_message_id=mailbox_message_id or f"provider-{uuid4()}",
        sender_address=sender,
        action=action,
        thread_id=thread_id,
        original_rfc_message_id=rfc_message_id,
    )
    return case


async def seed_ask_outbox_row(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    case_id: UUID,
    status: str = OutboxStatus.SENT.value,
) -> MailOutbox:
    """One ask row for the count reads — company-less, naming no letter."""
    row = MailOutbox(
        tenant_id=tenant_id,
        company_id=None,
        confirmation_request_id=None,
        attribution_case_id=case_id,
        destination="client",
        status=status,
        kind=OutboxKind.ATTRIBUTION_ASK.value,
        sent_at=AT if status == OutboxStatus.SENT.value else None,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_two_company_tenant(
    session: AsyncSession,
) -> tuple[Tenant, Company, Company]:
    """One tenant whose sender is registered against two companies (A-class)."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    companies: list[Company] = []
    for cr_number in ("1234567", "7654321"):
        company = Company(
            tenant_id=tenant.id,
            cr_number=cr_number,
            br_number=f"BR{cr_number}",
            date_of_incorporation="2010-03-01",
            anniversary_date="2026-03-01",
            type="private",
        )
        session.add(company)
        await session.flush()
        companies.append(company)
    return tenant, companies[0], companies[1]


async def seed_shared_channel(
    session: AsyncSession,
    tenant_id: UUID,
    companies: Sequence[Company],
    *,
    identifier: str,
) -> None:
    """One channel registered against several companies (the A-class sender):
    a single person and channel row — the identifier is unique per tenant —
    with one company association per company."""
    person = Person(tenant_id=tenant_id, name_en="CHAN TAI MAN", is_external_contact=True)
    session.add(person)
    await session.flush()
    session.add(
        ContactChannel(
            tenant_id=tenant_id,
            person_id=person.id,
            channel_type="email",
            identifier=identifier,
            status="active",
        )
    )
    for company in companies:
        session.add(
            CompanyExternalContact(tenant_id=tenant_id, company_id=company.id, person_id=person.id)
        )
    await session.flush()


def company_references(
    session: AsyncSession, *companies: Company
) -> list[CompanyReference]:
    """The answer grammar's input, as the caller resolves it (contacts plus
    master data): one reference per company, the name in force beside its
    numbers."""
    references = []
    for company in companies:
        record = CompanyNameRecord(
            tenant_id=company.tenant_id,
            company_id=company.id,
            effective_date=date(2015, 1, 1),
            source_type="manual",
            name_en=f"Company {company.cr_number} Limited",
            name_zh=None,
            business_name=None,
        )
        session.add(record)
        references.append(
            CompanyReference(
                company_id=company.id,
                name=f"Company {company.cr_number} Limited",
                cr_number=company.cr_number,
                br_number=company.br_number,
            )
        )
    return references


async def test_open_case_opens_the_two_company_less_classes(
    session: AsyncSession,
) -> None:
    """The unthreaded asks open company-less rows: the first-contact ask reuses
    ①'s status and kind, the which-company ask carries the new pair, and the
    audit entry writes ``company_id = NULL`` (ruling D10)."""
    tenant, _company = await seed_company(session)

    first_contact = await open_company_less_case(
        session, tenant.id, status=AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL
    )
    clarification = await open_company_less_case(session, tenant.id)

    assert first_contact.company_id is None
    assert first_contact.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    assert first_contact.ask_kind == AttributionAskKind.REGISTERED_CHANNEL.value
    assert clarification.company_id is None
    assert clarification.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value
    assert clarification.ask_kind == AttributionAskKind.CLARIFICATION.value
    entries = await audit_entries(session, tenant.id)
    opened = [item for item in entries if item.action == AuditAction.ATTRIBUTION_CASE_OPENED.value]
    assert len(opened) == 2
    assert all(item.company_id is None for item in opened)


async def test_open_case_refuses_the_action_company_mismatches(
    session: AsyncSession,
) -> None:
    """The two families are exclusive in the company they carry: the unthreaded
    asks open company-less (a company is refused), and ①'s asks need the
    chain's company (none is refused)."""
    tenant, company = await seed_company(session)

    with pytest.raises(ValueError):
        await attribution.open_case(
            session,
            tenant_id=tenant.id,
            company_id=company.id,
            mailbox_message_id=f"provider-{uuid4()}",
            sender_address=SENDER,
            action=UnthreadedAction.ASK_CLARIFICATION,
        )
    with pytest.raises(ValueError):
        await attribution.open_case(
            session,
            tenant_id=tenant.id,
            company_id=None,
            mailbox_message_id=f"provider-{uuid4()}",
            sender_address=SENDER,
            action=AttributionAction.ASK_CONFIRMATION,
        )
    with pytest.raises(ValueError):
        await attribution.open_case(
            session,
            tenant_id=tenant.id,
            company_id=None,
            mailbox_message_id=f"provider-{uuid4()}",
            sender_address=SENDER,
            action=UnthreadedAction.QUARANTINE,
        )
    assert await list_cases(session, tenant_id=tenant.id) == []


async def test_a_reopened_message_returns_the_same_company_less_case(
    session: AsyncSession,
) -> None:
    """One row per message, ever, extends verbatim: the overlapping cycle's
    re-examination returns the winner's row with no second audit entry."""
    tenant, _company = await seed_company(session)
    first, created = await attribution.open_case(
        session,
        tenant_id=tenant.id,
        company_id=None,
        mailbox_message_id="provider-twice",
        sender_address=SENDER,
        action=UnthreadedAction.ASK_CLARIFICATION,
    )
    again, reopened = await attribution.open_case(
        session,
        tenant_id=tenant.id,
        company_id=None,
        mailbox_message_id="provider-twice",
        sender_address=SENDER,
        action=UnthreadedAction.ASK_CLARIFICATION,
    )
    assert (created, reopened) == (True, False)
    assert again.id == first.id
    entries = await audit_entries(session, tenant.id)
    assert len(entries) == 1


async def test_open_cases_for_anchor_finds_company_less_cases_by_either_anchor(
    session: AsyncSession,
) -> None:
    """The tenant-scoped read sees what the company-scoped read cannot: cases
    with no company, matched by the provider thread id or any RFC token in
    the caller's set — and only the open ones, only this tenant's."""
    tenant, company = await seed_company(session)
    by_thread = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-a", rfc_message_id=None
    )
    by_rfc = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-b", thread_id="other-thread"
    )
    settled = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-c"
    )
    await set_status(
        session,
        tenant_id=tenant.id,
        case_id=settled.id,
        status=AttributionCaseStatus.DISMISSED,
        at=AT,
    )
    _, other_company = await seed_company(session)
    other_case, _created = await attribution.open_case(
        session,
        tenant_id=(await session.get(Company, other_company.id)).tenant_id,
        company_id=None,
        mailbox_message_id=f"provider-{uuid4()}",
        sender_address=SENDER,
        action=UnthreadedAction.ASK_CLARIFICATION,
        thread_id=THREAD,
        original_rfc_message_id=RFC_ID,
    )

    by_thread_read = await open_cases_for_anchor(
        session, tenant_id=tenant.id, thread_id=THREAD, rfc_message_ids=[]
    )
    assert [case.id for case in by_thread_read] == [by_thread.id]
    by_rfc_read = await open_cases_for_anchor(
        session, tenant_id=tenant.id, thread_id=None, rfc_message_ids=["<unused>", RFC_ID]
    )
    assert [case.id for case in by_rfc_read] == [by_rfc.id]
    assert other_case.id not in [case.id for case in by_rfc_read]
    # ①'s company-scoped read stays company-scoped: the company-less rows are
    # invisible to it.
    company_scoped = await open_cases_for_thread(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        thread_id=THREAD,
        rfc_message_ids=[RFC_ID],
    )
    assert company_scoped == []


async def test_latest_ask_for_anchor_excludes_the_case_itself(
    session: AsyncSession,
) -> None:
    """The suppression read excludes the caller's own case: its re-asks are the
    two-ask bound's business, and only another open case already owning the
    anchor's ask suppresses."""
    tenant, _company = await seed_company(session)
    asked = await open_company_less_case(session, tenant.id)
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)
    own = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-own"
    )

    assert (
        await latest_ask_for_anchor(
            session,
            tenant_id=tenant.id,
            thread_id=THREAD,
            rfc_message_ids=[RFC_ID],
            exclude_case_id=own.id,
        )
    ).id == asked.id
    assert (
        await latest_ask_for_anchor(
            session,
            tenant_id=tenant.id,
            thread_id=THREAD,
            rfc_message_ids=[RFC_ID],
            exclude_case_id=asked.id,
        )
        is None
    )


async def test_count_asks_for_case_counts_only_issued_asks(
    session: AsyncSession,
) -> None:
    """The two-ask bound's read: sent and pending rows are asks that left (or
    are leaving); cancelled and non-ask rows are not."""
    tenant, _company = await seed_company(session)
    case = await open_company_less_case(session, tenant.id)

    assert await count_asks_for_case(session, tenant_id=tenant.id, case_id=case.id) == 0
    await seed_ask_outbox_row(session, tenant.id, case_id=case.id)
    assert await count_asks_for_case(session, tenant_id=tenant.id, case_id=case.id) == 1
    await seed_ask_outbox_row(
        session, tenant.id, case_id=case.id, status=OutboxStatus.PENDING.value
    )
    assert await count_asks_for_case(session, tenant_id=tenant.id, case_id=case.id) == 2
    await seed_ask_outbox_row(
        session, tenant.id, case_id=case.id, status=OutboxStatus.CANCELLED.value
    )
    assert await count_asks_for_case(session, tenant_id=tenant.id, case_id=case.id) == 2


async def test_count_asks_since_splits_the_two_hourly_pools(
    session: AsyncSession,
) -> None:
    """①'s cap counts the company-bound asks and ②'s the company-less ones, so
    neither pool can starve the other (ruling D7); ``None`` counts both."""
    tenant, company = await seed_company(session)
    bound = await open_case(session, tenant.id, company.id)
    await set_ask_sent(session, tenant_id=tenant.id, case_id=bound.id, at=AT)
    less = await open_company_less_case(session, tenant.id)
    await set_ask_sent(session, tenant_id=tenant.id, case_id=less.id, at=AT)
    since = AT - timedelta(hours=1)

    assert await count_asks_since(session, tenant_id=tenant.id, since=since) == 2
    assert (
        await count_asks_since(session, tenant_id=tenant.id, since=since, company_less=False)
        == 1
    )
    assert (
        await count_asks_since(session, tenant_id=tenant.id, since=since, company_less=True)
        == 1
    )


async def test_enqueue_company_less_ask_honors_the_anti_abuse_headers(
    session: AsyncSession,
) -> None:
    """①'s two envelope guards apply to the convenience asks unchanged (C6)."""
    tenant, _company = await seed_company(session)
    case = await open_company_less_case(session, tenant.id)

    auto = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted="auto-replied",
        return_path=None,
    )
    bounced = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted=None,
        return_path="<>",
    )
    assert auto.reason == attribution.ASK_NOT_ALLOWED
    assert bounced.reason == attribution.ASK_NOT_ALLOWED
    assert case.ask_sent_at is None


async def test_enqueue_company_less_ask_honors_the_convenience_cap(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clarification asks' own ceiling: the company-less pool's count, not
    ①'s — and ``0`` means never ask (ruling D7)."""
    tenant, _company = await seed_company(session)
    case = await open_company_less_case(session, tenant.id)
    asked = await open_company_less_case(
        session,
        tenant.id,
        mailbox_message_id="provider-asked",
        thread_id="thread-other",
        rfc_message_id="<other@client.example>",
    )
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)

    monkeypatch.setattr(settings, "clarification_ask_hourly_cap", 1)
    report = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted=None,
        return_path=None,
        now=AT,
    )
    assert report.enqueued is False
    assert report.reason == attribution.ASK_CAP_REACHED

    monkeypatch.setattr(settings, "clarification_ask_hourly_cap", 0)
    never = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted=None,
        return_path=None,
        now=AT,
    )
    assert never.reason == attribution.ASK_CAP_REACHED


async def test_enqueue_company_less_ask_enforces_the_two_ask_bound(
    session: AsyncSession,
) -> None:
    """One ask, one re-ask, then never again (ruling D7): the bound fires
    before the composition, whatever the registry would say."""
    tenant, _company = await seed_company(session)
    case = await open_company_less_case(session, tenant.id)
    await seed_ask_outbox_row(session, tenant.id, case_id=case.id)
    await seed_ask_outbox_row(session, tenant.id, case_id=case.id)

    report = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted=None,
        return_path=None,
    )
    assert report.enqueued is False
    assert report.reason == attribution.ASK_LIMIT_REACHED


async def test_enqueue_company_less_ask_suppresses_a_second_case_on_the_anchor(
    session: AsyncSession,
) -> None:
    """One ask per (tenant, sender, anchor): another open case already owning
    the anchor's ask keeps a new case on the same anchor quiet (ruling D7)."""
    tenant, _company = await seed_company(session)
    asked = await open_company_less_case(session, tenant.id)
    await set_ask_sent(session, tenant_id=tenant.id, case_id=asked.id, at=AT)
    second = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-second"
    )

    report = await attribution.enqueue_ask(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=second,
        recipients=(SENDER,),
        rfc_message_ids=[],
        original_subject="Question",
        original_received_at=AT,
        auto_submitted=None,
        return_path=None,
    )
    assert report.enqueued is False
    assert report.reason == attribution.ASK_SUPPRESSED


async def test_enqueue_company_less_ask_sends_letters_a_and_b(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0035 switch, at the enqueue: the healthy company-less ask composes
    Letters A and B in full, registers the letter tenant-scoped under the
    ``_correspondence`` fallback (migration 0035), queues the request-less
    company-less outbox row, stamps ``ask_sent_at``, and writes the one
    ``attribution_ask_enqueued`` entry with the company column NULL (ruling
    D10). The letter's bytes are the founder-gated draft, unchanged."""
    tenant, _company = await seed_company(session)
    licensed(monkeypatch)
    for kind_status, expected_kind in (
        (AttributionCaseStatus.AWAITING_CLARIFICATION, AttributionAskKind.CLARIFICATION),
        (
            AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
            AttributionAskKind.REGISTERED_CHANNEL,
        ),
    ):
        case = await open_company_less_case(
            session,
            tenant.id,
            mailbox_message_id=f"provider-{expected_kind.value}",
            thread_id=f"thread-{expected_kind.value}",
            rfc_message_id=f"<{expected_kind.value}@client.example>",
            status=kind_status,
        )
        assert case.status == kind_status.value

        report = await attribution.enqueue_ask(
            session,
            store,
            FakeScanner(),
            tenant_id=tenant.id,
            case=case,
            recipients=(SENDER,),
            rfc_message_ids=[],
            original_subject="Question",
            original_received_at=AT,
            auto_submitted=None,
            return_path=None,
            now=AT,
        )
        assert report.enqueued is True
        assert report.reason is None
        assert report.met_pending_row is False
        assert case.ask_sent_at == AT

        (row,) = await outbox_rows(session, tenant.id)
        assert row.company_id is None
        assert row.attribution_case_id == case.id
        assert row.kind == OutboxKind.ATTRIBUTION_ASK.value
        letter = await documents.get_document(
            session, tenant_id=tenant.id, company_id=None, document_id=row.letter_document_id
        )
        assert letter.storage_key.startswith("_correspondence/")
        assert store.objects[letter.storage_key].startswith(b"From: ")

        entries = await audit_entries(session, tenant.id)
        enqueued = [
            item
            for item in entries
            if item.action == AuditAction.ATTRIBUTION_ASK_ENQUEUED.value
            and item.entity_id == case.id
        ]
        assert len(enqueued) == 1
        assert enqueued[0].company_id is None
        assert enqueued[0].details["ask_kind"] == expected_kind.value
        # The next kind's enqueue starts clean.
        await session.delete(row)
        await session.flush()


async def test_set_case_company_stamps_the_answered_company(
    session: AsyncSession,
) -> None:
    tenant, company = await seed_company(session)
    case = await open_company_less_case(session, tenant.id)

    stamped = await set_case_company(
        session, tenant_id=tenant.id, case_id=case.id, company_id=company.id
    )
    assert stamped is not None
    assert stamped.company_id == company.id
    # The composite FK pins the pair: another tenant's company is refused.
    _, other_company = await seed_company(session)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await set_case_company(
                session, tenant_id=tenant.id, case_id=case.id, company_id=other_company.id
            )


async def test_resolve_clarification_case_resolves_on_exactly_one_match(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer grammar's happy road: the first line names exactly one of the
    sender's companies, the case takes it, the parked original is ingested for
    it, and one entry names the act (ruling D6)."""
    tenant, first, second = await seed_two_company_tenant(session)
    await seed_shared_channel(session, tenant.id, [first, second], identifier=SENDER)
    await name_company(session, tenant.id, first.id, name_en="Company 1234567 Limited")
    await cover(session, tenant.id, first.id)
    licensed(monkeypatch)
    fake = FakeImap(username=MAILBOX, password="imap-app-password-t7")
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    original_id = "<original-ambiguous@client.example>"
    fake.add_message(
        subject="Fwd: appointment of a director",
        sender=f"CHAN TAI MAN <{SENDER}>",
        body="Please handle the enclosed.",
        message_id=original_id,
        received_at=AT,
    )
    case = await open_company_less_case(
        session, tenant.id, mailbox_message_id=original_id, sender=SENDER
    )
    references = company_references(session, first, second)

    result = await attribution.resolve_clarification_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        first_line="company 1234567 limited",
        companies=references,
        now=AT,
    )

    assert result.resolved is True
    assert result.company_id == first.id
    assert result.communication_id is not None
    await session.refresh(case)
    assert case.status == AttributionCaseStatus.RESOLVED.value
    assert case.company_id == first.id
    assert case.resolved_communication_id == result.communication_id
    communication = await session.get(Communication, result.communication_id)
    assert communication.company_id == first.id
    entries = await audit_entries(session, tenant.id)
    (resolved_entry,) = [
        item for item in entries if item.action == AuditAction.ATTRIBUTION_CASE_RESOLVED.value
    ]
    assert resolved_entry.company_id == first.id
    assert resolved_entry.actor_user_id is None
    assert resolved_entry.details["case_id"] == str(case.id)


async def test_resolve_clarification_case_matches_cr_digits(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digit-sequence half of the grammar: the CR number on the first line
    resolves to the company that holds it."""
    tenant, first, second = await seed_two_company_tenant(session)
    await seed_shared_channel(session, tenant.id, [first, second], identifier=SENDER)
    await cover(session, tenant.id, first.id)
    await cover(session, tenant.id, second.id)
    licensed(monkeypatch)
    fake = FakeImap(username=MAILBOX, password="imap-app-password-t7")
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    original_id = "<original-cr-match@client.example>"
    fake.add_message(
        subject="Change of address",
        sender=f"CHAN TAI MAN <{SENDER}>",
        body="Please update the register.",
        message_id=original_id,
        received_at=AT,
    )
    case = await open_company_less_case(
        session, tenant.id, mailbox_message_id=original_id
    )
    references = company_references(session, first, second)

    result = await attribution.resolve_clarification_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        first_line="CR 7654321",
        companies=references,
        now=AT,
    )

    assert result.resolved is True
    assert result.company_id == second.id
    assert case.company_id == second.id


async def test_resolve_clarification_case_stays_awaiting_on_zero_and_hands_off_on_several(
    session: AsyncSession,
) -> None:
    """Zero matches is staff's case, never a silent pick: the case stays
    company-less and awaiting, and the report says ``answer_unresolved`` —
    unchanged by workstream ③. Several is the E2 handoff: the case closes
    with the multi-company handoff named in its audit (the company column
    stays NULL — the handoff is the record), the matched set rides the
    report, and nothing is read from the mailbox."""
    tenant, first, second = await seed_two_company_tenant(session)
    references = company_references(session, first, second)
    no_match = await open_company_less_case(session, tenant.id)
    several = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-several"
    )
    same_name = CompanyReference(
        company_id=second.id,
        name="Company 1234567 Limited",
        cr_number="7654321",
        br_number=None,
    )

    zero = await attribution.resolve_clarification_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=no_match,
        first_line="Some Other Limited",
        companies=references,
        now=AT,
    )
    assert zero.resolved is False
    assert zero.reason == attribution.ANSWER_UNRESOLVED
    assert zero.handoff is False
    await session.refresh(no_match)
    assert no_match.company_id is None
    assert no_match.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value

    several_result = await attribution.resolve_clarification_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=several,
        first_line="company 1234567 limited",
        companies=[references[0], same_name],
        now=AT,
    )
    assert several_result.resolved is False
    assert several_result.handoff is True
    assert sorted(several_result.matched_company_ids) == sorted([first.id, second.id])
    await session.refresh(several)
    assert several.company_id is None  # the handoff stamps no company
    assert several.status == AttributionCaseStatus.CLOSED.value
    assert several.closed_at is not None
    entries = await audit_entries(session, tenant.id)
    closed = next(
        entry
        for entry in entries
        if entry.action == AuditAction.ATTRIBUTION_CASE_CLOSED.value
    )
    assert closed.company_id is None
    assert closed.details["handoff"] == "multi_company_split"
    assert closed.details["matched_company_ids"] == sorted([str(first.id), str(second.id)])
    assert [item.action for item in entries] == [
        AuditAction.ATTRIBUTION_CASE_OPENED.value,
        AuditAction.ATTRIBUTION_CASE_OPENED.value,
        AuditAction.ATTRIBUTION_CASE_CLOSED.value,
    ]


async def test_close_first_contact_cases_closes_only_the_company_less_21_cases(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D8: the company-less awaiting_registered_channel cases on the
    read close — their company-bound cousins are ①'s and stay open, as do the
    other awaits — the queued ask is cancelled, and one entry with
    ``company_id = NULL`` names the ids."""
    tenant, company = await seed_company(session)
    licensed(monkeypatch)
    first_contact = await open_company_less_case(
        session, tenant.id, status=AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL
    )
    clarification = await open_company_less_case(
        session, tenant.id, mailbox_message_id="provider-clarify"
    )
    bound, _created = await attribution.open_case(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        mailbox_message_id=f"provider-{uuid4()}",
        sender_address="outsider@client.example",
        action=AttributionAction.ASK_REGISTERED_CHANNEL,
        thread_id=THREAD,
        original_rfc_message_id=RFC_ID,
    )
    letter = await seed_document(session, tenant.id, company.id)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=None,
        case_id=first_contact.id,
        letter_document_id=letter.id,
    )

    closed = await attribution.close_first_contact_cases(
        session, tenant_id=tenant.id, cases=[first_contact, clarification, bound], now=AT
    )

    assert tuple(case.id for case in closed) == (first_contact.id,)
    await session.refresh(first_contact)
    assert first_contact.status == AttributionCaseStatus.CLOSED.value
    assert first_contact.closed_at is not None
    await session.refresh(clarification)
    await session.refresh(bound)
    assert clarification.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value
    assert bound.status == AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value
    cancelled = await session.get(MailOutbox, enqueued.row.id)
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_CLOSE
    entries = await audit_entries(session, tenant.id)
    (closed_entry,) = [
        item for item in entries if item.action == AuditAction.ATTRIBUTION_CASE_CLOSED.value
    ]
    assert closed_entry.company_id is None
    assert closed_entry.details == {"case_ids": [str(first_contact.id)]}


async def test_process_open_case_refuses_the_company_less_case_before_the_mailbox(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D9: staff cannot force-process what has no company. The refusal
    is raised before the mailbox is resolved — no credential, no provider
    call — and the case stays exactly as it was."""
    tenant, _company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    case = await open_company_less_case(session, tenant.id)

    def no_mailbox(**_kwargs: object) -> object:
        raise AssertionError("the mailbox must not be touched for a company-less case")

    monkeypatch.setattr("app.application.mail.reader", no_mailbox)
    with pytest.raises(attribution.AttributionCaseHasNoCompanyError):
        await attribution.process_open_case(
            session,
            FakeStore(),
            FakeScanner(),
            tenant_id=tenant.id,
            case_id=case.id,
            actor_user_id=actor.id,
            now=AT,
        )

    await session.refresh(case)
    assert case.status == AttributionCaseStatus.AWAITING_CLARIFICATION.value
    assert case.company_id is None
    # Only the opening was ever recorded: the refusal writes nothing.
    entries = await audit_entries(session, tenant.id)
    assert [item.action for item in entries] == [AuditAction.ATTRIBUTION_CASE_OPENED.value]


async def test_process_open_case_keeps_todays_shape_for_settled_company_less_rows(
    session: AsyncSession,
) -> None:
    """D9's other half: a settled case keeps today's shape — the not-open
    refusal precedes the company check, so a resolved company-less row is
    ``already_closed``, not the new code."""
    tenant, _company = await seed_company(session)
    actor = await seed_user(session, tenant.id)
    case = await open_company_less_case(session, tenant.id)
    await set_status(
        session,
        tenant_id=tenant.id,
        case_id=case.id,
        status=AttributionCaseStatus.RESOLVED,
        resolved_communication_id=None,
    )

    with pytest.raises(attribution.AttributionCaseNotOpenError):
        await attribution.process_open_case(
            session,
            FakeStore(),
            FakeScanner(),
            tenant_id=tenant.id,
            case_id=case.id,
            actor_user_id=actor.id,
            now=AT,
        )


def test_the_company_less_compose_addresses_the_sender_with_the_tcsp_named_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5/D8's composition, pinned against the registry wall: the letter that
    would go out is addressed to the sender alone (even though a registered
    channel may sit in the message's To/Cc — the caller passes the sender), is
    led by the TCSP-named subject (C10), rides the case's anchor back, and its
    body is the founder-gated draft verbatim — the exact message the ``0035``
    switch will register and queue."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    case = AttributionCase(
        id=uuid4(),
        tenant_id=uuid4(),
        company_id=None,
        mailbox_message_id="provider-compose",
        sender_address=SENDER,
        status=AttributionCaseStatus.AWAITING_CLARIFICATION.value,
        ask_kind=AttributionAskKind.CLARIFICATION.value,
        thread_id=THREAD,
        original_rfc_message_id=RFC_ID,
    )

    clarification = attribution._compose_company_less_message(
        case=case,
        kind=AttributionAskKind.CLARIFICATION,
        tcsp_name="Example TCSP Limited",
        recipients=(SENDER,),
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=AT,
        at=AT,
    )
    assert clarification["From"] == MAILBOX
    assert clarification["To"] == SENDER
    assert clarification["Subject"] == companyless_ask_subject(
        tcsp_name="Example TCSP Limited", kind=AttributionAskKind.CLARIFICATION
    )
    assert clarification["In-Reply-To"] == RFC_ID
    assert clarification["References"] == RFC_ID
    assert clarification.get_content() == crlf(
        clarification_ask_letter(
            sender_address=SENDER,
            original_subject=ORIGINAL_SUBJECT,
            original_received_at=AT,
        )
    )

    first_contact = attribution._compose_company_less_message(
        case=case,
        kind=AttributionAskKind.REGISTERED_CHANNEL,
        tcsp_name="Example TCSP Limited",
        recipients=(SENDER,),
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=AT,
        at=AT,
    )
    assert first_contact["To"] == SENDER
    assert first_contact["Subject"] == companyless_ask_subject(
        tcsp_name="Example TCSP Limited", kind=AttributionAskKind.REGISTERED_CHANNEL
    )
    assert first_contact["In-Reply-To"] == RFC_ID
    assert first_contact.get_content() == crlf(
        first_contact_ask_letter(
            sender_address=SENDER,
            original_subject=ORIGINAL_SUBJECT,
            original_received_at=AT,
        )
    )


async def test_resolve_clarification_case_cancels_the_pending_ask(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolution settles the case the R6 way: a queued ask is cancelled
    (a settled case is never asked) with the bounded reason, alongside the
    resolved entry — the same I2b rule ①'s settles share."""
    tenant, first, second = await seed_two_company_tenant(session)
    await seed_shared_channel(session, tenant.id, [first, second], identifier=SENDER)
    await name_company(session, tenant.id, first.id, name_en="Company 1234567 Limited")
    await cover(session, tenant.id, first.id)
    licensed(monkeypatch)
    fake = FakeImap(username=MAILBOX, password="imap-app-password-t7")
    monkeypatch.setattr(settings, "imap_host", "imap.tcsp.example")
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    original_id = "<original-with-ask@client.example>"
    fake.add_message(
        subject="Fwd: appointment of a director",
        sender=f"CHAN TAI MAN <{SENDER}>",
        body="Please handle the enclosed.",
        message_id=original_id,
        received_at=AT,
    )
    case = await open_company_less_case(
        session, tenant.id, mailbox_message_id=original_id, sender=SENDER
    )
    letter = await seed_document(session, tenant.id, first.id)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant.id,
        company_id=None,
        case_id=case.id,
        letter_document_id=letter.id,
    )
    references = company_references(session, first, second)

    result = await attribution.resolve_clarification_case(
        session,
        FakeStore(),
        FakeScanner(),
        tenant_id=tenant.id,
        case=case,
        first_line="company 1234567 limited",
        companies=references,
        now=AT,
    )

    assert result.resolved is True
    cancelled = await session.get(MailOutbox, enqueued.row.id)
    assert cancelled.status == OutboxStatus.CANCELLED.value
    assert cancelled.last_error == attribution.ASK_CANCELLED_ON_RESOLVE
    entries = await audit_entries(session, tenant.id)
    assert any(
        entry.action == AuditAction.ATTRIBUTION_CASE_RESOLVED.value for entry in entries
    )


# --- 11. The registry widening's own round trip (0035, the wall's removal) -----

REVISION_0035 = _load_revision_module(
    "0035_registry_widening.py", "revision_0035_registry_widening"
)

# The company-less rows 0035 admits and the narrower schema refuses: the
# clarification loop's stored letter and its delivered exchange — NULL company
# on both, the document keyed by its own id for the exchange's raw leg.
INSERT_COMPANY_LESS_DOCUMENT = text(
    "INSERT INTO documents (id, tenant_id, company_id, kind, origin, retention_class,"
    " sha256, storage_key, byte_size, content_type, scan_status)"
    " VALUES (:id, :tenant, NULL, 'generated_draft', 'generated', 'client_communication',"
    " :sha, :key, 1, 'message/rfc822', 'clean')"
)
INSERT_COMPANY_LESS_EXCHANGE = text(
    "INSERT INTO communications (id, tenant_id, company_id, direction, channel, kind,"
    " received_at, raw_document_id)"
    " VALUES (:id, :tenant, NULL, 'outbound', 'email', 'attribution_ask', now(), :document)"
)
INSERT_DOCUMENT_FOR_FOREIGN_COMPANY = text(
    "INSERT INTO documents (id, tenant_id, company_id, kind, origin, retention_class,"
    " sha256, storage_key, byte_size, content_type, scan_status)"
    " VALUES (:id, :tenant, :company, 'generated_draft', 'generated', 'client_communication',"
    " :sha, :key, 1, 'message/rfc822', 'clean')"
)


async def test_the_registry_widening_migration_round_trips(session: AsyncSession) -> None:
    """0035's upgrade drops the two NOT NULLs (``documents.company_id``,
    ``communications.company_id`` — the composite foreign keys stay, simply
    not checked on a NULL), and the company-less pair the clarification
    loop's asks store — the letter, then its delivered exchange — inserts
    under the widened schema; a non-null company keeps its F1 pinning. The
    downgrade restores 0034's shape and refuses what only the wider schema
    admits: a company-less document, a company-less exchange — and the
    upgrade makes it live again. The expected failures run on savepoints,
    the round trip's own remedy (delete the company-less rows) precedes the
    downgrade, as 0033's docstring prescribes for vocabulary rows."""

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION_0035, direction)()

        return _drive

    tenant, company = await seed_company(session)
    _, other_company = await seed_company(session)
    await seed_document(session, tenant.id, company.id)

    connection = await session.connection()
    await connection.run_sync(_run("upgrade"))

    assert await connection.run_sync(
        lambda c: _column_nullable(c, "documents", "company_id")
    )
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "communications", "company_id")
    )

    # The company-less pair inserts: the stored letter, then its exchange.
    letter_id = uuid4()
    await session.execute(
        INSERT_COMPANY_LESS_DOCUMENT,
        {
            "id": letter_id,
            "tenant": tenant.id,
            "sha": "b" * 64,
            "key": f"test/{uuid4()}.eml",
        },
    )
    await session.execute(
        INSERT_COMPANY_LESS_EXCHANGE,
        {"id": uuid4(), "tenant": tenant.id, "document": letter_id},
    )
    # The composite FK is unchanged for non-null companies: another tenant's
    # company under this tenant is refused exactly as before.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_DOCUMENT_FOR_FOREIGN_COMPANY,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "company": other_company.id,
                    "sha": "c" * 64,
                    "key": f"test/{uuid4()}.eml",
                },
            )

    # The narrowing cannot run over the company-less rows this schema
    # legitimately holds — they leave first, as 0033's own remedy prescribes.
    await session.execute(text("DELETE FROM communications WHERE company_id IS NULL"))
    await session.execute(text("DELETE FROM documents WHERE company_id IS NULL"))
    await connection.run_sync(_run("downgrade"))

    assert not await connection.run_sync(
        lambda c: _column_nullable(c, "documents", "company_id")
    )
    assert not await connection.run_sync(
        lambda c: _column_nullable(c, "communications", "company_id")
    )
    # The narrower schema refuses what only the wider one admits: a
    # company-less document, and a company-less exchange.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_COMPANY_LESS_DOCUMENT,
                {
                    "id": uuid4(),
                    "tenant": tenant.id,
                    "sha": "d" * 64,
                    "key": f"test/{uuid4()}.eml",
                },
            )
    held_document = await seed_document(session, tenant.id, company.id)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_COMPANY_LESS_EXCHANGE,
                {"id": uuid4(), "tenant": tenant.id, "document": held_document.id},
            )

    # The upgrade makes the company-less shape live again.
    await connection.run_sync(_run("upgrade"))
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "documents", "company_id")
    )
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "communications", "company_id")
    )
    letter_id = uuid4()
    await session.execute(
        INSERT_COMPANY_LESS_DOCUMENT,
        {
            "id": letter_id,
            "tenant": tenant.id,
            "sha": "e" * 64,
            "key": f"test/{uuid4()}.eml",
        },
    )
    await session.execute(
        INSERT_COMPANY_LESS_EXCHANGE,
        {"id": uuid4(), "tenant": tenant.id, "document": letter_id},
    )
    await session.flush()


# --- 12. The company-less registration (0035's seam — tenant-scoped letters) ---


async def test_a_company_less_letter_registers_under_the_tenant_fallback(
    session: AsyncSession,
) -> None:
    """The wall's removal at the seam: a company-less generated draft — the
    clarification loop's letter — registers through the same single write
    path, lands under the tenant-level ``_correspondence`` fallback segment,
    carries a NULL company on the row and its audit entry (ruling D10), and
    reads back through the company-less read. A same-day sibling gets the
    content-hash suffix, never an overwrite (WORM)."""
    tenant, _company = await seed_company(session)
    store = FakeStore()
    data = b"From: ai-filings@tcsp.example\r\nTo: client@example.test\r\n\r\nLetter body.\r\n"

    first = await documents.register_upload(
        session,
        store,
        tenant_id=tenant.id,
        company_id=None,
        actor_user_id=None,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=data,
        scanner=FakeScanner(),
    )
    second = await documents.register_upload(
        session,
        store,
        tenant_id=tenant.id,
        company_id=None,
        actor_user_id=None,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=data + b"again",
        scanner=FakeScanner(),
    )

    for letter in (first, second):
        assert letter.company_id is None
        assert letter.kind == DocumentKind.GENERATED_DRAFT.value
        assert letter.origin == DocumentOrigin.GENERATED.value
        # The kind's default retention class (the seam's single domain rule);
        # the letter caller passes client_communication explicitly, T3's switch.
        assert letter.retention_class == RetentionClass.GENERATED_DRAFT.value
        assert letter.scan_status == "clean"
        assert letter.storage_key.startswith("_correspondence/")
    assert second.storage_key != first.storage_key  # the collision suffix, not an overwrite
    assert store.objects[first.storage_key] == data
    assert store.objects[second.storage_key] == data + b"again"

    # The company-less read returns the row; an unknown id is not found.
    found = await documents.get_document(
        session, tenant_id=tenant.id, company_id=None, document_id=first.id
    )
    assert found.id == first.id
    with pytest.raises(documents.DocumentNotFoundError):
        await documents.get_document(
            session, tenant_id=tenant.id, company_id=None, document_id=uuid4()
        )

    # The registrations' audit entries write the company column NULL.
    registered = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == AuditAction.DOCUMENT_REGISTERED.value
    ]
    assert len(registered) == 2
    assert all(entry.company_id is None for entry in registered)
    assert {entry.entity_id for entry in registered} == {first.id, second.id}


async def test_the_company_less_read_never_returns_a_company_document(
    session: AsyncSession,
) -> None:
    """The read is scoped exactly: a company-bound document is not reachable
    through the company-less shape (and vice versa the company read keeps its
    company scoping)."""
    tenant, company = await seed_company(session)
    bound = await seed_document(session, tenant.id, company.id)

    with pytest.raises(documents.DocumentNotFoundError):
        await documents.get_document(
            session, tenant_id=tenant.id, company_id=None, document_id=bound.id
        )
    found = await documents.get_document(
        session, tenant_id=tenant.id, company_id=company.id, document_id=bound.id
    )
    assert found.id == bound.id


async def test_company_less_registration_refuses_non_generated_origins(
    session: AsyncSession,
) -> None:
    """Only the product's own generated writing registers company-less: a
    client file or an inbound reply always has its company by the time it
    reaches the registry, so a company-less non-generated origin is a caller
    bug, refused before anything is written."""
    tenant, _company = await seed_company(session)

    with pytest.raises(ValueError):
        await documents.register_upload(
            session,
            FakeStore(),
            tenant_id=tenant.id,
            company_id=None,
            actor_user_id=None,
            kind=DocumentKind.CLIENT_DOCUMENT,
            origin=DocumentOrigin.UPLOAD,
            content_type="application/pdf",
            data=b"%PDF-1.7 a client file",
            scanner=FakeScanner(),
        )
    assert list((await session.scalars(select(Document))).all()) == []
