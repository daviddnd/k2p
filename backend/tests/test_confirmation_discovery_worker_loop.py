"""The confirmation-discovery loop: cadence, backoff, opt-in, shutdown.

The loop is the piece of the discovery slice that runs with no request behind
it, so its rules are checked without a database: a failing cycle is retried
on a growing delay and never ends the loop, the loop stops when its task is
cancelled, a deployment without exactly one tenant sweeps nothing, and the
app only starts the loop when the deployment has opted in. The cycle itself
is stubbed — the DB suite owns what a sweep does — and so is the session
factory, because this file is about control flow.

The one thing this file pins about the *product*: the loop runs the same
sweep ``POST /api/v1/mail/confirmation-discovery`` runs, so a deployment that
wants discovery with no staff action enables it (or presses the route).
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.application.confirmation_discovery import DiscoveryCycleReport
from app.core.config import settings
from app.main import create_app
from app.workers import confirmation_discovery as loop

TENANT_ID = "5b1f0e6a-0000-0000-0000-000000000001"


def report() -> DiscoveryCycleReport:
    return DiscoveryCycleReport(now=datetime.now(UTC), outcomes=())


class _FakeStore:
    """The one thing the loop does with the store: pass it to the sweep."""


async def test_the_loop_runs_its_cycles_and_ends_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycles: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_sweep(
        session: object, store: object, scanner: object, **kwargs: object
    ) -> DiscoveryCycleReport:
        cycles.append(1)
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.confirmation_discovery, "sweep", fake_sweep)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=3)

    assert len(cycles) == 3


async def test_a_failing_cycle_neither_ends_the_loop_nor_stops_it_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def broken_sweep(
        session: object, store: object, scanner: object, **kwargs: object
    ) -> DiscoveryCycleReport:
        attempts.append(1)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.confirmation_discovery, "sweep", broken_sweep)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_a_deployment_without_one_tenant_sweeps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninitialised deployment has nothing to discover, and no crash either."""
    cycles: list[int] = []

    async def no_tenant(session: object) -> None:
        return None

    async def fake_sweep(
        session: object, store: object, scanner: object, **kwargs: object
    ) -> DiscoveryCycleReport:
        cycles.append(1)
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", no_tenant)
    monkeypatch.setattr(loop.confirmation_discovery, "sweep", fake_sweep)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert cycles == []


async def test_the_sweep_gets_the_store_a_scanner_the_tenant_and_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    store = _FakeStore()

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_sweep(
        session: object, got_store: object, scanner: object, *, tenant_id: object
    ) -> DiscoveryCycleReport:
        seen["session"] = session
        seen["store"] = got_store
        seen["scanner"] = scanner
        seen["tenant_id"] = tenant_id
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.confirmation_discovery, "sweep", fake_sweep)

    await loop.run_forever(store=store, interval_seconds=0.0, max_cycles=1)

    assert seen["store"] is store
    assert seen["scanner"] is not None
    assert seen["tenant_id"] == TENANT_ID
    assert seen["session"] is not None


async def test_the_loop_stops_when_its_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_sweep(
        session: object, store: object, scanner: object, **kwargs: object
    ) -> DiscoveryCycleReport:
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.confirmation_discovery, "sweep", fake_sweep)

    task = asyncio.create_task(
        loop.run_forever(store=_FakeStore(), interval_seconds=30.0)
    )
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_confirmation_discovery_worker(task)

    assert task.done()


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[int] = []

    def fake_start(store: object) -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_confirmation_discovery_worker", fake_start)
    monkeypatch.setattr(settings, "mail_confirmation_discovery_enabled", enabled)
    # The lifespan hands the worker the process-wide storage client; building
    # one here would reach for the deployment's bucket configuration.
    monkeypatch.setattr("app.main.get_storage", lambda: _FakeStore())

    with TestClient(create_app()):
        pass  # the lifespan runs, and cleans its task up on the way out

    assert started == ([1] if enabled else [])


class _FakeSessionFactory:
    """The one thing the loop does with a session: open it and commit it."""

    def __call__(self) -> "_FakeSession":
        return _FakeSession()


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None
