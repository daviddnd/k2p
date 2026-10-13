"""Domain tests for service packages and the automation-gating decision
(ADR-0013 item 4; data-model §2.2 capability vocabulary, §2.9
``ServicePackage``/``ServiceAssignment``, §5.1 gating invariant).

Pure and database-free: the gate is exercised as a truth table over its
three legs in the §5.1 order — (a) tenant tier capability, (b) assignment
active and within coverage, (c) automation type in ``included_services``.

One exception to the file's purity, and it is deliberate: the tests of
``covered_forms`` read ``backend/forms/`` through the registry loader, because
the claim under test is that this list's vocabulary *is* the registry's codes.
Checking it against anything but the registry would be checking it against
itself.
"""

from datetime import date, timedelta

import pytest

from app.adapters.forms.registry import image_registry
from app.domain.prior_filing import FormCode
from app.domain.service_packages import (
    AutomationType,
    CoveredForm,
    CoveredFormsError,
    GatingReason,
    UnknownServiceError,
    covered_form,
    decide_automation,
    validate_covered_forms,
    validate_included_services,
)

ALL_CAPABILITIES = frozenset(AutomationType)
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)
AS_OF = date(2026, 6, 30)


def decide(**overrides: object):
    """The gate with every leg passing; ``overrides`` break one leg at a time."""
    kwargs: dict = {
        "automation_type": AutomationType.AI_REPLY_PARSING,
        "tenant_capabilities": ALL_CAPABILITIES,
        "assignment_status": "active",
        "coverage_start": COVERAGE_START,
        "coverage_end": COVERAGE_END,
        "included_services": [AutomationType.AI_REPLY_PARSING.value],
        "as_of": AS_OF,
    }
    kwargs.update(overrides)
    return decide_automation(**kwargs)


# --- §2.2 vocabulary guard ---


def test_automation_type_vocabulary_matches_contract_2_2() -> None:
    assert {member.value for member in AutomationType} == {
        "calendar_sync",
        "automated_prefill",
        "email_ingestion",
        "document_extraction",
        "ai_reply_parsing",
        "confirmation_generation",
        "workflow_orchestration",
    }


# --- validate_included_services ---


def test_validate_accepts_the_full_vocabulary() -> None:
    values = [member.value for member in AutomationType]
    assert validate_included_services(values) == values


def test_validate_allows_an_empty_list_for_a_manual_only_package() -> None:
    assert validate_included_services([]) == []


def test_validate_deduplicates_preserving_order() -> None:
    assert validate_included_services(
        ["ai_reply_parsing", "automated_prefill", "ai_reply_parsing"]
    ) == ["ai_reply_parsing", "automated_prefill"]


def test_validate_lists_every_unknown_value() -> None:
    with pytest.raises(UnknownServiceError) as excinfo:
        validate_included_services(["ai_reply_parsing", "invoicing", "teleportation"])
    assert excinfo.value.unknown == ("invoicing", "teleportation")


# --- covered_forms: the work dimension ---


def test_the_covered_form_vocabulary_is_what_the_registry_ships() -> None:
    """``covered_forms`` carries the registry's form codes, and this is where
    that is checked rather than asserted: every code a package may name is a
    code a manifest ships, and every form the registry ships can be covered —
    so a form landing in ``backend/forms/`` fails here until it is declared
    coverable."""
    shipped = {image_registry(form).form_code for form in FormCode}
    assert {member.value for member in CoveredForm} == shipped


def test_the_contract_form_vocabulary_maps_onto_the_registry_codes() -> None:
    """``covered_form`` translates the contract's ``form_type`` — the registry
    *directory* — into the CR form code a manifest carries, and the pairing is
    checked here against the registry itself rather than trusted: a code moving
    in ``backend/forms/`` fails this test instead of silently making a form
    uncoverable."""
    for form in FormCode:
        assert covered_form(form.value) is CoveredForm(image_registry(form).form_code)


def test_an_unknown_form_type_names_no_registry_code() -> None:
    """The two vocabularies are easy to confuse, so both directions are pinned:
    a form this build does not handle names nothing, and so does a registry
    code passed where the contract's ``form_type`` belongs (``NAR1`` is not
    ``nar1``)."""
    assert covered_form("nnc2") is None
    assert covered_form("NAR1") is None
    assert covered_form("") is None


def test_validate_covered_forms_accepts_the_vocabulary() -> None:
    values = [member.value for member in CoveredForm]
    assert validate_covered_forms(values) == values


def test_validate_covered_forms_deduplicates_and_trims_preserving_order() -> None:
    assert validate_covered_forms([" ND2A ", "NAR1", "ND2A"]) == ["ND2A", "NAR1"]


@pytest.mark.parametrize("values", [[], ["  "], [""]])
def test_validate_covered_forms_refuses_a_package_that_covers_nothing(
    values: list[str],
) -> None:
    """Unlike ``included_services``, an empty list is refused: a package with
    no covered work would release nothing at all, and covering everything by
    omission is the permissive default the declared list exists to avoid."""
    with pytest.raises(CoveredFormsError) as excinfo:
        validate_covered_forms(values)
    assert str(excinfo.value) == "covered_forms must name at least one form"


def test_validate_covered_forms_lists_every_unknown_value() -> None:
    with pytest.raises(CoveredFormsError) as excinfo:
        validate_covered_forms(["NAR1", "NN3", "NR1"])
    assert str(excinfo.value) == "unknown forms: NN3, NR1"


def test_the_two_violations_cannot_both_be_true() -> None:
    """The emptiness check comes first and the unknown-value check cannot fire
    on an empty list, so the message is always the one situation that holds —
    which is why the error carries a sentence rather than a list of them."""
    with pytest.raises(CoveredFormsError) as excinfo:
        validate_covered_forms([" "])
    assert "unknown" not in str(excinfo.value)


# --- decide_automation: the §5.1 three-leg invariant ---


def test_all_three_legs_pass_allows() -> None:
    decision = decide()
    assert decision.allowed is True
    assert decision.reason is None


def test_leg_a_tier_capability_missing_denies() -> None:
    decision = decide(tenant_capabilities=frozenset({AutomationType.AUTOMATED_PREFILL}))
    assert decision.allowed is False
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING


def test_leg_a_empty_tenant_capabilities_fail_closed() -> None:
    decision = decide(tenant_capabilities=frozenset())
    assert decision.allowed is False
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING


def test_leg_b_no_assignment_denies() -> None:
    decision = decide(assignment_status=None, coverage_start=None, coverage_end=None)
    assert decision.allowed is False
    assert decision.reason is GatingReason.NO_ASSIGNMENT


def test_leg_b_ended_assignment_denies() -> None:
    decision = decide(assignment_status="ended")
    assert decision.allowed is False
    assert decision.reason is GatingReason.ASSIGNMENT_ENDED


def test_leg_b_coverage_not_yet_started_denies() -> None:
    decision = decide(as_of=COVERAGE_START - timedelta(days=1))
    assert decision.allowed is False
    assert decision.reason is GatingReason.OUT_OF_COVERAGE


def test_leg_b_coverage_expired_denies() -> None:
    decision = decide(as_of=date(2027, 1, 1))
    assert decision.allowed is False
    assert decision.reason is GatingReason.OUT_OF_COVERAGE


def test_coverage_boundaries_are_inclusive() -> None:
    assert decide(as_of=COVERAGE_START).allowed is True
    assert decide(as_of=COVERAGE_END).allowed is True


def test_leg_c_service_not_included_denies() -> None:
    decision = decide(included_services=["automated_prefill"])
    assert decision.allowed is False
    assert decision.reason is GatingReason.SERVICE_NOT_INCLUDED


def test_reasons_report_the_first_failing_leg_in_contract_order() -> None:
    # Tier fails; the assignment is also ended and the service unlisted.
    decision = decide(tenant_capabilities=frozenset(), assignment_status="ended")
    assert decision.reason is GatingReason.TIER_CAPABILITY_MISSING
    # Assignment legs fail; the service is also unlisted.
    decision = decide(assignment_status="ended", included_services=[])
    assert decision.reason is GatingReason.ASSIGNMENT_ENDED
