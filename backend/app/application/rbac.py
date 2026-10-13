"""Loads an actor's RBAC context from the identity schema.

The API layer's shared dependency calls this once per request; the domain
``is_allowed`` function makes the decision from the returned context.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import Company, CompanyAccessGrant, User
from app.domain.rbac import ActorContext, Permission, Role


async def load_actor_context(
    session: AsyncSession, *, user_id: UUID, tenant_id: UUID
) -> ActorContext | None:
    """Return the actor's RBAC context, or None if the user is unknown or
    belongs to a different tenant (tenant mismatch is never authorized)."""
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        return None
    # Grants are confined to the user's tenant at both paths: the schema's
    # composite foreign keys make cross-tenant rows uninsertable, and this
    # join filters them at read time regardless of how a row was written.
    result = await session.scalars(
        select(CompanyAccessGrant.company_id)
        .join(Company, Company.id == CompanyAccessGrant.company_id)
        .where(CompanyAccessGrant.user_id == user.id, Company.tenant_id == tenant_id)
    )
    return ActorContext(
        is_active=user.is_active,
        role=Role(user.role),
        permissions=frozenset(
            permission
            for permission, granted in [
                (Permission.REVIEW_EVENTS, user.review_events),
                (Permission.APPROVE_EVENTS, user.approve_events),
                (Permission.REVIEW_DRAFTS, user.review_drafts),
                (Permission.APPROVE_DRAFTS, user.approve_drafts),
            ]
            if granted
        ),
        company_ids=frozenset(result.all()),
    )
