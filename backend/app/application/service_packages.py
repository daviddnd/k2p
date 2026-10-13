"""Service-package orchestration: the only write path for packages and
assignments, and the single automation-gating seam (ADR-0013 item 4;
data-model §2.9, §5.1 invariant).

- ``create_package`` / ``update_package`` / ``delete_package`` /
  ``get_package`` / ``list_packages`` manage tenant-level package
  definitions. ``included_services`` is validated by the single domain
  rule (``app.domain.service_packages.validate_included_services``) and so is
  ``covered_forms`` (``validate_covered_forms`` — the registry's form codes,
  at least one of them); the per-tenant unique ``name`` is pre-checked with the
  constraint as backstop. ``delete_package`` refuses while any assignment
  references the package — the commercial history is kept.
- ``create_assignment`` / ``update_assignment`` /
  ``list_company_assignments`` manage company coverage. At most one
  ``active`` assignment per company — pre-checked here, with the partial
  unique index as the database backstop — so the gating decision never
  arbitrates between competing packages. Renewal is explicit: end the
  current assignment, then create the next.
- ``check_automation`` is the one application service every automation
  trigger consults (ADR-0013: the check lives in exactly one place). It
  resolves the company's relevant assignment (the active one, else the
  latest) plus its package, sources the tenant's tier capability leg from
  the entitlement seam (``app.application.entitlement`` — deployment
  configuration today, an activation lease later; a lapsed lease degrades
  the capability set to Free, §4.3), and delegates to the pure domain
  decision.
- ``gating_decisions_for_companies`` reads the same gate for many companies
  at once — it reports what the triggers would decide (the dashboard's
  "service not covered" flags, contract §5.1 Dashboard row) through the
  same helpers and the same pure decision, without being a trigger.

Cross-tenant ids are indistinguishable from unknown (404 both ways) via the
shared ``_get_package`` seam and the ``company_master.get_company`` seam.

Services flush but never commit — the API session dependency commits at
the request edge. Every write path emits its audit event on the same
session via ``app.application.audit.record_event`` (ADR-0009).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.service_packages import ServiceAssignment, ServicePackage
from app.application import audit, company_master, entitlement
from app.domain.audit import AuditAction
from app.domain.service_packages import (
    AssignmentStatus,
    AutomationGateDecision,
    AutomationType,
    decide_automation,
    validate_covered_forms,
    validate_included_services,
)


class PackageNotFoundError(Exception):
    """No package with this id exists in the tenant (cross-tenant → unknown)."""


class PackageNameInUseError(Exception):
    """The package name is already taken in the tenant."""


class PackageInUseError(Exception):
    """The package cannot be deleted while assignments reference it."""


class AssignmentNotFoundError(Exception):
    """No assignment with this id exists on the company (cross-tenant → unknown)."""


class ActiveAssignmentExistsError(Exception):
    """The company already has an active assignment (end it before re-assigning)."""


class InvalidCoverageError(Exception):
    """The update would leave coverage_start after coverage_end."""


class InvalidFeeError(Exception):
    """The fee is not a valid non-negative decimal amount."""


async def _get_package(
    session: AsyncSession, *, tenant_id: UUID, package_id: UUID
) -> ServicePackage:
    """The package, or ``PackageNotFoundError`` when it is not in the tenant."""
    package = await session.scalar(
        select(ServicePackage).where(
            ServicePackage.id == package_id, ServicePackage.tenant_id == tenant_id
        )
    )
    if package is None:
        raise PackageNotFoundError
    return package


def _parse_fee(fee: str) -> Decimal:
    """The fee as a ``Decimal`` within the ``Numeric(19, 2)`` column."""
    try:
        amount = Decimal(fee.strip())
    except InvalidOperation:
        raise InvalidFeeError from None
    if (
        amount.is_nan()
        or amount.is_infinite()
        or amount < 0
        or amount > Decimal("99999999999999999.99")
    ):
        raise InvalidFeeError
    return amount


# --- Packages ---


async def create_package(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    name: str,
    covered_forms: Collection[str],
    included_services: Collection[str] = (),
    fee: str | None = None,
) -> ServicePackage:
    """Insert a package in the actor's tenant.

    The two lists are validated by their single domain rules (unknown values
    raise ``UnknownServiceError`` / ``CoveredFormsError``) — and
    ``covered_forms`` is a required argument with no default, so a package
    cannot be created without declaring the work it covers. A duplicate name in
    the tenant raises ``PackageNameInUseError`` with the unique constraint as
    backstop.
    """
    services = validate_included_services(included_services)
    forms = validate_covered_forms(covered_forms)
    existing = await session.scalar(
        select(ServicePackage.id).where(
            ServicePackage.tenant_id == tenant_id, ServicePackage.name == name
        )
    )
    if existing is not None:
        raise PackageNameInUseError
    package = ServicePackage(
        tenant_id=tenant_id,
        name=name,
        included_services=services,
        covered_forms=forms,
        fee=_parse_fee(fee) if fee is not None else None,
    )
    session.add(package)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SERVICE_PACKAGE_CREATED,
        actor_user_id=actor_user_id,
        entity_type="service_package",
        entity_id=package.id,
        details={
            "name": package.name,
            "included_services": list(package.included_services),
            "covered_forms": list(package.covered_forms),
            "fee": package.fee,
        },
    )
    return package


async def update_package(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    package_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> ServicePackage:
    """Partially overwrite a package in place.

    Supported ``changes`` keys are exactly ``name``, ``included_services``,
    ``covered_forms``, ``fee``; a ``fee`` present with value ``None`` clears it
    (packages are not effective-dated — no regulatory as-of semantics). A
    renamed package is re-checked against the per-tenant unique name, and a new
    ``covered_forms`` list is validated like a creation's — including its
    non-emptiness, so a package can never be narrowed to covering nothing.
    """
    package = await _get_package(session, tenant_id=tenant_id, package_id=package_id)
    before = {field: getattr(package, field) for field in changes}
    if "name" in changes:
        new_name = changes["name"]
        if new_name != package.name:
            existing = await session.scalar(
                select(ServicePackage.id).where(
                    ServicePackage.tenant_id == tenant_id,
                    ServicePackage.name == new_name,
                    ServicePackage.id != package.id,
                )
            )
            if existing is not None:
                raise PackageNameInUseError
            package.name = new_name
    if "included_services" in changes:
        package.included_services = validate_included_services(changes["included_services"])
    if "covered_forms" in changes:
        package.covered_forms = validate_covered_forms(changes["covered_forms"])
    if "fee" in changes:
        fee = changes["fee"]
        package.fee = _parse_fee(fee) if fee is not None else None
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SERVICE_PACKAGE_UPDATED,
        actor_user_id=actor_user_id,
        entity_type="service_package",
        entity_id=package.id,
        details={
            "before": before,
            "after": {field: getattr(package, field) for field in changes},
        },
    )
    return package


async def get_package(
    session: AsyncSession, *, tenant_id: UUID, package_id: UUID
) -> ServicePackage:
    """One package; 404 for unknown or cross-tenant ids."""
    return await _get_package(session, tenant_id=tenant_id, package_id=package_id)


async def list_packages(session: AsyncSession, *, tenant_id: UUID) -> list[ServicePackage]:
    """Every package in the tenant, ordered for a stable listing."""
    rows = await session.scalars(
        select(ServicePackage)
        .where(ServicePackage.tenant_id == tenant_id)
        .order_by(ServicePackage.created_at)
    )
    return list(rows.all())


async def delete_package(
    session: AsyncSession, *, tenant_id: UUID, package_id: UUID, actor_user_id: UUID | None
) -> None:
    """Delete a package; ``PackageInUseError`` while assignments reference it.

    The assignment history is the TCSP's commercial record, so a referenced
    package is never removed underneath it.
    """
    package = await _get_package(session, tenant_id=tenant_id, package_id=package_id)
    referenced = await session.scalar(
        select(func.count())
        .select_from(ServiceAssignment)
        .where(ServiceAssignment.package_id == package.id)
    )
    if referenced:
        raise PackageInUseError
    await session.delete(package)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SERVICE_PACKAGE_DELETED,
        actor_user_id=actor_user_id,
        entity_type="service_package",
        entity_id=package.id,
        details={"name": package.name},
    )


# --- Assignments ---


async def _get_active_assignment(
    session: AsyncSession, *, company_id: UUID
) -> ServiceAssignment | None:
    assignment: ServiceAssignment | None = await session.scalar(
        select(ServiceAssignment).where(
            ServiceAssignment.company_id == company_id,
            ServiceAssignment.status == AssignmentStatus.ACTIVE.value,
        )
    )
    return assignment


async def create_assignment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    package_id: UUID,
    actor_user_id: UUID | None,
    coverage_start: date,
    coverage_end: date,
) -> ServiceAssignment:
    """Assign a company to a package with a coverage period (status ``active``).

    Both the company and the package must belong to the tenant — unknown
    and cross-tenant ids are indistinguishable via the shared seams, and
    the composite foreign keys make a cross-tenant row uninsertable
    regardless. A company with an existing active assignment raises
    ``ActiveAssignmentExistsError`` (end it first — renewal is explicit);
    the partial unique index is the database backstop.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    package = await _get_package(session, tenant_id=tenant_id, package_id=package_id)
    if await _get_active_assignment(session, company_id=company.id) is not None:
        raise ActiveAssignmentExistsError
    assignment = ServiceAssignment(
        company_id=company.id,
        package_id=package.id,
        tenant_id=tenant_id,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        status=AssignmentStatus.ACTIVE.value,
    )
    session.add(assignment)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SERVICE_ASSIGNMENT_CREATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="service_assignment",
        entity_id=assignment.id,
        details={
            "package_id": package.id,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
        },
    )
    return assignment


async def update_assignment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    assignment_id: UUID,
    actor_user_id: UUID | None,
    changes: Mapping[str, Any],
) -> ServiceAssignment:
    """Partially update coverage dates and/or status (``None`` values omitted).

    Supported ``changes`` keys are exactly ``coverage_start``,
    ``coverage_end``, ``status`` (an ``AssignmentStatus``). The resulting
    window must satisfy ``coverage_start <= coverage_end``
    (``InvalidCoverageError``; the DB check is the backstop). Re-activating
    an ended assignment is rejected while another active assignment exists
    for the company (``ActiveAssignmentExistsError``).
    """
    await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    assignment = await session.scalar(
        select(ServiceAssignment).where(
            ServiceAssignment.id == assignment_id,
            ServiceAssignment.company_id == company_id,
            ServiceAssignment.tenant_id == tenant_id,
        )
    )
    if assignment is None:
        raise AssignmentNotFoundError
    before = {field: getattr(assignment, field) for field in changes}
    if "coverage_start" in changes:
        assignment.coverage_start = changes["coverage_start"]
    if "coverage_end" in changes:
        assignment.coverage_end = changes["coverage_end"]
    if assignment.coverage_start > assignment.coverage_end:
        raise InvalidCoverageError
    if "status" in changes:
        status = changes["status"]
        activating = (
            status is AssignmentStatus.ACTIVE
            and assignment.status != AssignmentStatus.ACTIVE
        )
        if activating and await _get_active_assignment(session, company_id=company_id) is not None:
            raise ActiveAssignmentExistsError
        assignment.status = status.value
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SERVICE_ASSIGNMENT_UPDATED,
        actor_user_id=actor_user_id,
        company_id=company_id,
        entity_type="service_assignment",
        entity_id=assignment.id,
        details={
            "before": before,
            "after": {field: getattr(assignment, field) for field in changes},
        },
    )
    return assignment


async def list_company_assignments(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[ServiceAssignment]:
    """The company's assignments (current and historical), stable order."""
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = await session.scalars(
        select(ServiceAssignment)
        .where(ServiceAssignment.company_id == company.id)
        .order_by(ServiceAssignment.created_at)
    )
    return list(rows.all())


# --- Commercial coverage, read for the gates that need it ---


@dataclass(frozen=True)
class ServiceCoverage:
    """A company's assignment and the work its package covers, resolved.

    The §5.1 gate's legs b and c as plain values, plus the package's
    ``covered_forms`` — the work dimension no ``AutomationType`` leg can
    express. ``assignment_status``/``coverage_*`` are ``None`` together when
    the company has no assignment at all, and ``covered_forms`` is then empty
    too: the legs an assignment would have satisfied are exactly what refuses
    it first.
    """

    assignment_status: str | None
    coverage_start: date | None
    coverage_end: date | None
    covered_forms: tuple[str, ...]


def _coverage(
    current: ServiceAssignment | None, package: ServicePackage | None
) -> ServiceCoverage:
    """One company's coverage from already-resolved rows — the tail the single
    and the batch read share, so the two cannot drift."""
    return ServiceCoverage(
        assignment_status=current.status if current is not None else None,
        coverage_start=current.coverage_start if current is not None else None,
        coverage_end=current.coverage_end if current is not None else None,
        covered_forms=tuple(package.covered_forms) if package is not None else (),
    )


async def service_coverage(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> ServiceCoverage:
    """The company's current coverage: the relevant assignment and its package.

    The same resolution the §5.1 gate uses — ``_current_assignment`` and
    ``_package_of``, the helpers ``check_automation`` itself calls, because two
    gates that disagreed about which package covers a company would be a bug
    neither of them could see. It is not itself a gate: it says what the rows
    hold and decides nothing (``app.domain.service_renewals.
    release_block_reason`` decides). ``CompanyNotFoundError`` for an unknown or
    cross-tenant company, like every other company-scoped read.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    current = _current_assignment(await _assignments_of_company(session, company_id=company.id))
    package = await _package_of(session, tenant_id=tenant_id, assignment=current)
    return _coverage(current, package)


async def service_coverage_for_companies(
    session: AsyncSession, *, company_ids: Sequence[UUID]
) -> dict[UUID, ServiceCoverage]:
    """The same coverage for many companies, in a few queries.

    The §5.1.9 panel's own read: it lists periods per company, so resolving
    each company's assignment and package separately would be two queries per
    company for data that does not change within one request. Every visible
    company gets an entry — a company with no assignment gets the empty
    coverage rather than a missing key — and the same ``_coverage`` tail builds
    them, the way ``gating_decisions_for_companies`` loads its own legs once.
    """
    assignments = list(
        (
            await session.scalars(
                select(ServiceAssignment)
                .where(ServiceAssignment.company_id.in_(company_ids))
                .order_by(ServiceAssignment.company_id, ServiceAssignment.created_at)
            )
        ).all()
    )
    by_company: dict[UUID, list[ServiceAssignment]] = {}
    for assignment in assignments:
        by_company.setdefault(assignment.company_id, []).append(assignment)
    package_ids = {assignment.package_id for assignment in assignments}
    package_rows = await session.scalars(
        select(ServicePackage).where(ServicePackage.id.in_(package_ids))
    )
    packages = {package.id: package for package in package_rows.all()}
    coverage: dict[UUID, ServiceCoverage] = {}
    for company_id in company_ids:
        current = _current_assignment(by_company.get(company_id, []))
        package = packages.get(current.package_id) if current is not None else None
        coverage[company_id] = _coverage(current, package)
    return coverage


# --- The single automation-gating seam (ADR-0013, §5.1 invariant) ---


async def check_automation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    automation_type: AutomationType,
    as_of: date,
) -> AutomationGateDecision:
    """The §5.1 gate for one automated action on one company.

    Every automation trigger (confirmation requests, reminders, AI reply
    parsing, document extraction, draft generation, calendar sync) consults
    this one service — the check lives in exactly one place per ADR-0013.
    The relevant assignment is the active one, else the latest by creation,
    so the denial reason distinguishes ``no_assignment`` / ``assignment_ended``
    / ``out_of_coverage`` for the dashboard's "service not covered" flags.
    The tier capability leg is read from the entitlement seam here, not
    supplied by the caller: an unlicensed (or lapsed-lease) deployment fails
    closed with ``tier_capability_missing``.
    """
    capabilities = (await entitlement.get_entitlement(session)).capabilities
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    current = _current_assignment(await _assignments_of_company(session, company_id=company.id))
    package = await _package_of(session, tenant_id=tenant_id, assignment=current)
    return _decide(
        automation_type=automation_type,
        capabilities=capabilities,
        current=current,
        package=package,
        as_of=as_of,
    )


async def gating_decisions_for_companies(
    session: AsyncSession, *, company_ids: Sequence[UUID], as_of: date
) -> dict[UUID, dict[AutomationType, AutomationGateDecision]]:
    """The §5.1 gate for **every** automation type, read for many companies.

    The dashboard's "service not covered" read (contract §5.1 Dashboard
    row): it reports what the triggers would decide, resolving the same
    three legs through the same helpers as ``check_automation`` — the tier
    capability set from the entitlement seam, each company's active-or-latest
    assignment, its package — but loads them for a portfolio in a few
    queries instead of two per company, then delegates to the same pure
    ``decide_automation``. It is not itself a trigger: nothing here is
    granted or denied, only reported. Every ``AutomationType`` is decided
    for every company (definition order), so flags can name the type
    (``service_not_included``) as well as the company (the leg-b reasons).
    """
    capabilities = (await entitlement.get_entitlement(session)).capabilities
    assignments = list(
        (
            await session.scalars(
                select(ServiceAssignment)
                .where(ServiceAssignment.company_id.in_(company_ids))
                .order_by(ServiceAssignment.company_id, ServiceAssignment.created_at)
            )
        ).all()
    )
    by_company: dict[UUID, list[ServiceAssignment]] = {}
    for assignment in assignments:
        by_company.setdefault(assignment.company_id, []).append(assignment)
    package_ids = {assignment.package_id for assignment in assignments}
    package_rows = await session.scalars(
        select(ServicePackage).where(ServicePackage.id.in_(package_ids))
    )
    packages = {package.id: package for package in package_rows.all()}
    decisions: dict[UUID, dict[AutomationType, AutomationGateDecision]] = {}
    for company_id in company_ids:
        current = _current_assignment(by_company.get(company_id, []))
        package = packages.get(current.package_id) if current is not None else None
        decisions[company_id] = {
            automation_type: _decide(
                automation_type=automation_type,
                capabilities=capabilities,
                current=current,
                package=package,
                as_of=as_of,
            )
            for automation_type in AutomationType
        }
    return decisions


async def _assignments_of_company(
    session: AsyncSession, *, company_id: UUID
) -> list[ServiceAssignment]:
    rows = await session.scalars(
        select(ServiceAssignment)
        .where(ServiceAssignment.company_id == company_id)
        .order_by(ServiceAssignment.created_at)
    )
    return list(rows.all())


def _current_assignment(assignments: Sequence[ServiceAssignment]) -> ServiceAssignment | None:
    """The company's relevant assignment per the §5.1 gate: the active one,
    else the latest by creation (so the denial reason names ``assignment_ended``
    / ``out_of_coverage`` rather than ``no_assignment``)."""
    return next(
        (a for a in assignments if a.status == AssignmentStatus.ACTIVE.value),
        assignments[-1] if assignments else None,
    )


async def _package_of(
    session: AsyncSession, *, tenant_id: UUID, assignment: ServiceAssignment | None
) -> ServicePackage | None:
    if assignment is None:
        return None
    return await _get_package(session, tenant_id=tenant_id, package_id=assignment.package_id)


def _decide(
    *,
    automation_type: AutomationType,
    capabilities: Collection[AutomationType],
    current: ServiceAssignment | None,
    package: ServicePackage | None,
    as_of: date,
) -> AutomationGateDecision:
    """One decision from already-resolved rows — the tail both the single
    and the portfolio gate share, so the two can never drift."""
    return decide_automation(
        automation_type=automation_type,
        tenant_capabilities=capabilities,
        assignment_status=current.status if current is not None else None,
        coverage_start=current.coverage_start if current is not None else None,
        coverage_end=current.coverage_end if current is not None else None,
        included_services=package.included_services if package is not None else (),
        as_of=as_of,
    )
