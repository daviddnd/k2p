"""The gateway orchestration: the only path in the product that calls an LLM.

Every inference call in the execution plane lands here, and nowhere else.
``infer`` runs one deliberate pipeline, each stage fail-closed:

1. **Load the company row** (the tenant-scoped read; a foreign company is
   ``CompanyNotFoundError``, never a silent default).
2. **Pre-flight** — gather the plain facts (tier and licence state from the
   entitlement read, the deployment's Q1.3 gate from config, the operator's
   acknowledgment of the risk-disclosure statement from the audit chain via
   the one read helper (``app.application.risk_acknowledgment``), activation
   credential presence from the credential file, the local-LLM endpoint's
   configuration from config, and the company's ``ai_disabled`` objection
   from the row) and run the pure rule
   (``app.domain.ai_gateway.decide_call``). A refusal raises
   ``InferenceRefused(reason)`` with the closed reason vocabulary — **no
   audit entry, no transport** (ruling R5: a call that never happened writes
   nothing; the register and flags are the record of the gate).
3. **Compose** — the composed snippet is the unit of analysis
   (``app.domain.redaction.compose_snippet``). The company anchor is derived
   from the company row, never caller-supplied: the BRN is the on-form
   register identifier, and the minimization bar owns what identifies the
   company. For ordinance Q&A the one admitted payload is the staff-authored
   non-client question (controller ruling R11) — ``content`` is the question
   string. For the other three tasks ``content`` is the task's field mapping,
   enforced field-by-field against the profile's allow-list.
4. **Tokenize** — profiles with ``tokenize_names`` replace the known person
   names (the reply-parsing profile's name-bearing field) with
   ``[PERSON_nn]`` placeholders everywhere they appear in the snippet; the
   re-identification key lives only in request-scoped memory. The caller may
   pass ``tokenize_values``, its own typed master-data lexicon (company
   names, its BRN, both script forms of each officer), which the tokenizer
   uses as replacement sources — the lexicon itself is never sent and is not
   a snippet field. Profiles that opted out (with their recorded
   justification) and ordinance Q&A pass through unchanged.
5. **Transport** — ``llm_client(decision.transport)`` is the seam, mirroring
   ``app.application.lease.client()``: the Standard path hands the composed
   text to the control-plane channel with the activation token; Enterprise
   Offline posts to the locally configured endpoint. A failure *after the
   attempt* — refused or unreachable — books the audit entry (the snippet
   left: null token counts, ``details.error`` the bounded code) and raises
   ``InferenceUnavailable(code)``.
6. **Validate, audit, restore** — the response envelope is validated by the
   domain rule before a single field is trusted; an untrusted envelope is
   booked as a failed attempt and refused. A trusted envelope is audited
   *first*: the success ``llm_call`` entry is appended before anything is
   restored, because the attempt happened and the record must say so whatever
   follows. Only then are placeholders restored over ``result``, locally, from
   the merge of the two request-scoped tables: the compose scrub's entries
   (ruling D3) and the tokenizer's, the tokenizer winning on a collision as the
   more specific claim about the text — a collision the tokenizer refuses up
   front (ruling R-T7-2), so the merge's order is a tie-break that no caller of
   this task can trigger. Restore is strict (review focus 6): the caller may
   declare the bare literal vocabulary its task admits (``allowed_literals``,
   the reply parser's closed enum set); every other value the model returns must
   be a token from these tables, and a value literal it never saw — or a
   placeholder this call never minted — refuses the answer
   (``RedactionRefused("invented_value")``). That refusal comes *after* the
   audit entry for exactly the reason the entry was written first: the snippet
   left, the call happened, and the chain records it even though the answer is
   refused and nothing derived from it is stored. Free evidence prose is not a
   value and passes to the caller's own grammar. The appended entry —
   ``llm_call`` — carries exactly the closed Q1.5 list: never the raw prompt
   (the digest pair describes the scrubbed, tokenized outbound bytes), never
   response content, and never either table (ruling D4).

The audit entry rides the same session as the call (ADR-0009: the chain
append and the call commit or roll back together at the request edge).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.llm.control_plane_channel import (
    InferenceChannel,
    InferenceChannelRefused,
    InferenceChannelUnavailable,
)
from app.adapters.llm.local_provider import LocalProvider, tls_allowed
from app.adapters.llm.port import LlmTransport as LlmTransportPort
from app.application import audit, company_master, entitlement, risk_acknowledgment
from app.core.config import settings
from app.domain.ai_gateway import (
    CallFacts,
    CallRefusalReason,
    LlmTransport,
    TaskType,
    decide_call,
    response_envelope_violations,
)
from app.domain.audit import AuditAction
from app.domain.redaction import (
    PROFILES,
    ComposedSnippet,
    TokenizedText,
    compose_snippet,
    restore_tokens,
    snippet_digest,
    tokenize,
)

# The audit mapping's closed key list (Q1.5): exactly these seven facts,
# nothing else — never the raw prompt, never response content.
LLM_CALL_KEYS = frozenset(
    {
        "task_type",
        "provider",
        "model_id",
        "prompt_tokens",
        "completion_tokens",
        "snippet_summary",
        "snippet_sha256",
    }
)

# The reply-parsing profile's name-bearing field: the one field whose value
# is a natural-person name known to the gateway before the call, so its
# value is the PERSON-token source for profiles that tokenize names. The
# profile's allow-list is what makes this closed: any other field name is
# refused at compose, so no other field can smuggle a name list in.
_NAME_FIELDS = frozenset({"officer_name"})

# Bounded machine codes for the audited failure modes. The refused/unavailable
# pair mirrors the channel's typed errors; the envelope code covers any
# response the domain rule will not trust. All are fixed strings or the
# channel's already-bounded refusal code — nothing a provider echoed.
_ENVELOPE_REJECTED = "invalid_response_envelope"


class InferenceRefused(Exception):
    """The pre-flight rule refused the call.

    ``reason`` is the closed ``CallRefusalReason`` vocabulary. Nothing was
    composed into a payload, nothing transited, and nothing was audited
    (ruling R5) — the per-company register and flags are the record.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class InferenceResult:
    """What one allowed call returns to its caller.

    ``result`` is the restored structured answer — placeholders re-identified
    locally, the only place the restored content exists. ``provider``,
    ``model_id``, ``prompt_name`` and ``prompt_version`` are the pinned
    envelope facts a caller records for its own audit trail (§2.7's
    assessment ``model`` pin): identifiers, never content.
    """

    result: Any
    provider: str
    model_id: str
    prompt_name: str
    prompt_version: int


@dataclass(frozen=True)
class SplitCallResult:
    """What the split's one call returns (workstream ③, rulings D2/D4) —
    the company-less counterpart of ``InferenceResult``.

    ``result`` is the **unrestored** answer: the span grammar validates
    against the tokens, so restoring here would defeat it — the caller
    restores each span locally, per work item, after the grammar passes.
    ``tokens`` is the merged request-scoped re-identification table (the
    compose scrub's entries under the tokenizer's, the same merge the
    anchored restore uses); the caller restores slices with it, and it dies
    with the request (ruling D4). ``text`` is the tokenized **content** the
    spans divide — the profile's one field, without its wire label: ruling
    D3's coverage rule reconstructs the content, not the labelled snippet
    the provider read.
    """

    result: Any
    tokens: Mapping[str, str]
    text: str
    provider: str
    model_id: str
    prompt_name: str
    prompt_version: int


class InferenceUnavailable(Exception):
    """The snippet left and the call failed, or the answer could not be
    trusted.

    ``code`` is a bounded machine string safe for the append-only audit
    chain. The attempt is audited (null token counts, ``details.error``) —
    the snippet transited, and the record of what left is a compliance fact.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def llm_client(transport: LlmTransport) -> LlmTransportPort:
    """The transport for one custody option — the seam tests replace.

    Mirrors ``app.application.lease.client()``: the Standard path reads the
    activation credential from the credential file (never the database) and
    hands it to the channel as the bearer token; Enterprise Offline builds
    the local provider from ``AICS_LLM_*``. Configuration gaps the pre-flight
    rule also checks are re-checked here, fail-closed: by the time this runs
    the decision is made, and a missing credential or base URL — including a
    base URL the URL parser itself rejects, which the adapters refuse at
    construction with their bounded ``malformed_base_url`` code — must raise
    ``InferenceRefused`` (nothing sent, nothing audited), not construct a
    client that fails on the wire.
    """
    if transport is LlmTransport.CONTROL_PLANE:
        credential = ActivationStore(settings.cp_activation_token_file).load().credential
        if credential is None or not settings.cp_base_url:
            raise InferenceRefused(CallRefusalReason.AI_NOT_CONFIGURED.value)
        # The channel's result is deliberately a sibling dataclass, not this
        # module's TransportResult (the channel imports nothing from app.*),
        # so the structural Protocol match is stated, not inferred. A
        # construction-time refusal is a configuration gap (today: a
        # malformed base URL), not an attempted call: the gateway's closed
        # vocabulary names it, and nothing is sent or audited.
        try:
            return cast(
                LlmTransportPort,
                InferenceChannel(
                    base_url=settings.cp_base_url,
                    token=credential.token,
                    timeout=settings.cp_request_timeout_seconds,
                ),
            )
        except InferenceChannelRefused as exc:
            raise InferenceRefused(CallRefusalReason.AI_NOT_CONFIGURED.value) from exc
    if not settings.llm_base_url:
        raise InferenceRefused(CallRefusalReason.AI_NOT_CONFIGURED.value)
    try:
        return cast(
            LlmTransportPort,
            LocalProvider(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                allow_insecure=settings.llm_allow_insecure,
            ),
        )
    except InferenceChannelRefused as exc:
        raise InferenceRefused(CallRefusalReason.AI_NOT_CONFIGURED.value) from exc


async def infer(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    task_type: TaskType | str,
    language: str,
    content: Mapping[str, str] | str,
    tokenize_values: Mapping[str, Sequence[str]] = {},
    allowed_literals: frozenset[str] = frozenset(),
    actor_user_id: UUID | None = None,
) -> InferenceResult:
    """One inference call for one company, end to end.

    ``content`` is the task's payload: a field mapping for the three
    client-content tasks, or the staff-authored question string for ordinance
    Q&A (ruling R11). ``tokenize_values`` is the caller's typed master-data
    lexicon (label from ``TOKEN_LABELS`` to exact values); it is used only as
    the tokenizer's replacement sources — it is not a snippet field, is never
    composed into the payload, and never transits. ``allowed_literals`` is the
    task's own closed vocabulary of bare words the answer may carry without a
    token (the reply parser's enum set, ``reply_parsing.PARSE_LITERALS``): the
    strict restore admits a declared literal and refuses any other value the
    model wrote itself that is not a token from these tables — a value literal
    it never saw or a placeholder this call never minted — with
    ``RedactionRefused("invented_value")``. Returns the restored
    structured result — re-identified locally from the merge of the compose
    scrub's table and the tokenizer's (rulings D3/D4; the tokenizer's entries
    win a collision) — together with
    the pinned provider facts (§2.7's ``model`` pin) — the envelope's content
    itself is never handed out, only these identifiers. Raises
    ``InferenceRefused`` on any pre-flight refusal (no audit, nothing
    transited), ``RedactionRefused`` when the content cannot be made
    defensible (``field_not_allowed`` and its siblings: likewise nothing
    transited — the compose failure is before the attempt) or when the
    returned answer invents a value (``invented_value``: the snippet left and
    the attempt is audited, but the answer is refused), and
    ``InferenceUnavailable`` when the attempt was made and
    failed or the response could not be trusted (the attempt is audited). A
    refusal never partially sends, and no refused answer is returned.
    """
    task = TaskType(task_type)
    if not isinstance(language, str) or not language:
        raise ValueError("language must be a non-empty string")
    company = await company_master.get_company(
        session, tenant_id=tenant_id, company_id=company_id
    )
    view = await entitlement.get_entitlement(session)
    decision = decide_call(
        CallFacts(
            tier=view.tier,
            license_state=view.license_state,
            gate=settings.ai_q13_gate,
            activation_credential_present=(
                ActivationStore(settings.cp_activation_token_file).load().credential
                is not None
            ),
            local_llm_configured=_local_llm_configured(),
            company_ai_disabled=company.ai_disabled,
            acknowledgment_current=await risk_acknowledgment.is_acknowledged_current(
                session, tenant_id=tenant_id
            ),
        )
    )
    if not decision.allowed or decision.transport is None or decision.reason is not None:
        reason = (
            decision.reason.value
            if decision.reason is not None
            else CallRefusalReason.AI_NOT_CONFIGURED.value
        )
        raise InferenceRefused(reason)

    composed = _compose(task, company.br_number, content)
    outbound = _tokenize(composed, tokenize_values)
    digest = snippet_digest(outbound.text)

    try:
        outcome = await llm_client(decision.transport).complete(
            task_type=task.value, language=language, snippet=outbound.text
        )
    except (InferenceChannelRefused, InferenceChannelUnavailable) as exc:
        code = exc.code if isinstance(exc, InferenceChannelRefused) else "unreachable"
        await _book_call(
            session,
            tenant_id=tenant_id,
            company_id=company.id,
            actor_user_id=actor_user_id,
            llm_call=_failed_llm_call(task, digest),
            error=code,
        )
        raise InferenceUnavailable(code) from exc

    if response_envelope_violations(outcome.envelope):
        await _book_call(
            session,
            tenant_id=tenant_id,
            company_id=company.id,
            actor_user_id=actor_user_id,
            llm_call=_failed_llm_call(task, digest),
            error=_ENVELOPE_REJECTED,
        )
        raise InferenceUnavailable(_ENVELOPE_REJECTED)

    envelope = outcome.envelope
    await _book_call(
        session,
        tenant_id=tenant_id,
        company_id=company.id,
        actor_user_id=actor_user_id,
        llm_call={
            "task_type": task.value,
            "provider": envelope["provider"],
            "model_id": envelope["model_id"],
            "prompt_tokens": envelope["usage"]["prompt_tokens"],
            "completion_tokens": envelope["usage"]["completion_tokens"],
            "snippet_summary": digest.summary,
            "snippet_sha256": digest.sha256,
        },
    )
    return InferenceResult(
        # Local restore over the merged request-scoped tables (rulings D3/D4):
        # the compose scrub's entries first, the tokenizer's after, so a marker
        # both produced resolves to the tokenizer's more specific value — a
        # collision the tokenizer refuses up front (R-T7-2), so no caller of a
        # client-content task can reach it. The merge lives here and dies with
        # the request — it is never audited. The restore is strict: a value the
        # model wrote itself that is neither a token here nor a declared literal
        # (``allowed_literals``) refuses the answer (``invented_value``) — after
        # the attempt, which is audited above, and before anything is returned.
        result=restore_tokens(
            envelope["result"],
            {**composed.tokens, **outbound.tokens},
            allowed_literals=allowed_literals,
        ),
        provider=envelope["provider"],
        model_id=envelope["model_id"],
        prompt_name=envelope["prompt_name"],
        prompt_version=envelope["prompt_version"],
    )


# The split profile's one admitted field — the coverage content is this
# field's tokenized value, not the labelled wire text (see SplitCallResult).
_SPLIT_CONTENT_FIELD = "content"


async def infer_split(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    parent_id: UUID,
    language: str,
    content: str,
    tokenize_values: Mapping[str, Sequence[str]] = {},
    actor_user_id: UUID | None = None,
) -> SplitCallResult:
    """The split's one call (workstream ③, ADR-0019 §3) — company-less by
    design, through this same sole-LLM seam.

    Ruling D2 makes the call anchor-free: the profile admits one tokenized
    free-text field and no company anchor, so the involved companies never
    enter the payload — the provider divides placeholders and prose, and the
    token-to-company binding stays with the caller. Ruling D4 makes the call
    indivisible: the per-company legs (any ``ai_disabled``, every
    ``AI_REPLY_PARSING`` gate) are the caller's all-or-nothing pre-flight,
    already run before this seam is reached, so the deployment-posture legs
    (tier, licence state, the Q1.3 gate, the acknowledgment, the transport
    custody) are checked here with the per-company facts satisfied — passing
    them again per company would re-decide what D4 decides for the whole
    parent. The attempt's audit is the parent's own event: ``company_id``
    NULL, entity the split parent (ruling D10), the same closed ``llm_call``
    list as every call. The answer is **not** restored: the span grammar
    validates against the tokens (``app.domain.ai_gateway.split_violations``),
    and the caller restores each span locally — a restore here would defeat
    the check. Raises the same typed vocabulary as ``infer``.
    """
    if not isinstance(language, str) or not language:
        raise ValueError("language must be a non-empty string")
    view = await entitlement.get_entitlement(session)
    decision = decide_call(
        CallFacts(
            tier=view.tier,
            license_state=view.license_state,
            gate=settings.ai_q13_gate,
            activation_credential_present=(
                ActivationStore(settings.cp_activation_token_file).load().credential
                is not None
            ),
            local_llm_configured=_local_llm_configured(),
            # Ruling D4's all-or-nothing per-company legs — the caller's
            # pre-flight (decide_split_pre_flight) ran before this seam; this
            # fact says only that it passed.
            company_ai_disabled=False,
            acknowledgment_current=await risk_acknowledgment.is_acknowledged_current(
                session, tenant_id=tenant_id
            ),
        )
    )
    if not decision.allowed or decision.transport is None or decision.reason is not None:
        reason = (
            decision.reason.value
            if decision.reason is not None
            else CallRefusalReason.AI_NOT_CONFIGURED.value
        )
        raise InferenceRefused(reason)

    composed = compose_snippet(task_type=TaskType.SPLIT, fields={_SPLIT_CONTENT_FIELD: content})
    outbound = _tokenize(composed, tokenize_values)
    digest = snippet_digest(outbound.text)

    try:
        outcome = await llm_client(decision.transport).complete(
            task_type=TaskType.SPLIT.value, language=language, snippet=outbound.text
        )
    except (InferenceChannelRefused, InferenceChannelUnavailable) as exc:
        code = exc.code if isinstance(exc, InferenceChannelRefused) else "unreachable"
        await _book_split_call(
            session,
            tenant_id=tenant_id,
            parent_id=parent_id,
            actor_user_id=actor_user_id,
            llm_call=_failed_llm_call(TaskType.SPLIT, digest),
            error=code,
        )
        raise InferenceUnavailable(code) from exc

    if response_envelope_violations(outcome.envelope):
        await _book_split_call(
            session,
            tenant_id=tenant_id,
            parent_id=parent_id,
            actor_user_id=actor_user_id,
            llm_call=_failed_llm_call(TaskType.SPLIT, digest),
            error=_ENVELOPE_REJECTED,
        )
        raise InferenceUnavailable(_ENVELOPE_REJECTED)

    envelope = outcome.envelope
    await _book_split_call(
        session,
        tenant_id=tenant_id,
        parent_id=parent_id,
        actor_user_id=actor_user_id,
        llm_call={
            "task_type": TaskType.SPLIT.value,
            "provider": envelope["provider"],
            "model_id": envelope["model_id"],
            "prompt_tokens": envelope["usage"]["prompt_tokens"],
            "completion_tokens": envelope["usage"]["completion_tokens"],
            "snippet_summary": digest.summary,
            "snippet_sha256": digest.sha256,
        },
    )
    return SplitCallResult(
        # Unrestored, by design: the span grammar checks the tokens, and the
        # caller restores each span locally for its work item. The coverage
        # text is the content field's tokenized value — the profile admits
        # exactly one field, so the labelled wire text less its label is the
        # content the provider was asked to divide.
        result=envelope["result"],
        tokens={**composed.tokens, **outbound.tokens},
        text=outbound.text.removeprefix(f"{_SPLIT_CONTENT_FIELD}: "),
        provider=envelope["provider"],
        model_id=envelope["model_id"],
        prompt_name=envelope["prompt_name"],
        prompt_version=envelope["prompt_version"],
    )


async def _book_split_call(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    parent_id: UUID,
    actor_user_id: UUID | None,
    llm_call: Mapping[str, Any],
    error: str | None = None,
) -> None:
    """The split call's audit entry: the parent's own event — ``company_id``
    NULL (ruling D10), the entity the split parent, the same closed
    ``llm_call`` list and ``details.error`` discipline as ``_book_call``.
    """
    assert set(llm_call) == LLM_CALL_KEYS
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.LLM_CALL,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="split_parent",
        entity_id=parent_id,
        details=None if error is None else {"error": error},
        llm_call=llm_call,
    )


def _local_llm_configured() -> bool:
    """Whether the local custody option is configured: a base URL that the
    TLS rule accepts (the same rule the provider's constructor enforces, so
    a cleartext URL reads as ``ai_not_configured`` at pre-flight — nothing
    ever reaches the constructor with a key to offer). A base URL the parser
    itself rejects is likewise a configuration gap, not a transport fact:
    the rule's refusal reads as "not configured"."""
    if not settings.llm_base_url:
        return False
    try:
        return tls_allowed(settings.llm_base_url, allow_insecure=settings.llm_allow_insecure)
    except InferenceChannelRefused:
        return False


def _compose(
    task: TaskType, anchor: str, content: Mapping[str, str] | str
) -> ComposedSnippet:
    """The one composition point, with the content shape the profile admits.

    A string is ordinance Q&A's staff-authored question — the only profile
    that admits no client content (ruling R11). A mapping is the client-
    content tasks' field set, anchored on the company row's BRN. A mismatched
    shape is a programming error, not a redaction event: ``TypeError``.
    """
    if not PROFILES[task].allows_client_content:
        if not isinstance(content, str):
            raise TypeError(
                f"content for {task.value} must be the question string"
            )
        return compose_snippet(task_type=task, non_client_text=content)
    if not isinstance(content, Mapping):
        raise TypeError(
            f"content for {task.value} must be a mapping of task fields"
        )
    return compose_snippet(task_type=task, company_anchor=anchor, fields=content)


def _tokenize(
    composed: ComposedSnippet, tokenize_values: Mapping[str, Sequence[str]] = {}
) -> TokenizedText:
    """Cross-cutting rule 3 for one composed snippet.

    A profile that tokenizes names replaces its known person names with
    placeholders everywhere they appear in the snippet; the re-identification
    key stays request-scoped. The person names come from two sources: the
    profile's name-bearing field (the caller that passes a name directly),
    and the caller's typed ``tokenize_values`` lexicon (company names, BRN,
    both script forms of each officer), which overrides the field for the
    ``PERSON`` label — the reply parser supplies the real roster there rather
    than the JSON list string its ``officer_name`` field carries. The lexicon
    is replacement sources only: it is never composed into the snippet.
    Profiles that opted out (justification recorded in the profile) and
    ordinance Q&A (no client content at all) pass through with an empty key —
    the tokenizer then adds nothing to the restore table, while the compose
    scrub's entries (``composed.tokens``, ruling D3) still apply.

    A ``tokenize_values`` label the scrubber reserves (``SCRUB_LABELS``) is a
    caller bug and is refused by ``tokenize`` with ``ValueError`` (ruling
    R-T7-2): the two passes share the ``[LABEL_nn]`` namespace, and a marker
    both tables claimed would re-identify to a lexicon value that never left
    the snippet.
    """
    if not PROFILES[composed.task_type].tokenize_names:
        return TokenizedText(text=composed.text, tokens={})
    persons = [value for name, value in composed.fields if name in _NAME_FIELDS]
    values = {"PERSON": persons, **tokenize_values}
    return tokenize(composed.text, values=values)


def _failed_llm_call(task: TaskType, digest: Any) -> dict[str, Any]:
    """The audited mapping for an attempted call that failed: the closed
    list with the provider facts null (the provider never answered) — the
    digest still binds the exact bytes that left."""
    return {
        "task_type": task.value,
        "provider": None,
        "model_id": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "snippet_summary": digest.summary,
        "snippet_sha256": digest.sha256,
    }


async def _book_call(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    llm_call: Mapping[str, Any],
    error: str | None = None,
) -> None:
    """Append the one audit entry for a call whose snippet left.

    Success: the closed Q1.5 facts. Failure: the same list with null provider
    facts plus ``details.error`` — the bounded code, never anything the
    provider echoed. The single write path (ADR-0009), same session.
    """
    assert set(llm_call) == LLM_CALL_KEYS
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.LLM_CALL,
        actor_user_id=actor_user_id,
        company_id=company_id,
        entity_type="company",
        entity_id=company_id,
        details=None if error is None else {"error": error},
        llm_call=llm_call,
    )
