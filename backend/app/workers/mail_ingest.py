"""The mail-ingestion loop: one cycle per interval, quietly, forever.

The loop is a background task started from the app lifespan and only when the
deployment opts in (``AICS_MAIL_INGEST_ENABLED``, off by default): a
deployment that runs no loop still has the synchronous trigger
(``POST /api/v1/mail/ingest``), and nothing in this module is on the path of
any request.

It is the command worker's loop with a different cycle behind it, and it keeps
the same two rules (that module's ``backoff_delay`` is imported rather than
copied — a second scheduler would be a second thing to get wrong):

- **each cycle gets its own session**, committed when the cycle returns, so
  the loop outlives any request and never holds one open;
- **nothing escapes a cycle**. A failure — an unreachable mailbox, a database
  hiccup, a bug — is the cycle's own business, and the loop backs off before
  trying again: exponentially up to a cap, so a dead grant costs one attempt
  every few minutes instead of one per interval forever.

The scanner is built through the application seam on every cycle, so a
deployment whose scanner comes back recovers without a restart; the store is
passed in by the lifespan, which owns the process-wide client.

Nothing here logs: the audit trail is where a deployment's record lives, and
a log line is exactly the place a message body must never appear.
"""

import asyncio
import contextlib

from app.adapters.persistence.session import session_factory
from app.application import mail_ingest, scanning
from app.application.documents import DocumentStore
from app.core.config import settings
from app.workers.commands import backoff_delay


def start_mail_ingest_worker(store: DocumentStore) -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    return asyncio.create_task(
        run_forever(store=store, interval_seconds=settings.mail_ingest_interval_seconds)
    )


async def run_forever(
    *,
    store: DocumentStore,
    interval_seconds: float,
    max_cycles: int | None = None,
) -> None:
    """One cycle per interval, backing off while cycles keep failing.

    ``max_cycles`` exists for tests, which need the loop to end; production
    passes nothing and runs until the task is cancelled at shutdown.
    """
    consecutive_failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            async with session_factory() as session:
                await mail_ingest.run_cycle(
                    session, store=store, scanner=scanning.scanner()
                )
                await session.commit()
            consecutive_failures = 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)
        if delay > 0:
            await asyncio.sleep(delay)


async def stop_mail_ingest_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and wait for it, swallowing the cancellation itself."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
