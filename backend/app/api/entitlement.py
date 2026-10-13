"""Entitlement read and refresh endpoints (ADR-0012/0016; data-model §2.2
payload shape, §4.3 licence state machine, CP1b lease client).

``GET /entitlement`` is the dashboard's renewal-prompt source and the
operational health check: the provisioned or leased tier, the derived licence
state, the capability set and caps actually in force, and **which source they
came from** (``lease`` once the deployment has activated, ``configuration``
otherwise). This read is also where an opportunistic lease refresh happens —
a read may therefore take one control-plane round trip, and a failed one
never changes what it returns (``app.application.entitlement``).

``POST /entitlement/refresh`` is the same refresh on demand, for demos and
operations: it ignores the refresh schedule and reports what the attempt did.

There is deliberately no endpoint that *sets* a tier — the tier is leased by
the vendor or provisioned by deployment configuration, never user-editable
(design D5). The endpoints that produce a lease are in ``app.api.activation``.

Administrator-only via ``require_admin``: the tier is commercial
configuration, consistent with the packages and tenant-config decisions.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import require_admin
from app.application import entitlement
from app.domain.entitlement import EntitlementSource, LicenseState, Tier
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["entitlement"])

NOT_ACTIVATED = "not_activated"


class EntitlementResponse(BaseModel):
    """The derived entitlement: every field computed at read time."""

    tier: Tier
    license_state: LicenseState
    capabilities: list[str]
    seat_cap: int | None
    company_cap: int | None
    license_expires_at: datetime | None
    entitlement_version: int
    source: EntitlementSource


class RefreshResponse(EntitlementResponse):
    """The entitlement in force after one explicit refresh attempt."""

    refreshed: bool
    reason: str | None


def _response(view: entitlement.EntitlementView) -> EntitlementResponse:
    return EntitlementResponse(
        tier=view.tier,
        license_state=view.license_state,
        capabilities=sorted(capability.value for capability in view.capabilities),
        seat_cap=view.seat_cap,
        company_cap=view.company_cap,
        license_expires_at=view.license_expires_at,
        entitlement_version=view.entitlement_version,
        source=view.source,
    )


@router.get("/entitlement")
async def get_entitlement(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EntitlementResponse:
    """The deployment's entitlement: tier, licence state, capabilities, caps.

    ``license_state`` is derived from the lease expiry at request time and
    ``capabilities`` reflects the state — a lease that has run out collapses
    any tier to the Free capability set (§4.3). ``seat_cap``/``company_cap``
    are ``null`` when the tier has no fixed cap (per subscription/license).
    ``source`` names which of the two entitlement sources is in force.
    """
    return _response(await entitlement.get_entitlement(session))


@router.post("/entitlement/refresh")
async def refresh_entitlement(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RefreshResponse:
    """Attempt one lease refresh now, whatever the schedule says.

    ``refreshed`` is false with a machine-readable ``reason`` when the
    deployment is not activated (``not_activated``), the vendor service
    cannot be reached, or it refused the lease — the cached lease stands
    either way, so the entitlement returned here is the one in force.
    """
    outcome = await entitlement.refresh_now(session)
    view = await entitlement.get_entitlement(session)
    return RefreshResponse(
        **_response(view).model_dump(),
        refreshed=outcome.refreshed if outcome is not None else False,
        reason=outcome.reason if outcome is not None else NOT_ACTIVATED,
    )
