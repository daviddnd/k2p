"""The single LLM boundary's vocabulary and protocol shapes (M6; the counsel
package in
``research/legal/internal-counsel-drafts/flow-1-inference-channel-personal-data-opinion.md``).

This module is the execution-plane side of the one channel over which any
client data reaches an inference provider. Everything that later tasks route
through the gateway — the call-decision rule (M6.2), the application
orchestration (M6.4) — speaks the vocabulary declared here:

- **``TaskType``** — Q1.2's table has exactly four task types, and the
  redaction profiles in ``app.domain.redaction`` are keyed by them. A task
  the redaction layer has no profile for has no defensible payload, so the
  vocabulary is closed at four and pinned as strings.
- **``INFERENCE_PROTOCOL_VERSION``** — the channel versions one integer each
  way (contract §5.2's rule): the execution plane sends only the version it
  knows and treats anything else as ``unsupported_protocol_version`` without
  processing it.
- **Response-envelope validation** — the control plane's response is
  validated before a single field of it is trusted. The key set is closed
  (a provider echoing the snippet back under a new key is a leak, not a
  field to ignore), the task type must be one of the four, and ``usage`` is
  the closed Q1.5 token pair — no other shape reaches the caller.
- **The call-decision rule** (``decide_call``) — the pre-flight gate every
  inference call passes through before a single byte is composed or sent.
  The reason vocabulary is closed (``CallRefusalReason``) and the checks
  run in one deliberate precedence, documented on the rule itself:
  tier/licence first (the outermost commercial gate, mirroring the §5.1
  leg order), then the deliberate per-company objection, then the
  deployment-wide legal gate, then the operator's acknowledgment of the
  risk-disclosure statement (the 2026-10-04 rulings: deployment → operator),
  and the per-tier configuration last — so the reason always names the first
  blocker a caller can act on, and the transport follows the tier only when
  every leg passes.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeGuard

from app.domain.entitlement import LicenseState, Tier

# The one integer each way (contract §5.2): version 1 is the only version
# this build of the execution plane speaks.
INFERENCE_PROTOCOL_VERSION = 1

# The response envelope's closed key set — the plan's "Inference channel
# protocol" response, no room for unvalidated provider additions.
RESPONSE_ENVELOPE_KEYS = frozenset(
    {
        "protocol_version",
        "provider",
        "task_type",
        "model_id",
        "prompt_name",
        "prompt_version",
        "result",
        "usage",
        "completed_at",
    }
)

# Usage is the closed Q1.5 pair — token counts only, never content.
USAGE_KEYS = frozenset({"prompt_tokens", "completion_tokens"})


class TaskType(StrEnum):
    """Q1.2's task types — the only tasks the gateway will ever send.

    The values are the wire vocabulary: they travel in the request envelope,
    select the redaction profile, and come back in the response envelope.
    Each value is the unit the profile table in ``app.domain.redaction``
    describes: what the minimum defensible snippet is, what is hard-excluded,
    and whether names are tokenized. Q1.2 named four; workstream ③
    (ADR-0019 §3, the multi-company split, contract-amendment ruling) adds
    the fifth — the split divides one message's tokenized content across
    the sender's several companies, one indivisible call.
    """

    REPLY_PARSING = "reply_parsing"
    CONFIRMATION_REQUEST = "confirmation_request"
    DOCUMENT_EXTRACTION = "document_extraction"
    ORDINANCE_QA = "ordinance_qa"
    SPLIT = "split"


# The set, for closed-vocabulary membership checks.
TASK_TYPES = frozenset(TaskType)


class Q13Gate(StrEnum):
    """The counsel-adopted Q1.3 resolutions that enable production inference
    (ADR-0014 open action 2, adopted 2026-09-30): an archived enterprise DPA
    with explicit zero-retention/no-training clauses, a fallback provider
    passing R1-R6, or counsel's written residual-risk acceptance. The config
    value ``AICS_AI_Q13_GATE`` is empty by default and holds one of these
    three when resolved — at call time the gate is binary, and anything
    outside this closed set fails closed to ``ai_gate_unset``.
    """

    ENTERPRISE_DPA = "enterprise_dpa"
    FALLBACK_PROVIDER = "fallback_provider"
    RESIDUAL_RISK_ACCEPTED = "residual_risk_accepted"


class CallRefusalReason(StrEnum):
    """Why a call is refused, in the precedence ``decide_call`` checks them.

    The closed set the M6 plan and contract §5.1.3 pin; the dashboard renders
    these strings, so the vocabulary cannot grow without a contract change.
    """

    AI_TIER_FREE = "ai_tier_free"
    AI_DISABLED = "ai_disabled"
    AI_GATE_UNSET = "ai_gate_unset"
    AI_ACKNOWLEDGMENT_MISSING = "ai_acknowledgment_missing"
    AI_NOT_CONFIGURED = "ai_not_configured"


class LlmTransport(StrEnum):
    """Where the minimized snippet goes when the call is allowed (ADR-0014
    §2's custody split): the vendor control plane carries the provider key
    for Standard; Enterprise Offline talks to a locally configured endpoint."""

    CONTROL_PLANE = "control_plane"
    LOCAL = "local"


@dataclass(frozen=True)
class CallFacts:
    """The plain values the pre-flight rule needs, gathered by the
    application layer (M6.4) from the entitlement, the deployment config
    and the company row. No ORM objects, no I/O — the rule stays pure and
    deterministic so the truth table pins it completely."""

    tier: Tier
    license_state: LicenseState
    gate: str | None
    activation_credential_present: bool
    local_llm_configured: bool
    company_ai_disabled: bool
    acknowledgment_current: bool


@dataclass(frozen=True)
class CallDecision:
    """The outcome of the pre-flight rule: ``allowed`` with the transport,
    or refused with the first reason in the precedence order."""

    allowed: bool
    reason: CallRefusalReason | None
    transport: LlmTransport | None


_Q13_GATE_VALUES = frozenset(member.value for member in Q13Gate)


def is_q13_gate_set(value: str | None) -> bool:
    """Whether a configured value resolves the deployment-wide Q1.3 gate.

    The closed ``Q13Gate`` vocabulary, nothing else: ``None``, empty and any
    unknown string all fail closed to unset — the single statement of "the
    gate is resolved", which ``decide_call`` re-checks at every call.
    """
    return value in _Q13_GATE_VALUES


def decide_call(facts: CallFacts) -> CallDecision:
    """The Q1.3 pre-flight rule: allowed, or refused with the reason.

    The checks run in one deliberate precedence — tier/licence, then the
    per-company objection, then the deployment-wide gate, then the operator
    acknowledgment, then per-tier configuration:

    1. **Tier/licence** — a Free tier, or any tier in the Free licence
       state (the §4.3 degradation rule), refuses ``ai_tier_free``. This is
       the outermost gate: Free has no AI features at all (ADR-0014 §3), so
       per-company state and configuration are moot, and the reason names
       the commercial fact rather than inviting configuration work. First
       by the same leg-order logic as the §5.1 automation gate.
    2. **``ai_disabled``** — the deliberate per-company objection, the TCSP's
       objection-handling valve (ADR-0019 §2): an explicit human objection
       beats missing paperwork, and the recorded objection is the most
       specific fact about the company.
    3. **The Q1.3 deployment gate** — unset (empty, ``None``, or any value
       outside the closed ``Q13Gate`` vocabulary — fail closed) refuses
       ``ai_gate_unset``. The deployment-wide legal precondition precedes
       the operator and configuration legs, so the reason names the earliest
       layer a caller can act on.
    4. **The operator acknowledgment** (the 2026-10-04 rulings) — an
       operator who has not confirmed the current risk-disclosure statement
       refuses ``ai_acknowledgment_missing``. This is the operator layer,
       between the deployment-wide gate and the per-tier configuration:
       deployment → operator. It is checked before the configuration leg so
       the reason names the earlier layer a caller can act on.
    5. **Per-tier configuration** — Standard without its activation
       credential (the control-plane channel needs one) and Enterprise
       Offline without a configured local LLM endpoint both refuse
       ``ai_not_configured``: ADR-0014 §2's priority ends at "no inference
       configured — degraded/manual" when neither custody option is set up.

    When every leg passes the transport follows the tier: Standard →
    ``control_plane``, Enterprise Offline → ``local``.
    """
    if facts.tier is Tier.FREE or facts.license_state is LicenseState.FREE:
        return CallDecision(False, CallRefusalReason.AI_TIER_FREE, None)
    if facts.company_ai_disabled:
        return CallDecision(False, CallRefusalReason.AI_DISABLED, None)
    if not is_q13_gate_set(facts.gate):
        return CallDecision(False, CallRefusalReason.AI_GATE_UNSET, None)
    if not facts.acknowledgment_current:
        return CallDecision(False, CallRefusalReason.AI_ACKNOWLEDGMENT_MISSING, None)
    if facts.tier is Tier.STANDARD:
        if not facts.activation_credential_present:
            return CallDecision(False, CallRefusalReason.AI_NOT_CONFIGURED, None)
        return CallDecision(True, None, LlmTransport.CONTROL_PLANE)
    if not facts.local_llm_configured:
        return CallDecision(False, CallRefusalReason.AI_NOT_CONFIGURED, None)
    return CallDecision(True, None, LlmTransport.LOCAL)


def response_envelope_violations(value: Any) -> list[str]:
    """Every reason a control-plane response envelope is untrusted.

    The empty list is the only pass. Validation is fail-closed by
    construction: the caller refuses the response on any violation, because a
    response that cannot be fully validated is a response that cannot be
    restored, metered, or audited. ``result`` is validated only for presence
    and JSON-ness — its task-specific shape belongs to the task's own parser.
    """
    if not isinstance(value, dict):
        return ["response envelope is not a JSON object"]
    violations: list[str] = []
    keys = set(value)
    for key in sorted(keys - RESPONSE_ENVELOPE_KEYS):
        violations.append(f"unexpected key: {key}")
    for key in sorted(RESPONSE_ENVELOPE_KEYS - keys):
        violations.append(f"missing key: {key}")

    version = value.get("protocol_version")
    if not _is_int(version) or version != INFERENCE_PROTOCOL_VERSION:
        violations.append(
            f"protocol_version must be the integer {INFERENCE_PROTOCOL_VERSION}"
        )

    task_type = value.get("task_type")
    if not isinstance(task_type, str) or task_type not in TASK_TYPES:
        violations.append(f"task_type must be one of {sorted(t.value for t in TaskType)}")

    for key in ("provider", "model_id", "prompt_name", "completed_at"):
        if not isinstance(value.get(key), str) or not value[key]:
            violations.append(f"{key} must be a non-empty string")

    prompt_version = value.get("prompt_version")
    if not _is_int(prompt_version) or prompt_version < 1:
        violations.append("prompt_version must be a positive integer")

    if "result" not in value or value["result"] is None:
        violations.append("result must be present and non-null")

    usage = value.get("usage")
    if not isinstance(usage, dict):
        violations.append("usage must be a JSON object")
    else:
        usage_keys = set(usage)
        for key in sorted(usage_keys - USAGE_KEYS):
            violations.append(f"usage has unexpected key: {key}")
        for key in sorted(USAGE_KEYS - usage_keys):
            violations.append(f"usage is missing key: {key}")
        for key in sorted(usage_keys & USAGE_KEYS):
            if not _is_int(usage[key]) or usage[key] < 0:
                violations.append(f"usage.{key} must be a non-negative integer")
    return violations


def _is_int(value: Any) -> TypeGuard[int]:
    """A real integer — ``bool`` is an ``int`` subclass in Python and is not
    a protocol version, a prompt version, or a token count."""
    return isinstance(value, int) and not isinstance(value, bool)


# --- The split (workstream ③, ADR-0019 §3) ------------------------------------
#
# The fifth task type's own grammar and pre-flight. The split is one
# indivisible call over one message attributed to several of the sender's
# own companies: the provider is asked only to *divide* the tokenized
# content — the involved set and the token→company binding stay local
# (ruling D1), so what crosses the wire is placeholders and prose alone.


class SplitViolation(StrEnum):
    """Why a split answer is refused, in the order ``split_violations``
    checks them (ruling D3).

    The closed set names what the provider's answer did wrong; any member
    parks the parent for staff — the division is never guessed at. The
    values are staff-facing vocabulary, pinned like every other reason set.
    """

    MALFORMED_SPANS = "malformed_spans"
    UNKNOWN_COMPANY_TOKEN = "unknown_company_token"
    DUPLICATE_COMPANY_SPAN = "duplicate_company_span"
    MISSING_COMPANY_SPAN = "missing_company_span"
    EMPTY_SPAN = "empty_span"
    COVERAGE_MISMATCH = "coverage_mismatch"


def _normalized_words(text: str) -> list[str]:
    """The whitespace-normalized comparison form of one side of the coverage
    rule: runs of whitespace collapse to single spaces and the ends trim, so
    the provider's spacing choices cannot manufacture (or repair) a
    violation."""
    return text.split()


def split_violations_parsed(
    parsed: Any,
    *,
    tokens: Collection[str],
    company_tokens: Collection[str],
    content: str,
) -> tuple[SplitViolation, ...]:
    """The object-level half of ``split_violations`` — the same checks over
    an already-decoded answer. The response envelope's ``result`` arrives
    parsed, so the application validates that shape directly; the string
    wrapper is the raw-body entry (a JSON text that will not decode is
    itself ``malformed_spans``). A non-list parsed value is malformed here
    exactly as it is there.
    """
    if not isinstance(parsed, list):
        return (SplitViolation.MALFORMED_SPANS,)
    known = set(tokens)
    involved = set(company_tokens)
    found: list[SplitViolation] = []

    def record(violation: SplitViolation) -> None:
        if violation not in found:
            found.append(violation)

    spans: list[tuple[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            record(SplitViolation.MALFORMED_SPANS)
            continue
        company = item.get("company")
        text = item.get("text")
        if not isinstance(company, str) or not isinstance(text, str):
            record(SplitViolation.MALFORMED_SPANS)
            continue
        spans.append((company, text))
    if SplitViolation.MALFORMED_SPANS in found:
        return tuple(found)

    seen: set[str] = set()
    for company, text in spans:
        if company not in known:
            record(SplitViolation.UNKNOWN_COMPANY_TOKEN)
        if company in seen:
            record(SplitViolation.DUPLICATE_COMPANY_SPAN)
        seen.add(company)
        if not text.strip():
            record(SplitViolation.EMPTY_SPAN)
    if involved - seen:
        record(SplitViolation.MISSING_COMPANY_SPAN)
    joined = "".join(text for _, text in spans)
    if _normalized_words(joined) != _normalized_words(content):
        record(SplitViolation.COVERAGE_MISMATCH)
    return tuple(found)


def split_violations(
    answer: str,
    *,
    tokens: Collection[str],
    company_tokens: Collection[str],
    content: str,
) -> tuple[SplitViolation, ...]:
    """Validate one split answer against the call's facts (ruling D3).

    ``answer`` is the provider's raw response body — a JSON list of
    ``{"company": "<TOKEN>", "text": "<span>"}`` spans. ``tokens`` is every
    placeholder this call minted (the scrub and tokenizer tables' keys), and
    ``company_tokens`` the company placeholders among them — the involved
    set the caller tokenized, never a provider claim. ``content`` is the
    tokenized text the call sent. An empty tuple is a valid division; any
    member of the returned tuple parks the parent for staff, and the kinds
    are reported once each, in check order — the staff reader needs what the
    answer did, not how often.

    The checks, in order: the answer parses as a list of objects carrying
    exactly a string ``company`` and a string ``text``
    (``malformed_spans``); every named company is a token this call minted
    (``unknown_company_token``); no company carries two spans
    (``duplicate_company_span``); every involved company carries one
    (``missing_company_span``); no span is empty (``empty_span``); and the
    spans in order, concatenated, whitespace-normalized, reconstruct the
    tokenized content exactly (``coverage_mismatch``) — a division that
    drops, adds, or reorders content is a rewrite, not a split.
    """
    try:
        parsed = json.loads(answer)
    except (TypeError, ValueError):
        return (SplitViolation.MALFORMED_SPANS,)
    return split_violations_parsed(
        parsed, tokens=tokens, company_tokens=company_tokens, content=content
    )


class SplitPreFlightDecision(StrEnum):
    """Where one split parent goes before any gateway call (ruling D4)."""

    SPLIT = "split"
    STAFF_ONLY = "staff_only"


class SplitRefusalReason(StrEnum):
    """Why a parent parks for staff instead of splitting — the closed set,
    in the precedence ``decide_split_pre_flight`` checks them."""

    AI_DISABLED = "ai_disabled"
    AUTOMATION_DENIED = "automation_denied"
    TOO_MANY_COMPANIES = "too_many_companies"
    DAILY_CAP_REACHED = "daily_cap_reached"


@dataclass(frozen=True)
class SplitPreFlight:
    """The pre-flight's verdict: where the parent goes, and why not (when it
    parks). ``reason`` is ``None`` exactly when the decision is ``split``."""

    decision: SplitPreFlightDecision
    reason: SplitRefusalReason | None


def decide_split_pre_flight(
    *,
    involved_count: int,
    max_companies: int,
    daily_splits_for_sender: int,
    daily_cap_per_sender: int,
    company_facts: Collection[tuple[bool, bool]],
) -> SplitPreFlight:
    """Decide one split parent's road, before any gateway call (rulings
    D4/D5) — one indivisible call, so one indivisible gate.

    ``company_facts`` carries one ``(ai_disabled, automation_allowed)`` pair
    per involved company — the facts the caller resolved locally (the
    per-company opt-out flag and the ``AI_REPLY_PARSING`` automation gate).
    If **any** involved company opted out, or fails the gate, the whole
    parent parks ``staff_only``: no partial splits, no per-company gateway
    decisions — the ADR's non-cloud path is all-or-nothing. The D4 checks
    precede D5's bounds: the opt-out names the company the client excluded,
    and a gate denial names a licensing fact, where a bound would only say
    "too much". The bounds then cap the involved set's size
    (``too_many_companies``) and the sender's automatic splits per day
    (``daily_cap_reached`` — a cap of ``0`` means no automatic splits at
    all: every parent parks).
    """
    if any(disabled for disabled, _ in company_facts):
        return SplitPreFlight(SplitPreFlightDecision.STAFF_ONLY, SplitRefusalReason.AI_DISABLED)
    if any(not allowed for _, allowed in company_facts):
        return SplitPreFlight(
            SplitPreFlightDecision.STAFF_ONLY, SplitRefusalReason.AUTOMATION_DENIED
        )
    if involved_count > max_companies:
        return SplitPreFlight(
            SplitPreFlightDecision.STAFF_ONLY, SplitRefusalReason.TOO_MANY_COMPANIES
        )
    if daily_splits_for_sender >= daily_cap_per_sender:
        return SplitPreFlight(
            SplitPreFlightDecision.STAFF_ONLY, SplitRefusalReason.DAILY_CAP_REACHED
        )
    return SplitPreFlight(SplitPreFlightDecision.SPLIT, None)
