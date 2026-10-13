"""The integrity-verification loop: one check per interval, quietly, forever.

The loop is a background task started from the app lifespan and only when the
deployment opts in (``AICS_INTEGRITY_CHECK_ENABLED``, off by default). A
deployment that runs no loop still has the synchronous trigger —
``POST /api/v1/admin/integrity-check`` runs one check on demand — so
**continuous verification is a deployment choice**: ADR-0011's post-restore
verification is the same check run once, by hand or by cron, before the
instance returns to service.

It is the reminder loop with a different cycle behind it, and it keeps the
same two rules (the command worker's ``backoff_delay`` is imported rather
than copied — a second scheduler would be a second thing to get wrong):

- **each cycle gets its own session**, committed when the cycle returns, so
  the loop outlives any request and never holds one open;
- **nothing escapes a cycle**. A failure — a database hiccup, a bug — is the
  cycle's own business, and the loop backs off before trying again:
  exponentially up to a cap, so a broken cycle costs one attempt every few
  minutes instead of one per interval forever.

The tenant is the deployment's own single one (ADR-0002), resolved the same
way the send loop resolves it; a deployment without exactly one tenant row
checks nothing and the cycle is a no-op, which is what an uninitialised
deployment should do. On a completed cycle the loop records the same single
``integrity_check`` audit entry the Administrator route records — actor NULL
= system — on the cycle's session, so the entry commits with the cycle.

Nothing here logs: the audit trail is where a deployment's record lives.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

from app.adapters.persistence.session import session_factory
from app.application import audit, integrity, mail_send
from app.application.documents import DocumentStore
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.integrity import audit_details
from app.workers.commands import backoff_delay


def start_integrity_worker(store: DocumentStore) -> asyncio.Task[None]:
    """Start the loop as a background task; the caller owns cancelling it."""
    return asyncio.create_task(
        run_forever(
            store=store,
            interval_seconds=settings.integrity_check_interval_hours * 3600.0,
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
                    result = await integrity.run_integrity_check(
                        session, store, tenant_id=tenant_id, now=datetime.now(UTC)
                    )
                    await audit.record_event(
                        session,
                        tenant_id=tenant_id,
                        action=AuditAction.INTEGRITY_CHECK,
                        actor_user_id=None,  # system
                        details=audit_details(result),
                    )
                await session.commit()
            consecutive_failures = 0
        except Exception:  # a cycle must never end the loop
            consecutive_failures += 1
        cycles += 1
        delay = backoff_delay(interval_seconds, consecutive_failures)
        if delay > 0:
            await asyncio.sleep(delay)


async def stop_integrity_worker(task: asyncio.Task[None]) -> None:
    """Cancel the loop and wait for it, swallowing the cancellation itself."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
