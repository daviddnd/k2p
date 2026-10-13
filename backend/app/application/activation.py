"""The activation handshake, deployment side (ADR-0016 point 1).

Four functions over two stores — the credential file
(``app.adapters.entitlement.activation_store``) and the lease cache
(``app.application.lease``):

- ``begin`` is what the Administrator's POST does: mint the deployment
  fingerprint once, mint the PKCE verifier and its S256 challenge, register
  the intent with the control plane, and leave the pending attempt on disk.
  The verifier exists nowhere else, and the attempt expires exactly when the
  vendor's registration does.
- ``complete`` is what the browser's return hits; the API layer passes it the
  one-time code and nothing else. It is **unauthenticated by necessity** —
  the browser arrives from the vendor's site and carries no session — so its
  authority comes from the pending attempt: the code is single-use and, per
  PKCE, redeemable only with a verifier that never left this deployment. A
  stolen code is therefore worthless, which is the property that lets this
  route stay open. Success writes the credential (0600 file) and caches the
  first lease; a first-lease failure still activates — the credential is
  valid, and the read path's next refresh will fill the cache.
- ``deactivate`` forgets the credential, drops the cached lease, and returns
  the deployment to the configuration source. The fingerprint survives:
  re-activating has to land on the same vendor tenant.
- ``status`` is the Administrator's read of all of the above, from local state
  only — never a control-plane call, so it answers during an outage.

Both ends of the state change are audited (ADR-0009) against the tenant of
the administrator who started the attempt — the callback has no actor of its
own, and the attempt records who it was. No credential, verifier, or code is
ever written to an audit entry, a log, or the database.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import ControlPlaneRefused
from app.adapters.persistence.entitlement import LeaseCache
from app.application import audit, lease
from app.application.lease import ControlPlaneNotConfigured
from app.core.config import settings
from app.domain.activation import (
    ActivationResponseInvalid,
    PendingActivation,
    code_challenge_for,
    fingerprint_for,
    new_code_verifier,
    parse_activation_request_response,
    parse_activation_token_response,
)
from app.domain.audit import AuditAction
from app.domain.entitlement import EntitlementSource, Tier


class ActivationRefused(Exception):
    """The callback had nothing to redeem, or the vendor refused the redemption."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ActivationStart:
    """Where to send the administrator's browser — or the completed activation.

    ``activated`` true is the 2026-10-11 amendment's silent path: the vendor
    accepted the registration code as the authorization, the credential is
    already stored, and the first lease is cached; there is no URL to open.
    """

    authorization_url: str | None
    request_id: UUID | None
    expires_at: datetime | None
    activated: bool = False


@dataclass(frozen=True)
class ActivationResult:
    """What a completed activation did (for the callback's page)."""

    lease_fetched: bool
    reason: str | None = None


@dataclass(frozen=True)
class CachedLease:
    """The lease cache as an administrator reads it, without touching the source."""

    tier: Tier | None
    license_expires_at: datetime | None
    entitlement_version: int | None
    fetched_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class ActivationStatus:
    """This deployment's activation state (the Administrator's view)."""

    activated: bool
    source: EntitlementSource
    deployment_fingerprint: str | None
    control_plane_tenant_id: UUID | None
    token_expires_at: datetime | None
    authorization_url: str | None
    pending_expires_at: datetime | None
    cache: CachedLease | None


def authorization_url(request_id: UUID) -> str:
    """The vendor URL the administrator's browser opens to name this deployment."""
    return (
        f"{settings.cp_base_url.rstrip('/')}/api/v1/authorize"
        f"?{urlencode({'request_id': str(request_id)})}"
    )


async def begin(
    *,
    session: AsyncSession,
    local_tenant_id: UUID,
    local_user_id: UUID,
    registration_code: str | None = None,
    now: datetime | None = None,
) -> ActivationStart:
    """Register an activation attempt — or complete it silently.

    Replaces any pending attempt: one activation at a time is what a
    single-tenant deployment needs, and the newest attempt is the one the
    administrator is looking at. ``registration_code`` is the claim code
    from the vendor's self-service signup, forwarded untouched — the vendor
    binds it to this deployment's fingerprint, or refuses it.

    The vendor's answer discriminates (ADR-0016 amendment 2026-10-11): a
    valid code is answered with the credential directly — no pending attempt
    is created; the credential file, the lease cache, and the audit entry
    land in this call, exactly as the browser callback's completion does.
    Without a code the pending attempt is stored for the callback as always.
    """
    at = now if now is not None else datetime.now(UTC)
    if not settings.cp_base_url:
        raise ControlPlaneNotConfigured
    store = ActivationStore(settings.cp_activation_token_file)
    state = store.load()
    fingerprint = fingerprint_for(state)
    verifier = new_code_verifier()
    raw = await lease.client().register_activation(
        redirect_uri=settings.cp_activation_redirect_uri,
        code_challenge=code_challenge_for(verifier),
        deployment_fingerprint=fingerprint,
        registration_code=registration_code,
    )
    parsed = parse_activation_request_response(raw)
    if parsed.activated:
        if parsed.credential is None:
            raise ActivationRefused("credential_invalid:missing")
        outcome = await lease.refresh_lease(
            session,
            control_plane_tenant_id=parsed.credential.control_plane_tenant_id,
            token=parsed.credential.token,
            now=at,
        )
        await audit.record_event(
            session,
            tenant_id=local_tenant_id,
            action=AuditAction.CONTROL_PLANE_ACTIVATED,
            actor_user_id=local_user_id,
            entity_type="control_plane_lease",
            entity_id=parsed.credential.control_plane_tenant_id,
            details={
                "control_plane_tenant_id": parsed.credential.control_plane_tenant_id,
                "tier": outcome.row.tier,
                "entitlement_version": outcome.row.entitlement_version,
                "silent": True,
            },
        )
        store.save(
            replace(
                state,
                deployment_fingerprint=fingerprint,
                credential=parsed.credential,
                pending=None,
            )
        )
        return ActivationStart(
            authorization_url=None,
            request_id=None,
            expires_at=parsed.credential.expires_at,
            activated=True,
        )
    if parsed.request_id is None or parsed.expires_at is None:
        raise ActivationRefused("request_invalid:missing")
    store.save(
        replace(
            state,
            deployment_fingerprint=fingerprint,
            pending=PendingActivation(
                request_id=parsed.request_id,
                verifier=verifier,
                local_tenant_id=local_tenant_id,
                local_user_id=local_user_id,
                expires_at=parsed.expires_at,
            ),
        )
    )
    return ActivationStart(
        authorization_url=authorization_url(parsed.request_id),
        request_id=parsed.request_id,
        expires_at=parsed.expires_at,
    )


async def complete(
    session: AsyncSession, *, code: str, now: datetime | None = None
) -> ActivationResult:
    """Redeem the browser's code and activate this deployment.

    Raises ``ActivationRefused`` when there is no usable attempt or the
    vendor refused the redemption (the attempt is then kept, so a legitimate
    retry after a client-side mistake still works — the vendor consumes a
    code only on a successful redemption). ``ControlPlaneUnavailable``
    propagates: the vendor being unreachable is not a refusal, and nothing
    local has changed.
    """
    at = now if now is not None else datetime.now(UTC)
    store = ActivationStore(settings.cp_activation_token_file)
    state = store.load()
    pending = state.pending
    fingerprint = state.deployment_fingerprint
    if pending is None or fingerprint is None:
        raise ActivationRefused("no_pending_activation")
    if pending.is_expired(now=at):
        store.save(replace(state, pending=None))
        raise ActivationRefused("activation_expired")
    try:
        raw = await lease.client().exchange_activation_code(
            code=code,
            code_verifier=pending.verifier,
            deployment_fingerprint=fingerprint,
        )
    except ControlPlaneRefused as exc:
        raise ActivationRefused(f"code_refused:{exc.reason}") from exc
    try:
        credential = parse_activation_token_response(raw)
    except ActivationResponseInvalid as exc:
        raise ActivationRefused(f"credential_invalid:{exc.reason}") from exc
    outcome = await lease.refresh_lease(
        session,
        control_plane_tenant_id=credential.control_plane_tenant_id,
        token=credential.token,
        now=at,
    )
    await audit.record_event(
        session,
        tenant_id=pending.local_tenant_id,
        action=AuditAction.CONTROL_PLANE_ACTIVATED,
        actor_user_id=pending.local_user_id,
        entity_type="control_plane_lease",
        entity_id=credential.control_plane_tenant_id,
        details={
            "control_plane_tenant_id": credential.control_plane_tenant_id,
            "tier": outcome.row.tier,
            "entitlement_version": outcome.row.entitlement_version,
        },
    )
    # The credential file is the last write: it is what makes the deployment
    # activated, so it follows the cache and the audit entry into the same
    # request. A failed first fetch does not stop it — the credential is
    # valid, and the read path's next refresh fills the cache.
    store.save(replace(state, credential=credential, pending=None))
    return ActivationResult(lease_fetched=outcome.refreshed, reason=outcome.reason)


async def status(session: AsyncSession) -> ActivationStatus:
    """What the deployment is activated as, from local state only.

    Reads the credential file and the cached lease row; deliberately does not
    touch the control plane, so this view stays available (and cheap) during
    an outage, when an operator most wants it.
    """
    state = ActivationStore(settings.cp_activation_token_file).load()
    credential = state.credential
    pending = state.pending
    return ActivationStatus(
        activated=credential is not None,
        source=(
            EntitlementSource.LEASE if credential is not None else EntitlementSource.CONFIGURATION
        ),
        deployment_fingerprint=state.deployment_fingerprint,
        control_plane_tenant_id=credential.control_plane_tenant_id if credential else None,
        token_expires_at=credential.expires_at if credential else None,
        authorization_url=(
            authorization_url(pending.request_id)
            if pending is not None and settings.cp_base_url
            else None
        ),
        pending_expires_at=pending.expires_at if pending is not None else None,
        cache=_cached_lease(await lease.load_cache(session)),
    )


async def deactivate(
    session: AsyncSession,
    *,
    local_tenant_id: UUID,
    local_user_id: UUID,
) -> None:
    """Forget the credential and the cached lease; configuration takes over again."""
    store = ActivationStore(settings.cp_activation_token_file)
    state = store.load()
    credential = state.credential
    if credential is None and state.pending is None:
        return
    row = await lease.load_cache(session)
    if row is not None:
        await session.delete(row)
    await audit.record_event(
        session,
        tenant_id=local_tenant_id,
        action=AuditAction.CONTROL_PLANE_DEACTIVATED,
        actor_user_id=local_user_id,
        entity_type="control_plane_lease",
        entity_id=credential.control_plane_tenant_id if credential else None,
        details={
            "control_plane_tenant_id": (
                credential.control_plane_tenant_id if credential else None
            )
        },
    )
    store.save(replace(state, credential=None, pending=None))


def _cached_lease(row: LeaseCache | None) -> CachedLease | None:
    if row is None:
        return None
    return CachedLease(
        tier=Tier(row.tier) if row.tier is not None else None,
        license_expires_at=row.license_expires_at,
        entitlement_version=row.entitlement_version,
        fetched_at=row.fetched_at,
        last_attempt_at=row.last_attempt_at,
        last_error=row.last_error,
    )
