"""Domain authorization decision tests (scope §2.14, data-model §2.9).

The decision must deny by default: an actor is allowed only when the role,
the per-account permission grant, and the per-company access grant all say so.
"""

from uuid import uuid4

from app.domain.rbac import (
    ActorContext,
    Permission,
    Role,
    can_access_company,
    is_active,
    is_allowed,
)

COMPANY_A = uuid4()
COMPANY_B = uuid4()


def general_context(
    *,
    is_active: bool = True,
    permissions: frozenset[Permission] = frozenset(),
    company_ids: frozenset = frozenset(),
) -> ActorContext:
    return ActorContext(
        is_active=is_active,
        role=Role.GENERAL,
        permissions=permissions,
        company_ids=company_ids,
    )


def admin_context(*, is_active: bool = True) -> ActorContext:
    return ActorContext(
        is_active=is_active,
        role=Role.ADMINISTRATOR,
        permissions=frozenset(),
        company_ids=frozenset(),
    )


def test_denies_everything_by_default() -> None:
    context = general_context()
    for permission in Permission:
        assert not is_allowed(context, permission, COMPANY_A)


def test_general_with_permission_and_company_grant_is_allowed() -> None:
    context = general_context(
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    assert is_allowed(context, Permission.REVIEW_EVENTS, COMPANY_A)


def test_general_without_permission_is_denied_even_with_company_grant() -> None:
    context = general_context(company_ids=frozenset({COMPANY_A}))
    assert not is_allowed(context, Permission.REVIEW_EVENTS, COMPANY_A)


def test_general_permission_does_not_leak_across_companies() -> None:
    context = general_context(
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    assert not is_allowed(context, Permission.REVIEW_EVENTS, COMPANY_B)


def test_general_with_wrong_permission_is_denied() -> None:
    context = general_context(
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    assert not is_allowed(context, Permission.APPROVE_EVENTS, COMPANY_A)


def test_general_denied_when_no_company_in_scope() -> None:
    context = general_context(
        permissions=frozenset({Permission.REVIEW_EVENTS}),
        company_ids=frozenset({COMPANY_A}),
    )
    assert not is_allowed(context, Permission.REVIEW_EVENTS, None)


def test_inactive_account_is_denied_everything() -> None:
    assert not is_allowed(
        general_context(
            is_active=False,
            permissions=frozenset(Permission),
            company_ids=frozenset({COMPANY_A}),
        ),
        Permission.REVIEW_EVENTS,
        COMPANY_A,
    )
    assert not is_allowed(admin_context(is_active=False), Permission.APPROVE_DRAFTS, COMPANY_A)


def test_administrator_may_perform_any_action() -> None:
    context = admin_context()
    for permission in Permission:
        assert is_allowed(context, permission, COMPANY_A)
    assert is_allowed(context, Permission.APPROVE_EVENTS, None)


# --- can_access_company: grant-only access, no §2.9 permission flag ---


def test_access_denies_general_by_default() -> None:
    assert not can_access_company(general_context(), COMPANY_A)


def test_access_general_with_company_grant_is_allowed_without_permission() -> None:
    context = general_context(company_ids=frozenset({COMPANY_A}))
    assert can_access_company(context, COMPANY_A)


def test_access_general_grant_does_not_leak_across_companies() -> None:
    context = general_context(company_ids=frozenset({COMPANY_A}))
    assert not can_access_company(context, COMPANY_B)


def test_access_inactive_account_is_denied() -> None:
    assert not can_access_company(
        general_context(is_active=False, company_ids=frozenset({COMPANY_A})), COMPANY_A
    )
    assert not can_access_company(admin_context(is_active=False), COMPANY_A)


def test_access_administrator_may_access_any_company() -> None:
    assert can_access_company(admin_context(), COMPANY_A)


# --- is_active: the inactivity gate for the portfolio-wide reads ---


def test_active_account_is_active() -> None:
    assert is_active(general_context())
    assert is_active(admin_context())


def test_inactive_account_is_not_active() -> None:
    assert not is_active(general_context(is_active=False, company_ids=frozenset({COMPANY_A})))
    assert not is_active(admin_context(is_active=False))
