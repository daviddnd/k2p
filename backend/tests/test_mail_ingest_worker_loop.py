"""The mail-ingestion loop: cadence, backoff, opt-in, shutdown.

The loop is the one piece of the ingestion slice that runs with no request
behind it, so its rules are checked without a database: a failing cycle is
retried on a growing delay and never ends the loop, the loop stops when its
task is cancelled, and the app only starts it when the deployment has opted
in. The cycle itself is stubbed — the DB suites own what a cycle does — and so
is the session factory, because this file is about control flow and nothing
the loop touches.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.application.mail_ingest import CycleReport
from app.core.config import settings
from app.main import create_app
from app.workers import mail_ingest as loop


def report() -> CycleReport:
    return CycleReport(cursor_received_at=None, query=None, examined=0, ingested=0)


class _FakeStore:
    """The one thing the loop does with the store: pass it to the cycle."""


async def test_the_loop_runs_its_cycles_and_ends_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycles: list[int] = []

    async def fake_cycle(session: object, **kwargs: object) -> CycleReport:
        cycles.append(1)
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_ingest, "run_cycle", fake_cycle)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=3)

    assert len(cycles) == 3


async def test_a_failing_cycle_neither_ends_the_loop_nor_stops_it_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def broken_cycle(session: object, **kwargs: object) -> CycleReport:
        attempts.append(1)
        raise RuntimeError("the mailbox went away")

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_ingest, "run_cycle", broken_cycle)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_the_loop_stops_when_its_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_cycle(session: object, **kwargs: object) -> CycleReport:
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_ingest, "run_cycle", fake_cycle)

    task = asyncio.create_task(
        loop.run_forever(store=_FakeStore(), interval_seconds=30.0)
    )
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_mail_ingest_worker(task)

    assert task.done()


async def test_the_cycle_gets_the_store_the_scanner_and_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    store = _FakeStore()

    async def fake_cycle(session: object, *, store: object, scanner: object) -> CycleReport:
        seen["session"] = session
        seen["store"] = store
        seen["scanner"] = scanner
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_ingest, "run_cycle", fake_cycle)

    await loop.run_forever(store=store, interval_seconds=0.0, max_cycles=1)

    assert seen["store"] is store
    assert seen["scanner"] is not None
    assert seen["session"] is not None


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[int] = []

    def fake_start(store: object) -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_mail_ingest_worker", fake_start)
    monkeypatch.setattr(settings, "mail_ingest_enabled", enabled)
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
