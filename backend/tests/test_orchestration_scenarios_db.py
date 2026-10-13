"""The M7 exit gate: the orchestration pulse, end to end, with zero PII.

The last walk of the M7 slice: the vendor's scheduler and the deployment's
composed sweep, both real, over the CP3 command-envelope channel. The control
plane is the real service in a subprocess on loopback (``VendorService``, from
``tests/vendor_harness.py`` — the shared harness the cross-deployable suites
use); the scheduler tick is the real ``due_tenants`` +
``commands.enqueue`` run in a second subprocess against the vendor database
(the two deployables cannot share an interpreter — both are named ``app``);
the deployment is this deployable's own app, activated, licensed Standard and
driven through its real ``POST /api/v1/commands/run`` endpoint.

Six walks, one per review focus of the M7 plan plus the discovery leg's two:

1. ``test_the_pulse_round_trip`` — a pulse on an empty, unconfigured
   deployment succeeds with every count at 0 (the common case must never be
   an alarm);
2. ``test_the_pulse_generates_the_due_companys_confirmation`` — the discovery
   leg is live end to end: a due, unasked company is seeded, the pulse
   generates its confirmation request, and ``confirmations_generated`` reads
   1 on the wire, in the deployment's stored result, and in the control
   plane's stored telemetry columns — while the release gate stops the letter
   at the send leg, exactly as §5.1.17 records;
3. ``test_a_failed_leg_fails_the_command_honestly`` — an unreadable send-mode
   block fails the reminders leg alone: the command is ``failed`` /
   ``execution_failed``, the reason names the leg, ``cycles_failed == 1`` and
   the other legs' counts — the discovery leg's triple among them — still
   travel;
4. ``test_the_discovery_leg_fails_the_command_honestly`` — an unreadable
   milestone block through the real compose fails the discovery leg (and the
   reminders leg behind it, which reads the same block): the command is
   ``failed`` with both causes named in leg order, every leg still ran, and
   the counts (including the discovery triple) are preserved;
5. ``test_no_pile_up`` — two scheduler ticks before any poll leave exactly
   one command in the vendor's queue;
6. ``test_free_tier_refuses_and_records`` — a lease that lapses to Free
   between enqueue and execution is refused by the deployment's own tier leg,
   recorded locally, and the refusal reaches the vendor.

Every walk ends with the exit-gate sweep the CP3 suite runs, extended over
the stored audit rows and the eight orchestration count columns (the M7 five
plus the discovery leg's ``confirmations_generated`` /
``confirmations_refused`` / ``confirmations_skipped``, migration
``0008_discovery_telemetry``): every envelope and result on both sides passes
the closed vocabulary in ``app.domain.commands``, and every stored string —
envelope, result, audit detail — is one of a timestamp, an opaque id, an
address, or a vocabulary word; free text matches nothing.

Skipped unless both databases are reachable and migrated; see
``tests/control_plane_integration.py``. The vendor service commits for real
(it is a separate process), so its rows are removed again at the end.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.commands import CommandInbox
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.contacts import (
    CompanyExternalContact,
    ContactChannel,
    Person,
)
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Tenant
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef
from app.application import documents, scanning
from app.application.commands import ORCHESTRATION_COUNT_KEYS
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.commands import (
    ENVELOPE_VERSION,
    CommandName,
    command_violations,
    result_violations,
)
from app.domain.compliance import anniversary_in_year
from app.domain.confirmation import RequestStatus
from app.domain.documents import MESSAGE_CONTENT_TYPE
from app.domain.mail_send import OutboxStatus
from app.domain.milestones import DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE
from tests.control_plane_integration import CONTROL_PLANE_DIR, REPOSITORY_ROOT
from tests.test_command_envelope_integration_db import (
    activate_deployment,
    make_client,
    read_queue_row,
    run_cycle,
    seed_tenant_with_admin,
    set_tier,
    vendor_documents,
)
from tests.vendor_harness import VendorService, remove_vendor_rows

# The one credential this suite hands the vendor service — the same value the
# command-channel suite uses, so the subprocess environment is unchanged from
# when this suite imported the harness copy from there.
VENDOR_ADMIN_KEY = "command-channel-vendor-key"

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

PULSE = CommandName.SCHEDULE_ORCHESTRATION.value
# What a command-outcome audit entry may carry (``commands._record``): the
# outcome, never the envelope's parameters.
_AUDIT_DETAIL_KEYS = frozenset({"command", "status", "error_code", "reason"})
_VALID_TEMPLATES = {
    "document": "{CompanyCode}-{Form}",
    "generated": "{CompanyCode}-{Form}-{Version}",
}
_UNREADABLE_MILESTONES = {"reminder_cadence_days": "not-a-list"}
_UNREADABLE_SEND_MODES = {"client": "not-a-mode"}

# The discovery round trip's seed (the discovery scenario suite's shapes,
# restated — this family restates rather than imports). The company's
# anniversary sits the configured data-confirmation offset out, so the ask
# date is the day the walk runs; last year's period is seeded asked, closing
# it, so the sweep attempts this year.
MAILBOX = "ai-filings@tcsp.example"
CONTACT = "director@brightlantern.example.hk"
DUE_ANNIVERSARY = date.today() + timedelta(days=DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE)
COVERAGE = {"start": "2020-01-01", "end": "2030-12-31"}

_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A vocabulary word: command names, statuses, error codes, telemetry keys,
# ``leg:cause`` reasons — identifier-shaped text only. Uppercase is admitted
# solely for the CamelCase class names in ``leg:cause`` reasons
# (``reminders:RuntimeError``); prose stays banned — only timestamps, UUIDs,
# identifiers and version strings pass alongside vocabulary words.
_WORD_TEXT = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:+-]{0,127}$")
# A version string, the ``_VERSION_TEXT`` grammar of the envelope rules.
_VERSION_VALUE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")
# An address. The audit chain records ids, **addresses**, codes and outcomes
# by design (``app.application.mail_send``'s docstring: the drafted request's
# entry carries the recipients) — an address is a bounded identifier, not free
# text, and the no-PII gate governs what the control plane holds, which never
# includes the chain. The discovery round trip is the first walk whose seed
# generates client mail, so the allowance is stated here, beside the rule.
_ADDRESS_TEXT = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}$")
# The records registry's own coordinates, which its ``document_registered``
# entry carries by design (ADR-0009: the chain records the digest and the
# storage key, never the content): a SHA-256 hex digest, and a storage key —
# identifier segments the §5.3 template renders, separated by ``/``.
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_KEY_TEXT = re.compile(r"^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")
# A failure reason: one ``leg:cause`` pair, or several joined by ``;`` —
# the reason grammar §5.1.15 records (the causes joined, in leg order).
_CAUSES_TEXT = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9_.+-]*(:[a-zA-Z0-9_.+-]+)?(;[a-zA-Z][a-zA-Z0-9_.+-]*(:[a-zA-Z0-9_.+-]+)?)*$"
)

# One orchestrator tick of the real control plane, run in a subprocess: the
# two deployables cannot share an interpreter (both are named ``app``), so the
# scheduler tick — the real ``due_tenants`` plus the worker's ``_pulse`` —
# leaves the process, the way ``control_plane_server.py`` does for the HTTP
# side. The tick commits for real; the caller removes the vendor rows after.
# Environment: ``AICS_CP_DIR`` and ``AICS_CP_DATABASE_URL``.
_TICK_DRIVER = """
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["AICS_CP_DIR"]).resolve()))


async def _tick():
    from app.adapters.persistence.session import session_factory
    from app.application.orchestrator import due_tenants
    from app.workers.orchestrator import _pulse

    async with session_factory() as session:
        due = await due_tenants(session)
        await _pulse(session)
        await session.commit()
    return [str(tenant_id) for tenant_id in due]


print(json.dumps({"due": asyncio.run(_tick())}))
"""


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after the test.

    ``create_savepoint`` because the send leg commits per outbox row (its
    pre-existing behaviour, §5.1.11): the discovery round trip leaves a queued
    row the send leg attempts, and a bare session would commit the outer
    transaction mid-test instead of releasing a savepoint (the phase-7 exit
    gate suite's fixture, restated).
    """
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


# --- The vendor's scheduler -------------------------------------------------


def run_scheduler_tick(database_url: str) -> list[str]:
    """Run one real scheduler tick against the vendor database.

    Returns the tenant ids the tick saw as due, so a walk can assert the pulse
    decision itself, not just its side effects.
    """
    environment = {
        **os.environ,
        "AICS_CP_DIR": str(CONTROL_PLANE_DIR),
        "AICS_CP_DATABASE_URL": database_url,
    }
    environment.pop("PYTHONPATH", None)  # the driver decides what "app" means
    completed = subprocess.run(
        [sys.executable, "-c", _TICK_DRIVER],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, f"the scheduler tick failed: {completed.stderr}"
    due = json.loads(completed.stdout)["due"]
    assert isinstance(due, list)
    return [str(item) for item in due]


def read_queue_rows(database_url: str, vendor_tenant_id: str) -> list[dict[str, object]]:
    """Every command-queue row of one tenant, read from the vendor database."""
    engine_sync = create_engine(database_url)
    try:
        with engine_sync.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM command_queue WHERE tenant_id = :id "
                        "ORDER BY issued_at, command_id"
                    ),
                    {"id": UUID(vendor_tenant_id)},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]
    finally:
        engine_sync.dispose()


# --- The deployment's half of the setup --------------------------------------


def standard_deployment(client: TestClient, vendor: VendorService) -> str:
    """Activated, Standard-licensed, and reported at version 1.

    The version report is the deployment's first poll, exactly how a real
    deployment introduces itself; the scheduler will not pulse a deployment
    that has never reported (``stale_state``).
    """
    vendor_tenant_id = activate_deployment(client, vendor)
    assert set_tier(vendor, vendor_tenant_id, "standard").status_code == 200
    refreshed = client.post("/api/v1/entitlement/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["tier"] == "standard"
    first = run_cycle(client)
    assert (first["received"], first["executed"], first["reported"]) == (0, 0, 0)
    assert first["poll_error"] is None
    return vendor_tenant_id


# --- The discovery round trip's seed ------------------------------------------
#
# The company shell and its coverage walk through the real routes (their audit
# entries are coordinates and vocabulary words, so the exit-gate sweep stays
# green). The name fact, the client contact, and last year's asked period are
# seeded as rows, not routes: the audited write paths carry natural-language
# payloads (the name, the person's name), and the sweep's rule is that free
# text matches nothing — fixture seeding must not fail the gate it is testing.


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes (the discovery scenario
    suite's shape, restated)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


def make_company(client: TestClient, *, anniversary: date = DUE_ANNIVERSARY) -> str:
    response = client.post(
        "/api/v1/companies",
        json={
            "br_number": f"{uuid4().int % 100000000:08d}",
            "cr_number": f"{uuid4().int % 10000000:07d}",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": anniversary.isoformat(),
            "type": "private",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def seed_name_fact(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> None:
    """The company's registered name as a fact row (no audited write)."""
    session.add(
        CompanyNameRecord(
            tenant_id=tenant_id,
            company_id=company_id,
            effective_date=date(2026, 1, 1),
            valid_from=None,
            valid_to=None,
            source_type="manual",
            source_id=None,
            created_by=None,
            name_en="Bright Lantern Design Limited",
            name_zh=None,
            business_name=None,
        )
    )
    await session.flush()


async def seed_client_contact(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> None:
    """One associated client contact with an active email channel (rows only).

    ``app.application.contacts.create_person`` would audit the person's
    natural-language name; the walk seeds the same rows the write path lands
    so the exit-gate sweep sees only what the compose itself wrote.
    """
    person = Person(
        tenant_id=tenant_id,
        person_type="natural_person",
        name_en="CHAN TAI MAN",
        is_external_contact=True,
    )
    session.add(person)
    await session.flush()
    session.add(
        ContactChannel(
            tenant_id=tenant_id,
            person_id=person.id,
            channel_type="email",
            identifier=CONTACT,
        )
    )
    session.add(
        CompanyExternalContact(tenant_id=tenant_id, company_id=company_id, person_id=person.id)
    )
    await session.flush()


def cover(client: TestClient, company_id: str) -> None:
    """An assignment whose package covers the confirmation generation gate.

    The package name is identifier-shaped on purpose: its creation is audited,
    and the exit-gate sweep reads the chain.
    """
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


def letter_bytes(message_id: str) -> bytes:
    """A minimal stored letter carrying its own Message-ID."""
    return (
        f"From: {MAILBOX}\r\n"
        f"To: {CONTACT}\r\n"
        f"Subject: [Bright Lantern — CR 1234567] Annual Return Confirmation\r\n"
        f"Message-ID: {message_id}\r\n"
        f"\r\n"
        f"Please confirm. / 請確認。\r\n"
    ).encode()


async def seed_prior_year_request(
    session: AsyncSession,
    store: FakeStore,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> None:
    """Last year's period asked and superseded: the realistic history that
    closes last year's candidate so the sweep attempts this year.

    Seeded as rows (the discovery scenario suite's ``seed_request``, restated)
    because the trigger's own reply-window guard refuses a generation for a
    period whose window has closed.
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
        data=letter_bytes(f"<hist-{uuid4()}@tcsp.example>"),
        scanner=scanning.scanner(),
        form="nar1",
        retention_class=documents.RetentionClass.CLIENT_COMMUNICATION,
        version=1,
    )
    session.add(
        ConfirmationRequest(
            tenant_id=tenant_id,
            company_id=company_id,
            form_type="nar1",
            target_date=anniversary_in_year(DUE_ANNIVERSARY, DUE_ANNIVERSARY.year - 1),
            reply_by=DUE_ANNIVERSARY - timedelta(days=365 + 20),
            statutory_due=None,
            status=RequestStatus.SUPERSEDED.value,
            version_no=1,
            document_id=document.id,
            checklist=[],
            recipients=[{"person_id": str(uuid4()), "address": CONTACT}],
            created_by=None,
        )
    )
    await session.flush()


# --- The no-PII sweep --------------------------------------------------------


def _free_text_findings(value: object, path: str) -> list[str]:
    """Every stored string that is neither a timestamp, an opaque id, nor a
    vocabulary word — the mechanical form of "free text matches nothing"."""
    findings: list[str] = []
    if isinstance(value, str):
        if not _allowed_string(value):
            findings.append(f"{path}: free text {value!r}")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            findings += _free_text_findings(key, f"{path}.{key}")
            findings += _free_text_findings(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings += _free_text_findings(item, f"{path}[{index}]")
    return findings


def _allowed_string(value: str) -> bool:
    if value == "":
        return True
    try:
        datetime.fromisoformat(value)
    except ValueError:
        pass
    else:
        return True
    return bool(
        _UUID_TEXT.match(value)
        or _WORD_TEXT.match(value)
        or _VERSION_VALUE.match(value)
        or _ADDRESS_TEXT.match(value)
        or _SHA256_TEXT.match(value)
        or _STORAGE_KEY_TEXT.match(value)
        or _CAUSES_TEXT.match(value)
    )


def _sweep_document(
    label: str,
    envelope: Mapping[str, object],
    result: Mapping[str, object] | None,
) -> list[str]:
    """The CP3 exit-gate checks for one stored command, plus the free-text
    sweep over both documents."""
    findings = command_violations(envelope)
    findings += _free_text_findings(envelope, f"{label}.envelope")
    if result is not None:
        findings += result_violations(result)
        findings += _free_text_findings(result, f"{label}.result")
    return findings


async def assert_no_pii_stored(
    session: AsyncSession,
    *,
    tenant: Tenant,
    command_ids: list[UUID],
    cp_database: str,
    vendor_tenant_id: str,
) -> None:
    """The exit gate, extended: envelopes, results and audits on both sides.

    The CP3 sweep checked the four envelopes of one command; M7's telemetry
    adds five count keys, the discovery leg adds three more (stored as
    0008's columns on the vendor side), and the walks add audit rows, so the
    sweep covers every stored row of the walk — the vendor's queue rows, the
    deployment's inbox rows, and every audit entry the tenant gained — on
    both the closed vocabulary and the free-text rule.
    """
    findings: list[str] = []

    # The deployment's inbox rows for this walk's commands — none in the
    # pile-up walk, whose deployment never polls; the query is total over
    # what is stored either way.
    inbox_rows = list(
        (
            await session.scalars(
                select(CommandInbox).where(CommandInbox.command_id.in_(command_ids))
            )
        ).all()
    )
    for row in inbox_rows:
        findings += _sweep_document(f"inbox:{row.command_id}", row.envelope, row.result)

    audit_rows = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert audit_rows  # activation and the outcome both audit
    for entry in audit_rows:
        findings += _free_text_findings(entry.details or {}, f"audit:{entry.action}")
        if entry.entity_type == "command":
            extra = set(entry.details or {}) - _AUDIT_DETAIL_KEYS
            if extra:
                findings.append(f"audit:{entry.action}: unexpected keys {sorted(extra)}")

    vendor_rows = read_queue_rows(cp_database, vendor_tenant_id)
    assert len(vendor_rows) == len(command_ids)  # the vendor's record, total too
    for stored in vendor_rows:
        label = f"vendor:{stored['command_id']}"
        if stored["completed_at"] is None:
            pending_envelope: dict[str, object] = {
                "envelope_version": stored["envelope_version"],
                "command_id": str(stored["command_id"]),
                "command": stored["command"],
                "params": stored["params"],
                "expected_outputs": stored["expected_outputs"],
                "issued_at": stored["issued_at"].isoformat(),
            }
            findings += _sweep_document(label, pending_envelope, None)
        else:
            vendor_envelope, vendor_result = vendor_documents(stored)
            findings += _sweep_document(label, vendor_envelope, vendor_result)

    assert findings == []


# --- The walks ---------------------------------------------------------------


async def test_the_pulse_round_trip(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus 1: the pulse succeeds on an empty, unconfigured
    deployment — every count 0, the outcome still ``succeeded``."""
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            due = run_scheduler_tick(control_plane_database)
            assert vendor_tenant_id in due

            report = run_cycle(client)
            assert (report["received"], report["executed"], report["reported"]) == (1, 1, 1)
            outcome = report["outcomes"][0]
            assert outcome["command_id"] is not None
            assert outcome["command"] == PULSE
            assert outcome["status"] == "succeeded"
            assert outcome["error_code"] is None

            # The vendor's record: succeeded, and the five counts stored as
            # columns on command_queue, every one of them 0.
            stored = read_queue_row(control_plane_database, outcome["command_id"])
            assert stored["status"] == "succeeded"
            assert stored["error_code"] is None
            assert stored["dispatched_at"] is not None
            assert stored["reported_envelope_version"] == ENVELOPE_VERSION
            for key in ORCHESTRATION_COUNT_KEYS:
                assert stored[key] == 0, key

            # The deployment's own record: the same keys in the stored
            # result, the outcome audited through the single write path.
            row = await session.scalar(
                select(CommandInbox).where(
                    CommandInbox.command_id == UUID(str(outcome["command_id"]))
                )
            )
            assert row is not None
            assert row.status == "succeeded"
            assert row.reported_at is not None
            assert row.result is not None
            for key in ORCHESTRATION_COUNT_KEYS:
                assert row.result["telemetry"][key] == 0, key
            assert row.result["telemetry"]["queue_depth"] == 1

            entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_EXECUTED.value)
            )
            assert entry is not None
            assert entry.entity_id == row.command_id
            assert entry.tenant_id == tenant.id
            assert entry.details == {
                "command": PULSE,
                "status": "succeeded",
                "error_code": None,
                "reason": None,
            }

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[row.command_id],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )

            settled = run_cycle(client)
            assert (settled["received"], settled["executed"], settled["reported"]) == (0, 0, 0)
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)


async def test_the_pulse_generates_the_due_companys_confirmation(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery leg, live end to end: one due, unasked company; the pulse
    generates its confirmation request and the discovery counts travel — on
    the wire, in the deployment's stored result, and in the control plane's
    stored telemetry columns."""
    store = FakeStore()
    monkeypatch.setattr(documents, "get_storage", lambda: store)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            company_id = make_company(client)
            await seed_name_fact(session, tenant_id=tenant.id, company_id=UUID(company_id))
            await seed_client_contact(session, tenant_id=tenant.id, company_id=UUID(company_id))
            cover(client, company_id)
            await seed_prior_year_request(
                session, store, tenant_id=tenant.id, company_id=UUID(company_id)
            )

            due = run_scheduler_tick(control_plane_database)
            assert vendor_tenant_id in due

            report = run_cycle(client)
            assert (report["received"], report["executed"], report["reported"]) == (1, 1, 1)
            outcome = report["outcomes"][0]
            assert outcome["command"] == PULSE
            assert outcome["status"] == "succeeded"
            assert outcome["error_code"] is None

            # Every count key landed: the discovery leg generated exactly one
            # request, nothing else moved, and no leg failed.
            expected = {
                "mail_ingested": 0,
                "replies_parsed": 0,
                "confirmations_generated": 1,
                "confirmations_refused": 0,
                "confirmations_skipped": 0,
                "reminders_enqueued": 0,
                "messages_sent": 0,
                "cycles_failed": 0,
            }
            row = await session.scalar(
                select(CommandInbox).where(
                    CommandInbox.command_id == UUID(str(outcome["command_id"]))
                )
            )
            assert row is not None and row.result is not None
            for key in ORCHESTRATION_COUNT_KEYS:
                assert row.result["telemetry"][key] == expected[key], key

            # The vendor's record: the discovery outcome is a stored column
            # on command_queue (0007's five plus 0008's three), not just a
            # JSONB fact inside the result document.
            stored = read_queue_row(control_plane_database, outcome["command_id"])
            assert stored["status"] == "succeeded"
            for key in ORCHESTRATION_COUNT_KEYS:
                assert stored[key] == expected[key], key

            # The generation is the trigger's own: this year's request,
            # queued under the tenant's automatic mode, its client outbox row
            # pends — and the send leg then refuses delivery at the release
            # gate (no continuation on file), exactly as §5.1.17 records for a
            # generated automatic request: the letter is blocked, not sent, and
            # the command still succeeds.
            requests = list(
                (
                    await session.scalars(
                        select(ConfirmationRequest).where(
                            ConfirmationRequest.company_id == UUID(company_id)
                        )
                    )
                ).all()
            )
            (request,) = [
                item for item in requests if item.status != RequestStatus.SUPERSEDED.value
            ]
            assert request.target_date == DUE_ANNIVERSARY
            assert request.status == RequestStatus.QUEUED.value
            jobs = list(
                (
                    await session.scalars(
                        select(MailOutbox).where(
                            MailOutbox.confirmation_request_id == request.id
                        )
                    )
                ).all()
            )
            (job,) = jobs
            assert job.status == OutboxStatus.PENDING.value
            assert job.blocked_reason is not None
            assert job.blocked_reason.startswith("continuation_not_recorded")

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[row.command_id],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)


async def test_a_failed_leg_fails_the_command_honestly(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus 2: an unreadable send-mode block fails the reminders leg
    alone — ``failed``/``execution_failed``, the reason names the leg, the
    other legs' counts — the discovery triple among them — still travel."""
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            # A hand-edited send-mode block the reminder sweep cannot read
            # (the discovery leg reads only the milestone block — still
            # readable — and this deployment has no companies, so the
            # discovery leg runs cleanly and examines nothing): the cycle
            # refuses to guess hold from a block nobody can read, so the
            # reminders leg fails and names itself.
            session.add(
                TenantConfig(
                    tenant_id=tenant.id,
                    naming_templates=dict(_VALID_TEMPLATES),
                    send_modes=dict(_UNREADABLE_SEND_MODES),
                )
            )
            await session.flush()

            due = run_scheduler_tick(control_plane_database)
            assert vendor_tenant_id in due

            report = run_cycle(client)
            outcome = report["outcomes"][0]
            assert outcome["command"] == PULSE
            assert outcome["status"] == "failed"
            assert outcome["error_code"] == "execution_failed"

            # The vendor's record carries the honest telemetry: the failed
            # leg counted, the others — the discovery leg among them, which
            # ran cleanly and examined nothing — reported their zeros.
            stored = read_queue_row(control_plane_database, outcome["command_id"])
            assert stored["status"] == "failed"
            assert stored["error_code"] == "execution_failed"
            assert stored["cycles_failed"] == 1
            for key in (
                "mail_ingested",
                "replies_parsed",
                "confirmations_generated",
                "confirmations_refused",
                "confirmations_skipped",
                "reminders_enqueued",
                "messages_sent",
            ):
                assert stored[key] == 0, key
            assert stored["duration_ms"] is None  # no honest duration on this path

            row = await session.scalar(
                select(CommandInbox).where(
                    CommandInbox.command_id == UUID(str(outcome["command_id"]))
                )
            )
            assert row is not None and row.result is not None
            telemetry = row.result["telemetry"]
            assert telemetry["cycles_failed"] == 1
            for key in (
                "mail_ingested",
                "replies_parsed",
                "confirmations_generated",
                "confirmations_refused",
                "confirmations_skipped",
                "reminders_enqueued",
                "messages_sent",
            ):
                assert telemetry[key] == 0, key
            assert telemetry["queue_depth"] == 1
            assert "duration_ms" not in telemetry

            entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_FAILED.value)
            )
            assert entry is not None
            assert entry.details["reason"] == "reminders:config_error"

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[row.command_id],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)


async def test_the_discovery_leg_fails_the_command_honestly(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery leg's containment through the real compose: the same
    unreadable milestone block stops the sweep before it starts, so the
    discovery leg — like the reminders leg, which reads the same block —
    reports ``config_error``: the command is ``failed``/``execution_failed``,
    the reason names both legs in leg order, every leg still ran, and the
    counts, including the discovery triple, are preserved."""
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            session.add(
                TenantConfig(
                    tenant_id=tenant.id,
                    naming_templates=dict(_VALID_TEMPLATES),
                    milestones=dict(_UNREADABLE_MILESTONES),
                )
            )
            await session.flush()

            due = run_scheduler_tick(control_plane_database)
            assert vendor_tenant_id in due

            report = run_cycle(client)
            outcome = report["outcomes"][0]
            assert outcome["command"] == PULSE
            assert outcome["status"] == "failed"
            assert outcome["error_code"] == "execution_failed"

            # The reason names the discovery leg first — the leg the block
            # stops before any company is examined — and the reminders leg
            # behind it; one counted failure per cause.
            stored = read_queue_row(control_plane_database, outcome["command_id"])
            assert stored["status"] == "failed"
            assert stored["error_code"] == "execution_failed"
            assert stored["cycles_failed"] == 2
            assert stored["duration_ms"] is None  # no honest duration on this path
            expected = {
                "mail_ingested": 0,
                "replies_parsed": 0,
                "confirmations_generated": 0,
                "confirmations_refused": 0,
                "confirmations_skipped": 0,
                "reminders_enqueued": 0,
                "messages_sent": 0,
            }
            for key, value in expected.items():
                assert stored[key] == value, key

            row = await session.scalar(
                select(CommandInbox).where(
                    CommandInbox.command_id == UUID(str(outcome["command_id"]))
                )
            )
            assert row is not None and row.result is not None
            telemetry = row.result["telemetry"]
            assert telemetry["cycles_failed"] == 2
            for key, value in expected.items():
                assert telemetry[key] == value, key
            assert "duration_ms" not in telemetry

            entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_FAILED.value)
            )
            assert entry is not None
            assert entry.details["reason"] == "discovery:config_error;reminders:config_error"

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[row.command_id],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)


async def test_no_pile_up(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus 3: two scheduler ticks before any poll leave exactly one
    command in the vendor's queue."""
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            first = run_scheduler_tick(control_plane_database)
            assert first == [vendor_tenant_id]  # one tenant, seen as due
            second = run_scheduler_tick(control_plane_database)
            assert second == []  # the unfinished pulse re-opens nothing

            rows = read_queue_rows(control_plane_database, vendor_tenant_id)
            assert len(rows) == 1
            assert rows[0]["command"] == PULSE
            assert rows[0]["status"] is None  # unanswered, exactly once

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[rows[0]["command_id"]],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)


async def test_free_tier_refuses_and_records(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus 5: the lease lapses to Free between enqueue and
    execution — the deployment's own tier leg refuses, records the refusal
    locally, and the vendor stores it."""
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
            vendor_tenant_id = standard_deployment(client, vendor)

            due = run_scheduler_tick(control_plane_database)
            assert vendor_tenant_id in due
            queued = read_queue_rows(control_plane_database, vendor_tenant_id)
            assert len(queued) == 1 and queued[0]["command"] == PULSE

            # The licence lapses before the deployment polls; the deployment
            # picks the new tier up on its lease refresh (ADR-0016).
            assert set_tier(vendor, vendor_tenant_id, "free").status_code == 200
            refreshed = client.post("/api/v1/entitlement/refresh")
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["tier"] == "free"

            report = run_cycle(client)
            assert (report["received"], report["executed"], report["reported"]) == (1, 1, 1)
            outcome = report["outcomes"][0]
            assert outcome["command"] == PULSE
            assert outcome["status"] == "rejected"
            assert outcome["error_code"] == "entitlement_denied"

            # The deployment's own record: the refusal, nothing executed.
            row = await session.scalar(
                select(CommandInbox).where(
                    CommandInbox.command_id == UUID(str(outcome["command_id"]))
                )
            )
            assert row is not None
            assert row.status == "rejected"
            assert row.reported_at is not None
            assert row.result is not None
            assert row.result["error_code"] == "entitlement_denied"
            assert row.result["telemetry"] == {"queue_depth": 1}  # no counts: nothing ran

            entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_REJECTED.value)
            )
            assert entry is not None
            assert entry.details["reason"] == "tier_capability_missing"

            # The vendor's record of the refusal.
            stored = read_queue_row(control_plane_database, outcome["command_id"])
            assert stored["status"] == "rejected"
            assert stored["error_code"] == "entitlement_denied"
            for key in ORCHESTRATION_COUNT_KEYS:
                assert stored[key] is None, key

            await assert_no_pii_stored(
                session,
                tenant=tenant,
                command_ids=[row.command_id],
                cp_database=control_plane_database,
                vendor_tenant_id=vendor_tenant_id,
            )
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)
