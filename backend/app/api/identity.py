"""Identity endpoints: tenant bootstrap and staff user management.

``POST /tenants`` is the deployment bootstrap seam: it is unauthenticated by
necessity (no user can exist before the first tenant) and fails closed with
409 once any tenant exists (single-tenant deployment, ADR-0002). While the
deployment is uninitialised, whoever reaches it first becomes the
Administrator; the opt-in setup-token guard (``require_setup_token``, armed
by ``AICS_SETUP_TOKEN``) closes that first-run race on an exposed
deployment. Every other
route in this module runs through the single authorization mechanism
(``require_permission``, AGENTS.md) and is Administrator-only because it is
company-less (see ``deps.py``).

``POST /users`` also enforces the entitlement seat cap (design D6): once the
tenant holds as many accounts as its tier allows, a new user is refused with
409 ``seat_cap_exceeded``. Existing accounts keep working — caps never gate
reads.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_permission, require_setup_token
from app.application import entitlement, identity
from app.domain.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, password_policy_violation
from app.domain.rbac import ActorContext, Permission, Role

router = APIRouter(prefix="/api/v1", tags=["identity"])

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"


def _check_password_policy(password: str) -> str:
    """The one creation-time policy gate, shared by every account-creation
    request model (founder ruling 2026-10-13: >= 8 chars, all four classes)."""
    violation = password_policy_violation(password)
    if violation is not None:
        raise ValueError(violation)
    return password


class CreateTenantRequest(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=200)
    admin_email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)
    admin_display_name: str = Field(min_length=1, max_length=200)
    admin_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("admin_password")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        return _check_password_policy(value)


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    role: Role
    # The Administrator sets the initial password; self-service change is
    # Phase 7 hardening, not this slice.
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        return _check_password_policy(value)
    # Fail-safe default: every grant is denied unless explicitly requested.
    review_events: bool = False
    approve_events: bool = False
    review_drafts: bool = False
    approve_drafts: bool = False


class UserResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    role: Role
    review_events: bool
    approve_events: bool
    review_drafts: bool
    approve_drafts: bool
    is_active: bool


class TenantBootstrapResponse(BaseModel):
    tenant_id: UUID
    tenant_name: str
    administrator: UserResponse


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        review_events=user.review_events,
        approve_events=user.approve_events,
        review_drafts=user.review_drafts,
        approve_drafts=user.approve_drafts,
        is_active=user.is_active,
    )


@router.post("/tenants", status_code=201)
async def create_tenant(
    payload: CreateTenantRequest,
    _: Annotated[None, Depends(require_setup_token)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantBootstrapResponse:
    try:
        tenant, admin = await identity.create_tenant_with_administrator(
            session,
            tenant_name=payload.tenant_name,
            admin_email=payload.admin_email,
            admin_display_name=payload.admin_display_name,
            admin_password=payload.admin_password,
        )
    except identity.TenantAlreadyExistsError:
        raise HTTPException(status_code=409, detail="tenant already exists") from None
    return TenantBootstrapResponse(
        tenant_id=tenant.id, tenant_name=tenant.name, administrator=_user_response(admin)
    )


@router.post("/users", status_code=201)
async def create_user(
    payload: CreateUserRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    # Company-less route: require_permission resolves to Administrators-only
    # (deps.py); the permission argument is not meaningful for tenant-scoped
    # routes. The user lands in the actor's own tenant — the body carries no
    # tenant_id, so cross-tenant creation is impossible by construction.
    context: Annotated[ActorContext, Depends(require_permission(Permission.REVIEW_EVENTS))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    try:
        user = await identity.create_user(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            email=payload.email,
            display_name=payload.display_name,
            role=payload.role,
            password=payload.password,
            review_events=payload.review_events,
            approve_events=payload.approve_events,
            review_drafts=payload.review_drafts,
            approve_drafts=payload.approve_drafts,
        )
    except identity.EmailAlreadyInUseError:
        raise HTTPException(status_code=409, detail="email already in use") from None
    except entitlement.SeatCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _user_response(user)
