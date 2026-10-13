"""Hermetic tests for the gateway orchestration (M6.4; no database).

``app.application.ai_gateway.infer`` is the only path that calls an LLM.
These tests drive it with its collaborators replaced — the module-level
seams ``company_master.get_company``, ``entitlement.get_entitlement``,
``audit.record_event`` and ``llm_client`` — over a dummy session, mirroring
the dependency-override pattern the API suites use. What is proven:

- the pre-flight rule's refusals short-circuit: no transport call, no audit
  entry (ruling R5 — a refused call writes nothing);
- the allowed path composes → scrubs → tokenizes → sends exactly the
  composed text, restores placeholders in the result, and audits the closed
  Q1.5 list on the same session — never the raw prompt, never response
  content beyond the list;
- a transport failure after the attempt still books the audit entry with
  null token counts and the bounded error code;
- an untrusted envelope is refused, not restored.

Test data is synthetic throughout.
"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.llm.control_plane_channel import (
    InferenceChannelRefused,
    InferenceChannelUnavailable,
)
from app.adapters.llm.port import TransportResult
from app.application import ai_gateway
from app.application.entitlement import EntitlementView
from app.core.config import settings
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.ai_gateway import TaskType
from app.domain.entitlement import EntitlementSource, LicenseState, Tier
from app.domain.redaction import RedactionRefused, snippet_digest
from app.domain.reply_parsing import PARSE_LITERALS

COMPANY_ID = uuid4()
TENANT_ID = uuid4()
BRN = "22334455"
OFFICER = "CHAN Tai Man"
COMPOSED_MARKER = "MARKERCOMPOSED7F3D"
REPLY_TEXT = f"{OFFICER} confirmed the {COMPOSED_MARKER} relocation."

CONTENT = {
    "officer_name": OFFICER,
    "reply_text": REPLY_TEXT,
    "role_reference": "director",
}

QUESTION = "Under Cap. 622 s.584, what filing window applies?"

ENVELOPE: dict[str, Any] = {
    "protocol_version": 1,
    "provider": "stub-provider",
    "task_type": "reply_parsing",
    "model_id": "stub-model",
    "prompt_name": "reply_parser",
    "prompt_version": 1,
    "result": {"events": [{"officer": "[PERSON_01]", "note": "resigned"}]},
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "completed_at": "2026-09-30T10:00:00+00:00",
}


def make_company(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": COMPANY_ID,
        "br_number": BRN,
        "ai_disabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_view(**overrides: object) -> EntitlementView:
    values: dict[str, object] = {
        "tier": Tier.STANDARD,
        "license_state": LicenseState.ACTIVE,
        "capabilities": frozenset(),
        "seat_cap": None,
        "company_cap": None,
        "license_expires_at": datetime(2027, 9, 30, tzinfo=UTC),
        "entitlement_version": 1,
        "source": EntitlementSource.CONFIGURATION,
    }
    values.update(overrides)
    return EntitlementView(**values)


class FakeTransport:
    """The replaced port: records the call, then answers or raises typed."""

    def __init__(
        self,
        *,
        envelope: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, str]] = []
        self._envelope = envelope if envelope is not None else ENVELOPE
        self._error = error

    async def complete(self, *, task_type: str, language: str, snippet: str) -> TransportResult:
        self.calls.append({"task_type": task_type, "language": language, "snippet": snippet})
        if self._error is not None:
            raise self._error
        return TransportResult(envelope=dict(self._envelope))


class GatewayHarness:
    """The module's collaborators, replaced and recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.transport = FakeTransport()
        self.decisions: list[object] = []
        self.audit_entries: list[dict[str, Any]] = []

        async def fake_get_company(session: object, **kwargs: object) -> SimpleNamespace:
            self.company_kwargs = kwargs
            return make_company()

        async def fake_get_entitlement(session: object, **kwargs: object) -> EntitlementView:
            return make_view()

        async def fake_record_event(session: object, **kwargs: object) -> None:
            self.audit_entries.append(kwargs)

        async def fake_acknowledged_current(session: object, **kwargs: object) -> bool:
            return True

        def fake_llm_client(transport: object) -> FakeTransport:
            self.decisions.append(transport)
            return self.transport

        monkeypatch.setattr(ai_gateway.company_master, "get_company", fake_get_company)
        monkeypatch.setattr(ai_gateway.entitlement, "get_entitlement", fake_get_entitlement)
        monkeypatch.setattr(ai_gateway.audit, "record_event", fake_record_event)
        monkeypatch.setattr(
            ai_gateway.risk_acknowledgment, "is_acknowledged_current", fake_acknowledged_current
        )
        monkeypatch.setattr(ai_gateway, "llm_client", fake_llm_client)

    def override_company(self, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
        async def fake_get_company(session: object, **kwargs: object) -> SimpleNamespace:
            return make_company(**overrides)

        monkeypatch.setattr(ai_gateway.company_master, "get_company", fake_get_company)

    def override_view(self, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
        async def fake_get_entitlement(session: object, **kwargs: object) -> EntitlementView:
            return make_view(**overrides)

        monkeypatch.setattr(ai_gateway.entitlement, "get_entitlement", fake_get_entitlement)

    def override_acknowledgment(
        self, monkeypatch: pytest.MonkeyPatch, *, current: bool
    ) -> None:
        async def fake_acknowledged_current(session: object, **kwargs: object) -> bool:
            return current

        monkeypatch.setattr(
            ai_gateway.risk_acknowledgment, "is_acknowledged_current", fake_acknowledged_current
        )


def plant_credential(path: object) -> None:
    """A credential in the per-test activation file (the conftest tmp path)."""
    credential = ActivationCredential(
        token="activation-token",
        token_id=uuid4(),
        control_plane_tenant_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    ActivationStore(path).save(ActivationState(credential=credential))


async def infer(content: object = CONTENT, **overrides: object) -> Any:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "task_type": TaskType.REPLY_PARSING,
        "language": "en",
        "content": content,
    }
    kwargs.update(overrides)
    return await ai_gateway.infer(None, **kwargs)  # type: ignore[arg-type]


# --- Pre-flight refusals: no transport, no audit (ruling R5) ---


async def test_ai_disabled_refuses_without_audit_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)
    harness.override_company(monkeypatch, ai_disabled=True)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_disabled"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_gate_unset_refuses_without_audit_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "")
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_gate_unset"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_acknowledgment_missing_refuses_without_audit_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator layer (2026-10-04 rulings): a gated deployment whose
    operator has not confirmed the risk statement refuses
    ``ai_acknowledgment_missing`` — the precedence slot between the gate
    and the per-tier configuration, nothing sent, nothing audited."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)
    harness.override_acknowledgment(monkeypatch, current=False)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_acknowledgment_missing"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_free_tier_refuses_without_audit_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, tier=Tier.FREE)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_tier_free"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_free_license_state_refuses_even_on_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §4.3 degradation: a lapsed licence reads Free, and Free has no AI."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, license_state=LicenseState.FREE)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_tier_free"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_standard_without_credential_refuses_not_configured(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The per-test credential file is empty: Standard's channel has nothing
    to authenticate with, and the refusal is a pre-flight one — no audit."""
    assert not activation_credential_file.exists()  # type: ignore[union-attr]
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_not_configured"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_enterprise_without_local_llm_refuses_not_configured(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "llm_base_url", "")
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, tier=Tier.ENTERPRISE_OFFLINE)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_not_configured"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_enterprise_with_cleartext_local_llm_refuses_not_configured(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The local provider's TLS rule applied at pre-flight: a cleartext base
    URL is a configuration refusal (nothing ever reaches the constructor)."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.local:11434")
    monkeypatch.setattr(settings, "llm_allow_insecure", False)
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, tier=Tier.ENTERPRISE_OFFLINE)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_not_configured"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


# --- The allowed path: compose → scrub → tokenize → send → restore → audit ---


async def test_allowed_call_sends_the_composed_tokenized_snippet(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    result = await infer()
    # The wire payload is the composed, scrubbed, tokenized text: the
    # officer's real name never crosses; the placeholder does.
    (call,) = harness.transport.calls
    assert call["task_type"] == "reply_parsing"
    assert call["language"] == "en"
    assert OFFICER not in call["snippet"]
    assert "[PERSON_01]" in call["snippet"]
    assert call["snippet"].startswith(f"company: {BRN}")
    # The transport follows the tier: Standard → the control-plane channel.
    assert harness.decisions == [ai_gateway.LlmTransport.CONTROL_PLANE]
    # The response's placeholders are restored locally before returning.
    assert result.result == {"events": [{"officer": OFFICER, "note": "resigned"}]}
    # The pinned provider facts travel with the result for the caller's
    # own record (§2.7's model pin) — identifiers, never content.
    assert result.model_id == "stub-model"
    assert result.prompt_name == "reply_parser"
    assert result.prompt_version == 1
    # Exactly one audit entry, on the closed Q1.5 list, digesting exactly the
    # bytes that left.
    (entry,) = harness.audit_entries
    assert entry["action"].value == "llm_call"
    assert entry["company_id"] == COMPANY_ID
    llm_call = entry["llm_call"]
    assert set(llm_call) == {
        "task_type",
        "provider",
        "model_id",
        "prompt_tokens",
        "completion_tokens",
        "snippet_summary",
        "snippet_sha256",
    }
    assert llm_call["task_type"] == "reply_parsing"
    assert llm_call["provider"] == "stub-provider"
    assert llm_call["model_id"] == "stub-model"
    assert llm_call["prompt_tokens"] == 12
    assert llm_call["completion_tokens"] == 3
    digest = snippet_digest(call["snippet"])
    assert llm_call["snippet_summary"] == digest.summary
    assert llm_call["snippet_sha256"] == digest.sha256
    # The raw name and the raw payload never appear in the audit record. The
    # composed-snippet marker may — the summary describes the outbound
    # snippet, and that text is scrubbed and tokenized, not client prose.
    assert OFFICER not in str(entry["llm_call"])
    assert REPLY_TEXT not in str(entry["llm_call"])
    assert COMPOSED_MARKER in llm_call["snippet_summary"]


# --- The scrub table at the application seam (Task 6: D3 and D4) ---

RAW_HKID = "A123456(7)"


async def test_the_token_table_is_not_audited_and_restores_locally(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """Ruling D3 at the seam that actually sends, and ruling D4's second half:
    a mask the scrubber made is re-identified locally from the compose table —
    this call passes no HKID lexicon value, so the entry can only come from
    there — while the audit entry, the one artifact that outlives the request,
    carries the closed Q1.5 list and neither the raw value nor the table. The
    wire gets the marker the summary describes."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    envelope = {
        **ENVELOPE,
        "result": {"events": [{"officer": "[PERSON_01]", "hkid": "[HKID_01]"}]},
    }
    harness.transport = FakeTransport(envelope=envelope)
    content = {
        "officer_name": OFFICER,
        "reply_text": f"{OFFICER} confirmed; HKID {RAW_HKID} is on file.",
        "role_reference": "director",
    }
    result = await infer(content)
    assert result.result == {"events": [{"officer": OFFICER, "hkid": RAW_HKID}]}
    (call,) = harness.transport.calls
    assert RAW_HKID not in call["snippet"]
    assert "[HKID_01]" in call["snippet"]
    (entry,) = harness.audit_entries
    llm_call = entry["llm_call"]
    # The closed Q1.5 list, and every value in it a scalar fact: the marker may
    # appear inside the digest's summary (it describes the scrubbed, tokenized
    # bytes), but no marker-to-value mapping is recorded anywhere.
    assert set(llm_call) == ai_gateway.LLM_CALL_KEYS
    assert all(
        isinstance(value, (str, int)) or value is None for value in llm_call.values()
    ), "the audit entry carries only Q1.5's scalar facts, never the table"
    assert RAW_HKID not in str(entry)


async def test_a_scrub_reserved_lexicon_label_is_refused(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """Ruling R-T7-2 at the seam that merges the tables: a caller lexicon under a
    scrub label would collide with a scrub marker, so the merge could re-identify
    a marker to a value that never left the snippet. It is a caller bug —
    ``ValueError``, the same signal as an unknown label — not a client-data
    refusal, and nothing is composed or sent."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(ValueError, match="HKID"):
        await infer(
            {"reply_text": f"My HKID is {RAW_HKID}, recorded."},
            tokenize_values={"HKID": ["B987654(3)"]},
        )
    assert harness.transport.calls == []
    assert harness.audit_entries == []


# --- The typed master-data lexicon at the application seam (Task 3) ---
#
# The leak this closes: ``reply_parsing._content`` hands the gateway the
# known officer names as a JSON *list string* under ``officer_name``, and the
# pre-change ``_tokenize`` forwarded that whole string as the tokenizer's one
# person value — so a name written inside ``reply_text`` was never matched.
# The fix threads a typed lexicon (``tokenize_values``) from the caller to the
# typed tokenizer; these cases prove it at the seam that actually sends.

LEXICON_OFFICER_EN = "Chan Tai Man"
LEXICON_OFFICER_ZH = "陳大文"
LEXICON_COMPANY = "Example Limited"
LEXICON_COMPANY_ZH = "例子有限公司"
LEXICON_BRN = "99887766"
LEXICON_REPLY = (
    f"{LEXICON_OFFICER_EN} signed the resolution; {LEXICON_OFFICER_ZH} agreed; "
    f"{LEXICON_COMPANY} (BRN {LEXICON_BRN}) confirmed."
)


async def test_the_caller_lexicon_tokenizes_known_values_in_the_reply(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The application-seam proof the T1 reviewer asked for: through the real
    ``infer`` path, a known officer's name written inside ``reply_text`` — and
    the officer_name field's JSON list string, the exact shape the live caller
    sends — reaches the wire as ``[PERSON_nn]``, never as the name. The
    company's own name tokenizes in the reply body, and the composed snippet's
    ``company: <brn>`` anchor — the company row's BRN, the same value the live
    lexicon carries (``reply_parsing.py``) — becomes ``company: [BRN_01]``.
    The lexicon is replacement sources only: it is not a snippet field and
    never transits."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    # The live caller's company row and its lexicon carry the *same* BRN, so
    # align the harness row with the lexicon: the anchor is then the value the
    # tokenizer must replace, and the ``company: [BRN_01]`` assertion below
    # proves the anchor path rather than a BRN that happens to sit in the
    # reply body.
    harness.override_company(monkeypatch, br_number=LEXICON_BRN)
    content = {
        "reply_text": LEXICON_REPLY,
        "role_reference": "director",
        "officer_name": json.dumps(
            [LEXICON_OFFICER_EN, LEXICON_OFFICER_ZH], ensure_ascii=False
        ),
    }
    await infer(
        content,
        tokenize_values={
            "PERSON": [LEXICON_OFFICER_EN, LEXICON_OFFICER_ZH],
            "COMPANY": [LEXICON_COMPANY],
            "BRN": [LEXICON_BRN],
        },
    )
    (call,) = harness.transport.calls
    snippet = call["snippet"]
    for raw in (LEXICON_OFFICER_EN, LEXICON_OFFICER_ZH, LEXICON_COMPANY, LEXICON_BRN):
        assert raw not in snippet, f"raw lexicon value reached the wire: {raw!r}"
    assert "[PERSON_01]" in snippet
    assert "[COMPANY_01]" in snippet
    # The anchor path, named: the composed snippet's first line is the company
    # row's BRN and it is tokenized — the literal BRN (and the harness's
    # default row BRN) is nowhere, the anchor is the placeholder.
    assert "company: [BRN_01]" in snippet
    assert LEXICON_BRN not in snippet
    assert BRN not in snippet
    # The allow-listed field set is unchanged — the lexicon added no field.
    assert "tokenize_values" not in snippet


async def test_reply_parsing_content_supplies_the_typed_lexicon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller half: ``reply_parsing._content`` hands the gateway both
    script forms of every in-office officer (a person with both names yields
    two separate PERSON values), the company's own names, and its BRN — keyed
    PERSON / COMPANY / BRN. The profile-admitted field mapping is unchanged."""
    from app.application import reply_parsing

    master = SimpleNamespace(
        name=SimpleNamespace(name_en=LEXICON_COMPANY, name_zh=LEXICON_COMPANY_ZH),
        company=SimpleNamespace(br_number=LEXICON_BRN),
    )
    appointments = [
        SimpleNamespace(
            person=SimpleNamespace(
                name_en=LEXICON_OFFICER_EN,
                name_zh=LEXICON_OFFICER_ZH,
                surname_en=None,
                other_names_en=None,
            )
        ),
        SimpleNamespace(
            person=SimpleNamespace(
                name_en=None,
                name_zh=None,
                surname_en="Wong",
                other_names_en="Ka Yan",
            )
        ),
    ]

    async def fake_master(session: object, **kwargs: object) -> object:
        return master

    async def fake_appointments(session: object, **kwargs: object) -> object:
        return appointments

    monkeypatch.setattr(reply_parsing.company_master, "get_company_master", fake_master)
    monkeypatch.setattr(reply_parsing.officers, "list_appointments", fake_appointments)

    context = await reply_parsing._content(
        None,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        as_of=date(2026, 10, 6),
        checklist=[],
        reply_text="the director is unchanged",
    )
    assert set(context.fields) == {"reply_text", "role_reference", "officer_name"}
    assert context.tokenize_values["PERSON"] == [
        LEXICON_OFFICER_EN,
        LEXICON_OFFICER_ZH,
        "Wong Ka Yan",
    ]
    assert context.tokenize_values["COMPANY"] == [LEXICON_COMPANY, LEXICON_COMPANY_ZH]
    assert context.tokenize_values["BRN"] == [LEXICON_BRN]


async def test_ordinance_qa_sends_the_question_without_a_company_anchor(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """R11: the staff-authored question is the whole payload — no anchor, no
    tokenization — and the digest covers it exactly."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    envelope = {
        **ENVELOPE,
        "task_type": "ordinance_qa",
        "result": {"answer": "Within 42 days."},
    }
    harness.transport = FakeTransport(envelope=envelope)
    result = await infer(QUESTION, task_type=TaskType.ORDINANCE_QA)
    assert result.result == {"answer": "Within 42 days."}
    (call,) = harness.transport.calls
    assert call["snippet"] == QUESTION
    assert "company:" not in call["snippet"]
    assert BRN not in call["snippet"]
    (entry,) = harness.audit_entries
    digest = snippet_digest(QUESTION)
    assert entry["llm_call"]["snippet_sha256"] == digest.sha256
    assert entry["llm_call"]["task_type"] == "ordinance_qa"


async def test_local_transport_is_chosen_for_enterprise_offline(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "llm_base_url", "https://llm.local:11434")
    monkeypatch.setattr(settings, "llm_allow_insecure", False)
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, tier=Tier.ENTERPRISE_OFFLINE)
    await infer()
    assert harness.decisions == [ai_gateway.LlmTransport.LOCAL]


# --- Failures after the attempt: the snippet left, so the call is audited ---


async def test_refused_transport_audits_null_counts_and_raises_bounded_code(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    harness.transport = FakeTransport(error=InferenceChannelRefused("entitlement_denied"))
    with pytest.raises(ai_gateway.InferenceUnavailable) as unavailable:
        await infer()
    assert unavailable.value.code == "entitlement_denied"
    (entry,) = harness.audit_entries
    llm_call = entry["llm_call"]
    assert llm_call["prompt_tokens"] is None
    assert llm_call["completion_tokens"] is None
    assert llm_call["provider"] is None
    assert llm_call["model_id"] is None
    assert entry["details"] == {"error": "entitlement_denied"}
    # The digest still binds the exact bytes that left.
    (call,) = harness.transport.calls
    assert llm_call["snippet_sha256"] == snippet_digest(call["snippet"]).sha256
    assert OFFICER not in str(entry["llm_call"])


async def test_unavailable_transport_audits_with_unreachable_code(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    harness.transport = FakeTransport(
        error=InferenceChannelUnavailable("POST /api/v1/inference: ConnectError")
    )
    with pytest.raises(ai_gateway.InferenceUnavailable) as unavailable:
        await infer()
    assert unavailable.value.code == "unreachable"
    (entry,) = harness.audit_entries
    assert entry["details"] == {"error": "unreachable"}
    assert entry["llm_call"]["prompt_tokens"] is None


async def test_untrusted_envelope_is_refused_and_audited(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """A malformed envelope is not restored and not trusted: the call is
    booked as a failed attempt — the snippet transited, the response may not."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    broken = {key: value for key, value in ENVELOPE.items() if key != "usage"}
    harness.transport = FakeTransport(envelope=broken)
    with pytest.raises(ai_gateway.InferenceUnavailable) as unavailable:
        await infer()
    assert unavailable.value.code == "invalid_response_envelope"
    (entry,) = harness.audit_entries
    assert entry["details"] == {"error": "invalid_response_envelope"}
    assert entry["llm_call"]["prompt_tokens"] is None


async def test_extra_envelope_key_is_untrusted_and_audited(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """A provider echoing the snippet back under a new key is a leak, not a
    field to ignore — the closed key set refuses it."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    echoing = {**ENVELOPE, "snippet_echo": "ECHOLEAK9A2B"}
    harness.transport = FakeTransport(envelope=echoing)
    with pytest.raises(ai_gateway.InferenceUnavailable) as unavailable:
        await infer()
    assert unavailable.value.code == "invalid_response_envelope"
    (entry,) = harness.audit_entries
    assert "ECHOLEAK9A2B" not in str(entry["llm_call"])
    assert "ECHOLEAK9A2B" not in str(entry["details"])


# --- Failures before the attempt: nothing left, nothing audited ---


async def test_unscrubbable_content_refuses_before_transport(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """A protected-class shape the scrubber cannot consume fails the whole
    call closed before a byte is sent — no audit, no transport."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(RedactionRefused):
        await infer({"reply_text": "HKID A123456, check digit to follow."})
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_excluded_field_refuses_before_transport(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(RedactionRefused, match="field_not_allowed"):
        await infer({"reply_text": REPLY_TEXT, "hkid_full": "A123456(7)"})
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_content_shape_is_checked_against_the_task(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """A string question is ordinance-Q&A-only; client tasks take a field
    mapping. The shape is a programming error, not a redaction refusal."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(TypeError):
        await infer("a bare question for a client task")
    with pytest.raises(TypeError):
        await infer({"question": QUESTION}, task_type=TaskType.ORDINANCE_QA)
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_response_placeholder_unknown_to_the_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """Task 7 (review focus 6): a model inventing its own placeholder is refused,
    not passed through — only tokens from this call's map are restorable, and a
    marker nothing can restore is an invention. The snippet left and the attempt
    is audited (the call happened); the answer is never returned."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    envelope = {**ENVELOPE, "result": {"note": "[PERSON_99] stayed"}}
    harness.transport = FakeTransport(envelope=envelope)
    with pytest.raises(RedactionRefused) as refused:
        await infer()
    assert refused.value.reason == "invented_value"
    (entry,) = harness.audit_entries
    assert entry["action"].value == "llm_call"


async def test_an_invented_value_in_the_answer_is_refused(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The value-literal half of the same rule: an identifier the model returns
    raw (never tokenized in the snippet, so it cannot have seen it) is refused
    at the seam that would otherwise hand it to the parser to store."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    harness.transport = FakeTransport(
        envelope={**ENVELOPE, "result": {"answer": "A987654(3)"}}
    )
    with pytest.raises(RedactionRefused) as refused:
        await infer()
    assert refused.value.reason == "invented_value"
    (entry,) = harness.audit_entries
    assert entry["action"].value == "llm_call"


async def test_a_realistic_reply_parse_answer_passes_the_strict_restore(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The scope of the rule, at the seam the reply parser drives: the strict
    restore refuses value literals, not the reply parse's own grammar. A
    realistic answer — the closed enums, a checklist ``fact_id``, free evidence
    prose, a derived ISO date, and the officer as the ``[PERSON_01]`` token it
    was sent — restores, with the token re-identified locally. This is the
    contract the reply parser's ``PARSE_LITERALS`` lands on; a stricter rule
    that refused the prose would refuse the parser's own schema."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    plant_credential(activation_credential_file)
    harness = GatewayHarness(monkeypatch)
    answer = {
        "status": "changes_reported",
        "language": "en",
        "confirmations": [
            {
                "fact_id": "officers.0.name_en",
                "answer": "confirmed",
                "evidence_span": "the director is unchanged",
                "uncertain": False,
            }
        ],
        "change_events": [
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": "[PERSON_01]",
                    "name_en": "[PERSON_01]",
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": "[PERSON_01] joined",
                    "uncertain": False,
                },
                "effective_date": "2026-02-01",
                "effective_date_text": "1 February 2026",
                "evidence_span": "[PERSON_01] joined on 1 February 2026",
                "uncertain": False,
                "informed_date": None,
                "new_values": None,
                "notes": None,
            }
        ],
        "multi_company_signals": {"company_mentions": []},
    }
    harness.transport = FakeTransport(envelope={**ENVELOPE, "result": answer})
    result = await infer(allowed_literals=PARSE_LITERALS)
    assert result.result["change_events"][0]["party"]["name_as_written"] == OFFICER
    assert result.result["status"] == "changes_reported"
    (entry,) = harness.audit_entries
    assert entry["action"].value == "llm_call"


async def test_unknown_task_type_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = GatewayHarness(monkeypatch)
    with pytest.raises(ValueError):
        await infer(task_type="not_a_task")  # type: ignore[arg-type]
    assert harness.transport.calls == []
    assert harness.audit_entries == []


def test_llm_call_key_list_is_closed() -> None:
    """The audit mapping the gateway writes is exactly the Q1.5 list."""
    assert frozenset(
        {
            "task_type",
            "provider",
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "snippet_summary",
            "snippet_sha256",
        }
    ) == ai_gateway.LLM_CALL_KEYS


async def test_missing_company_propagates_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.company_master import CompanyNotFoundError

    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    harness = GatewayHarness(monkeypatch)

    async def not_found(session: object, **kwargs: object) -> object:
        raise CompanyNotFoundError

    monkeypatch.setattr(ai_gateway.company_master, "get_company", not_found)
    with pytest.raises(CompanyNotFoundError):
        await infer()
    assert harness.transport.calls == []
    assert harness.audit_entries == []


def test_uuid_typing() -> None:
    """Module-level sanity: the constants used above are what they claim."""
    assert isinstance(TENANT_ID, UUID)
    assert isinstance(COMPANY_ID, UUID)


# --- Malformed base URLs: a configuration refusal, not an attempted call ---


async def test_enterprise_with_malformed_local_llm_refuses_not_configured(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """A base URL the parser itself rejects reads as not configured at
    pre-flight (the TLS rule's parse fails closed) — nothing sent, no audit,
    and the bounded adapter code never escapes as a raw httpx error."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "llm_base_url", "https://[::1")
    harness = GatewayHarness(monkeypatch)
    harness.override_view(monkeypatch, tier=Tier.ENTERPRISE_OFFLINE)
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_not_configured"
    assert harness.transport.calls == []
    assert harness.audit_entries == []


async def test_standard_with_malformed_control_plane_url_refuses_not_configured(
    monkeypatch: pytest.MonkeyPatch, activation_credential_file: object
) -> None:
    """The channel's construction-time parse refusal maps to the gateway's
    closed vocabulary at the client seam — a malformed base URL is a
    configuration gap, not an attempted call, so nothing is audited. The
    real ``llm_client`` is what is being exercised; only the row/entitlement
    reads and the audit path are replaced."""
    monkeypatch.setattr(settings, "ai_q13_gate", "enterprise_dpa")
    monkeypatch.setattr(settings, "cp_base_url", "::::")
    plant_credential(activation_credential_file)

    async def fake_get_company(session: object, **kwargs: object) -> SimpleNamespace:
        return make_company()

    async def fake_get_entitlement(session: object, **kwargs: object) -> EntitlementView:
        return make_view()

    audit_entries: list[dict[str, Any]] = []

    async def fake_record_event(session: object, **kwargs: Any) -> None:
        audit_entries.append(kwargs)

    monkeypatch.setattr(ai_gateway.company_master, "get_company", fake_get_company)
    monkeypatch.setattr(ai_gateway.entitlement, "get_entitlement", fake_get_entitlement)
    monkeypatch.setattr(ai_gateway.audit, "record_event", fake_record_event)

    async def fake_acknowledged_current(session: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        ai_gateway.risk_acknowledgment, "is_acknowledged_current", fake_acknowledged_current
    )

    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await infer()
    assert refused.value.reason == "ai_not_configured"
    assert audit_entries == []
