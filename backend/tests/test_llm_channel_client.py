"""Wire tests for the inference channel client (M6.4; the Standard tier's LLM
transport).

The real client — real httpx, real JSON — against a small in-process fake
of the control plane's ``POST /api/v1/inference`` over
``httpx.ASGITransport``, mirroring ``test_control_plane_client.py``. What is
exercised is the wire contract (bearer token, request envelope keys) and the
failure taxonomy (unreachable, refused, malformed), plus the custody rule
that matters most at this seam: **the activation token and the snippet never
appear in any error string** — the levers stage refusals and outages whose
bodies echo the token and the snippet back precisely so the assertion has
something to catch. No test touches the network.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.adapters.llm.control_plane_channel import (
    InferenceChannel,
    InferenceChannelRefused,
    InferenceChannelUnavailable,
)
from tests.fake_control_plane import UnreachableTransport

BASE_URL = "http://control-plane.test"
TOKEN = "activation-token-issued-by-the-vendor"
SNIPPET = "company: 22334455\nreply_text: Director [PERSON_01] confirmed."

ENVELOPE: dict[str, Any] = {
    "protocol_version": 1,
    "provider": "stub-provider",
    "task_type": "reply_parsing",
    "model_id": "stub-model",
    "prompt_name": "reply_parser",
    "prompt_version": 1,
    "result": {"events": []},
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "completed_at": "2026-09-30T10:00:00+00:00",
}


class FakeInferencePlane:
    """The one route the channel speaks, with levers for its failure modes."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self.app.add_api_route("/api/v1/inference", self._inference, methods=["POST"])
        # What the deployment presented, for the wire-contract assertions.
        self.requests: list[dict[str, Any]] = []
        # Levers: the answer's status, body, and whether the body is even JSON.
        self.status = 200
        self.body: Any = ENVELOPE
        self.raw_body: str | None = None

    async def _inference(self, request: Request):
        self.requests.append(
            {
                "authorization": request.headers.get("authorization"),
                "body": await request.json() if self.raw_body is None else None,
            }
        )
        if self.raw_body is not None:
            return PlainTextResponse(self.raw_body, status_code=self.status)
        return JSONResponse(self.body, status_code=self.status)


def channel_for(fake: FakeInferencePlane, *, timeout: float = 1.0) -> InferenceChannel:
    """The real channel pointed at the fake service, in-process."""
    return InferenceChannel(
        base_url=BASE_URL,
        token=TOKEN,
        timeout=timeout,
        transport=httpx.ASGITransport(app=fake.app),
    )


async def complete(channel: InferenceChannel) -> Any:
    return await channel.complete(
        task_type="reply_parsing", language="en", snippet=SNIPPET
    )


# --- The wire contract ---


async def test_posts_the_envelope_with_the_bearer_token() -> None:
    fake = FakeInferencePlane()
    result = await complete(channel_for(fake))

    assert result.envelope == ENVELOPE
    assert len(fake.requests) == 1
    recorded = fake.requests[0]
    assert recorded["authorization"] == f"Bearer {TOKEN}"
    assert recorded["body"] == {
        "protocol_version": 1,
        "task_type": "reply_parsing",
        "language": "en",
        "snippet": SNIPPET,
    }


# --- The failure taxonomy ---


async def test_an_unreachable_host_raises_unavailable() -> None:
    channel = InferenceChannel(
        base_url=BASE_URL, token=TOKEN, timeout=1.0, transport=UnreachableTransport()
    )

    with pytest.raises(InferenceChannelUnavailable) as failure:
        await complete(channel)

    assert str(failure.value) == "POST /api/v1/inference: ConnectError"
    assert TOKEN not in str(failure.value)
    assert SNIPPET not in str(failure.value)


async def test_a_refusal_surfaces_the_vendors_bounded_reason() -> None:
    fake = FakeInferencePlane()
    fake.status = 403
    fake.body = {"detail": "entitlement_denied"}

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    assert refusal.value.reason == "entitlement_denied"
    assert refusal.value.code == "entitlement_denied"


async def test_a_refusal_without_a_detail_narrows_to_the_status() -> None:
    fake = FakeInferencePlane()
    fake.status = 502
    fake.body = {"detail": f"upstream echoed {TOKEN} and {SNIPPET}"}

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    # The vendor's free text is never carried — even though it echoed the
    # credential and the snippet back, the reason is the bounded code.
    assert refusal.value.reason == "http_502"
    assert TOKEN not in refusal.value.reason
    assert SNIPPET not in refusal.value.reason


async def test_an_object_refusal_narrows_to_code_reason() -> None:
    fake = FakeInferencePlane()
    fake.status = 403
    fake.body = {"detail": {"code": "tenant_suspended", "reason": "past_due"}}

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    assert refusal.value.reason == "tenant_suspended:past_due"
    assert refusal.value.code == "tenant_suspended"


async def test_an_object_refusal_with_free_text_narrows_to_the_status() -> None:
    """The ``reason`` half of the object form is vendor free text too — if
    it is not a machine-readable token it does not travel into the audit
    chain either."""
    fake = FakeInferencePlane()
    fake.status = 403
    fake.body = {
        "detail": {"code": "tenant_suspended", "reason": f"account {TOKEN} {SNIPPET}"}
    }

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    assert refusal.value.reason == "http_403"
    assert TOKEN not in refusal.value.reason
    assert SNIPPET not in refusal.value.reason


async def test_a_non_json_success_body_is_malformed() -> None:
    fake = FakeInferencePlane()
    fake.raw_body = f"not json: {SNIPPET}"

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    assert refusal.value.reason == "malformed_response"


async def test_a_non_object_success_body_is_malformed() -> None:
    fake = FakeInferencePlane()
    fake.body = [f"{TOKEN}", f"{SNIPPET}"]

    with pytest.raises(InferenceChannelRefused) as refusal:
        await complete(channel_for(fake))

    assert refusal.value.reason == "malformed_response"
    assert TOKEN not in refusal.value.reason
    assert SNIPPET not in refusal.value.reason


# --- Timeout plumbing (a timeout needs a real socket, so nothing here
# touches one — the control-plane client's own test pins the plumbing) ---


async def test_the_configured_timeout_reaches_the_request() -> None:
    recording = _RecordingTransport(httpx.Response(200, json=ENVELOPE))
    channel = InferenceChannel(base_url=BASE_URL, token=TOKEN, timeout=0.5, transport=recording)

    await complete(channel)

    assert recording.timeouts == [{"connect": 0.5, "read": 0.5, "write": 0.5, "pool": 0.5}]


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Answers with a fixed response and records the timeout each request carried."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.timeouts: list[dict[str, float]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        extensions = request.extensions.get("timeout")
        if isinstance(extensions, dict):
            self.timeouts.append(extensions)
        return self._response


# --- Malformed base URLs: the fail-closed seam (final-review fix wave) ---


async def test_a_malformed_base_url_is_a_bounded_refusal_at_construction() -> None:
    """``httpx.InvalidURL`` is not an ``HTTPError``, so the request path's
    taxonomy cannot catch it — the constructor parses the base URL and maps
    the failure to the bounded ``malformed_base_url`` code (mirroring the
    control plane's provider), with nothing sent and no raw httpx escape."""
    fake = FakeInferencePlane()
    with pytest.raises(InferenceChannelRefused) as refused:
        InferenceChannel(
            base_url="::::",
            token=TOKEN,
            timeout=1.0,
            transport=httpx.ASGITransport(app=fake.app),
        )
    assert refused.value.code == "malformed_base_url"
    assert TOKEN not in refused.value.reason
    assert fake.requests == []
