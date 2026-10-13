"""The reply-parser loop: one parse cycle per interval, quietly, forever.

The loop is a background task started from the app lifespan and only when the
deployment opts in (``AICS_MAIL_PARSE_REPLIES_ENABLED``, off by default). A
deployment that runs no loop still has the synchronous trigger —
``POST /api/v1/mail/parse-replies`` runs one cycle on demand — so parsing
client replies without a staff action is a deployment choice, and one that
leaves the loop off loses nothing the route cannot do.

It is the reminder loop with a different cycle behind it, and it keeps the
same two rules (the command worker's ``backoff_delay`` is imported rather
than copied — a second scheduler would be a second thing to get wrong):

- **each cycle gets its own session**, committed when the cycle returns, so
  the loop outlives any request and never holds one open;
- **nothing escapes a cycle**. A failure — a database hiccup, a gateway
  outage's aftermath, a bug — is the cycle's own business, and the loop backs
  off before trying again: exponentially up to a cap, so a broken cycle costs
  one attempt every few minutes instead of one per interval forever. (The
  cycle itself never raises for one message: refusals, denials and letter
  failures are per-message outcomes, and this loop is the third thing that
  says so.)

The tenant is the deployment's own single one (ADR-0002), resolved the same
way the send and reminder loops resolve it; a deployment without exactly one
tenant row parses nothing and the cycle is a no-op, which is what an
uninitialised deployment should do. The scanner is built through the
application seam on every cycle, so a deployment whose scanner comes back
recovers without a restart; the store is passed in by the lifespan, which
owns the process-wide client. The cycle runs ``as_of`` the loop's own day —
the gate and the date rules read the day a cycle runs, exactly as the route's
cycle reads the day it is pressed.

Nothing here logs: the audit trail is where a deployment's record lives, and
a log line is exactly the place a reply's words must never appear.
"""

import asyncio
import contextlib
from datetime import date

from app.adapters.persistence.session import session_factory
from app.application import mail_send, reply_parsing, scanning
from app.application.documents import DocumentStore
from app.core.config import settings
from app.workers.commands import backoff_delay


def start_parse_replies_worker(store: DocumentStore) -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    return asyncio.create_task(
        run_forever(
            store=store, interval_seconds=settings.mail_parse_replies_interval_seconds
        )
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
                    await reply_parsing.run_cycle(
                        session,
                        store=store,
                        scanner=scanning.scanner(),
                        tenant_id=tenant_id,
                        as_of=date.today(),
                    )
                await session.commit()
            consecutive_failures = 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)
        if delay > 0:
            await asyncio.sleep(delay)


async def stop_parse_replies_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and wait for it, swallowing the cancellation itself."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
