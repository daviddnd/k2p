"""The exit gate: one command envelope, end to end, with zero PII.

Phase 1's exit gate carried this item (`.agents/plans/2026-09-21-phase-2-
visibility-and-control-plane.md`): **one command envelope executed end-to-end
through the local worker with zero PII transmitted, verified by payload
inspection.** This is that test, and it is the one to run to see the item
pass.

Every part of it is the real thing:

- the vendor service is the real ``control_plane`` application, served by a
  subprocess on a loopback socket (``tests/control_plane_server.py`` — both
  deployables answer to the package name ``app``, so the side that is not
  under test is the one that leaves the process);
- the deployment activates against it for real: this deployable's own
  activation endpoints run the ADR-0016 handshake, with the browser and
  Google played by the test over that same socket;
- the version policy is exercised against the real service in both
  directions — a queue for a deployment that has never reported is refused
  with ``stale_state``, and the worker's poll is what unblocks it;
- the worker is the real worker, run by the real trigger endpoint; and
- the payload inspection is total: the envelope as the vendor's queue holds
  it, the result the vendor recorded, the envelope as the deployment's inbox
  kept it, and the result the deployment reported — all four checked against
  the closed vocabulary in ``app.domain.commands``, which is the mechanical
  form of ADR-0012's no-client-PII rule.

What it does not cover: a hosted vendor deployment and real Google
credentials (ADR-0016's founder prerequisites), which is why the identity
provider here is the fake one the control plane's own suite uses.

Skipped unless both databases are reachable and migrated; see
``tests/control_plane_integration.py``. The vendor service commits for real
(it is a separate process), so its rows are removed again at the end.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.commands import CommandInbox
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.commands import (
    ENVELOPE_VERSION,
    command_violations,
    result_violations,
)
from app.main import create_app
from tests.control_plane_integration import redeem_activation_code
from tests.vendor_harness import VendorService, remove_vendor_rows, vendor_headers

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

RUN_URL = "/api/v1/commands/run"
ACTIVATION_URL = "/api/v1/entitlement/activation"
# The one credential this test hands the vendor service — through the
# environment, so no secret is printed or parsed out of a subprocess.
VENDOR_ADMIN_KEY = "command-channel-vendor-key"


@pytest.fixture
async def session() -> Iterator[AsyncSession]:  # type: ignore[misc]
    """A session inside an outer transaction, rolled back after the test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- The deployment -------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> Iterator[AsyncSession]:  # type: ignore[misc]
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


def activate_deployment(client: TestClient, vendor: VendorService) -> str:
    """Run the real activation handshake; returns the vendor tenant id.

    The browser leg is played here: the deployment's own activation endpoint
    registers the attempt and hands back the vendor URL, this test follows
    the redirect chain through the vendor's authorize endpoint to the fake
    Google and back, and the deployment's callback redeems the one-time code
    with the PKCE verifier that never left it. Nothing is stubbed on either
    side except Google itself.
    """
    started = client.post(ACTIVATION_URL)
    assert started.status_code == 201, started.text
    authorization_url = started.json()["authorization_url"]
    assert authorization_url.startswith(f"{vendor.base_url}/api/v1/authorize")

    with httpx.Client(follow_redirects=False, timeout=10.0) as browser:
        to_google = browser.get(authorization_url)
        assert to_google.status_code == 302, to_google.text
        google = browser.get(to_google.headers["location"])
        assert google.status_code == 302, google.text
        back_to_deployment = browser.get(google.headers["location"])
        assert back_to_deployment.status_code == 302, back_to_deployment.text
        query = parse_qs(urlsplit(back_to_deployment.headers["location"]).query)
        code, state = query["code"][0], query["state"][0]

    redeem_activation_code(client, code, state)

    status = client.get(ACTIVATION_URL).json()
    assert status["activated"] is True
    assert status["cache"]["tier"] == "free"  # a fresh tenant starts Free
    return str(status["control_plane_tenant_id"])


def run_cycle(client: TestClient) -> dict[str, object]:
    response = client.post(RUN_URL)
    assert response.status_code == 200, response.text
    return dict(response.json())


def set_tier(vendor: VendorService, vendor_tenant_id: str, tier: str) -> httpx.Response:
    with httpx.Client(timeout=10.0) as http:
        return http.put(
            f"{vendor.base_url}/api/v1/tenants/{vendor_tenant_id}/entitlement",
            json={"tier": tier},
            headers=vendor_headers(vendor),
        )


def enqueue_ping(vendor: VendorService, vendor_tenant_id: str) -> httpx.Response:
    with httpx.Client(timeout=10.0) as http:
        return http.post(
            f"{vendor.base_url}/api/v1/tenants/{vendor_tenant_id}/commands",
            json={"command": "ping"},
            headers=vendor_headers(vendor),
        )


# --- Reading the vendor's own record --------------------------------------


def read_queue_row(database_url: str, command_id: str) -> dict[str, object]:
    """The queued row, read from the vendor's database.

    Directly, because the service deliberately has no route that reads a
    tenant's queue: the payload inspection needs the stored bytes, and the
    table names are already known to whatever inspects them.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM command_queue WHERE command_id = :id"),
                    {"id": UUID(command_id)},
                )
                .mappings()
                .one()
            )
            return dict(row)
    finally:
        engine.dispose()


def vendor_documents(row: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """The stored row as the two envelopes it holds, in the wire shapes."""
    envelope: dict[str, object] = {
        "envelope_version": row["envelope_version"],
        "command_id": str(row["command_id"]),
        "command": row["command"],
        "params": row["params"],
        "expected_outputs": row["expected_outputs"],
        "issued_at": _isoformat(row["issued_at"]),
    }
    telemetry: dict[str, object] = {}
    # Every stored count column, in leg order: 0007's five and the discovery
    # leg's three (0008). A column the command reported none of stays absent,
    # the presence rule §5.2 records.
    for key in (
        "duration_ms",
        "queue_depth",
        "uptime_seconds",
        "app_version",
        "mail_ingested",
        "replies_parsed",
        "confirmations_generated",
        "confirmations_refused",
        "confirmations_skipped",
        "reminders_enqueued",
        "messages_sent",
        "cycles_failed",
    ):
        if row[key] is not None:
            telemetry[key] = row[key]
    if row["clock"] is not None:
        telemetry["clock"] = _isoformat(row["clock"])
    result: dict[str, object] = {
        "envelope_version": row["reported_envelope_version"],
        "command_id": str(row["command_id"]),
        "status": row["status"],
        "error_code": row["error_code"],
        "telemetry": telemetry,
        "completed_at": _isoformat(row["completed_at"]),
    }
    return envelope, result


def _isoformat(value: object) -> str:
    assert isinstance(value, datetime)  # a stored timestamp column
    return value.isoformat()


def _instant(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _without_timestamps(document: dict[str, object]) -> dict[str, object]:
    """The document without its timestamp fields (compared as instants instead)."""
    return {
        key: value
        for key, value in document.items()
        if key not in {"issued_at", "completed_at"}
    }


# --- The exit gate --------------------------------------------------------


async def test_one_command_envelope_runs_end_to_end_with_zero_pii(
    session: AsyncSession,
    control_plane_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_started_at = datetime.now(UTC) - timedelta(seconds=5)
    with VendorService(control_plane_database, admin_key=VENDOR_ADMIN_KEY) as vendor:
        try:
            tenant, admin = await seed_tenant_with_admin(session)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)
            client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

            vendor_tenant_id = activate_deployment(client, vendor)
            # The back office marks the tenant paid; the deployment picks the
            # tier up on its next lease refresh (ADR-0016).
            assert set_tier(vendor, vendor_tenant_id, "standard").status_code == 200
            refreshed = client.post("/api/v1/entitlement/refresh")
            assert refreshed.status_code == 200
            assert refreshed.json()["tier"] == "standard"
            assert refreshed.json()["refreshed"] is True

            # The version policy, from the vendor's side: a deployment that
            # has never reported cannot be given a command at all.
            early = enqueue_ping(vendor, vendor_tenant_id)
            assert early.status_code == 409, early.text
            assert early.json()["detail"] == "stale_state"

            # Cycle 1: the worker polls, which is how a deployment reports the
            # envelope version it speaks.
            first = run_cycle(client)
            assert first["activated"] is True
            assert first["poll_error"] is None
            assert (first["received"], first["executed"], first["reported"]) == (0, 0, 0)

            queued = enqueue_ping(vendor, vendor_tenant_id)
            assert queued.status_code == 201, queued.text
            envelope = dict(queued.json())
            assert envelope["envelope_version"] == ENVELOPE_VERSION
            assert envelope["params"] == {"local_record_refs": {}}

            # Cycle 2: received, executed, reported — through the real worker.
            second = run_cycle(client)
            assert (second["received"], second["executed"], second["reported"]) == (1, 1, 1)
            assert second["outcomes"] == [
                {
                    "command_id": envelope["command_id"],
                    "command": "ping",
                    "status": "succeeded",
                    "error_code": None,
                }
            ]

            # The deployment's own record: executed and audited locally.
            local = await session.scalar(select(CommandInbox))
            assert local is not None
            assert str(local.command_id) == envelope["command_id"]
            assert local.status == "succeeded" and local.reported_at is not None
            assert local.result is not None
            audit_entry = await session.scalar(
                select(AuditLog).where(AuditLog.action == AuditAction.COMMAND_EXECUTED.value)
            )
            assert audit_entry is not None
            assert audit_entry.entity_id == local.command_id
            assert audit_entry.tenant_id == tenant.id

            # The vendor's record of the same run.
            stored = read_queue_row(control_plane_database, envelope["command_id"])
            assert stored["status"] == "succeeded"
            assert stored["error_code"] is None
            assert stored["dispatched_at"] is not None
            assert stored["reported_envelope_version"] == ENVELOPE_VERSION
            assert stored["app_version"] is not None
            assert stored["duration_ms"] is not None
            vendor_envelope, vendor_result = vendor_documents(stored)
            # What was queued is what was stored. The wire spells UTC as
            # ``Z`` where the column round-trips as ``+00:00`` — the same
            # instant, so the timestamps are compared as instants.
            assert _without_timestamps(vendor_envelope) == _without_timestamps(envelope)
            assert _instant(vendor_envelope["issued_at"]) == _instant(envelope["issued_at"])

            # **The exit gate.** All four envelopes — the vendor's copy of the
            # one it sent and of the result it recorded, the deployment's copy
            # of the one it received and of the result it reported — pass the
            # closed vocabulary. Nothing could have been transmitted that is
            # not an opaque id, a count, a duration, a version, a timestamp,
            # or a command name.
            assert command_violations(vendor_envelope) == []
            assert command_violations(local.envelope) == []
            assert result_violations(vendor_result) == []
            assert result_violations(local.result) == []

            # And the two records are the same report: the vendor holds what
            # the deployment sent, field for field.
            assert vendor_result["status"] == local.result["status"]
            assert _instant(vendor_result["completed_at"]) == _instant(local.result["completed_at"])
            assert vendor_result["telemetry"]["app_version"] == (
                local.result["telemetry"]["app_version"]
            )
            assert vendor_result["telemetry"]["duration_ms"] == (
                local.result["telemetry"]["duration_ms"]
            )

            # A third cycle has nothing left to do, and the vendor does not
            # re-deliver an answered command.
            settled = run_cycle(client)
            assert (settled["received"], settled["executed"], settled["reported"]) == (0, 0, 0)
        finally:
            remove_vendor_rows(control_plane_database, since=run_started_at)
