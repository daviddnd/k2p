"""Entitlement orchestration: the single consumer of the entitlement seam
(ADR-0012/0016; entitlement design D2/D3/D4/D6; CP1b lease client).

- ``get_entitlement`` is the read seam. Two sources feed it, and the
  precedence is the whole of the CP1b rule: **a deployment that has activated
  takes its entitlement from the lease the control plane issued, and never
  from deployment configuration again; a deployment that has not activated
  (or has deactivated) reads configuration, exactly as it did in Phase 1.**
  Within the lease source the cached payload is the entitlement, Free stands
  in when there is no cache yet, and a refresh is attempted in-band when the
  lease has aged past the interval and the backoff window allows it
  (``app.application.lease``) — a refresh failure never changes what is read.
- ``ensure_seat_available`` / ``ensure_company_available`` are the cap checks
  the creation paths call. Caps gate creation only: existing rows are never
  locked, hidden, or refused on downgrade — the customer is never locked out
  of their own data (ADR-0012) — which is also what makes degradation
  demonstrable (D6).

The licence state degrades the capability set (a lease that has run out
collapses any tier to the Free set, §4.3); caps follow the payload's tier, so
a lapsed lease still leaves existing records usable. Nothing about the lease
is stored here beyond the cache the source owns; the state is derived at read
time from the expiry, which is what lets a control plane outage degrade a
deployment without any code path failing.

The derived state has exactly one local consequence, applied here because
this is the one place it is derived: cached licensed packs are **frozen** on
``free`` and unfrozen on recovery (``app.application.packs``, §4.3). Every
automation trigger and the dashboard already read the entitlement through
this function, so a lapse freezes the pack cache without a job runner and
without a caller having to remember to ask. Frozen content stays readable,
and nothing local is deleted.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.config_source import load_entitlement
from app.adapters.persistence.models import Company, User
from app.application import lease, packs
from app.core.config import settings
from app.domain.entitlement import (
    EntitlementSource,
    LicenseState,
    Tier,
    free_entitlement,
    refresh_due,
)
from app.domain.service_packages import AutomationType


@dataclass(frozen=True)
class EntitlementView:
    """The derived entitlement of this deployment, computed at read time."""

    tier: Tier
    license_state: LicenseState
    capabilities: frozenset[AutomationType]
    seat_cap: int | None
    company_cap: int | None
    license_expires_at: datetime | None
    entitlement_version: int
    source: EntitlementSource


class SeatCapExceededError(Exception):
    """The tenant is at its seat cap; another user would exceed it."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(f"seat_cap_exceeded: seat cap of {cap} reached")


class CompanyCapExceededError(Exception):
    """The tenant is at its company cap; another company would exceed it."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(f"company_cap_exceeded: company cap of {cap} reached")


async def get_entitlement(
    session: AsyncSession, *, now: datetime | None = None
) -> EntitlementView:
    """This deployment's entitlement, derived at ``now`` (default: the clock).

    Reads the credential file and, when activated, the cached lease — and may
    therefore make one control-plane call (the opportunistic refresh). It
    never raises for a control-plane failure: the cached lease stays and the
    §4.3 state machine does the degrading.
    """
    at = now if now is not None else datetime.now(UTC)
    state = ActivationStore(settings.cp_activation_token_file).load()
    credential = state.credential
    if credential is None:
        payload = load_entitlement()
        source = EntitlementSource.CONFIGURATION
    else:
        row = await lease.load_cache(session)
        if refresh_due(
            fetched_at=None if row is None else row.fetched_at,
            last_attempt_at=None if row is None else row.last_attempt_at,
            now=at,
            interval=timedelta(hours=settings.cp_refresh_interval_hours),
            backoff=timedelta(minutes=settings.cp_refresh_backoff_minutes),
        ):
            outcome = await lease.refresh_lease(
                session,
                control_plane_tenant_id=credential.control_plane_tenant_id,
                token=credential.token,
                now=at,
            )
            row = outcome.row
        # Fail closed: activated with nothing cached is Free, not the tier
        # deployment configuration happens to name.
        payload = lease.cached_payload(row) or free_entitlement()
        source = EntitlementSource.LEASE
    view = EntitlementView(
        tier=payload.tier,
        license_state=payload.license_state_at(now=at),
        capabilities=payload.capabilities_at(now=at),
        seat_cap=payload.effective_seat_cap,
        company_cap=payload.effective_company_cap,
        license_expires_at=payload.license_expires_at,
        entitlement_version=payload.entitlement_version,
        source=source,
    )
    await packs.apply_license_state(
        session, tier=view.tier, license_state=view.license_state, now=at
    )
    return view


async def refresh_now(
    session: AsyncSession, *, now: datetime | None = None
) -> lease.LeaseRefreshOutcome | None:
    """Force one lease refresh attempt, ignoring the refresh schedule.

    ``None`` when this deployment has not activated — there is no credential
    to refresh with, and the caller should not report a failure that is
    simply "not applicable". Like the opportunistic path, every failure is
    recorded and returned, never raised.
    """
    at = now if now is not None else datetime.now(UTC)
    credential = ActivationStore(settings.cp_activation_token_file).load().credential
    if credential is None:
        return None
    return await lease.refresh_lease(
        session,
        control_plane_tenant_id=credential.control_plane_tenant_id,
        token=credential.token,
        now=at,
    )


async def ensure_seat_available(session: AsyncSession, *, tenant_id: UUID) -> None:
    """Raise ``SeatCapExceededError`` when the tenant is at its seat cap.

    Every user account in the tenant counts — the bootstrap Administrator
    included (ADR-0012: the Free tier's two seats, the bootstrap
    Administrator counting among them).
    """
    cap = (await get_entitlement(session)).seat_cap
    if cap is None:
        return
    seats = await session.scalar(
        select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
    )
    if seats is not None and seats >= cap:
        raise SeatCapExceededError(cap)


async def ensure_company_available(session: AsyncSession, *, tenant_id: UUID) -> None:
    """Raise ``CompanyCapExceededError`` when the tenant is at its company cap."""
    cap = (await get_entitlement(session)).company_cap
    if cap is None:
        return
    companies = await session.scalar(
        select(func.count()).select_from(Company).where(Company.tenant_id == tenant_id)
    )
    if companies is not None and companies >= cap:
        raise CompanyCapExceededError(cap)
