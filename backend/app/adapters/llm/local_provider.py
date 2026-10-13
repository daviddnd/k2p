"""The local LLM provider (M6.4): Enterprise Offline's transport.

A hand-rolled OpenAI-compatible ``POST {base}/chat/completions`` client over
httpx — no SDK, per the project's dependency rule (``httpx`` is the only
approved HTTP dependency; the shape is twenty lines of JSON). The
deployment's own endpoint answers, and its reply is mapped onto the same
response envelope the control-plane channel returns, so the gateway's
validate → restore → audit path is one code path for both custody options
(ADR-0014 §2: own key → local endpoint → none/degraded).

**TLS is the transport.** A cleartext ``http://`` base URL is refused by
name (``plaintext_not_allowed``) before the API key is offered — the IMAP
leg's rule and its development-only override: ``allow_insecure`` exists for
a local test server, never a deployment holding client data. A base URL the
URL parser itself rejects is refused as ``malformed_base_url`` at
construction (``tls_allowed`` parses; a startswith scheme check cannot see
it) — never a raw ``httpx.InvalidURL`` at request time. This module is
the backstop; the gateway's pre-flight fact-gathering applies the same rule
so a cleartext URL reads as ``ai_not_configured`` (a pre-flight refusal, no
audit, nothing transited) rather than ever reaching this constructor.

**Key custody (ADR-0014).** The API key travels only in the
``Authorization`` header of the one request this client makes. It is never
in a message body, an error string, a log line, or a stored row — error
messages carry the method, the path, and the exception's class name. The
refusal-reason narrowing is the channel's one mechanism, imported rather
than reimplemented.

**No persistence.** The snippet exists only in request-scoped memory; the
client holds the key and no connection, answers one call, and remembers
nothing.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.adapters.llm.control_plane_channel import (
    InferenceChannelRefused,
    InferenceChannelUnavailable,
    refusal_reason,
)

# The provider id the local path reports in the audit's closed Q1.5 list —
# the custody fact ("our own endpoint"), not a vendor brand.
LOCAL_PROVIDER = "local"


@dataclass(frozen=True)
class LocalResult:
    """The decoded response envelope of one call — structurally
    ``app.adapters.llm.port.TransportResult`` (a frozen dataclass with a
    single ``envelope`` field); see the channel's ``ChannelResult``."""

    envelope: Mapping[str, Any]


class LocalProvider:
    """One locally configured OpenAI-compatible endpoint, one call at a time.

    ``transport`` exists for tests (a recording fake, no socket);
    production constructs the provider from ``AICS_LLM_BASE_URL`` /
    ``AICS_LLM_API_KEY`` / ``AICS_LLM_MODEL`` / ``AICS_LLM_TIMEOUT_SECONDS``
    and no transport.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        allow_insecure: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not tls_allowed(base_url, allow_insecure=allow_insecure):
            raise InferenceChannelRefused("plaintext_not_allowed")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport

    async def complete(
        self, *, task_type: str, language: str, snippet: str
    ) -> LocalResult:
        """POST one snippet; return the mapped envelope or raise typed."""
        answer = await self._request(
            task_type=task_type, language=language, snippet=snippet
        )
        return LocalResult(envelope=_to_envelope(answer, model=self._model, task_type=task_type))

    async def _request(
        self, *, task_type: str, language: str, snippet: str
    ) -> Mapping[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, transport=self._transport
        ) as client:
            try:
                response = await client.post(
                    "/chat/completions",
                    json={
                        "model": self._model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    f"K2P inference task '{task_type}' "
                                    f"(language: '{language}'). "
                                    "Answer with one JSON value."
                                ),
                            },
                            {"role": "user", "content": snippet},
                        ],
                    },
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.InvalidURL as exc:
                # Defense in depth: the constructor parses the base URL, but
                # a URL that fails at request-build time maps to the same
                # bounded configuration refusal — nothing was sent, and the
                # message carries no key and no snippet.
                raise InferenceChannelRefused("malformed_base_url") from exc
            except httpx.HTTPError as exc:
                # Transport-level failure: outage, DNS, TLS, timeout. The
                # message carries no key and no snippet — neither the URL
                # nor the headers are included.
                raise InferenceChannelUnavailable(
                    f"POST /chat/completions: {exc.__class__.__name__}"
                ) from exc
        if response.status_code >= 400:
            raise InferenceChannelRefused(refusal_reason(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise InferenceChannelRefused("malformed_response") from exc
        if not isinstance(body, dict):
            raise InferenceChannelRefused("malformed_response")
        return body


def tls_allowed(base_url: str, *, allow_insecure: bool) -> bool:
    """HTTPS always; HTTP only behind the development-only override.

    A port is not a security decision (the IMAP leg's rule): what is refused
    is the scheme, and any scheme that is not plain HTTP or verified HTTPS
    has no defined transport here either — the same refusal answers both.
    The URL is parsed, not prefix-checked, because a base URL the parser
    itself rejects (``https://[::1``) would otherwise pass the scheme check
    and die at request time as a raw ``httpx.InvalidURL`` — it is a
    configuration refusal with the bounded ``malformed_base_url`` code (the
    control-plane provider's precedent), raised here at construction.
    Named public because the gateway's pre-flight applies the same rule when
    building its call facts, so a cleartext URL reads as ``ai_not_configured``
    there instead of ever reaching this constructor.
    """
    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        raise InferenceChannelRefused("malformed_base_url") from exc
    if parsed.scheme == "https":
        return True
    return allow_insecure and parsed.scheme == "http"


def _to_envelope(answer: Mapping[str, Any], *, model: str, task_type: str) -> dict[str, Any]:
    """Map an OpenAI-compatible answer onto the response envelope.

    The result message's content must be one JSON value — the same
    JSON-ness the domain envelope rule requires of ``result``. Usage is the
    closed Q1.5 pair; ``total_tokens`` and any other provider extensions are
    dropped, not forwarded. An offline deployment has no prompt registry:
    the task type names its local prompt and the version is 1, stated here
    because the envelope shape requires both fields.
    """
    choices = answer.get("choices")
    content: Any = None
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
    if not isinstance(content, str):
        raise InferenceChannelRefused("malformed_response")
    try:
        result = json.loads(content)
    except ValueError as exc:
        raise InferenceChannelRefused("malformed_response") from exc

    usage = answer.get("usage")
    if not isinstance(usage, dict):
        raise InferenceChannelRefused("malformed_response")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if (
        not isinstance(prompt_tokens, int)
        or isinstance(prompt_tokens, bool)
        or prompt_tokens < 0
        or not isinstance(completion_tokens, int)
        or isinstance(completion_tokens, bool)
        or completion_tokens < 0
    ):
        raise InferenceChannelRefused("malformed_response")

    return {
        "protocol_version": 1,
        "provider": LOCAL_PROVIDER,
        "task_type": task_type,
        "model_id": model,
        "prompt_name": task_type,
        "prompt_version": 1,
        "result": result,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "completed_at": datetime.now(UTC).isoformat(),
    }
