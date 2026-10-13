"""AI-gateway domain tests (the M6 counsel package: Q1.2 task table; the
protocol in the M6 plan's "Inference channel protocol" section).

Pure rules, no I/O: the four Q1.2 task types as the only vocabulary, the
inference protocol version, the call-decision truth table (the Q1.3
pre-flight rule: tier/licence, per-company objection, deployment gate,
operator acknowledgment, per-tier configuration), and the response-envelope
validation the
execution plane runs on every control-plane response before trusting it.
Everything here is synthetic; no real person's data appears anywhere.
"""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.domain.ai_gateway import (
    INFERENCE_PROTOCOL_VERSION,
    RESPONSE_ENVELOPE_KEYS,
    TASK_TYPES,
    CallFacts,
    CallRefusalReason,
    LlmTransport,
    Q13Gate,
    SplitPreFlightDecision,
    SplitRefusalReason,
    SplitViolation,
    TaskType,
    decide_call,
    decide_split_pre_flight,
    is_q13_gate_set,
    response_envelope_violations,
    split_violations,
)
from app.domain.entitlement import LicenseState, Tier


def _valid_envelope(**overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "protocol_version": INFERENCE_PROTOCOL_VERSION,
        "provider": "provider-test",
        "task_type": "reply_parsing",
        "model_id": "test-model-1",
        "prompt_name": "reply_parser",
        "prompt_version": 3,
        "result": {"events": []},
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        "completed_at": "2026-09-30T00:00:00+00:00",
    }
    envelope.update(overrides)
    return envelope


# --- The task vocabulary is Q1.2's ---


def test_the_task_vocabulary_is_the_five_closed_task_types() -> None:
    """Q1.2's counsel table names four task types; workstream ③ (ADR-0019 §3)
    adds the fifth — the split — by the same contract amendment that carried
    ①②. Pinned as strings so a rename cannot quietly leave the wire accepting
    a task the redaction profiles no longer describe."""
    assert [task.value for task in TaskType] == [
        "reply_parsing",
        "confirmation_request",
        "document_extraction",
        "ordinance_qa",
        "split",
    ]
    assert frozenset(TaskType) == TASK_TYPES
    assert TaskType.SPLIT.value == "split"


def test_a_split_envelope_is_a_valid_envelope() -> None:
    """The split rides the same response envelope as every task — the task
    type's membership check is the closed vocabulary's, so "split" validates
    exactly like its four siblings."""
    violations = response_envelope_violations(_valid_envelope(task_type="split"))
    assert violations == []


def test_the_control_plane_vocabulary_mirrors_the_execution_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two deployables cannot share an interpreter (both package as
    ``app``), so the vendor's copy is loaded by file path — the twin test the
    commands module already sets the pattern for (``test_envelope_version_unchanged``):
    the task vocabulary and the prompt-binding table the registry serves must
    be exactly the deployment side's, or one side would accept a task the
    other's redaction profiles never described."""
    root = Path(__file__).resolve().parents[2] / "control_plane"
    if not (root / "app" / "domain" / "inference.py").is_file():
        pytest.skip("control_plane/ is not in this checkout")
    # The vendor module's one sibling import resolves against the vendor's own
    # entitlement module, also by file path — injected under the shared package
    # name for the duration of the exec, then restored by the monkeypatch.
    spec = importlib.util.spec_from_file_location(
        "control_plane_entitlement", root / "app" / "domain" / "entitlement.py"
    )
    assert spec is not None and spec.loader is not None
    cp_entitlement = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp_entitlement)
    monkeypatch.setitem(sys.modules, "app.domain.entitlement", cp_entitlement)
    spec = importlib.util.spec_from_file_location(
        "control_plane_inference", root / "app" / "domain" / "inference.py"
    )
    assert spec is not None and spec.loader is not None
    cp_inference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp_inference)

    assert {task.value for task in cp_inference.TaskType} == {
        task.value for task in TaskType
    }
    assert cp_inference.TASK_PROMPT_NAMES == {
        "reply_parsing": "reply_parser",
        "confirmation_request": "confirmation_request_drafter",
        "document_extraction": "document_extractor",
        "ordinance_qa": "ordinance_qa",
        "split": "content_splitter",
    }


def test_the_protocol_version_is_one() -> None:
    """The channel versions one integer each way (the §5.2 versioning rule):
    the execution plane sends only the version it knows and refuses anything
    else, so the wire constant stays pinned at 1."""
    assert INFERENCE_PROTOCOL_VERSION == 1


def test_the_envelope_key_set_is_closed() -> None:
    """The response envelope carries exactly the plan's nine keys — no room
    for a provider to add fields the gateway would silently ignore."""
    assert {
        "protocol_version",
        "provider",
        "task_type",
        "model_id",
        "prompt_name",
        "prompt_version",
        "result",
        "usage",
        "completed_at",
    } == RESPONSE_ENVELOPE_KEYS


# --- Response-envelope validation ---


def test_a_valid_envelope_has_no_violations() -> None:
    assert response_envelope_violations(_valid_envelope()) == []


def test_a_non_mapping_envelope_is_rejected() -> None:
    assert response_envelope_violations(["not", "an", "object"]) != []


def test_wrong_protocol_version_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(protocol_version=2))
    assert any("protocol_version" in item for item in violations)


def test_a_bool_protocol_version_is_rejected() -> None:
    """``True`` is an ``int`` in Python; it is not a protocol version."""
    violations = response_envelope_violations(_valid_envelope(protocol_version=True))
    assert any("protocol_version" in item for item in violations)


def test_unknown_task_type_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(task_type="portfolio_audit"))
    assert any("task_type" in item for item in violations)


def test_missing_key_is_rejected() -> None:
    envelope = _valid_envelope()
    del envelope["model_id"]
    assert response_envelope_violations(envelope) != []


def test_extra_key_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(snippet_echo="leak"))
    assert any("snippet_echo" in item for item in violations)


def test_empty_provider_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(provider=""))
    assert any("provider" in item for item in violations)


def test_non_positive_prompt_version_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(prompt_version=0))
    assert any("prompt_version" in item for item in violations)


def test_null_result_is_rejected() -> None:
    violations = response_envelope_violations(_valid_envelope(result=None))
    assert any("result" in item for item in violations)


def test_negative_token_count_is_rejected() -> None:
    envelope = _valid_envelope()
    envelope["usage"] = {"prompt_tokens": -1, "completion_tokens": 34}
    violations = response_envelope_violations(envelope)
    assert any("prompt_tokens" in item for item in violations)


def test_extra_usage_key_is_rejected() -> None:
    """Usage is the closed Q1.5 pair — a provider echoing the snippet into
    ``usage`` is a leak, and the envelope must not admit it."""
    envelope = _valid_envelope()
    envelope["usage"] = {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "snippet_echo": "leak",
    }
    assert response_envelope_violations(envelope) != []


def test_non_integer_token_count_is_rejected() -> None:
    envelope = _valid_envelope()
    envelope["usage"] = {"prompt_tokens": "12", "completion_tokens": 34}
    violations = response_envelope_violations(envelope)
    assert any("prompt_tokens" in item for item in violations)


# --- The call-decision rule (M6.2): the Q1.3 pre-flight truth table ---


def _company_facts(**overrides: Any) -> CallFacts:
    """A fully-passing Standard-tier call; tests override what they break.

    ``acknowledgment_current`` defaults True so the cases pinned before the
    operator-acknowledgment layer existed keep testing exactly what they
    pinned; only the new acknowledgment cases exercise False."""
    facts = CallFacts(
        tier=Tier.STANDARD,
        license_state=LicenseState.ACTIVE,
        gate=Q13Gate.ENTERPRISE_DPA,
        activation_credential_present=True,
        local_llm_configured=True,
        company_ai_disabled=False,
        acknowledgment_current=True,
    )
    return replace(facts, **overrides)


def test_every_reason_in_the_refusal_vocabulary_is_distinct() -> None:
    """The reason vocabulary is the closed set the plan names — what the
    dashboard renders and the contract §5.1.3 pins, no room for drift."""
    assert {reason.value for reason in CallRefusalReason} == {
        "ai_tier_free",
        "ai_disabled",
        "ai_gate_unset",
        "ai_acknowledgment_missing",
        "ai_not_configured",
    }


def test_a_fully_configured_standard_call_uses_the_control_plane_transport() -> None:
    decision = decide_call(_company_facts())
    assert decision.allowed
    assert decision.reason is None
    assert decision.transport is LlmTransport.CONTROL_PLANE


def test_a_fully_configured_enterprise_offline_call_uses_the_local_transport() -> None:
    decision = decide_call(_company_facts(tier=Tier.ENTERPRISE_OFFLINE))
    assert decision.allowed
    assert decision.reason is None
    assert decision.transport is LlmTransport.LOCAL


def test_a_fully_configured_call_in_grace_still_runs() -> None:
    """``grace`` retains the tier's full capabilities (§4.3), so the licence
    leg passes and the transport still follows the tier."""
    decision = decide_call(
        _company_facts(license_state=LicenseState.GRACE, tier=Tier.ENTERPRISE_OFFLINE)
    )
    assert decision.allowed
    assert decision.transport is LlmTransport.LOCAL


def test_free_tier_refuses_regardless_of_everything_else() -> None:
    decision = decide_call(
        _company_facts(
            tier=Tier.FREE,
            company_ai_disabled=True,
            gate=None,
            activation_credential_present=False,
            local_llm_configured=False,
        )
    )
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_TIER_FREE
    assert decision.transport is None


def test_free_licence_state_refuses_even_on_a_standard_tier() -> None:
    """The §4.3 degradation rule: a Free licence state strips any tier to the
    Free capability set, so the call refuses with the tier reason even though
    the provisioned tier is Standard and everything else passes."""
    decision = decide_call(_company_facts(license_state=LicenseState.FREE))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_TIER_FREE


def test_an_enterprise_offline_tier_in_free_licence_state_refuses() -> None:
    decision = decide_call(
        _company_facts(tier=Tier.ENTERPRISE_OFFLINE, license_state=LicenseState.FREE)
    )
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_TIER_FREE


def test_company_ai_disabled_refuses_even_with_everything_else_passing() -> None:
    decision = decide_call(_company_facts(company_ai_disabled=True))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_DISABLED
    assert decision.transport is None


def test_ai_disabled_wins_over_an_unset_gate() -> None:
    decision = decide_call(_company_facts(company_ai_disabled=True, gate=None))
    assert decision.reason is CallRefusalReason.AI_DISABLED


def test_an_unset_deployment_gate_refuses() -> None:
    """The Q1.3 gate is the deployment-wide legal precondition: an unset gate
    refuses ``ai_gate_unset`` before the operator and configuration legs."""
    decision = decide_call(_company_facts(gate=None))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_GATE_UNSET


def test_an_empty_string_gate_is_unset() -> None:
    decision = decide_call(_company_facts(gate=""))
    assert decision.reason is CallRefusalReason.AI_GATE_UNSET


def test_an_unknown_gate_value_fails_closed_to_unset() -> None:
    """A misconfigured deployment gets no inference, never the benefit of the
    doubt (the ``coerce_tier`` fail-closed rule, applied to the gate)."""
    decision = decide_call(_company_facts(gate="some_future_gate"))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_GATE_UNSET


def test_every_resolved_gate_value_allows_the_call() -> None:
    """The gate is binary at call time: any of the three counsel-adopted
    resolutions (enterprise DPA, passing fallback provider, counsel's
    residual-risk acceptance) satisfies it."""
    for gate in Q13Gate:
        decision = decide_call(_company_facts(gate=gate))
        assert decision.allowed, gate


def test_standard_without_an_activation_credential_is_not_configured() -> None:
    """Standard's channel is the control plane, which needs the deployment's
    activation credential (ADR-0016); without it there is no transport."""
    decision = decide_call(_company_facts(activation_credential_present=False))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_NOT_CONFIGURED
    assert decision.transport is None


def test_enterprise_offline_without_a_local_llm_endpoint_is_not_configured() -> None:
    """ADR-0014 §2's third option: no inference configured — AI features are
    disabled and the deployment degrades to the manual path."""
    decision = decide_call(
        _company_facts(tier=Tier.ENTERPRISE_OFFLINE, local_llm_configured=False)
    )
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_NOT_CONFIGURED
    assert decision.transport is None


def test_enterprise_offline_does_not_care_about_the_activation_credential() -> None:
    """The credential mediates the control-plane channel; an offline
    deployment never uses that channel, so the credential is irrelevant to
    its transport selection."""
    decision = decide_call(
        _company_facts(tier=Tier.ENTERPRISE_OFFLINE, activation_credential_present=False)
    )
    assert decision.allowed
    assert decision.transport is LlmTransport.LOCAL


def test_standard_does_not_care_about_the_local_llm_config() -> None:
    """Converse of the above: Standard's transport is the control plane, so a
    stray local endpoint changes nothing."""
    decision = decide_call(_company_facts(local_llm_configured=False))
    assert decision.allowed
    assert decision.transport is LlmTransport.CONTROL_PLANE


def test_tier_wins_when_every_fact_is_wrong_at_once() -> None:
    """Precedence pin: tier/licence → ai_disabled → gate → acknowledgment →
    config. When the whole row is wrong the coarsest, outermost gate reports —
    the Free tier names why nothing was ever configurable, and it matches the
    §5.1 leg order's tier-first rule."""
    decision = decide_call(
        _company_facts(
            tier=Tier.FREE,
            company_ai_disabled=True,
            gate=None,
            acknowledgment_current=False,
            activation_credential_present=False,
            local_llm_configured=False,
        )
    )
    assert decision.reason is CallRefusalReason.AI_TIER_FREE


def test_the_gate_wins_over_a_missing_credential() -> None:
    """Deployment-wide blockers precede configuration gaps."""
    decision = decide_call(_company_facts(gate=None, activation_credential_present=False))
    assert decision.reason is CallRefusalReason.AI_GATE_UNSET


# --- The operator-acknowledgment layer (2026-10-04 rulings; R3's slot) ---


def test_an_unacknowledged_deployment_refuses() -> None:
    """The acknowledgment is the operator layer, checked after the deployment
    gate: a gated deployment whose operator has not confirmed the risk
    statement fails closed even with everything else green."""
    decision = decide_call(_company_facts(acknowledgment_current=False))
    assert not decision.allowed
    assert decision.reason is CallRefusalReason.AI_ACKNOWLEDGMENT_MISSING
    assert decision.transport is None


def test_the_acknowledgment_wins_over_a_missing_credential() -> None:
    """Deployment → operator → configuration: the missing operator
    confirmation is the earlier layer, so it reports before the per-tier
    configuration gap."""
    decision = decide_call(
        _company_facts(acknowledgment_current=False, activation_credential_present=False)
    )
    assert decision.reason is CallRefusalReason.AI_ACKNOWLEDGMENT_MISSING


def test_an_unset_gate_wins_over_a_missing_acknowledgment() -> None:
    """The deployment-wide legal precondition precedes the operator layer: a
    deployment with an unset Q1.3 gate still reads ``ai_gate_unset`` first."""
    decision = decide_call(_company_facts(gate=None, acknowledgment_current=False))
    assert decision.reason is CallRefusalReason.AI_GATE_UNSET


# --- The Q1.3 gate-resolution predicate ---------------------------------------


def test_q13_gate_resolution_is_the_closed_vocabulary() -> None:
    """The call rule's single statement of "the gate is resolved": the closed
    ``Q13Gate`` vocabulary, nothing else — ``None``, empty and any unknown
    string all fail closed to unset."""
    for member in Q13Gate:
        assert is_q13_gate_set(member.value)
    for unset in (None, "", "yes", "Enterprise DPA", "pending"):
        assert not is_q13_gate_set(unset)


# --- The split's output grammar (workstream ③, ruling D3) ---------------------

# The tokenized content this call sent, and the token sets: every placeholder
# minted for the call, and the company tokens among them (the involved set —
# the sender's own companies, never the provider's say).
_SPLIT_CONTENT = "Please update the register for [COMPANY_01] and file the [COMPANY_02] return."
_SPLIT_TOKENS = frozenset({"[COMPANY_01]", "[COMPANY_02]"})
_SPLIT_COMPANY_TOKENS = frozenset({"[COMPANY_01]", "[COMPANY_02]"})
_VALID_SPANS = (
    '[{"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]"},'
    ' {"company": "[COMPANY_02]", "text": " and file the [COMPANY_02] return."}]'
)


def test_the_split_violation_vocabulary_is_the_closed_six() -> None:
    assert [violation.value for violation in SplitViolation] == [
        "malformed_spans",
        "unknown_company_token",
        "duplicate_company_span",
        "missing_company_span",
        "empty_span",
        "coverage_mismatch",
    ]


def test_a_valid_split_answer_has_no_violations() -> None:
    """The happy grammar: one span per involved company, in order, covering
    the tokenized content exactly (whitespace-normalized)."""
    assert (
        split_violations(
            _VALID_SPANS,
            tokens=_SPLIT_TOKENS,
            company_tokens=_SPLIT_COMPANY_TOKENS,
            content=_SPLIT_CONTENT,
        )
        == ()
    )


def test_whitespace_differences_do_not_break_coverage_body() -> None:
    padded = (
        '[{"company": "[COMPANY_01]",'
        ' "text": "  Please  update\\n the register for [COMPANY_01]  "},'
        ' {"company": "[COMPANY_02]", "text": " and   file the [COMPANY_02] return. "}]'
    )
    assert (
        split_violations(
            padded,
            tokens=_SPLIT_TOKENS,
            company_tokens=_SPLIT_COMPANY_TOKENS,
            content=_SPLIT_CONTENT,
        )
        == ()
    )


@pytest.mark.parametrize(
    "answer",
    [
        "not json at all",
        '{"company": "[COMPANY_01]", "text": "x"}',  # an object, not a list
        "[1, 2]",
        '[{"company": "[COMPANY_01]"}]',  # missing text
        '[{"text": "a span"}]',  # missing company
        '[{"company": 7, "text": "a span"}]',
        '[{"company": "[COMPANY_01]", "text": 7}]',
    ],
)
def test_a_structurally_malformed_answer_violates_the_grammar(answer: str) -> None:
    assert (
        split_violations(
            answer,
            tokens=_SPLIT_TOKENS,
            company_tokens=_SPLIT_COMPANY_TOKENS,
            content=_SPLIT_CONTENT,
        )
        == (SplitViolation.MALFORMED_SPANS,)
    )


def test_a_span_naming_a_token_the_call_never_minted_is_a_violation() -> None:
    answer = (
        '[{"company": "[COMPANY_99]", "text": "Please update the register for [COMPANY_99]"},'
        ' {"company": "[COMPANY_02]", "text": " and file the [COMPANY_02] return."}]'
    )
    assert SplitViolation.UNKNOWN_COMPANY_TOKEN in split_violations(
        answer,
        tokens=_SPLIT_TOKENS,
        company_tokens=_SPLIT_COMPANY_TOKENS,
        content=_SPLIT_CONTENT,
    )


def test_two_spans_for_one_company_is_a_violation() -> None:
    answer = (
        '[{"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]"},'
        ' {"company": "[COMPANY_01]", "text": " and file the [COMPANY_02] return."}]'
    )
    violations = split_violations(
        answer,
        tokens=_SPLIT_TOKENS,
        company_tokens=_SPLIT_COMPANY_TOKENS,
        content=_SPLIT_CONTENT,
    )
    assert SplitViolation.DUPLICATE_COMPANY_SPAN in violations
    assert SplitViolation.MISSING_COMPANY_SPAN in violations


def test_an_involved_company_with_no_span_is_a_violation() -> None:
    answer = '[{"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]"}]'
    assert (
        split_violations(
            answer,
            tokens=_SPLIT_TOKENS,
            company_tokens=_SPLIT_COMPANY_TOKENS,
            content=_SPLIT_CONTENT,
        )
        == (SplitViolation.MISSING_COMPANY_SPAN, SplitViolation.COVERAGE_MISMATCH)
    )


def test_an_empty_span_is_a_violation() -> None:
    answer = (
        '[{"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]"},'
        ' {"company": "[COMPANY_02]", "text": "   "}]'
    )
    assert SplitViolation.EMPTY_SPAN in split_violations(
        answer,
        tokens=_SPLIT_TOKENS,
        company_tokens=_SPLIT_COMPANY_TOKENS,
        content=_SPLIT_CONTENT,
    )


@pytest.mark.parametrize(
    "answer",
    [
        # Dropped words: the concatenation no longer equals the content.
        '[{"company": "[COMPANY_01]", "text": "Please update the register"},'
        ' {"company": "[COMPANY_02]", "text": " and file the [COMPANY_02] return."}]',
        # Reordered spans: same text, wrong order — the division is rewritten,
        # not the content.
        '[{"company": "[COMPANY_02]", "text": " and file the [COMPANY_02] return."},'
        ' {"company": "[COMPANY_01]", "text": "Please update the register for [COMPANY_01]"}]',
    ],
)
def test_a_division_that_does_not_reconstruct_the_content_is_a_violation(
    answer: str,
) -> None:
    assert SplitViolation.COVERAGE_MISMATCH in split_violations(
        answer,
        tokens=_SPLIT_TOKENS,
        company_tokens=_SPLIT_COMPANY_TOKENS,
        content=_SPLIT_CONTENT,
    )


def test_a_repeated_violation_kind_is_reported_once() -> None:
    """The report names what the answer did, not how often — the staff reader
    needs the kinds, and the parent parks either way."""
    answer = (
        '[{"company": "[COMPANY_99]", "text": "Please update the register for [COMPANY_99]"},'
        ' {"company": "[COMPANY_98]", "text": " and file the [COMPANY_02] return."}]'
    )
    assert split_violations(
        answer,
        tokens=_SPLIT_TOKENS,
        company_tokens=_SPLIT_COMPANY_TOKENS,
        content=_SPLIT_CONTENT,
    ) == (
        SplitViolation.UNKNOWN_COMPANY_TOKEN,
        SplitViolation.MISSING_COMPANY_SPAN,
        SplitViolation.COVERAGE_MISMATCH,
    )


# --- The split's pre-flight (workstream ③, rulings D4/D5) ---------------------


def test_the_split_pre_flight_vocabularies_are_the_closed_pairs() -> None:
    assert [decision.value for decision in SplitPreFlightDecision] == [
        "split",
        "staff_only",
    ]
    assert [reason.value for reason in SplitRefusalReason] == [
        "ai_disabled",
        "automation_denied",
        "too_many_companies",
        "daily_cap_reached",
    ]


def _split_facts(*pairs: tuple[bool, bool]) -> list[tuple[bool, bool]]:
    """(ai_disabled, automation_allowed) per involved company."""
    return list(pairs)


def test_an_all_clear_parent_splits() -> None:
    decision = decide_split_pre_flight(
        involved_count=2,
        max_companies=10,
        daily_splits_for_sender=0,
        daily_cap_per_sender=5,
        company_facts=_split_facts((False, True), (False, True)),
    )
    assert decision.decision is SplitPreFlightDecision.SPLIT
    assert decision.reason is None


@pytest.mark.parametrize(
    "facts",
    [
        _split_facts((True, True), (False, True)),  # the first company opted out
        _split_facts((False, True), (False, True), (True, True)),  # any one of them
    ],
)
def test_any_involved_company_opted_out_parks_the_whole_parent(
    facts: list[tuple[bool, bool]],
) -> None:
    """Ruling D4: one indivisible call. A single ai_disabled company parks
    the entire parent — no partial splits, no per-company decisions."""
    decision = decide_split_pre_flight(
        involved_count=len(facts),
        max_companies=10,
        daily_splits_for_sender=0,
        daily_cap_per_sender=5,
        company_facts=facts,
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.AI_DISABLED


def test_any_involved_company_failing_the_gate_parks_the_whole_parent() -> None:
    decision = decide_split_pre_flight(
        involved_count=2,
        max_companies=10,
        daily_splits_for_sender=0,
        daily_cap_per_sender=5,
        company_facts=_split_facts((False, True), (False, False)),
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.AUTOMATION_DENIED


def test_an_opt_out_and_a_gate_denial_names_the_opt_out_first() -> None:
    """The D4 checks precede D5's bounds, and the opt-out names the company
    the client excluded — the stronger refusal leads."""
    decision = decide_split_pre_flight(
        involved_count=12,
        max_companies=10,
        daily_splits_for_sender=9,
        daily_cap_per_sender=5,
        company_facts=_split_facts((True, False), (False, False)),
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.AI_DISABLED


def test_more_companies_than_the_bound_parks_for_staff() -> None:
    decision = decide_split_pre_flight(
        involved_count=11,
        max_companies=10,
        daily_splits_for_sender=0,
        daily_cap_per_sender=5,
        company_facts=_split_facts(*[(False, True)] * 11),
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.TOO_MANY_COMPANIES


def test_the_daily_cap_parks_further_automatic_splits() -> None:
    decision = decide_split_pre_flight(
        involved_count=2,
        max_companies=10,
        daily_splits_for_sender=5,
        daily_cap_per_sender=5,
        company_facts=_split_facts((False, True), (False, True)),
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.DAILY_CAP_REACHED


def test_a_zero_daily_cap_means_no_automatic_splits() -> None:
    decision = decide_split_pre_flight(
        involved_count=2,
        max_companies=10,
        daily_splits_for_sender=0,
        daily_cap_per_sender=0,
        company_facts=_split_facts((False, True), (False, True)),
    )
    assert decision.decision is SplitPreFlightDecision.STAFF_ONLY
    assert decision.reason is SplitRefusalReason.DAILY_CAP_REACHED
