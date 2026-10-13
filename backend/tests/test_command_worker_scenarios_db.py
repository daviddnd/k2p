"""DB-backed command-worker scenarios (opt-in; needs a migrated PostgreSQL).

The worker's whole life against a real database and a faithful fake of the
vendor service (``tests/fake_control_plane.py``, no socket): a ``ping``
received, executed, audited, and reported; the refusals that never execute —
an unknown command, an unknown envelope version, a malformed envelope, a
licence that does not carry orchestration, a company the assignment does not
cover, a reference that no longer resolves; and the two properties the
protocol promises — a re-delivered envelope is executed once, and an outcome
whose report failed is kept locally and reported on the next successful
cycle, exactly once.

Two things are asserted rather than assumed throughout: that a refused
command leaves no trace of having run (the registry never saw it, and no
audit entry claims otherwise), and that everything the worker stores — the
envelope as received and the result it produced — passes the vocabulary guard
in ``app.domain.commands``. The ungated path is checked in the same tests:
a Free licence stops orchestration and nothing else.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back; the
credential file is per-test (``conftest.py``).
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import ControlPlaneClient
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.commands import CommandInbox
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import commands as worker
from app.application import service_packages
from app.core.config import settings
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.audit import AuditAction
from app.domain.commands import (
    CommandErrorCode,
    CommandStatus,
    command_violations,
    result_violations,
)
from app.domain.service_packages import AutomationType
from app.main import create_app
from tests.fake_control_plane import FakeControlPlane, UnreachableTransport

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

CONTROL_PLANE_URL = "http://control-plane.test"
TOKEN = "deployment-credential-under-test"
RUN_URL = "/api/v1/commands/run"
LIST_URL = "/api/v1/commands"
# Coverage wide enough that the worker's own clock (the date the gate is
# evaluated at) cannot take a test out of it.
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2099, 12, 31)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- Harness --------------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def use_control_plane(monkeypatch: pytest.MonkeyPatch, control_plane: FakeControlPlane) -> None:
    """Point the transport's client seam at the fake vendor service, in-process."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        worker.lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL,
            timeout=1.0,
            transport=httpx.ASGITransport(app=control_plane.app),
        ),
    )


def use_down_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every call to the vendor service fail like a dead host."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        worker.lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL, timeout=0.1, transport=UnreachableTransport()
        ),
    )


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


def credential_file() -> Path:
    return settings.cp_activation_token_file


def activate(
    session: AsyncSession,
    control_plane: FakeControlPlane,
    *,
    tier: str | None = "standard",
) -> None:
    """Leave the deployment as a completed activation does: credential + lease.

    The 0600 file and the cached lease are the CP1b slice's own shapes (its
    suite covers how they get written); this one starts from them so its
    subject — the worker — is what it exercises. ``tier=None`` seeds no lease,
    which is the activated-with-nothing-cached state: Free, fail closed.
    """
    now = datetime.now(UTC)
    control_plane.tokens[TOKEN] = now + timedelta(days=90)
    ActivationStore(credential_file()).save(
        ActivationState(
            deployment_fingerprint="command-worker-test-deployment",
            credential=ActivationCredential(
                token=TOKEN,
                token_id=uuid4(),
                control_plane_tenant_id=control_plane.tenant_id,
                expires_at=now + timedelta(days=90),
            ),
        )
    )
    if tier is not None:
        session.add(
            LeaseCache(
                control_plane_tenant_id=control_plane.tenant_id,
                tier=tier,
                license_expires_at=now + timedelta(days=30),
                entitlement_version=1,
                fetched_at=now,
                last_attempt_at=now,
            )
        )


async def seed_company(session: AsyncSession, tenant: Tenant) -> Company:
    company = Company(
        tenant_id=tenant.id,
        br_number=f"{uuid4().int % 10**8:08d}",
        date_of_incorporation=date(2020, 1, 2),
        anniversary_date=date(2026, 1, 2),
        type="private",
    )
    session.add(company)
    await session.flush()
    return company


async def cover(
    session: AsyncSession, tenant: Tenant, company_id: UUID, *, services: list[str]
) -> None:
    package = await service_packages.create_package(
        session,
        tenant_id=tenant.id,
        actor_user_id=None,
        name=f"pkg-{uuid4()}",
        included_services=services,
        covered_forms=["NAR1", "ND2A"],
    )
    await service_packages.create_assignment(
        session,
        tenant_id=tenant.id,
        company_id=company_id,
        package_id=package.id,
        actor_user_id=None,
        coverage_start=COVERAGE_START,
        coverage_end=COVERAGE_END,
    )


def run_cycle(client: TestClient) -> dict[str, object]:
    response = client.post(RUN_URL)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def inbox_rows(session: AsyncSession) -> list[CommandInbox]:
    rows = await session.scalars(select(CommandInbox).order_by(CommandInbox.received_at))
    return list(rows.all())


async def audit_actions(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(AuditLog.action).order_by(AuditLog.seq))).all())


def spy_handlers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the registry with one that records what it was asked to run."""
    called: list[str] = []

    async def handler(*, envelope, session, tenant_id, now):  # type: ignore[no-untyped-def]
        called.append(envelope.command)
        return worker.CommandRun(telemetry={"app_version": "test"})

    monkeypatch.setattr(worker, "HANDLERS", {"ping": handler})
    return called


# --- The round trip -------------------------------------------------------


async def test_a_cycle_executes_a_ping_and_reports_it_with_metadata_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    queued = control_plane.enqueue()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["received"] == 1
    assert report["executed"] == 1
    assert report["reported"] == 1
    assert report["poll_error"] is None and report["report_error"] is None
    assert report["outcomes"] == [
        {
            "command_id": queued["command_id"],
            "command": "ping",
            "status": "succeeded",
            "error_code": None,
        }
    ]

    # The vendor heard the outcome, and nothing but deployment metadata.
    assert len(control_plane.results) == 1
    reported = control_plane.results[0]
    assert reported["command_id"] == queued["command_id"]
    assert reported["status"] == "succeeded"
    assert reported["error_code"] is None
    assert set(reported["telemetry"]) == {
        "app_version",
        "uptime_seconds",
        "clock",
        "duration_ms",
        "queue_depth",
    }
    assert reported["telemetry"]["queue_depth"] == 1
    assert reported["telemetry"]["uptime_seconds"] >= 0
    # The version the worker reported with the poll is what the vendor needed.
    assert control_plane.reported_versions == [1]

    # The deployment's own record: the envelope as received, the result as
    # reported, and the audit entry — all three written before any report.
    rows = await inbox_rows(session)
    assert len(rows) == 1
    stored = rows[0]
    assert stored.command_id == UUID(str(queued["command_id"]))
    assert stored.status == "succeeded"
    assert stored.reported_at is not None and stored.completed_at is not None
    assert command_violations(stored.envelope) == []
    assert result_violations(stored.result or {}) == []

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_EXECUTED.value)
    )
    assert entry is not None
    assert entry.tenant_id == tenant.id
    assert entry.entity_id == stored.command_id
    assert entry.details["command"] == "ping"
    assert entry.details["status"] == "succeeded"
    # The audit entry records the outcome, never the envelope's parameters.
    assert "params" not in entry.details and "local_record_refs" not in str(entry.details)

    # A second cycle has nothing left to do.
    idle = run_cycle(client)
    assert (idle["received"], idle["executed"], idle["reported"]) == (0, 0, 0)


async def test_the_list_endpoint_shows_the_outcome_and_its_telemetry(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    queued = control_plane.enqueue()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    run_cycle(client)

    listed = client.get(LIST_URL)

    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["command_id"] == queued["command_id"]
    assert rows[0]["command"] == "ping"
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["reported_at"] is not None
    assert rows[0]["telemetry"]["queue_depth"] == 1
    # Nothing about the envelope's parameters is exposed by this view.
    assert "params" not in rows[0] and "envelope" not in rows[0]


# --- Refusals that never execute ------------------------------------------


async def test_an_unknown_command_is_rejected_and_never_executed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue(command="deliver_pack")
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["outcomes"][0]["error_code"] == CommandErrorCode.UNKNOWN_COMMAND.value
    assert report["outcomes"][0]["status"] == CommandStatus.REJECTED.value
    assert called == []  # nothing in the registry was asked to run
    assert (await inbox_rows(session))[0].status == "rejected"


async def test_an_unknown_envelope_version_is_refused_without_execution(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(command_envelope_version=2)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue(note="new in v2")
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["received"] == 1  # recorded, so the refusal can be answered
    outcome = report["outcomes"][0]
    assert outcome["error_code"] == CommandErrorCode.UNSUPPORTED_ENVELOPE_VERSION.value
    assert called == []
    assert control_plane.results[0]["error_code"] == "unsupported_envelope_version"


async def test_a_malformed_envelope_is_refused_locally(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue(
        params={"local_record_refs": {"company_ref": "Chan Tai Man Limited"}}
    )
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["outcomes"][0]["error_code"] == CommandErrorCode.LOCAL_VALIDATION_FAILED.value
    assert called == []
    # The refusal names the field path locally; nothing about the value is
    # sent back or stored in the audit entry.
    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_REJECTED.value)
    )
    assert entry is not None
    assert "Chan Tai Man" not in str(entry.details)


async def test_a_document_without_a_command_id_is_dropped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    unreadable = control_plane.enqueue()
    unreadable["command_id"] = "the-one-from-tuesday"
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    # There is nothing to record it under and nothing to answer against, so
    # the cycle counts it and moves on — and says so.
    assert (report["received"], report["dropped"], report["executed"]) == (0, 1, 0)
    assert called == []
    assert await inbox_rows(session) == []
    assert await audit_actions(session) == []


async def test_the_worker_refuses_execution_when_the_licence_lacks_orchestration(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(tier="free")
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane, tier=None)  # activated, nothing cached: Free
    control_plane.enqueue()
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["outcomes"][0]["error_code"] == CommandErrorCode.ENTITLEMENT_DENIED.value
    assert called == []
    assert control_plane.results[0]["error_code"] == "entitlement_denied"
    # ADR-0012's safety line: a lapsed licence stops orchestration and nothing
    # else — the ungated paths answer exactly as before.
    assert client.get("/api/v1/companies").status_code == 200
    assert client.get("/api/v1/entitlement").json()["capabilities"] == []


async def test_a_company_scoped_command_is_gated_on_the_company(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    manual_company = await seed_company(session, tenant)
    covered_company = await seed_company(session, tenant)
    # A package that leaves workflow orchestration manual: leg c denies for
    # the company it is assigned to, and only for that company.
    await cover(
        session,
        tenant,
        manual_company.id,
        services=[AutomationType.DOCUMENT_EXTRACTION.value],
    )
    await cover(
        session,
        tenant,
        covered_company.id,
        services=[AutomationType.WORKFLOW_ORCHESTRATION.value],
    )
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    control_plane.enqueue(
        params={"local_record_refs": {"company_ref": str(manual_company.id)}}
    )
    denied = run_cycle(client)

    assert denied["outcomes"][0]["error_code"] == CommandErrorCode.ENTITLEMENT_DENIED.value
    assert called == []

    # The same envelope shape for a company whose package covers it goes
    # through the same gate and runs.
    control_plane.enqueue(
        params={"local_record_refs": {"company_ref": str(covered_company.id)}}
    )
    allowed = run_cycle(client)

    assert allowed["outcomes"][0]["status"] == CommandStatus.SUCCEEDED.value
    assert called == ["ping"]


async def test_a_reference_to_a_company_that_does_not_resolve_is_stale_state(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue(params={"local_record_refs": {"company_ref": str(uuid4())}})
    called = spy_handlers(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["outcomes"][0]["error_code"] == CommandErrorCode.STALE_STATE.value
    assert called == []


async def test_nothing_is_executed_when_the_deployment_has_no_tenant_to_audit_under(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue()
    called = spy_handlers(monkeypatch)

    # Driven at the cycle's own seam: the trigger endpoint is
    # Administrator-only, and an Administrator is a member of a tenant, so no
    # request can reach this state — which is exactly why it is checked here.
    report = await worker.run_cycle(session)

    # No tenant means no audit chain to record the outcome in, and an
    # execution nobody can attribute is not permitted.
    assert report.received == 1
    assert report.executed == 0
    assert report.execution_error == worker.NO_LOCAL_TENANT
    assert called == []
    assert (await inbox_rows(session))[0].status is None
    assert await audit_actions(session) == []


async def test_nothing_is_executed_when_the_deployment_has_several_tenants(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_tenant_with_admin(session)
    await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue()

    report = await worker.run_cycle(session)

    # A multi-tenant database is not what ADR-0002 ships; rather than pick a
    # chain to write to, the cycle refuses to guess.
    assert report.executed == 0
    assert report.execution_error == worker.LOCAL_TENANT_AMBIGUOUS
    assert await audit_actions(session) == []


async def test_an_unactivated_deployment_polls_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    control_plane.enqueue()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["activated"] is False
    assert report["poll_error"] == worker.NOT_ACTIVATED
    assert control_plane.command_polls == 0
    assert await inbox_rows(session) == []


# --- Re-delivery and offline operation ------------------------------------


async def test_a_redelivered_envelope_is_executed_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    queued = control_plane.enqueue()
    # The same envelope twice in one poll (and the fake re-delivers it until a
    # result arrives, exactly as the real service does).
    control_plane.commands.append(dict(queued))
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    first = run_cycle(client)
    second = run_cycle(client)  # the envelope is still unanswered at the fake

    assert first["received"] == 1
    assert second["received"] == 0  # already recorded: re-delivery is a no-op
    assert (await audit_actions(session)).count(AuditAction.COMMAND_EXECUTED.value) == 1
    assert len(await inbox_rows(session)) == 1


async def test_an_outcome_survives_a_failed_report_and_is_sent_once(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue()
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    # The command runs, but the vendor never hears about it.
    control_plane.result_status = 503
    offline = run_cycle(client)

    assert (offline["received"], offline["executed"], offline["reported"]) == (1, 1, 0)
    assert offline["report_error"] is not None
    stored = (await inbox_rows(session))[0]
    assert stored.status == "succeeded"  # executed and audited regardless
    assert stored.reported_at is None
    executed = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_EXECUTED.value)
    )
    assert executed is not None  # the audit entry is local, report or no report
    assert control_plane.results == []

    # The vendor comes back: the next cycle reports the retained outcome and
    # does not execute the command again.
    control_plane.result_status = 200
    recovered = run_cycle(client)

    assert recovered["received"] == 0  # the row was already recorded
    assert (recovered["executed"], recovered["reported"]) == (0, 1)
    assert (await inbox_rows(session))[0].reported_at is not None

    # Exactly once: a third cycle has nothing to re-send.
    settled = run_cycle(client)
    assert settled["reported"] == 0
    assert len(control_plane.results) == 1
    assert (await audit_actions(session)).count(AuditAction.COMMAND_EXECUTED.value) == 1


async def test_a_cycle_that_cannot_reach_the_vendor_changes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    control_plane.enqueue()
    use_down_control_plane(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    report = run_cycle(client)

    assert report["poll_error"] == "control_plane_unreachable"
    assert await inbox_rows(session) == []
    assert await audit_actions(session) == []
    # The ungated product does not notice a vendor outage.
    assert client.get("/api/v1/companies").status_code == 200
