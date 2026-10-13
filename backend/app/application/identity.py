"""Identity orchestration: tenant bootstrap and user creation.

Services flush but never commit — the API session dependency
(``app.adapters.persistence.session.get_session``) commits at the request
edge on a clean return, so a failed request leaves no partial writes.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import Tenant, User
from app.application import audit, entitlement
from app.domain.audit import AuditAction
from app.domain.auth import hash_password
from app.domain.rbac import Role


class TenantAlreadyExistsError(Exception):
    """A tenant row already exists; the bootstrap path is closed."""


class EmailAlreadyInUseError(Exception):
    """The email is already taken by another user in the same tenant."""


async def create_tenant_with_administrator(
    session: AsyncSession,
    *,
    tenant_name: str,
    admin_email: str,
    admin_display_name: str,
    admin_password: str,
) -> tuple[Tenant, User]:
    """Create the deployment's tenant and its first Administrator.

    Single-tenant deployment (ADR-0002): once any tenant exists the bootstrap
    path is closed and raises ``TenantAlreadyExistsError``. The Administrator
    is created with all four permission flags denied — Administrators act via
    their role (scope §2.14 back-up rule) and need no grants.
    """
    existing = await session.scalar(select(Tenant.id).limit(1))
    if existing is not None:
        raise TenantAlreadyExistsError
    tenant = Tenant(name=tenant_name)
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=admin_email,
        display_name=admin_display_name,
        role=Role.ADMINISTRATOR,
        password_hash=hash_password(admin_password),
    )
    session.add(admin)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant.id,
        action=AuditAction.TENANT_CREATED,
        actor_user_id=admin.id,
        entity_type="tenant",
        entity_id=tenant.id,
        details={"tenant_name": tenant.name},
    )
    await audit.record_event(
        session,
        tenant_id=tenant.id,
        action=AuditAction.USER_CREATED,
        actor_user_id=admin.id,
        entity_type="user",
        entity_id=admin.id,
        details={"email": admin.email, "role": admin.role},
    )
    return tenant, admin


async def create_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    email: str,
    display_name: str,
    role: Role,
    password: str,
    review_events: bool = False,
    approve_events: bool = False,
    review_drafts: bool = False,
    approve_drafts: bool = False,
) -> User:
    """Create a staff user in ``tenant_id``; permission flags default to denied.

    The duplicate check is scoped to the tenant, matching the
    ``(tenant_id, email)`` unique constraint, which remains the database
    backstop. The entitlement seat cap is enforced here — the bootstrap
    Administrator counts towards it (ADR-0012: one combined admin+staff
    seat on Free) and a tenant at its cap raises ``SeatCapExceededError``.
    The initial password is set by the creating Administrator; self-service
    password change is out of this slice (Phase 7 hardening).
    """
    taken = await session.scalar(
        select(User.id).where(User.tenant_id == tenant_id, User.email == email)
    )
    if taken is not None:
        raise EmailAlreadyInUseError
    await entitlement.ensure_seat_available(session, tenant_id=tenant_id)
    user = User(
        tenant_id=tenant_id,
        email=email,
        display_name=display_name,
        role=role,
        review_events=review_events,
        approve_events=approve_events,
        review_drafts=review_drafts,
        approve_drafts=approve_drafts,
        password_hash=hash_password(password),
    )
    session.add(user)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.USER_CREATED,
        actor_user_id=actor_user_id,
        entity_type="user",
        entity_id=user.id,
        details={"email": user.email, "role": user.role},
    )
    return user
