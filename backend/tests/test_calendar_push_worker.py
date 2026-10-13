"""The calendar-push worker's loop: triggers, cadence, honest failure, opt-in.

The loop is the transport piece that runs with no request behind it, so its
rules are checked without a database: a queued write-time trigger wakes the
loop early, the interval is the reconciliation backstop, a failed cycle —
including a provider's typed auth/transient failure — is the cycle's own
business and never escapes, and the app only starts the loop when the
deployment opts in. The cycle itself is stubbed; Task 1's suite owns what a
sweep does.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.calendar.errors import CalendarProviderUnavailable
from app.application import calendar_sync
from app.core.config import settings
from app.main import create_app
from app.workers import calendar_push as loop


@pytest.fixture
def queue() -> asyncio.Queue:
    trigger_queue = loop.calendar_sync.begin_push_triggers()
    yield trigger_queue
    loop.calendar_sync.end_push_triggers()


def test_a_write_seam_enqueues_only_while_the_loop_runs(queue: asyncio.Queue) -> None:
    loop.calendar_sync.request_push_sweep(uuid4())
    assert queue.qsize() == 1
    queued = queue.get_nowait()
    assert isinstance(queued, type(uuid4()))

    loop.calendar_sync.end_push_triggers()
    # With no loop running the trigger is a no-op, not an error — every tier
    # keeps the feed, which never needed a loop.
    loop.calendar_sync.request_push_sweep(uuid4())


async def test_a_queued_trigger_wakes_the_loop_before_the_interval(
    monkeypatch: pytest.MonkeyPatch, queue: asyncio.Queue
) -> None:
    swept: list[object] = []
    tenant_id = uuid4()

    async def fake_sweep(session: object, *, tenant_id: object, as_of: object) -> object:
        swept.append(tenant_id)
        return None

    async def fake_resolve(session: object) -> object:
        return tenant_id

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_resolve)
    monkeypatch.setattr(calendar_sync, "sweep_tenant_pushes", fake_sweep)
    queue.put_nowait(uuid4())

    # The interval is far away; the queued trigger must carry the cycle.
    await loop.run_forever(queue=queue, interval_seconds=3600.0, max_cycles=1)

    assert swept == [tenant_id]


async def test_the_interval_fires_the_backstop_when_nothing_is_queued(
    monkeypatch: pytest.MonkeyPatch, queue: asyncio.Queue
) -> None:
    cycles: list[object] = []
    tenant_id = uuid4()

    async def fake_sweep(session: object, *, tenant_id: object, as_of: object) -> object:
        cycles.append(1)
        return None

    async def fake_resolve(session: object) -> object:
        return tenant_id

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_resolve)
    monkeypatch.setattr(calendar_sync, "sweep_tenant_pushes", fake_sweep)

    await loop.run_forever(queue=queue, interval_seconds=0.0, max_cycles=2)

    assert len(cycles) == 2


async def test_a_provider_failure_is_the_cycles_own_business(
    monkeypatch: pytest.MonkeyPatch, queue: asyncio.Queue
) -> None:
    attempts: list[object] = []

    async def broken_sweep(session: object, *, tenant_id: object, as_of: object) -> object:
        attempts.append(1)
        raise CalendarProviderUnavailable("http_500", status=500)

    async def fake_resolve(session: object) -> object:
        return uuid4()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_resolve)
    monkeypatch.setattr(calendar_sync, "sweep_tenant_pushes", broken_sweep)

    # The typed provider failure never leaves the loop; the cycles complete.
    await loop.run_forever(queue=queue, interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_the_loop_stops_when_its_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, queue: asyncio.Queue
) -> None:
    async def fake_sweep(session: object, *, tenant_id: object, as_of: object) -> object:
        return None

    async def fake_resolve(session: object) -> object:
        return None

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_resolve)
    monkeypatch.setattr(calendar_sync, "sweep_tenant_pushes", fake_sweep)

    task = asyncio.create_task(loop.run_forever(queue=queue, interval_seconds=30.0))
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_calendar_push_worker(task)

    assert task.done()
    assert loop.calendar_sync._push_triggers is None  # the triggers close with the loop


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[object] = []

    def fake_start() -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_calendar_push_worker", fake_start)
    monkeypatch.setattr(settings, "calendar_push_enabled", enabled)

    with TestClient(create_app()):
        pass  # the lifespan runs, and cleans its task up on the way out

    assert started == ([1] if enabled else [])


class _FakeSessionFactory:
    """The one things the loop does with a session: open it and commit it."""

    def __call__(self) -> "_FakeSession":
        return _FakeSession()


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None
