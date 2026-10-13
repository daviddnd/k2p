"""Activation endpoints (ADR-0016 point 1; contract §2.2, CP1b).

The handshake, from the deployment's side: an Administrator starts an
activation, the browser goes to the vendor, and the browser comes back to the
loopback callback.

The callback is the one **unauthenticated** route in this service, and it
cannot be otherwise: the browser arrives from the vendor's site carrying no
session, and the whole point of the exchange is that this deployment does not
yet hold a credential. Three properties make that safe, and all three are
required:

- the code is single-use, so a replayed one is refused by the vendor;
- the code is redeemable **only** with the PKCE verifier, which was minted
  here and never left this process — the vendor stores only its S256
  challenge — so a stolen code is useless to whoever stole it;
- the vendor pins the code to this deployment's fingerprint, so a code
  issued for another deployment cannot be redeemed by this one.

What the callback does *not* do is verify the ``state`` the vendor sends
back: that state is the vendor's own HMAC over its activation request, for
its own Google callback, and this deployment holds no key to check it. The
pending attempt on disk is the local authority — the callback refuses unless
one exists, has not expired, and still holds the verifier for the code's
challenge.

Administrator-only: starting an activation, reading its state, and
deactivating. The callback is not, by necessity.
"""

from datetime import datetime
from html import escape
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.control_plane_client import (
    ControlPlaneRefused,
    ControlPlaneUnavailable,
)
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import activation
from app.application.lease import ControlPlaneNotConfigured
from app.domain.entitlement import EntitlementSource, Tier
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/entitlement/activation", tags=["activation"])

# The page is a hand-off, not an application: no scripts, no client data, and
# never cached (a browser holding it could re-post a spent code on back).
_PAGE_HEADERS = {"Cache-Control": "no-store"}


class ActivationStartResponse(BaseModel):
    """The answer to an activation start.

    Browser path: the URL the administrator opens, with the attempt's id and
    expiry. Silent path (ADR-0016 amendment 2026-10-11): ``activated: true``
    and no URL — the credential is stored and the first lease is cached.
    """

    authorization_url: str | None
    request_id: UUID | None
    expires_at: datetime | None
    activated: bool = False


class ActivationStartRequest(BaseModel):
    """The operator's optional input to an activation start.

    ``registration_code`` is the claim code the vendor's self-service
    signup delivered (CP4a); presented, it is forwarded to the control
    plane, which binds the registration to this deployment's fingerprint
    before the handshake proceeds. Omitted — the whole body may be omitted
    — the request is exactly what it always was.
    """

    registration_code: str | None = Field(default=None, min_length=1, max_length=512)


class CachedLeaseResponse(BaseModel):
    tier: Tier | None
    license_expires_at: datetime | None
    entitlement_version: int | None
    fetched_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None


class ActivationStatusResponse(BaseModel):
    """The deployment's activation state — the operator's view of the lease."""

    activated: bool
    source: EntitlementSource
    deployment_fingerprint: str | None
    control_plane_tenant_id: UUID | None
    token_expires_at: datetime | None
    authorization_url: str | None
    pending_expires_at: datetime | None
    cache: CachedLeaseResponse | None


def _status_response(view: activation.ActivationStatus) -> ActivationStatusResponse:
    return ActivationStatusResponse(
        activated=view.activated,
        source=view.source,
        deployment_fingerprint=view.deployment_fingerprint,
        control_plane_tenant_id=view.control_plane_tenant_id,
        token_expires_at=view.token_expires_at,
        authorization_url=view.authorization_url,
        pending_expires_at=view.pending_expires_at,
        cache=(
            CachedLeaseResponse(
                tier=view.cache.tier,
                license_expires_at=view.cache.license_expires_at,
                entitlement_version=view.cache.entitlement_version,
                fetched_at=view.cache.fetched_at,
                last_attempt_at=view.cache.last_attempt_at,
                last_error=view.cache.last_error,
            )
            if view.cache is not None
            else None
        ),
    )


@router.post("", status_code=201)
async def start_activation(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    payload: ActivationStartRequest | None = None,
) -> ActivationStartResponse:
    """Register an activation attempt and return the vendor URL to open.

    With a valid ``registration_code`` the vendor may instead complete the
    activation in this call (the 2026-10-11 amendment): the response is then
    ``activated: true`` with no URL, the credential stored, the first lease
    cached. 503 when this deployment has no control-plane URL configured;
    502 when the vendor service cannot be reached or refuses to register the
    attempt — including a registration code the vendor will not honour, whose
    own reason surfaces as ``control_plane_refused:<code>``.
    """
    try:
        started = await activation.begin(
            session=session,
            local_tenant_id=actor.tenant_id,
            local_user_id=actor.user_id,
            registration_code=payload.registration_code if payload is not None else None,
        )
    except ControlPlaneNotConfigured:
        raise HTTPException(status_code=503, detail="control plane not configured") from None
    except ControlPlaneUnavailable:
        raise HTTPException(status_code=502, detail="control_plane_unreachable") from None
    except ControlPlaneRefused as exc:
        raise HTTPException(status_code=502, detail=f"control_plane_refused:{exc.reason}") from None
    return ActivationStartResponse(
        authorization_url=started.authorization_url,
        request_id=started.request_id,
        expires_at=started.expires_at,
        activated=started.activated,
    )


@router.get("")
async def get_activation_status(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActivationStatusResponse:
    """How this deployment is activated, from local state only.

    Never calls the vendor service, so it answers during an outage too; a
    suspected outage is what ``POST /api/v1/entitlement/refresh`` is for.
    """
    return _status_response(await activation.status(session))


@router.delete("")
async def deactivate(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActivationStatusResponse:
    """Forget the credential and the cached lease; configuration takes over.

    The vendor's token is not revoked by this — the vendor back office does
    that (ADR-0016 point 3) — and the deployment id is kept, so re-activating
    lands on the same vendor tenant.
    """
    await activation.deactivate(
        session, local_tenant_id=actor.tenant_id, local_user_id=actor.user_id
    )
    return _status_response(await activation.status(session))


@router.get("/callback", response_class=HTMLResponse)
async def activation_callback(
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = None,
    state: str | None = None,
) -> HTMLResponse:
    """The loopback redirect target: redeem the code, activate, show a page.

    ``state`` is accepted and deliberately unused (see the module docstring:
    the deployment holds no key to verify the vendor's HMAC, and the pending
    attempt is the local authority). Refusals are pages, not JSON: this route
    is reached by a browser, which should be told what happened in words.
    """
    if code is None:
        return _page(
            "Activation failed",
            "This deployment received no activation code.",
            status_code=400,
        )
    try:
        result = await activation.complete(session, code=code)
    except activation.ActivationRefused as exc:
        return _page("Activation failed", f"Reason: {exc.reason}", status_code=400)
    except ControlPlaneUnavailable:
        return _page(
            "Activation incomplete",
            "The vendor service could not be reached. Nothing changed; "
            "start the activation again from the administrator console.",
            status_code=502,
        )
    except ControlPlaneRefused as exc:
        return _page("Activation failed", f"Reason: {exc.reason}", status_code=502)
    if result.lease_fetched:
        return _page(
            "Activation complete",
            "This deployment is now licensed from the vendor control plane. "
            "You can close this window.",
        )
    return _page(
        "Activation complete",
        "This deployment is now licensed, but its first licence lease could not be "
        f"fetched ({result.reason}). It will be retried automatically; "
        "you can close this window.",
    )


def _page(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html>\n"
        '<html lang="en">\n<head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head>\n"
        f"<body><h1>{escape(title)}</h1><p>{escape(message)}</p></body>\n</html>\n"
    )
    return HTMLResponse(content=body, status_code=status_code, headers=_PAGE_HEADERS)
