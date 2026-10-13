"""The send loop: one cycle per interval, quietly, forever.

The loop is a background task started from the app lifespan and only when the
deployment opts in (``AICS_MAIL_SEND_ENABLED``, off by default). A deployment
that runs no loop still has the synchronous triggers —
``POST /api/v1/mail/send`` delivers the queue, and
``POST /companies/{id}/confirmation-requests/{id}/send`` releases and delivers
one request — so **automatic sending is a deployment choice**: a draft the
generator releases under the tenant's ``automatic`` mode waits for a cycle, and
the cycle is either this loop or an administrator pressing the route.

It is the mail-ingestion loop with a different cycle behind it, and it keeps
the same two rules (the command worker's ``backoff_delay`` is imported rather
than copied — a second scheduler would be a second thing to get wrong):

- **each cycle gets its own session**, and the cycle itself commits per row, so
  a client's letter that has gone out is recorded even if the next row's
  provider call fails (the sender's own docstring states why that is the one
  departure from "one cycle, one transaction");
- **nothing escapes a cycle**. A failure — an unreachable provider, a database
  hiccup, a bug — is the cycle's own business, and the loop backs off before
  trying again: exponentially up to a cap, so a dead grant costs one attempt
  every few minutes instead of one per interval forever.

The tenant is the deployment's own single one (ADR-0002); a deployment without
exactly one tenant row delivers nothing and the cycle is a no-op, which is what
an uninitialised deployment should do. The store is passed in by the lifespan,
which owns the process-wide client.

Nothing here logs: the audit trail is where a deployment's record lives, and a
log line is exactly the place a message body must never appear.
"""

import asyncio
import contextlib

from app.adapters.persistence.session import session_factory
from app.application import mail_send
from app.application.documents import DocumentStore
from app.core.config import settings
from app.workers.commands import backoff_delay


def start_mail_send_worker(store: DocumentStore) -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    return asyncio.create_task(
        run_forever(store=store, interval_seconds=settings.mail_send_interval_seconds)
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
                tenant_id = await mail_send.resolve_local_tenant(session)
                if tenant_id is not None:
                    await mail_send.run_cycle(session, store, tenant_id=tenant_id)
                await session.commit()
            consecutive_failures = 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)
        if delay > 0:
            await asyncio.sleep(delay)


async def stop_mail_send_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and wait for it, swallowing the cancellation itself."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
