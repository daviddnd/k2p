"""DB-backed scenarios for the multi-company split (workstream ③, ADR-0019 §3;
opt-in; needs a migrated PostgreSQL).

The launch-gated capability's record — this slice's T3 covers migration
``0036_multi_company_split``'s own round trip on the 0034/0035 pattern (the
HELD revision's upgrade shape, the parent/work-item rows the split stores, the
reply-parse work-item leg's exactly-one XOR, the widened assessment
vocabulary, the composite foreign keys' tenant pinning, and the downgrade's
honest refusals). The application behaviors — E1/E2 end-to-end, the pre-flight
parks, the grammar violations, the bounds, the RBAC surface — land in this
file's later tasks (T8's suite).

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head (with ``0036_multi_company_split`` applied — the revision is built HELD
for the founder's schema sign-off like its predecessors, and the throwaway
test database is migrated by the runner, never a review database). Every test
runs inside a transaction that is rolled back, so the review database stays
clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import Company, SplitParent, SplitWorkItem, Tenant, User
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.application import ai_gateway, reply_parsing, risk_acknowledgment, service_packages, split
from app.core.config import settings
from app.domain.attribution import CompanyReference
from app.domain.communications import message_text
from tests.fake_scanning import FakeScanner

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

AT = datetime(2026, 10, 9, 9, 30, tzinfo=UTC)


def _load_revision_module(filename: str, module_name: str) -> object:
    """One revision module, importable without the versions dir being a package."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION_0036 = _load_revision_module(
    "0036_multi_company_split.py", "revision_0036_multi_company_split"
)

# The rows the widened schema admits and the narrower one refuses.
INSERT_SPLIT_PARENT = text(
    "INSERT INTO split_parents (id, tenant_id, mailbox_message_id, sender_address,"
    " subject, received_at, raw_document_id, status)"
    " VALUES (:id, :tenant, :message, :sender, :subject, :received, :document, 'split_pending')"
)
INSERT_SPLIT_WORK_ITEM = text(
    "INSERT INTO split_work_items (id, tenant_id, parent_id, company_id, slice_document_id, status)"
    " VALUES (:id, :tenant, :parent, :company, :document, 'awaiting_parse')"
)
INSERT_WORK_ITEM_PARSE = text(
    "INSERT INTO reply_parses (id, tenant_id, company_id, communication_id,"
    " split_work_item_id, status, confirmations, event_ids)"
    " VALUES (:id, :tenant, :company, NULL, :item, 'confirmed_no_change', '[]', '[]')"
)
INSERT_COMMUNICATION_PARSE = text(
    "INSERT INTO reply_parses (id, tenant_id, company_id, communication_id,"
    " split_work_item_id, status, confirmations, event_ids)"
    " VALUES (:id, :tenant, :company, :communication, NULL, 'confirmed_no_change', '[]', '[]')"
)
INSERT_BOTH_LEGS_PARSE = text(
    "INSERT INTO reply_parses (id, tenant_id, company_id, communication_id,"
    " split_work_item_id, status, confirmations, event_ids)"
    " VALUES (:id, :tenant, :company, :communication, :item, 'confirmed_no_change', '[]', '[]')"
)
INSERT_NEITHER_LEG_PARSE = text(
    "INSERT INTO reply_parses (id, tenant_id, company_id, communication_id,"
    " split_work_item_id, status, confirmations, event_ids)"
    " VALUES (:id, :tenant, :company, NULL, NULL, 'confirmed_no_change', '[]', '[]')"
)
INSERT_ITEM_ASSESSMENT = text(
    "INSERT INTO extraction_assessments (id, tenant_id, subject_type, subject_id,"
    " component, model, field_scores, signal_log, overall_score)"
    " VALUES (:id, :tenant, 'split_work_item', :subject, 'reply_parser', 'reply-parser/v1',"
    " '{}', '{}', 0.9)"
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


async def audit_log(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    """The tenant's audit entries, in chain order — the split's own D10 pins."""
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


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


async def seed_tenant_company_document(
    session: AsyncSession,
) -> tuple[UUID, UUID, UUID]:
    """A tenant, one company, and one stored document — the F1 anchors the
    parent's raw leg and the item's company and slice legs pin to."""
    tenant_id = uuid4()
    await session.execute(text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                          {"id": tenant_id, "name": f"tenant-{uuid4()}"})
    company_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO companies (id, tenant_id, br_number, cr_number,"
            " date_of_incorporation, anniversary_date, type)"
            " VALUES (:id, :tenant, '12345678', '1234567', '2010-03-01', '2026-03-01', 'private')"
        ),
        {"id": company_id, "tenant": tenant_id},
    )
    document_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO documents (id, tenant_id, company_id, kind, origin, retention_class,"
            " sha256, storage_key, byte_size, content_type, scan_status)"
            " VALUES (:id, :tenant, :company, 'client_reply', 'email_attachment',"
            " 'client_communication', :sha, :key, 1, 'message/rfc822', 'clean')"
        ),
        {"id": document_id, "tenant": tenant_id, "company": company_id,
         "sha": "a" * 64, "key": f"test/{uuid4()}.eml"},
    )
    await session.flush()
    return tenant_id, company_id, document_id


def _column_nullable(sync_connection: object, table: str, column: str) -> bool:
    for found in inspect(sync_connection).get_columns(table):
        if found["name"] == column:
            return bool(found["nullable"])
    raise AssertionError(f"column {table}.{column} not found")


def _check_values(sync_connection: object, table: str, name: str) -> set[str]:
    for check in inspect(sync_connection).get_check_constraints(table):
        if check["name"] == name:
            return set(__import__("re").findall(r"'([^']*)'", str(check["sqltext"])))
    raise AssertionError(f"check {name} not found on {table}")


async def test_the_multi_company_split_migration_round_trips(session: AsyncSession) -> None:
    """The 0033-pattern round trip, downgrade first (the runner's database
    sits at head, 0036 applied): the downgrade restores 0035's shape and
    refuses what only the wider schema admits — a split_work_item
    assessment, and a parse with no communication under the restored NOT
    NULL; the upgrade creates the two split tables and the parse legs, the
    parent/work-item rows and the work-item parse insert under the widened
    schema, and the composite foreign keys pin non-null companies exactly as
    the family's do. The expected failures run on savepoints."""

    def _run(direction: str) -> object:
        def _drive(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            with Operations.context(context):
                getattr(REVISION_0036, direction)()

        return _drive

    tenant_id, company_id, document_id = await seed_tenant_company_document(session)
    _, other_company_id, _ = await seed_tenant_company_document(session)
    connection = await session.connection()

    # The downgrade first: 0036 comes off the (head) schema.
    await connection.run_sync(_run("downgrade"))
    tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert "split_parents" not in tables
    assert "split_work_items" not in tables
    assert not await connection.run_sync(
        lambda c: _column_nullable(c, "reply_parses", "communication_id")
    )
    assert "split_work_item_id" not in await connection.run_sync(
        lambda c: {col["name"] for col in inspect(c).get_columns("reply_parses")}
    )
    assert "split_work_item" not in await connection.run_sync(
        lambda c: _check_values(
            c, "extraction_assessments", "extraction_assessments_subject_type_check"
        )
    )
    assert "split_work_item" not in await connection.run_sync(
        lambda c: _check_values(c, "change_events", "change_events_source_type_check")
    )
    # The narrower schema refuses what only the wider one admits.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_ITEM_ASSESSMENT, {"id": uuid4(), "tenant": tenant_id, "subject": uuid4()}
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO reply_parses (id, tenant_id, company_id, communication_id,"
                    " status, confirmations, event_ids)"
                    " VALUES (:id, :tenant, :company, NULL, 'confirmed_no_change', '[]', '[]')"
                ),
                {"id": uuid4(), "tenant": tenant_id, "company": company_id},
            )

    # The upgrade: the split's shape is live.
    await connection.run_sync(_run("upgrade"))
    tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert {"split_parents", "split_work_items"} <= tables
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "reply_parses", "communication_id")
    )
    assert await connection.run_sync(
        lambda c: _column_nullable(c, "reply_parses", "split_work_item_id")
    )
    assert await connection.run_sync(
        lambda c: _check_values(c, "split_parents", "split_parents_status_check")
    ) == {"split_pending", "split_done", "staff_only", "failed"}
    assert await connection.run_sync(
        lambda c: _check_values(c, "split_work_items", "split_work_items_status_check")
    ) == {"awaiting_parse", "parsed", "failed"}
    assert "split_work_item" in await connection.run_sync(
        lambda c: _check_values(
            c, "extraction_assessments", "extraction_assessments_subject_type_check"
        )
    )
    assert "split_work_item" in await connection.run_sync(
        lambda c: _check_values(c, "change_events", "change_events_source_type_check")
    )

    # The parent and its work item insert; the parent's raw leg and the
    # item's company and slice legs are tenant-pinned.
    parent_id = uuid4()
    await session.execute(
        INSERT_SPLIT_PARENT,
        {
            "id": parent_id,
            "tenant": tenant_id,
            "message": "msg-original-1",
            "sender": "director@client.example",
            "subject": "Annual filings",
            "received": AT,
            "document": document_id,
        },
    )
    item_id = uuid4()
    await session.execute(
        INSERT_SPLIT_WORK_ITEM,
        {
            "id": item_id,
            "tenant": tenant_id,
            "parent": parent_id,
            "company": company_id,
            "document": document_id,
        },
    )
    # One item per (parent, company): a second row for the same pair refuses.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_SPLIT_WORK_ITEM,
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "parent": parent_id,
                    "company": company_id,
                    "document": document_id,
                },
            )
    # The composite FK pins non-null companies: another tenant's company
    # under this tenant refuses.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_SPLIT_WORK_ITEM,
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "parent": parent_id,
                    "company": other_company_id,
                    "document": document_id,
                },
            )

    # The parse legs' exactly-one rule: a work-item parse carries no
    # communication; both legs, or neither, refuse.
    await session.execute(
        INSERT_WORK_ITEM_PARSE,
        {"id": uuid4(), "tenant": tenant_id, "company": company_id, "item": item_id},
    )
    communication_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO communications (id, tenant_id, company_id, direction, channel,"
            " kind, received_at, raw_document_id)"
            " VALUES (:id, :tenant, :company, 'inbound', 'email', 'reply', now(), :document)"
        ),
        {"id": communication_id, "tenant": tenant_id, "company": company_id,
         "document": document_id},
    )
    await session.execute(
        INSERT_COMMUNICATION_PARSE,
        {
            "id": uuid4(),
            "tenant": tenant_id,
            "company": company_id,
            "communication": communication_id,
        },
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_BOTH_LEGS_PARSE,
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "company": company_id,
                    "communication": communication_id,
                    "item": item_id,
                },
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                INSERT_NEITHER_LEG_PARSE,
                {"id": uuid4(), "tenant": tenant_id, "company": company_id},
            )
    await session.execute(
        INSERT_ITEM_ASSESSMENT, {"id": uuid4(), "tenant": tenant_id, "subject": item_id}
    )
    await session.flush()


# --- The split application (T4): parents, items, the call, the parse leg -----


class FakeTransport:
    """The replaced gateway port: records the call, answers with the split (or
    parse) envelope it was given."""

    def __init__(self, *, envelope: dict) -> None:
        self.calls: list[dict[str, str]] = []
        self._envelope = envelope

    async def complete(self, *, task_type: str, language: str, snippet: str) -> object:
        self.calls.append({"task_type": task_type, "language": language, "snippet": snippet})
        return _TransportResult(envelope=dict(self._envelope))


class _TransportResult:
    def __init__(self, *, envelope: dict) -> None:
        self.envelope = envelope


def split_envelope(*, result: object) -> dict:
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


def parse_envelope() -> dict:
    return {
        "protocol_version": 1,
        "provider": "provider-test",
        "task_type": "reply_parsing",
        "model_id": "test-model",
        "prompt_name": "reply_parser",
        "prompt_version": 3,
        "result": {
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
        },
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        "completed_at": "2026-10-09T00:00:00+00:00",
    }


# The tokenized content the split is asked to divide: the raw's body with the
# two company names tokenized (the tokenizer numbers the COMPANY label's
# values longest-first — Synthetic Harbour is [COMPANY_01], Other Harbour
# [COMPANY_02]).
SPLIT_RAW = (
    b"From: director@client.example\r\n"
    b"Subject: Annual filings\r\n"
    b"Date: Tue, 23 Sep 2026 09:31:00 +0800\r\n"
    b"\r\n"
    b"Please update the register for Synthetic Harbour Limited.\r\n"
    b"File the Other Harbour Limited return.\r\n"
)
TOKENIZED_CONTENT = (
    "Please update the register for [COMPANY_01]. File the [COMPANY_02] return."
)
VALID_SPANS = [
    {"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]. "},
    {"company": "[COMPANY_02]", "text": "File the [COMPANY_02] return."},
]


async def seed_split_deployment(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    envelope: dict,
    ai_parsing: bool = True,
) -> tuple:
    """A tenant, an operator, two named and covered companies, the deployment
    facts the split's pre-flight and the call's posture read, and the fake
    transport answering the split. ``ai_parsing=False`` covers the companies
    for ingestion only — the ``AI_REPLY_PARSING`` gate the pre-flight reads
    denies them (the ``automation_denied`` park's setup)."""
    from app.adapters.persistence.service_packages import ServiceAssignment

    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    operator = User(
        tenant_id=tenant.id,
        email=f"operator-{uuid4()}@example.com",
        display_name="Operator",
        role="administrator",
    )
    session.add(operator)
    await session.flush()
    await risk_acknowledgment.accept(session, tenant_id=tenant.id, actor_user_id=operator.id)
    monkeypatch.setattr(settings, "license_expires_at", None)
    # The Q1.3 production gate, founder-accepted for the test deployment,
    # and the local custody endpoint (Enterprise Offline's transport — the
    # fake answers the seam, no socket).
    monkeypatch.setattr(settings, "ai_q13_gate", "residual_risk_accepted")
    monkeypatch.setattr(settings, "license_tier", "enterprise_offline")
    monkeypatch.setattr(settings, "llm_base_url", "https://llm.local/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    companies = []
    for cr_number, brn in (("1234567", "BR1234567"), ("7654321", "BR7654321")):
        company = Company(
            tenant_id=tenant.id,
            br_number=brn,
            cr_number=cr_number,
            date_of_incorporation="2010-03-01",
            anniversary_date="2026-03-01",
            type="private",
        )
        session.add(company)
        await session.flush()
        companies.append(company)
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=(
            ["email_ingestion", "ai_reply_parsing"] if ai_parsing else ["email_ingestion"]
        ),
        covered_forms=["NAR1", "ND2A"],
    )
    for company in companies:
        session.add(
            ServiceAssignment(
                tenant_id=tenant.id,
                company_id=company.id,
                package_id=package.id,
                coverage_start=date(2026, 1, 1),
                coverage_end=date(2026, 12, 31),
                status="active",
            )
        )
    await session.flush()
    for company, name in zip(
        companies, ("Synthetic Harbour Limited", "Other Harbour Limited"), strict=True
    ):
        session.add(
            CompanyNameRecord(
                tenant_id=tenant.id,
                company_id=company.id,
                effective_date=date(2015, 1, 1),
                source_type="manual",
                name_en=name,
                name_zh=None,
                business_name=None,
            )
        )
    await session.flush()
    transport = FakeTransport(envelope=envelope)
    monkeypatch.setattr(ai_gateway, "llm_client", lambda _transport: transport)
    references = [
        CompanyReference(
            company_id=company.id,
            name=name,
            cr_number=company.cr_number,
            br_number=company.br_number,
        )
        for company, name in zip(
            companies, ("Synthetic Harbour Limited", "Other Harbour Limited"), strict=True
        )
    ]
    return tenant, operator, companies, references, transport


async def test_split_message_creates_the_parent_items_and_the_audits(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy road end to end: the raw registers company-less, the one call
    rides the split task with a placeholder-only snippet, the grammar passes,
    and each item's restored slice stores under its own company — with the
    parent's audits writing the company column NULL and the one llm_call
    naming the split parent as its entity."""
    tenant, _operator, companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-1",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.SPLIT_DONE
    assert report.reason is None
    assert len(report.item_ids) == 2
    parent = await session.get(SplitParent, report.parent_id)
    assert parent is not None
    assert parent.status == "split_done"
    assert not hasattr(parent, "company_id")  # the parent is no company's record (D6)
    # The wire carried placeholders only.
    (call,) = transport.calls
    assert call["task_type"] == "split"
    assert "Synthetic Harbour" not in call["snippet"]
    assert "[COMPANY_01]" in call["snippet"] and "[COMPANY_02]" in call["snippet"]

    items = (
        await session.scalars(
            select(SplitWorkItem).where(SplitWorkItem.parent_id == parent.id)
        )
    ).all()
    assert len(items) == 2
    by_company = {item.company_id: item for item in items}
    assert set(by_company) == {companies[0].id, companies[1].id}
    for item in items:
        assert item.status == "awaiting_parse"
        slice_document = await session.get(Document, item.slice_document_id)
        assert slice_document.company_id == item.company_id
        slice_text = message_text(store.objects[slice_document.storage_key])
        # The restored slice names exactly its own company.
        own = next(ref.name for ref in references if ref.company_id == item.company_id)
        assert own in slice_text
        other = next(ref.name for ref in references if ref.company_id != item.company_id)
        assert other not in slice_text
    entries = await audit_log(session, tenant.id)
    actions = [entry.action for entry in entries]
    assert "split_parent_created" in actions
    assert "split_item_created" in actions
    assert "split_parent_done" in actions
    call_entry = next(entry for entry in entries if entry.action == "llm_call")
    assert call_entry.company_id is None
    assert call_entry.entity_type == "split_parent"
    assert call_entry.llm_call["task_type"] == "split"


async def test_split_message_parks_the_parent_when_any_company_opts_out(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D4: one opted-out company parks the whole parent, no call is
    made, and the reason is the pre-flight's own word."""
    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )
    _companies[0].ai_disabled = True

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-optout",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.STAFF_ONLY
    assert report.reason == "ai_disabled"
    assert transport.calls == []
    assert list((await session.scalars(select(SplitWorkItem))).all()) == []
    (parked,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_staff_only"
    ]
    assert parked.company_id is None
    assert parked.details["reason"] == "ai_disabled"


async def test_split_message_fails_the_parent_on_grammar_violations(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D3: an answer naming a token this call never minted fails the
    division — staff-visible, never guessed."""
    bad_spans = [
        {"company": "[COMPANY_99]", "text": TOKENIZED_CONTENT},
    ]
    tenant, _operator, _companies, references, _transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=bad_spans)
    )

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-bad",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.FAILED
    assert report.reason == "grammar_violations"
    assert list((await session.scalars(select(SplitWorkItem))).all()) == []
    (failed,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_failed"
    ]
    assert failed.details["violations"] == ["unknown_company_token", "missing_company_span"]


async def test_split_message_is_idempotent_per_message(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One parent per message, ever: the re-examination meets the winner's
    row and opens no second split."""
    tenant, _operator, _companies, references, _transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )
    kwargs = dict(
        session=session, store=store, scanner=FakeScanner(), tenant_id=tenant.id,
        mailbox_message_id="msg-split-twice", sender_address="director@client.example",
        subject="Annual filings", received_at=AT, raw=SPLIT_RAW, involved=references,
        auto_submitted=None, return_path=None, daily_splits_for_sender=0, now=AT,
    )
    first = await split.split_message(**kwargs)
    second = await split.split_message(**kwargs)

    assert second.parent_id == first.parent_id
    assert second.status is split.SplitParentStatus.SPLIT_DONE
    parents = list((await session.scalars(select(SplitParent))).all())
    assert len(parents) == 1
    items = list((await session.scalars(select(SplitWorkItem))).all())
    assert len(items) == 2


async def test_the_parse_cycles_work_item_lands_the_xor_leg(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D7: the item's slice parses through the one parse path — the
    parse row on the XOR leg, the assessment hanging off the item, the
    proposal landed per company with the split_work_item source, and the
    item moved to parsed."""
    from app.adapters.persistence.change_events import ChangeEvent
    from app.adapters.persistence.reply_parsing import ReplyParse

    tenant, _operator, companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )
    await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-parse",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )
    (item,) = [
        row
        for row in (await session.scalars(select(SplitWorkItem))).all()
        if row.company_id == companies[0].id
    ]
    transport._envelope = parse_envelope()

    parsed = await reply_parsing.parse_work_item(
        session,
        store=store,
        scanner=FakeScanner(),
        tenant_id=tenant.id,
        work_item_id=item.id,
        as_of=AT.date(),
    )

    assert parsed.outcome == "parsed"
    await session.refresh(item)
    assert item.status == "parsed"
    (parse_row,) = list((await session.scalars(select(ReplyParse))).all())
    assert parse_row.communication_id is None
    assert parse_row.split_work_item_id == item.id
    assert parse_row.company_id == item.company_id
    (assessment,) = list((await session.scalars(select(ExtractionAssessment))).all())
    assert assessment.subject_type == "split_work_item"
    assert assessment.subject_id == item.id
    (event,) = list((await session.scalars(select(ChangeEvent))).all())
    assert event.company_id == item.company_id
    assert event.source_type == "split_work_item"
    assert event.source_id == item.id
    # The reply leg's surfaces never see the work-item parse: the
    # communications list's newest-parse read stays empty.
    assert (
        await reply_parsing.latest_parses(
            session, tenant_id=tenant.id, company_id=item.company_id
        )
        == {}
    )


# --- T8: the pre-flight parks, the bounds' live keys, the grammar coverage ---

# The bounds' defaults (ruling D5) — pinned here so the scenario parks below,
# which rely on the live settings keys rather than injected facts, keep
# meaning what they say.
def test_the_bounds_config_keys_are_live_with_their_defaults() -> None:
    """``split_max_companies`` (10) and ``split_daily_cap_per_sender`` (5,
    ``0`` = no automatic splits) are the deployment's real settings — the
    park scenarios below read these, never a test-double mapping."""
    assert settings.split_max_companies == 10
    assert settings.split_daily_cap_per_sender == 5


async def test_the_automation_denial_parks_the_whole_parent(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D4's second leg: a company that fails the ``AI_REPLY_PARSING``
    gate parks the whole parent — the bound is licensing coverage, not the
    client's opt-out, so the reason is the pre-flight's own word."""
    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS), ai_parsing=False
    )

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-denied",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.STAFF_ONLY
    assert report.reason == "automation_denied"
    assert transport.calls == []
    assert list((await session.scalars(select(SplitWorkItem))).all()) == []
    (parked,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_staff_only"
    ]
    assert parked.company_id is None
    assert parked.details["reason"] == "automation_denied"


async def test_the_involved_set_over_the_live_max_parks(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D5's size bound against the LIVE default: eleven involved
    companies (the default cap is ten) park the parent before any call —
    no second package is needed for the nine extras, the park names the
    bound, not a company."""
    from app.adapters.persistence.service_packages import ServiceAssignment

    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=["email_ingestion", "ai_reply_parsing"],
        covered_forms=["NAR1", "ND2A"],
    )
    extras = []
    for index in range(9):
        company = Company(
            tenant_id=tenant.id,
            br_number=f"BR_EXTRA{index:02d}",
            cr_number=f"CR{index:04d}",
            date_of_incorporation="2010-03-01",
            anniversary_date="2026-03-01",
            type="private",
        )
        session.add(company)
        await session.flush()
        extras.append(company)
        session.add(
            ServiceAssignment(
                tenant_id=tenant.id,
                company_id=company.id,
                package_id=package.id,
                coverage_start=date(2026, 1, 1),
                coverage_end=date(2026, 12, 31),
                status="active",
            )
        )
    await session.flush()
    involved = [
        *references,
        *(
            CompanyReference(
                company_id=company.id,
                name=f"Extra Company {index} Limited",
                cr_number=company.cr_number,
                br_number=company.br_number,
            )
            for index, company in enumerate(extras)
        ),
    ]
    assert len(involved) == settings.split_max_companies + 1

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-too-many",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=involved,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.STAFF_ONLY
    assert report.reason == "too_many_companies"
    assert transport.calls == []
    assert list((await session.scalars(select(SplitWorkItem))).all()) == []
    (parked,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_staff_only"
    ]
    assert parked.details["reason"] == "too_many_companies"


async def test_the_senders_daily_cap_parks_at_the_live_default(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D5's rate bound against the LIVE default: a sender whose day
    already holds five automatic splits (the default cap) parks the next
    parent — the count rides the caller's fact, the cap the settings key."""
    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-capped",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=settings.split_daily_cap_per_sender,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.STAFF_ONLY
    assert report.reason == "daily_cap_reached"
    assert transport.calls == []
    (parked,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_staff_only"
    ]
    assert parked.details["reason"] == "daily_cap_reached"


async def test_a_zero_daily_cap_means_no_automatic_splits(
    session: AsyncSession, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling D5's ``0 = never``: with the cap set to zero, even the day's
    first parent parks — the deployment turns the whole capability off for
    automatic mail without touching any company's settings."""
    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=VALID_SPANS)
    )
    monkeypatch.setattr(settings, "split_daily_cap_per_sender", 0)

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id="msg-split-never",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.STAFF_ONLY
    assert report.reason == "daily_cap_reached"
    assert transport.calls == []


# One reachable case per grammar violation kind (ruling D3), each failing
# the parent staff-visibly — the kinds the checker reports, in its order.
GRAMMAR_CASES = [
    pytest.param(
        {"company": "[COMPANY_01]", "text": "not a list at all"},
        ["malformed_spans"],
        id="malformed_spans",
    ),
    pytest.param(
        [{"company": "[COMPANY_99]", "text": TOKENIZED_CONTENT}],
        ["unknown_company_token", "missing_company_span"],
        id="unknown_company_token",
    ),
    pytest.param(
        [
            {"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]. "},
            {"company": "[COMPANY_01]", "text": "File the [COMPANY_02] return."},
        ],
        ["duplicate_company_span", "missing_company_span"],
        id="duplicate_company_span",
    ),
    pytest.param(
        [{"company": "[COMPANY_01]", "text": TOKENIZED_CONTENT}],
        ["missing_company_span"],
        id="missing_company_span",
    ),
    pytest.param(
        [
            {"company": "[COMPANY_01]", "text": ""},
            {"company": "[COMPANY_02]", "text": TOKENIZED_CONTENT},
        ],
        ["empty_span"],
        id="empty_span",
    ),
    pytest.param(
        [
            {
                "company": "[COMPANY_01]",
                "text": "Please update the register for [COMPANY_01]. Extra words.",
            },
            {"company": "[COMPANY_02]", "text": "File the [COMPANY_02] return."},
        ],
        ["coverage_mismatch"],
        id="coverage_mismatch",
    ),
]


@pytest.mark.parametrize("result,violations", GRAMMAR_CASES)
async def test_each_grammar_violation_fails_the_parent_once(
    session: AsyncSession,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    result: object,
    violations: list[str],
) -> None:
    """Ruling D3's coverage at the application seam: every violation kind
    the span grammar names is reachable through one provider answer, and
    each parks the parent ``failed`` — received but not trustworthy, staff-
    visible, never guessed into items."""
    tenant, _operator, _companies, references, transport = await seed_split_deployment(
        session, monkeypatch, envelope=split_envelope(result=result)
    )

    report = await split.split_message(
        session,
        store,
        FakeScanner(),
        tenant_id=tenant.id,
        mailbox_message_id=f"msg-split-{violations[0]}",
        sender_address="director@client.example",
        subject="Annual filings",
        received_at=AT,
        raw=SPLIT_RAW,
        involved=references,
        auto_submitted=None,
        return_path=None,
        daily_splits_for_sender=0,
        now=AT,
    )

    assert report.status is split.SplitParentStatus.FAILED
    assert report.reason == "grammar_violations"
    assert transport.calls != []
    assert list((await session.scalars(select(SplitWorkItem))).all()) == []
    (parent,) = list((await session.scalars(select(SplitParent))).all())
    assert parent.status == "failed"  # staff-visible on the split surface
    (failed,) = [
        entry
        for entry in await audit_log(session, tenant.id)
        if entry.action == "split_parent_failed"
    ]
    assert failed.company_id is None
    assert failed.details["violations"] == violations
