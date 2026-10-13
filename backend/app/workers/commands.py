"""The command worker's loop: poll, execute, report — quietly, forever.

The loop is a background task started from the app lifespan and only when the
deployment opts in (``AICS_COMMAND_WORKER_ENABLED``, off by default): a
deployment that runs no loop still has the synchronous trigger
(``POST /api/v1/commands/run``), and nothing in this module is on the path of
any request.

Two rules make a background loop safe to leave running:

- **each cycle gets its own session**, committed when the cycle returns, so
  the loop outlives any request and never holds one open;
- **nothing escapes a cycle**. A failure — an unreachable vendor, a database
  hiccup, a bug — is the cycle's own business (its report carries it), and
  the loop backs off before trying again: exponentially up to a cap, so a
  vendor outage costs one attempt every few minutes instead of one per
  interval forever.

Nothing here logs: the audit trail is where a deployment's record lives, and
a log line is exactly the place a payload or a credential must never appear.
"""

import asyncio
import contextlib

from app.adapters.persistence.session import session_factory
from app.application import commands
from app.core.config import settings

# The backoff cap: a deployment whose vendor is unreachable should keep
# trying, but slowly enough that the attempts are invisible.
MAX_BACKOFF_SECONDS = 600.0


def backoff_delay(interval_seconds: float, consecutive_failures: int) -> float:
    """How long to wait after ``consecutive_failures`` failed cycles.

    One interval when nothing failed; doubling per consecutive failure up to
    ``MAX_BACKOFF_SECONDS`` (and never below the configured interval — the
    interval is the deployment's choice of cadence).
    """
    if consecutive_failures <= 0:
        return interval_seconds
    capped = min(interval_seconds * (2.0**consecutive_failures), MAX_BACKOFF_SECONDS)
    return max(interval_seconds, capped)


def failed(report: commands.CycleReport) -> bool:
    """Whether a cycle could not do its work (the backoff trigger).

    Polling a control plane that is not configured or not activated is the
    expected state of an unconfigured deployment, not a failure — but it is
    still not "work done", so it backs off like one and keeps the loop cheap.
    """
    return (
        report.poll_error is not None
        or report.report_error is not None
        or report.execution_error is not None
    )


def start_command_worker() -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    return asyncio.create_task(
        run_forever(interval_seconds=settings.command_worker_interval_seconds)
    )


async def run_forever(*, interval_seconds: float, max_cycles: int | None = None) -> None:
    """One cycle per interval, backing off while cycles keep failing.

    ``max_cycles`` exists for tests, which need the loop to end; production
    passes nothing and runs until the task is cancelled at shutdown.
    """
    consecutive_failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            async with session_factory() as session:
                report = await commands.run_cycle(session)
                await session.commit()
            consecutive_failures = consecutive_failures + 1 if failed(report) else 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)
        if delay > 0:
            await asyncio.sleep(delay)


async def stop_command_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and wait for it, swallowing the cancellation itself."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
