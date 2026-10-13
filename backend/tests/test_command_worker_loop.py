"""The command worker's loop: cadence, backoff, opt-in, shutdown.

The loop is the one piece of the transport that runs with no request behind
it, so its rules are checked without a database: a failed cycle is retried on
a growing delay (never faster than the deployment's interval, never slower
than the cap), the loop stops when its task is cancelled, and the app only
starts it when the deployment has opted in. The cycle itself is stubbed — the
DB suites own what a cycle does — and so is the session factory, because this
file is about control flow and nothing the loop touches.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.application.commands import CycleReport
from app.core.config import settings
from app.main import create_app
from app.workers import commands as loop


def report(**overrides: object) -> CycleReport:
    fields: dict[str, object] = {
        "activated": True,
        "received": 0,
        "dropped": 0,
        "executed": 0,
        "reported": 0,
    }
    fields.update(overrides)
    return CycleReport(**fields)  # type: ignore[arg-type]


def test_the_backoff_grows_with_failures_and_is_capped() -> None:
    assert loop.backoff_delay(30.0, 0) == 30.0
    assert loop.backoff_delay(30.0, 1) == 60.0
    assert loop.backoff_delay(30.0, 2) == 120.0
    assert loop.backoff_delay(30.0, 10) == loop.MAX_BACKOFF_SECONDS
    # The interval is the deployment's choice of cadence: never shorten it.
    assert loop.backoff_delay(900.0, 10) == 900.0


def test_a_cycle_that_could_not_do_its_work_triggers_the_backoff() -> None:
    assert loop.failed(report()) is False
    assert loop.failed(report(poll_error="control_plane_unreachable")) is True
    assert loop.failed(report(report_error="control_plane_unreachable")) is True
    assert loop.failed(report(execution_error="no_local_tenant")) is True
    # An unconfigured deployment is not an error, but it is also not work
    # done: the loop keeps the attempt cheap rather than spinning.
    assert loop.failed(report(activated=False, poll_error="not_activated")) is True


async def test_the_loop_runs_its_cycles_and_ends_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycles: list[int] = []

    async def fake_cycle(session: object) -> CycleReport:
        cycles.append(1)
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.commands, "run_cycle", fake_cycle)

    await loop.run_forever(interval_seconds=0.0, max_cycles=3)

    assert len(cycles) == 3


async def test_a_failing_cycle_does_not_end_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def broken_cycle(session: object) -> CycleReport:
        attempts.append(1)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.commands, "run_cycle", broken_cycle)

    await loop.run_forever(interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_the_loop_stops_when_its_task_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_cycle(session: object) -> CycleReport:
        return report()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.commands, "run_cycle", fake_cycle)

    task = asyncio.create_task(loop.run_forever(interval_seconds=30.0))
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_command_worker(task)

    assert task.done()


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[int] = []

    def fake_start() -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_command_worker", fake_start)
    monkeypatch.setattr(settings, "command_worker_enabled", enabled)

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
