"""The entitlement lease: asking the vendor for it, and remembering the answer.

Everything that touches the control plane's lease lives here, so the read
path (``app.application.entitlement``) and the activation callback
(``app.application.activation``) share one implementation of "make one
attempt, book it either way":

- ``client()`` is the seam — the single place a caller obtains a
  control-plane client, which is what the test suite replaces to point every
  call at a fake service (or at the real one in-process instead of over a
  socket);
- ``load_cache`` / ``cached_payload`` read what the deployment last stored;
- ``refresh_lease`` makes one attempt: fetch → validate the frozen §2.2
  shape → record. **Every failure is booked, none is raised**: a control
  plane outage must never break a read or an ungated action (ADR-0012), so
  the cached lease stays, keeps ageing, and the §4.3 machine does the
  degrading. The next attempt waits out the backoff window
  (``app.domain.entitlement.refresh_due``).

The bookkeeping is deliberately not an audit event: ADR-0009's chain is the
tenant's compliance record of its data, and a lease refresh changes none of
it. Activation and deactivation, which are administrative state changes, do
emit entries (``app.application.activation``).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
)
from app.adapters.persistence.entitlement import LeaseCache
from app.core.config import settings
from app.domain.entitlement import (
    EntitlementPayload,
    LeasePayloadInvalid,
    coerce_tier,
    parse_lease_payload,
)

NOT_CONFIGURED = "control_plane_not_configured"
UNREACHABLE = "control_plane_unreachable"
STALE_VERSION = "stale_entitlement_version"


class ControlPlaneNotConfigured(Exception):
    """This deployment has no control-plane URL, so nothing can be asked of one.

    Activation refuses with this; the read path never needs it (a deployment
    that has not activated reads configuration, and one that has cannot have
    got there without a URL).
    """


@dataclass(frozen=True)
class LeaseRefreshOutcome:
    """What one refresh attempt did, and what the cache now holds."""

    row: LeaseCache
    refreshed: bool
    reason: str | None = None


def client() -> ControlPlaneClient:
    """The control-plane client for this deployment (the seam tests replace)."""
    return ControlPlaneClient(
        base_url=settings.cp_base_url,
        timeout=settings.cp_request_timeout_seconds,
    )


async def load_cache(session: AsyncSession) -> LeaseCache | None:
    """The deployment's cached lease row, if it has one."""
    row: LeaseCache | None = await session.scalar(select(LeaseCache).limit(1))
    return row


def cached_payload(row: LeaseCache | None) -> EntitlementPayload | None:
    """The frozen payload the cache holds; ``None`` when it holds none yet."""
    if row is None or row.tier is None or row.license_expires_at is None:
        return None
    if row.entitlement_version is None:
        return None
    return EntitlementPayload(
        tier=coerce_tier(row.tier),
        license_expires_at=row.license_expires_at,
        seat_cap=row.seat_cap,
        company_cap=row.company_cap,
        entitlement_version=row.entitlement_version,
    )


async def refresh_lease(
    session: AsyncSession,
    *,
    control_plane_tenant_id: UUID,
    token: str,
    now: datetime,
) -> LeaseRefreshOutcome:
    """Make one lease attempt against the control plane and record the result.

    The row is created here when it does not exist yet — activation completes
    into this call, and a deployment whose first fetch fails still needs the
    attempt recorded so the backoff paces the retries.
    """
    row = await load_cache(session)
    if row is None or row.control_plane_tenant_id != control_plane_tenant_id:
        # A re-activation under a new deployment fingerprint lands on a new
        # vendor tenant; the old tenant's lease is no longer this
        # deployment's to serve.
        if row is not None:
            await session.delete(row)
        row = LeaseCache(control_plane_tenant_id=control_plane_tenant_id)
        session.add(row)
    row.last_attempt_at = now
    if not settings.cp_base_url:
        return await _book_failure(session, row, NOT_CONFIGURED)
    try:
        raw = await client().fetch_lease(token=token)
        payload = parse_lease_payload(raw)
    except ControlPlaneUnavailable:
        return await _book_failure(session, row, UNREACHABLE)
    except ControlPlaneRefused as exc:
        return await _book_failure(session, row, f"control_plane_refused:{exc.reason}")
    except LeasePayloadInvalid as exc:
        return await _book_failure(session, row, f"invalid_payload:{exc.reason}")
    if (
        row.entitlement_version is not None
        and payload.entitlement_version < row.entitlement_version
    ):
        # §2.2 gives the version one job: "tell a stale lease from a fresh
        # one". An older body is not a new fact about the entitlement.
        return await _book_failure(session, row, STALE_VERSION)
    row.tier = payload.tier.value
    row.license_expires_at = payload.license_expires_at
    row.seat_cap = payload.seat_cap
    row.company_cap = payload.company_cap
    row.entitlement_version = payload.entitlement_version
    row.fetched_at = now
    row.last_error = None
    await session.flush()
    return LeaseRefreshOutcome(row=row, refreshed=True)


async def _book_failure(
    session: AsyncSession, row: LeaseCache, reason: str
) -> LeaseRefreshOutcome:
    """Record a failed attempt on the cache row and keep the cached payload."""
    row.last_error = reason
    await session.flush()
    return LeaseRefreshOutcome(row=row, refreshed=False, reason=reason)
