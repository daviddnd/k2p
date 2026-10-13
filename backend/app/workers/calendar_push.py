"""The calendar-push reconciliation loop: write-time triggers plus the
interval backstop, quietly, forever (slice M8; ruling R4).

The loop is a background task started from the app lifespan and only when
the deployment opts in (``AICS_CALENDAR_PUSH_ENABLED``, off by default). It
is the sibling loops' shape with one addition — an in-process queue the
calendar-relevant write seams (a change event's ``apply``, a statics
correction that moves a due date) feed, so a deadline that moves does not
wait out the interval. A queued id names this deployment's single tenant
(ADR-0002), so the queue is the wake-up call, not a routing table: the
cycle still resolves the tenant the way the send and reminder loops do.

The same two rules hold (the command worker's ``backoff_delay`` is imported
rather than copied — a second scheduler would be a second thing to get
wrong):

- **each cycle gets its own session**, committed when the cycle returns, so
  the loop outlives any request and never holds one open;
- **nothing escapes a cycle**. A provider failure — a refused grant
  (``CalendarAuthFailed``), an outage or 5xx (``CalendarProviderUnavailable``)
  — is booked as the cycle's own failure: the push state stays untouched
  for the next sweep, the loop backs off, and the typed error is never
  raised into the loop. A tenant whose credential is missing or unprobed
  is not a failure at all: the sweep is an honest no-op that leaves the
  tenant on the feed.

Nothing here logs: the audit trail is where a deployment's record lives, and
a log line is exactly the place a credential must never appear.
"""

import asyncio
import contextlib
from datetime import date
from uuid import UUID

from app.adapters.persistence.session import session_factory
from app.application import calendar_sync, mail_send
from app.core.config import settings
from app.workers.commands import backoff_delay


def start_calendar_push_worker() -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    queue = calendar_sync.begin_push_triggers()
    return asyncio.create_task(
        run_forever(queue=queue, interval_seconds=settings.calendar_push_interval_seconds)
    )


async def run_forever(
    *,
    queue: asyncio.Queue[UUID],
    interval_seconds: float,
    max_cycles: int | None = None,
) -> None:
    """One reconciliation per interval, woken early by a queued trigger.

    The wait is the sleep: a queued tenant id wakes the loop for a cycle
    now, and the timeout firing is the interval backstop. After a failed
    cycle the wait grows at ``backoff_delay`` — never faster than the
    interval, never slower than the cap. ``max_cycles`` exists for tests,
    which need the loop to end; production passes nothing and runs until
    the task is cancelled at shutdown.
    """
    consecutive_failures = 0
    cycles = 0
    delay = interval_seconds
    while max_cycles is None or cycles < max_cycles:
        # A queued trigger wakes the loop for a cycle now; the timeout
        # firing is the interval backstop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=delay)
        # Drain the burst behind the wake; every id names the local tenant.
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            async with session_factory() as session:
                tenant_id = await mail_send.resolve_local_tenant(session)
                if tenant_id is not None:
                    await calendar_sync.sweep_tenant_pushes(
                        session, tenant_id=tenant_id, as_of=date.today()
                    )
                await session.commit()
            consecutive_failures = 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)


async def stop_calendar_push_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and close the trigger queue with it.

    Closing the queue matters: the write seams keep calling
    ``request_push_sweep`` after the loop stops, and that must be a no-op,
    not a queue the next loop's start did not expect.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    calendar_sync.end_push_triggers()
