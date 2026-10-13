"""The integrity-verification loop: cadence, backoff, opt-in, the tick's audit entry.

The loop is the piece of the integrity job that runs with no request behind
it, so its rules are checked without a database: a failing cycle is retried
on a growing delay and never ends the loop, the loop stops when its task is
cancelled, a deployment without exactly one tenant checks nothing, the app
only starts the loop when the deployment has opted in, and a completed tick
writes the one ``integrity_check`` audit entry with a system actor (NULL) —
the same single entry the Administrator route records, the route's precedent.

The cycle itself is stubbed — the DB suite owns what a check does — and so is
the session factory, because this file is about control flow.
"""

import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.integrity import IntegrityResult, audit_details, classify
from app.main import create_app
from app.workers import integrity as loop

TENANT_ID = "5b1f0e6a-0000-0000-0000-000000000002"


def result() -> IntegrityResult:
    return classify(True, 0, [])


class _FakeStore:
    """The one thing the loop does with the store: pass it to the check."""


async def test_the_loop_runs_its_cycles_and_ends_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_check(
        session: object, store: object, **kwargs: object
    ) -> IntegrityResult:
        checks.append(1)
        return result()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(loop.audit, "record_event", _no_audit)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=3)

    assert len(checks) == 3


async def test_a_completed_tick_writes_the_one_audit_entry_with_a_system_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[dict[str, object]] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_check(
        session: object, store: object, **kwargs: object
    ) -> IntegrityResult:
        return result()

    async def fake_record(*args: object, **kwargs: object) -> None:
        written.append(kwargs)

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(loop.audit, "record_event", fake_record)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    # One entry per completed tick — and the actor is NULL: the system ran
    # the check, not a person.
    assert len(written) == 2
    for entry in written:
        assert entry["action"] is AuditAction.INTEGRITY_CHECK
        assert entry["actor_user_id"] is None
        assert entry["tenant_id"] == TENANT_ID
        assert entry["details"] == audit_details(result())


async def test_a_failing_cycle_neither_ends_the_loop_nor_stops_it_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def broken_check(
        session: object, store: object, **kwargs: object
    ) -> IntegrityResult:
        attempts.append(1)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", broken_check)
    monkeypatch.setattr(loop.audit, "record_event", _no_audit)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert len(attempts) == 2


async def test_a_deployment_without_one_tenant_checks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninitialised deployment has nothing to verify, and no crash either."""
    checks: list[int] = []

    async def no_tenant(session: object) -> None:
        return None

    async def fake_check(
        session: object, store: object, **kwargs: object
    ) -> IntegrityResult:
        checks.append(1)
        return result()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", no_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(loop.audit, "record_event", _no_audit)

    await loop.run_forever(store=_FakeStore(), interval_seconds=0.0, max_cycles=2)

    assert checks == []


async def test_the_check_gets_the_store_the_tenant_and_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    store = _FakeStore()

    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_check(
        session: object, got_store: object, *, tenant_id: object, now: object
    ) -> IntegrityResult:
        seen["session"] = session
        seen["store"] = got_store
        seen["tenant_id"] = tenant_id
        seen["now"] = now
        return result()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(loop.audit, "record_event", _no_audit)

    await loop.run_forever(store=store, interval_seconds=0.0, max_cycles=1)

    assert seen["store"] is store
    assert seen["tenant_id"] == TENANT_ID
    assert isinstance(seen["now"], datetime)
    assert seen["session"] is not None


async def test_the_loop_stops_when_its_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_tenant(session: object) -> object:
        return TENANT_ID

    async def fake_check(
        session: object, store: object, **kwargs: object
    ) -> IntegrityResult:
        return result()

    monkeypatch.setattr(loop, "session_factory", _FakeSessionFactory())
    monkeypatch.setattr(loop.mail_send, "resolve_local_tenant", fake_tenant)
    monkeypatch.setattr(loop.integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(loop.audit, "record_event", _no_audit)

    task = asyncio.create_task(loop.run_forever(store=_FakeStore(), interval_seconds=30.0))
    await asyncio.sleep(0)  # let the first cycle start
    await loop.stop_integrity_worker(task)

    assert task.done()


@pytest.mark.parametrize("enabled", [False, True])
def test_the_app_starts_the_worker_only_when_the_deployment_opts_in(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    started: list[int] = []

    def fake_start(store: object) -> asyncio.Task[None]:
        started.append(1)
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(loop, "start_integrity_worker", fake_start)
    monkeypatch.setattr(settings, "integrity_check_enabled", enabled)
    # The lifespan hands the worker the process-wide storage client; building
    # one here would reach for the deployment's bucket configuration.
    monkeypatch.setattr("app.main.get_storage", lambda: _FakeStore())

    with TestClient(create_app()):
        pass  # the lifespan runs, and cleans its task up on the way out

    assert started == ([1] if enabled else [])


async def _no_audit(*args: object, **kwargs: object) -> None:
    return None


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
