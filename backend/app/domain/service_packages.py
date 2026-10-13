"""Service-package domain rules and the automation-gating decision
(ADR-0013 item 4; data-model §2.9 ``ServicePackage``/``ServiceAssignment``;
the §5.1 three-leg gating invariant).

A package carries **two lists on two different axes**, and telling them apart
is what this module exists for:

- ``included_services[]`` says **which automations may run**. Its values come
  from the §2.2 capability-flag vocabulary (``AutomationType``), and gating is
  per-service, not per-company: a package that omits ``ai_reply_parsing``
  leaves reply parsing manual for its companies even while its other
  automations run. An empty list is a valid manual-only package.
- ``covered_forms[]`` says **what work this package covers**. Its values are
  the form codes ``backend/forms/`` ships (``CoveredForm``). This is the axis
  the automation list cannot express: paying for the coming year's annual
  return authorises NAR1 work and *not* a director or address change (ND2A),
  however many automations the same package includes (founder direction,
  2026-09-24). Unlike the automation list, an empty list is refused — a
  package that covers no work would release nothing at all, and "covers
  everything" is exactly the permissive default a declared list avoids.

``decide_automation`` is the single pure decision behind the §5.1
invariant: an automated action proceeds only when (a) the tenant's tier
carries the capability flag, (b) the company's assignment is active and
within its coverage period, and (c) the automation type is listed in the
package's included services. It takes plain values (not ORM rows) so it
stays framework-free; the application layer resolves the rows and every
automation trigger consults the one application seam
(``app.application.service_packages.check_automation``).

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.domain.prior_filing import FormCode


class AutomationType(StrEnum):
    """The §2.2 capability-flag vocabulary reused for ``included_services[]``.

    Each value is both a tenant-tier capability flag (leg a — vendor-level
    licence) and an automation type a ``ServicePackage`` can cover (leg c —
    TCSP-level commercial coverage).
    """

    CALENDAR_SYNC = "calendar_sync"
    AUTOMATED_PREFILL = "automated_prefill"
    EMAIL_INGESTION = "email_ingestion"
    DOCUMENT_EXTRACTION = "document_extraction"
    AI_REPLY_PARSING = "ai_reply_parsing"
    CONFIRMATION_GENERATION = "confirmation_generation"
    WORKFLOW_ORCHESTRATION = "workflow_orchestration"


class CoveredForm(StrEnum):
    """The form codes ``covered_forms[]`` is drawn from — the work a package covers.

    The values are the codes the form registry ships in its manifests
    (``backend/forms/*/manifest.json``'s ``form_code``, ``NAR1`` and
    ``ND2A``), so this is the registry's vocabulary and not the contract's
    lower-case ``form_type``; ``covered_form`` translates between the two, and
    a test holds both this enum and that translation to the registry rather
    than to this file. A second list beside ``included_services`` on purpose:
    that one answers which automations may run, this one answers which work
    the client has paid for.
    """

    NAR1 = "NAR1"
    ND2A = "ND2A"


# The contract's form vocabulary (``prior_filing.FormCode``, whose value names
# a ``backend/forms/`` directory) and the CR form code that directory's
# manifest carries are the same form in two notations. The pairing is stated
# once, here, and pinned by a test that reads the registry's manifests — so a
# form whose registry code moves fails the test rather than silently ceasing
# to be coverable.
_REGISTRY_FORM_CODES: Mapping[FormCode, CoveredForm] = {
    FormCode.NAR1: CoveredForm.NAR1,
    FormCode.ND2A: CoveredForm.ND2A,
}


def covered_form(form_type: str) -> CoveredForm | None:
    """The registry form code a contract ``form_type`` names, or ``None``.

    ``None`` means the value names no form the registry ships, so no package
    can have declared coverage for it. A caller judging a piece of work reads
    that as "not covered" — the fail-safe answer — rather than as an error to
    raise.
    """
    try:
        directory = FormCode(form_type)
    except ValueError:
        return None
    return _REGISTRY_FORM_CODES.get(directory)


class AssignmentStatus(StrEnum):
    """``ServiceAssignment.status`` vocabulary (mirrors the DB check).

    ``ended`` is a manual early termination; lapse needs no status — it is
    derived from the coverage period at decision time.
    """

    ACTIVE = "active"
    ENDED = "ended"


class GatingReason(StrEnum):
    """Why an automated action is denied, in the §5.1 leg order.

    Feeds the dashboard's "service not covered — no automated action" flags
    (scope §2.16, contract §5.1 Dashboard row).
    """

    TIER_CAPABILITY_MISSING = "tier_capability_missing"
    NO_ASSIGNMENT = "no_assignment"
    ASSIGNMENT_ENDED = "assignment_ended"
    OUT_OF_COVERAGE = "out_of_coverage"
    SERVICE_NOT_INCLUDED = "service_not_included"


class UnknownServiceError(ValueError):
    """``included_services`` carries values outside the §2.2 vocabulary.

    Carries every unknown value (not just the first) so a response can
    report them all.
    """

    def __init__(self, unknown: list[str]) -> None:
        self.unknown = tuple(unknown)
        super().__init__(f"unknown services: {', '.join(unknown)}")


@dataclass(frozen=True)
class AutomationGateDecision:
    """The outcome of the §5.1 gate: allowed, or denied with the reason of
    the first failing leg (checked in contract order a → b → c)."""

    allowed: bool
    reason: GatingReason | None


def validate_included_services(values: Collection[str]) -> list[str]:
    """The trimmed, deduplicated service list, or ``UnknownServiceError``.

    Every value must be in the §2.2 vocabulary; duplicates collapse
    preserving first-seen order. An empty list is valid — a package that
    covers no automation types leaves its companies fully manual.
    """
    known = {member.value for member in AutomationType}
    unknown = sorted({value.strip() for value in values} - known)
    if unknown:
        raise UnknownServiceError(unknown)
    return list(dict.fromkeys(value.strip() for value in values))


class CoveredFormsError(ValueError):
    """``covered_forms`` is empty, or names codes the registry does not ship.

    One message rather than a list, because only one of the two can be true: a
    list that names no form has no unknown value to report. The message is what
    the API answers with (``422 unknown forms: NN3`` or ``422 covered_forms
    must name at least one form``), the way the naming-template rules report
    their own violations.
    """


def validate_covered_forms(values: Collection[str]) -> list[str]:
    """The trimmed, deduplicated covered-form list, or ``CoveredFormsError``.

    Every value must be a form code the registry ships (``CoveredForm``), and
    the list must name at least one: unlike ``included_services``, an empty
    list is not a valid package here — a package with no covered work would
    release nothing at all, and silently covering everything is the permissive
    default this declared list exists to avoid. Duplicates collapse preserving
    first-seen order.
    """
    forms = [form for form in dict.fromkeys(value.strip() for value in values) if form]
    if not forms:
        raise CoveredFormsError("covered_forms must name at least one form")
    unknown = sorted(set(forms) - {member.value for member in CoveredForm})
    if unknown:
        raise CoveredFormsError(f"unknown forms: {', '.join(unknown)}")
    return forms


def decide_automation(
    *,
    automation_type: AutomationType,
    tenant_capabilities: Collection[AutomationType],
    assignment_status: str | None,
    coverage_start: date | None,
    coverage_end: date | None,
    included_services: Collection[str],
    as_of: date,
) -> AutomationGateDecision:
    """The §5.1 three-leg gate, checked in contract order a → b → c.

    ``assignment_status``/``coverage_*`` are ``None`` when the company has
    no assignment at all (leg b fails as ``no_assignment``); the caller
    supplies them together otherwise. Coverage boundaries are inclusive — a
    retainer covers its first and last day. Legs b and c are only reached
    when the earlier legs pass, so the reason always names the first
    failing leg.
    """
    if automation_type not in tenant_capabilities:
        return AutomationGateDecision(allowed=False, reason=GatingReason.TIER_CAPABILITY_MISSING)
    if assignment_status is None:
        return AutomationGateDecision(allowed=False, reason=GatingReason.NO_ASSIGNMENT)
    if assignment_status != AssignmentStatus.ACTIVE:
        return AutomationGateDecision(allowed=False, reason=GatingReason.ASSIGNMENT_ENDED)
    assert coverage_start is not None and coverage_end is not None  # caller contract
    if not coverage_start <= as_of <= coverage_end:
        return AutomationGateDecision(allowed=False, reason=GatingReason.OUT_OF_COVERAGE)
    if automation_type.value not in included_services:
        return AutomationGateDecision(allowed=False, reason=GatingReason.SERVICE_NOT_INCLUDED)
    return AutomationGateDecision(allowed=True, reason=None)
