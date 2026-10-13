"""Authorization vocabulary and the single allow/deny decision (scope §2.14).

Pure and deterministic per ADR-0006: the API layer loads an ``ActorContext``
once per request and calls ``is_allowed``. This module is the only place the
decision is made; it denies unless role, permission grant, and per-company
access grant explicitly allow.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    """TCSP staff roles (scope §2.14). External contacts are never users."""

    ADMINISTRATOR = "administrator"
    GENERAL = "general"


class Permission(StrEnum):
    """Per-account permission grants on General accounts (data-model §2.9)."""

    REVIEW_EVENTS = "review_events"
    APPROVE_EVENTS = "approve_events"
    REVIEW_DRAFTS = "review_drafts"
    APPROVE_DRAFTS = "approve_drafts"


@dataclass(frozen=True)
class ActorContext:
    """An actor's RBAC-relevant facts, loaded once per request.

    ``company_ids`` holds the General account's per-company access grants;
    it is ignored for Administrators, who may act on every tenant company.
    """

    is_active: bool
    role: Role
    permissions: frozenset[Permission]
    company_ids: frozenset[UUID]


def is_allowed(context: ActorContext, permission: Permission, company_id: UUID | None) -> bool:
    """The single authorization decision. Deny unless explicitly allowed.

    - Inactive accounts may do nothing.
    - Administrators may perform any action in their tenant (scope §2.14:
      an Administrator can always act as back-up reviewer/approver).
    - General accounts need both the explicit permission grant and a
      per-company access grant for the target company.
    """
    if not context.is_active:
        return False
    if context.role is Role.ADMINISTRATOR:
        return True
    if permission not in context.permissions:
        return False
    return company_id is not None and company_id in context.company_ids


def is_admin(context: ActorContext) -> bool:
    """Whether the actor performs administrative actions: Administrators only.

    The administrative rule, stated once for every caller that has to pick
    an administrative path rather than grant-gated staff work (anchor
    statics correction, the plan's "no permission flag covers this" cases).
    Inactive accounts may do nothing; General accounts never pass, whatever
    grants they hold — the action is not theirs to perform at any scope.
    """
    return context.is_active and context.role is Role.ADMINISTRATOR


def is_active(context: ActorContext) -> bool:
    """Whether the account is usable at all — every decision's first leg.

    Stated once for callers that gate on nothing else: the portfolio-wide
    reads (dashboard, compliance calendar), which are open to every active
    member because the *data* scoping — Administrators see the whole
    tenant, General accounts exactly their grants — is the application
    layer's rule. An inactive account may read nothing.
    """
    return context.is_active


def can_access_company(context: ActorContext, company_id: UUID) -> bool:
    """Grant-only per-company access, for actions no §2.9 permission covers.

    - Inactive accounts may do nothing.
    - Administrators may access every tenant company (scope §2.14 back-up
      rule).
    - General accounts need a per-company access grant for the company; no
      permission flag is required — the four §2.9 flags gate only the
      review/approve workflow, while master-data reads and fact appends are
      ordinary staff work (founder decision 2026-08-24).

    Deny by default: no grant, no access.
    """
    if not context.is_active:
        return False
    if context.role is Role.ADMINISTRATOR:
        return True
    return company_id in context.company_ids
