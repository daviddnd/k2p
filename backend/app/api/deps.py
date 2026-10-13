"""Shared API-layer dependencies: the authentication seam and the single
RBAC check (AGENTS.md: one shared authorization mechanism, deny by default;
ADR-0006: no business logic in this layer — the decision lives in
``app.domain.rbac.is_allowed``).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import User
from app.adapters.persistence.session import get_session
from app.application import auth
from app.application.rbac import load_actor_context
from app.core.config import settings
from app.domain.rbac import (
    ActorContext,
    Permission,
    can_access_company,
    is_active,
    is_admin,
    is_allowed,
)
from app.domain.setup import setup_token_allows

#: The only paths an authenticated-but-unverified session may reach — the
#: MFA surface E4 mounts under ``/api/v1/auth/mfa`` (enrollment
#: begin/complete, the login challenge, and status) plus the auth surface
#: itself (the login route). Everything else, including E4's
#: ``/auth/mfa/clear`` — the recovery operator must be verified first — is
#: refused with the named 403 until the challenge passes. The gate binds
#: the session's ``mfa_verified`` flag, not the account's role: it held
#: Administrators from E4 (phase 7) and holds enrolled General staff from
#: slice ① (2026-10-11) alike. Each entry matches its own path and sub-paths
#: only (``_path_allowed``) — a future sibling route is never silently
#: allowlisted by prefix coincidence. E4's brief pins these exact prefixes;
#: the routes must stay under them.
MFA_ALLOWED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/login",
    "/api/v1/auth/mfa/enrollment",
    "/api/v1/auth/mfa/verify",
    "/api/v1/auth/mfa/status",
)


def _path_allowed(path: str) -> bool:
    """The gate's allowlist with a path boundary: an entry admits exactly its
    own path and paths beneath it, so a future sibling route (say
    ``/api/v1/auth/login-attempts``) can never be allowlisted by prefix
    coincidence.
    """
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in MFA_ALLOWED_PATH_PREFIXES
    )


@dataclass(frozen=True)
class ActorRef:
    """The authenticated identity attached to a request."""

    user_id: UUID
    tenant_id: UUID


async def get_current_actor(
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ActorRef:
    """Single authentication seam: Bearer session token → ``ActorRef``.

    Any missing, malformed, unknown, or expired credential fails closed with
    401 — unchanged by the MFA gate, which applies only after
    authentication. The session row carries both ids from the matched user
    row at login, so a token can never assert a user/tenant pair that does
    not exist.

    The MFA gate (phase 7, scope §8; slice ① widened it 2026-10-11): a
    session whose ``mfa_verified`` flag is False owes the TOTP step — an
    Administrator awaiting the challenge or forced enrollment, or an
    enrolled General staff account awaiting the challenge (the login flow
    issues unenrolled accounts a verified session). Such a session reaches
    only the allowlisted surface — ``MFA_ALLOWED_PATH_PREFIXES`` — and every
    other route refuses with one named 403 carrying the machine-readable
    code and the ``enrollment_state`` (``enrolled`` → challenge,
    ``pending`` → enrollment), which is all the application knows. The gate
    binds the flag, not the role: any unverified session is refused
    off-list, an administrator's and a staff member's alike. All verified
    sessions pass unchanged.
    """
    token = bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    auth_session = await auth.resolve_session(session, token=token)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if not auth_session.mfa_verified and not _path_allowed(request.url.path):
        user = await session.get(User, auth_session.user_id)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "mfa_verification_required",
                "enrollment_state": (
                    "enrolled" if user is not None and user.totp_secret is not None else "pending"
                ),
            },
        )
    return ActorRef(user_id=auth_session.user_id, tenant_id=auth_session.tenant_id)


def bearer_token(authorization: str | None) -> str | None:
    """The token from an ``Authorization: Bearer …`` header; anything else None.

    Public because the MFA surface (``app.api.auth``) re-resolves the
    presented session to get the row the challenge must mark verified — the
    gate authenticated the same token a moment earlier, so a ``None`` there
    is unreachable and fails closed.
    """
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def require_setup_token(
    x_setup_token: Annotated[str | None, Header()] = None,
) -> None:
    """The bootstrap guard seam: the one route open by necessity.

    ``POST /tenants`` cannot require a session (no account exists yet), so a
    deployment that arms ``AICS_SETUP_TOKEN`` gates it on this header
    instead. The refusal is uniform — 401 ``setup_token_required`` whether
    the header is missing or wrong — so a probe learns nothing from the
    body. Disarmed, the decision (``app.domain.setup``) allows everything:
    the pre-guard behaviour.
    """
    if not setup_token_allows(configured=settings.setup_token, presented=x_setup_token):
        raise HTTPException(status_code=401, detail="setup_token_required")


async def get_actor_context(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActorContext:
    """Resolve the actor's RBAC context; unknown or cross-tenant → 403."""
    context = await load_actor_context(session, user_id=actor.user_id, tenant_id=actor.tenant_id)
    if context is None:
        raise HTTPException(status_code=403, detail="no tenant membership")
    return context


def require_permission(
    permission: Permission,
) -> Callable[[Request, ActorContext], Awaitable[ActorContext]]:
    """Dependency factory: the single authorization check for endpoints.

    Company-scoped routes must name the path parameter ``company_id``; for
    General accounts the check then requires both the permission grant and a
    per-company access grant. Routes without ``company_id`` are open to
    Administrators only. Returns the actor's context on success.
    """

    async def checker(
        request: Request,
        context: Annotated[ActorContext, Depends(get_actor_context)],
    ) -> ActorContext:
        company_id = _path_company_id(request)
        if not is_allowed(context, permission, company_id):
            raise HTTPException(status_code=403, detail="forbidden")
        return context

    return checker


def require_admin() -> Callable[[ActorContext], Awaitable[ActorContext]]:
    """Dependency factory: Administrator-only, including on company-scoped routes.

    Uses the single administrative decision (``app.domain.rbac.is_admin``),
    so General accounts are denied regardless of their permission and
    access grants. Used for portfolio composition (create/list companies)
    and for the anchor-statics correction path (option (b) decision
    2026-08-24) — administrative functions that are not ordinary staff
    master-data maintenance.
    """

    async def checker(
        context: Annotated[ActorContext, Depends(get_actor_context)],
    ) -> ActorContext:
        if not is_admin(context):
            raise HTTPException(status_code=403, detail="forbidden")
        return context

    return checker


def require_active() -> Callable[[ActorContext], Awaitable[ActorContext]]:
    """Dependency factory: any active staff account, whatever its role or grants.

    For portfolio-wide reads whose *data* the application layer scopes by
    grant (the dashboard and compliance calendar): the role is not the gate
    — every active member may read their own slice — but an inactive
    account reads nothing (fail closed, consistent with every other route).
    Uses the single domain decision ``app.domain.rbac.is_active``.
    """

    async def checker(
        context: Annotated[ActorContext, Depends(get_actor_context)],
    ) -> ActorContext:
        if not is_active(context):
            raise HTTPException(status_code=403, detail="forbidden")
        return context

    return checker


def require_company_access() -> Callable[[Request, ActorContext], Awaitable[ActorContext]]:
    """Dependency factory: grant-only per-company access (no permission flag).

    For company-scoped routes whose action is ordinary staff work that no
    §2.9 permission flag covers — company master-data reads and fact
    appends (founder decision 2026-08-24). Administrators pass for any
    tenant company; General accounts need an access grant for the path
    ``company_id``. The route must name the path parameter ``company_id``;
    a missing or malformed one is a 422 client error, not an authorization
    failure. Uses the single domain decision ``can_access_company``.
    """

    async def checker(
        request: Request,
        context: Annotated[ActorContext, Depends(get_actor_context)],
    ) -> ActorContext:
        company_id = _path_company_id(request)
        if company_id is None:
            # The route guarantees the path parameter; a missing or malformed
            # value is a client error, not an authorization failure.
            raise HTTPException(status_code=422, detail="invalid company_id")
        if not can_access_company(context, company_id):
            raise HTTPException(status_code=403, detail="forbidden")
        return context

    return checker


def _path_company_id(request: Request) -> UUID | None:
    raw = request.path_params.get("company_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
