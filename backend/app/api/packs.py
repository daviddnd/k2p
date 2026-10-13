"""Pack cache read and refresh on the execution plane (ADR-0012; contract
§2.2 ``PackCache``).

Two Administrator-only routes, because licensed packs are deployment
plumbing of the same class as the entitlement lease and the command channel:

- ``POST /packs/refresh`` downloads and installs the latest release of each
  pack type, verifying every payload against the digest the vendor published.
  A per-type failure is a reported outcome, never an error page: an operator
  needs to see which type was refused and why. Two states *do* refuse the
  whole cycle with a status code — a ``free`` licence (§4.3: cached packs are
  frozen and the refusal carries its reason) and an unknown or absent
  credential path (nothing to ask) — and the licence refusal is returned as a
  response rather than raised, so the audit entry written with it survives
  the request instead of being rolled back by the exception.
- ``GET /packs`` is what this deployment holds: every cached version, whether
  it is frozen, whether the stored copy still hashes to the digest it was
  installed under, and the licence state that decides the first two.

There is deliberately no route that writes or deletes a pack: a pack arrives
by refreshing, and ADR-0012's promise is that what a customer already holds
is never taken away — freezing stops refreshing, it does not clear anything.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import entitlement, packs
from app.domain.entitlement import LicenseState
from app.domain.packs import PackType, RefreshAction
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/packs", tags=["packs"])


class CachedPackResponse(BaseModel):
    """One cached version, as the operator reads it — never the payload."""

    pack_type: PackType
    version: int
    sha256: str
    downloaded_at: datetime
    frozen: bool
    frozen_at: datetime | None
    # The stored copy recomputed against its own digest: false means the row
    # changed after it was installed, which no code path here does.
    payload_verified: bool


class PackCacheResponse(BaseModel):
    """The cache and the licence state that governs it."""

    license_state: LicenseState
    frozen: bool
    packs: list[CachedPackResponse]


class PackOutcomeResponse(BaseModel):
    """What the refresh did about one pack type."""

    pack_type: PackType
    action: RefreshAction
    version: int | None
    sha256: str | None
    reason: str | None


class PackRefreshResponse(BaseModel):
    """One refresh cycle's account of itself."""

    refreshed: bool
    reason: str | None
    outcomes: list[PackOutcomeResponse]


@router.post("/refresh", response_model=PackRefreshResponse)
async def refresh_packs(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PackRefreshResponse | JSONResponse:
    """Download and install the latest release of every pack type, now.

    403 ``packs_not_licensed`` while the licence does not carry packs —
    ``license_state_free`` for a lapsed lease, ``tier_not_licensed`` for a Free
    tier. Cached packs stay readable either way; the refresh that would change
    them does not run.
    """
    view = await entitlement.get_entitlement(session)
    try:
        report = await packs.refresh(
            session,
            tier=view.tier,
            license_state=view.license_state,
            actor_user_id=actor.user_id,
        )
    except packs.PacksNotLicensedError as exc:
        return JSONResponse(
            status_code=403,
            content={"code": "packs_not_licensed", "reason": exc.reason},
        )
    return PackRefreshResponse(
        refreshed=report.refreshed,
        reason=report.reason,
        outcomes=[
            PackOutcomeResponse(
                pack_type=outcome.pack_type,
                action=outcome.action,
                version=outcome.version,
                sha256=outcome.sha256,
                reason=outcome.reason,
            )
            for outcome in report.outcomes
        ],
    )


@router.get("", response_model=PackCacheResponse)
async def list_packs(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PackCacheResponse:
    """What is cached and whether it is frozen; no payload is returned."""
    view = await entitlement.get_entitlement(session)
    cached = await packs.cached(session)
    return PackCacheResponse(
        license_state=view.license_state,
        frozen=any(row.frozen for row in cached),
        packs=[
            CachedPackResponse(
                pack_type=PackType(row.pack_type),
                version=row.version,
                sha256=row.sha256,
                downloaded_at=row.downloaded_at,
                frozen=row.frozen,
                frozen_at=row.frozen_at,
                payload_verified=packs.verify_cached(row),
            )
            for row in cached
        ],
    )
