"""Service-package and service-assignment endpoints (ADR-0013 item 4;
data-model §5.1 Service packages row, §2.9).

Implements TCSP-defined service packages with per-service automation
gating: packages name the automation types they cover from the §2.2
capability vocabulary, and assignments give each client company a coverage
period. A package also declares the **work** it covers (`covered_forms`, the
registry's form codes) — the axis the automation list cannot express, and what
the client-release gate reads (§5.1.9): paying for the coming year's annual
return does not authorise a director or address change. This is tracking only
— no invoicing or billing in v1.0.

Every route is Administrator-only via ``require_admin`` (design decision
2026-08-24): packages and commercial coverage are administrative
configuration, consistent with the contacts (Flow 1 step 3) and
tenant-config decisions; fail-closed is the default.

Mutations route through ``app.application.service_packages`` — the only
write path. The §5.1 gating decision itself is not an endpoint: every
automation trigger consults the single application seam
(``check_automation``) directly. Every mutation emits its audit event in the
same transaction (ADR-0009).
"""

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.service_packages import ServiceAssignment, ServicePackage
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import service_packages
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext
from app.domain.service_packages import (
    AssignmentStatus,
    CoveredFormsError,
    UnknownServiceError,
)

router = APIRouter(prefix="/api/v1", tags=["service-packages"])

_FEE_PATTERN = r"^\d{1,17}(\.\d{1,2})?$"


# --- Request payloads ---


class CreatePackageRequest(BaseModel):
    """A new service package; an empty ``included_services`` is a valid
    manual-only package.

    ``covered_forms`` is required and has no default: a package must declare
    the work it covers rather than inherit it, because the release gate reads
    that list (§5.1.9). An empty list is refused by the domain rule — a package
    covering no work would release nothing, which is not a state to reach by
    omission.
    """

    name: str = Field(min_length=1, max_length=200)
    included_services: list[str] = Field(default_factory=list, max_length=20)
    covered_forms: list[str] = Field(max_length=20)
    fee: str | None = Field(default=None, pattern=_FEE_PATTERN)


class UpdatePackageRequest(BaseModel):
    """Partial update: omitted fields are unchanged; ``fee`` present with
    value ``null`` clears it. At least one field is required."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    included_services: list[str] | None = Field(default=None, max_length=20)
    # Deliberately not ``| None`` like the two above: an explicit null would
    # have nothing to mean, because the covered work cannot be emptied. A
    # request that names no field at all still leaves the list alone — only a
    # field the caller actually sent is passed on.
    covered_forms: list[str] = Field(default_factory=list, max_length=20)
    fee: str | None = Field(default=None, pattern=_FEE_PATTERN)

    @model_validator(mode="after")
    def _some_field_required(self) -> "UpdatePackageRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class CreateAssignmentRequest(BaseModel):
    package_id: UUID
    coverage_start: date
    coverage_end: date

    @model_validator(mode="after")
    def _coverage_ordered(self) -> "CreateAssignmentRequest":
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start must not be after coverage_end")
        return self


class UpdateAssignmentRequest(BaseModel):
    """Update coverage dates and/or status; at least one field is required."""

    coverage_start: date | None = None
    coverage_end: date | None = None
    status: AssignmentStatus | None = None

    @model_validator(mode="after")
    def _some_field_required(self) -> "UpdateAssignmentRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


# --- Responses ---


class PackageResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    included_services: list[str]
    covered_forms: list[str]
    fee: str | None


class AssignmentResponse(BaseModel):
    id: UUID
    company_id: UUID
    package_id: UUID
    coverage_start: date
    coverage_end: date
    status: str


def _package_response(package: ServicePackage) -> PackageResponse:
    return PackageResponse(
        id=package.id,
        tenant_id=package.tenant_id,
        name=package.name,
        included_services=list(package.included_services),
        covered_forms=list(package.covered_forms),
        fee=str(package.fee) if package.fee is not None else None,
    )


def _assignment_response(assignment: ServiceAssignment) -> AssignmentResponse:
    return AssignmentResponse(
        id=assignment.id,
        company_id=assignment.company_id,
        package_id=assignment.package_id,
        coverage_start=assignment.coverage_start,
        coverage_end=assignment.coverage_end,
        status=assignment.status,
    )


def _unknown_services(exc: UnknownServiceError) -> HTTPException:
    return HTTPException(status_code=422, detail=f"unknown services: {', '.join(exc.unknown)}")


def _invalid_covered_forms(exc: CoveredFormsError) -> HTTPException:
    """The covered-work refusal: the rule's own message, which names the one
    situation that holds (no form named at all, or a code the registry does not
    ship).

    The rules live in ``app.domain.service_packages`` — this maps them to a 422
    rather than restating either of them.
    """
    return HTTPException(status_code=422, detail=str(exc))


# --- Package routes ---


@router.post("/service-packages", status_code=201)
async def create_package(
    payload: CreatePackageRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PackageResponse:
    """Create a package in the actor's tenant.

    The body carries no ``tenant_id`` — the package lands in the actor's
    own tenant, so cross-tenant creation is impossible by construction.
    ``covered_forms`` is required, and an empty list is refused: a package
    declares the work it covers (§5.1.9's release gate reads it) rather than
    covering nothing by omission. 409 on a duplicate name; 422 on services
    outside the §2.2 vocabulary or forms the registry does not ship.
    """
    try:
        package = await service_packages.create_package(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            name=payload.name,
            included_services=payload.included_services,
            covered_forms=payload.covered_forms,
            fee=payload.fee,
        )
    except UnknownServiceError as exc:
        raise _unknown_services(exc) from None
    except CoveredFormsError as exc:
        raise _invalid_covered_forms(exc) from None
    except service_packages.PackageNameInUseError:
        raise HTTPException(
            status_code=409, detail="package name already in use in this tenant"
        ) from None
    return _package_response(package)


@router.get("/service-packages")
async def list_packages(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PackageResponse]:
    """Every package in the actor's tenant."""
    packages = await service_packages.list_packages(session, tenant_id=actor.tenant_id)
    return [_package_response(package) for package in packages]


@router.get("/service-packages/{package_id}")
async def get_package(
    package_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PackageResponse:
    """One package; 404 for unknown or cross-tenant ids."""
    try:
        package = await service_packages.get_package(
            session, tenant_id=actor.tenant_id, package_id=package_id
        )
    except service_packages.PackageNotFoundError:
        raise HTTPException(status_code=404, detail="package not found") from None
    return _package_response(package)


@router.patch("/service-packages/{package_id}")
async def update_package(
    package_id: UUID,
    payload: UpdatePackageRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PackageResponse:
    """Partial update of name/services/covered forms/fee; explicit ``null`` clears the fee."""
    changes: dict[str, Any] = payload.model_dump(include=payload.model_fields_set)
    try:
        package = await service_packages.update_package(
            session,
            tenant_id=actor.tenant_id,
            package_id=package_id,
            actor_user_id=actor.user_id,
            changes=changes,
        )
    except service_packages.PackageNotFoundError:
        raise HTTPException(status_code=404, detail="package not found") from None
    except UnknownServiceError as exc:
        raise _unknown_services(exc) from None
    except CoveredFormsError as exc:
        raise _invalid_covered_forms(exc) from None
    except service_packages.PackageNameInUseError:
        raise HTTPException(
            status_code=409, detail="package name already in use in this tenant"
        ) from None
    return _package_response(package)


@router.delete("/service-packages/{package_id}", status_code=204)
async def delete_package(
    package_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete a package; 409 while any assignment references it."""
    try:
        await service_packages.delete_package(
            session,
            tenant_id=actor.tenant_id,
            package_id=package_id,
            actor_user_id=actor.user_id,
        )
    except service_packages.PackageNotFoundError:
        raise HTTPException(status_code=404, detail="package not found") from None
    except service_packages.PackageInUseError:
        raise HTTPException(
            status_code=409, detail="package has assignments and cannot be deleted"
        ) from None


# --- Assignment routes ---


@router.post("/companies/{company_id}/service-assignments", status_code=201)
async def create_assignment(
    company_id: UUID,
    payload: CreateAssignmentRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentResponse:
    """Assign the company to a package with a coverage period.

    404 when the company or the package is unknown or cross-tenant; 409
    when the company already has an active assignment (end it first —
    renewal is explicit).
    """
    try:
        assignment = await service_packages.create_assignment(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            package_id=payload.package_id,
            actor_user_id=actor.user_id,
            coverage_start=payload.coverage_start,
            coverage_end=payload.coverage_end,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except service_packages.PackageNotFoundError:
        raise HTTPException(status_code=404, detail="package not found") from None
    except service_packages.ActiveAssignmentExistsError:
        raise HTTPException(
            status_code=409, detail="company already has an active assignment"
        ) from None
    return _assignment_response(assignment)


@router.get("/companies/{company_id}/service-assignments")
async def list_company_assignments(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AssignmentResponse]:
    """The company's assignments (current and historical)."""
    try:
        assignments = await service_packages.list_company_assignments(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_assignment_response(assignment) for assignment in assignments]


@router.patch("/companies/{company_id}/service-assignments/{assignment_id}")
async def update_assignment(
    company_id: UUID,
    assignment_id: UUID,
    payload: UpdateAssignmentRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssignmentResponse:
    """Update coverage dates and/or status.

    422 when the resulting window is out of order; 409 when re-activating
    while another active assignment exists for the company.
    """
    changes: dict[str, Any] = payload.model_dump(include=payload.model_fields_set)
    try:
        assignment = await service_packages.update_assignment(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            assignment_id=assignment_id,
            actor_user_id=actor.user_id,
            changes=changes,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except service_packages.AssignmentNotFoundError:
        raise HTTPException(status_code=404, detail="assignment not found") from None
    except service_packages.InvalidCoverageError:
        raise HTTPException(
            status_code=422, detail="coverage_start must not be after coverage_end"
        ) from None
    except service_packages.ActiveAssignmentExistsError:
        raise HTTPException(
            status_code=409, detail="company already has an active assignment"
        ) from None
    return _assignment_response(assignment)
