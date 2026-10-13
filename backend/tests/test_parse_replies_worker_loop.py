"""The reply-parser loop: cadence, backoff, opt-in, shutdown.

The loop is the piece of the reply-parser slice that runs with no request
behind it, so its rules are checked without a database: a failing cycle is
retried on a growing delay and never ends the loop, the loop stops when its
task is cancelled, a deployment without exactly one tenant parses nothing, and
the app only starts the loop when the deployment has opted in. The cycle
itself is stubbed — the DB suite owns what a parse cycle does — and so is the
session factory, because this file is about control flow.

The one thing this file pins about the *product*: the loop runs the same
``run_cycle`` ``POST /api/v1/mail/parse-replies`` runs — one mechanism, two
entry points — so a deployment that wants replies parsed with no staff action
enables it (or presses the route).
"""

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.application.reply_parsing import ParseCycleReport
from app.core.config import settings
from app.main import create_app
from app.workers import parse_replies as loop

TENANT_ID = "5b1f0e6a-0000-0000-0000-000000000001"


class _FakeStore:
    """The one thing the loop does with the store: pass it to the cycle."""


async def test_the_loop_runs_its_cycles_and_ends_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycles: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_cycle(session: object, **kwargs: object) -> ParseCycleReport:
        cycles.append(1)
        return ParseCycleReport(examined=0, parsed=0, outcomes=())

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.reply_parsing, "run_cycle", fake_cycle)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=3)

    assert len(cycles) == 3


async def test_a_failing_cycle_neither_ends_the_loop_nor_stops_it_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def broken_cycle(session: object, **kwargs: object) -> ParseCycleReport:
        attempts.append(1)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.reply_parsing, "run_cycle", broken_cycle)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_a_deployment_without_one_tenant_parses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninitialised deployment has nothing to parse, and no crash either."""
    cycles: list[int] = []

    async def no_tenant(session: object) -> None:
        return None

    async def fake_cycle(session: object, **kwargs: object) -> ParseCycleReport:
        cycles.append(1)
        return ParseCycleReport(examined=0, parsed=0, outcomes=())

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", no_tenant)
    monkeypatch.setattr(loop.reply_parsing, "run_cycle", fake_cycle)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert cycles == []


async def test_the_cycle_gets_the_store_a_scanner_the_tenant_and_todays_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    store = _FakeStore()

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_cycle(session: object, **kwargs: object) -> ParseCycleReport:
        seen.update(kwargs)
        seen["session"] = session
        return ParseCycleReport(examined=0, parsed=0, outcomes=())

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.reply_parsing, "run_cycle", fake_cycle)

    await loop.run_forever(store=store, interval_seconds=0.0, max_cycles=1)

    assert seen["store"] is store
    assert seen["scanner"] is not None
    assert seen["tenant_id"] == TENANT_ID
    assert seen["as_of"] == date.today()
    assert seen["session"] is not None


async def test_the_loop_stops_when_its_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_cycle(session: object, **kwargs: object) -> ParseCycleReport:
        return ParseCycleReport(examined=0, parsed=0, outcomes=())

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.reply_parsing, "run_cycle", fake_cycle)

    task = asyncio.create_task(loop.run_forever(store=_FakeStore(), interval_seconds=30.0))
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_parse_replies_worker(task)

    assert task.done()


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[int] = []

    def fake_start(store: object) -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_parse_replies_worker", fake_start)
    monkeypatch.setattr(settings, "mail_parse_replies_enabled", enabled)
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
