"""Wire tests for the local LLM provider (M6.4; the Enterprise Offline path).

The real client — real httpx, real JSON — over a recording fake transport,
so no test touches the network. What is exercised: the OpenAI-compatible
wire shape (``POST {base}/chat/completions``, bearer key, model + messages),
the response→envelope mapping (usage parsing, JSON result extraction), the
failure taxonomy, and the two custody rules that define this adapter: a
cleartext base URL is refused before any credential is offered (the IMAP
leg's ``plaintext_not_allowed`` precedent), and the API key appears only in
the ``Authorization`` header of the request — never in the body, never in
an error string.
"""

from typing import Any

import httpx
import pytest

from app.adapters.llm.control_plane_channel import (
    InferenceChannelRefused,
    InferenceChannelUnavailable,
)
from app.adapters.llm.local_provider import LocalProvider

BASE_URL = "https://llm.internal.example:8443"
API_KEY = "sk-local-key-that-must-stay-secret"
MODEL = "local-model-7b"
SNIPPET = "company: 22334455\nreply_text: Director [PERSON_01] confirmed."

CHAT_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"events": [{"officer": "[PERSON_01]"}]}'},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 25, "completion_tokens": 9, "total_tokens": 34},
}


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Answers with a fixed response (or raises) and records the request."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def provider_for(
    transport: httpx.AsyncBaseTransport,
    *,
    base_url: str = BASE_URL,
    allow_insecure: bool = False,
) -> LocalProvider:
    return LocalProvider(
        base_url=base_url,
        api_key=API_KEY,
        model=MODEL,
        timeout=1.0,
        allow_insecure=allow_insecure,
        transport=transport,
    )


async def complete(provider: LocalProvider) -> Any:
    return await provider.complete(
        task_type="reply_parsing", language="en", snippet=SNIPPET
    )


# --- The wire contract ---


async def test_posts_chat_completions_with_the_key_in_the_auth_header_only() -> None:
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))
    await complete(provider_for(recording))

    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.url.path == "/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    import json

    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert "reply_parsing" in body["messages"][0]["content"]
    assert "'en'" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == SNIPPET
    # The key is present-only in the auth header: nowhere else on the wire.
    assert API_KEY not in request.content.decode()


async def test_maps_the_openai_answer_into_the_envelope() -> None:
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))
    result = await complete(provider_for(recording))

    envelope = result.envelope
    assert envelope["protocol_version"] == 1
    assert envelope["provider"] == "local"
    assert envelope["task_type"] == "reply_parsing"
    assert envelope["model_id"] == MODEL
    assert envelope["prompt_name"] == "reply_parsing"
    assert envelope["prompt_version"] == 1
    assert envelope["result"] == {"events": [{"officer": "[PERSON_01]"}]}
    assert envelope["usage"] == {"prompt_tokens": 25, "completion_tokens": 9}
    assert isinstance(envelope["completed_at"], str) and envelope["completed_at"]


async def test_a_json_scalar_result_passes_through() -> None:
    response = {
        **CHAT_RESPONSE,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '"yes"'},
                "finish_reason": "stop",
            }
        ],
    }
    result = await complete(
        provider_for(_RecordingTransport(httpx.Response(200, json=response)))
    )
    assert result.envelope["result"] == "yes"


# --- The failure taxonomy ---


async def test_an_unreachable_endpoint_raises_unavailable() -> None:
    provider = provider_for(
        _RecordingTransport(
            error=httpx.ConnectError("down", request=httpx.Request("POST", BASE_URL))
        )
    )

    with pytest.raises(InferenceChannelUnavailable) as failure:
        await complete(provider)

    assert str(failure.value) == "POST /chat/completions: ConnectError"
    assert API_KEY not in str(failure.value)
    assert SNIPPET not in str(failure.value)


async def test_a_refusal_carries_only_the_bounded_reason() -> None:
    recording = _RecordingTransport(
        httpx.Response(429, json={"detail": f"rate limited for {API_KEY}"})
    )

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(provider_for(recording))

    assert refusal.value.reason == "http_429"
    assert API_KEY not in refusal.value.reason


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        {},
        {"choices": []},
        {"choices": [{"message": {"content": 42}}]},
        {"choices": [{"message": {"content": "not json at all"}}]},
        {"choices": [{"message": {"content": "{}"}}], "usage": {}},
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": -1, "completion_tokens": 0},
        },
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1.5, "completion_tokens": 0},
        },
    ],
    ids=[
        "non-object",
        "empty",
        "no-choices",
        "non-string-content",
        "non-json-content",
        "missing-usage",
        "negative-tokens",
        "non-integer-tokens",
    ],
)
async def test_malformed_answers_are_refused(body: Any) -> None:
    recording = _RecordingTransport(httpx.Response(200, json=body))

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(provider_for(recording))

    assert refusal.value.reason == "malformed_response"


# --- Cleartext refusal and the development override ---


async def test_a_cleartext_base_url_is_refused_before_any_credential_is_offered() -> None:
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))

    with pytest.raises(InferenceChannelRefused) as refusal:
        provider_for(recording, base_url="http://llm.internal:8080")

    assert refusal.value.reason == "plaintext_not_allowed"
    assert recording.requests == []  # refused before the request existed


async def test_a_non_http_scheme_is_refused() -> None:
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))

    with pytest.raises(InferenceChannelRefused) as refusal:
        provider_for(recording, base_url="ftp://llm.internal")

    assert refusal.value.reason == "plaintext_not_allowed"
    assert recording.requests == []


async def test_the_development_override_allows_a_loopback_cleartext_endpoint() -> None:
    """The IMAP leg's precedent: a plaintext connection exists only behind an
    explicit development-only flag — a local test server, never a deployment
    with client data."""
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))
    provider = provider_for(
        recording, base_url="http://127.0.0.1:8080", allow_insecure=True
    )

    result = await complete(provider)

    assert result.envelope["result"] == {"events": [{"officer": "[PERSON_01]"}]}
    assert len(recording.requests) == 1


async def test_a_malformed_base_url_is_a_bounded_refusal_at_construction() -> None:
    """A startswith scheme check cannot see a URL the parser itself rejects
    (``https://[::1`` passes ``tls_allowed`` and dies at request time as a
    raw ``httpx.InvalidURL``). The constructor now parses first and maps the
    failure to the bounded ``malformed_base_url`` code — nothing sent, no
    raw httpx escape (the control-plane provider's precedent)."""
    recording = _RecordingTransport(httpx.Response(200, json=CHAT_RESPONSE))
    with pytest.raises(InferenceChannelRefused) as refused:
        LocalProvider(
            base_url="https://[::1",
            api_key=API_KEY,
            model="stub-model",
            timeout=1.0,
            transport=recording,
        )
    assert refused.value.code == "malformed_base_url"
    assert API_KEY not in refused.value.reason
    assert recording.requests == []
