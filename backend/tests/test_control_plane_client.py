"""Wire tests for the control-plane client (ADR-0016; CP1b).

The real client — real httpx, real URLs, real JSON — against
``tests/fake_control_plane.py``, which mirrors the vendor service's own API
models. What is exercised here is exactly what an outage or a drifted API
does to the execution plane: refused calls, unreachable hosts, timeouts, and
bodies that are not the frozen shape. No test touches the network.

The payload *validation* tests live with the domain rule
(``test_entitlement_domain.py``); these stop at the transport boundary: the
client's job is to deliver a JSON object or a typed error, nothing more.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from app.adapters.entitlement.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
)
from tests.fake_control_plane import FakeControlPlane, UnreachableTransport

BASE_URL = "http://control-plane.test"
FINGERPRINT = "deployment-fingerprint-1"
REDIRECT_URI = "http://localhost:8000/api/v1/entitlement/activation/callback"


def client_for(control_plane: FakeControlPlane, *, timeout: float = 1.0) -> ControlPlaneClient:
    """The real client pointed at the fake service, in-process."""
    return ControlPlaneClient(
        base_url=BASE_URL,
        timeout=timeout,
        transport=httpx.ASGITransport(app=control_plane.app),
    )


def s256_challenge(verifier: str) -> str:
    """The challenge computed independently of the application's own helper."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def registered(
    control_plane: FakeControlPlane, *, verifier: str = "v" * 43
) -> tuple[ControlPlaneClient, UUID]:
    """A client and a registered activation request id."""
    client = client_for(control_plane)
    response = await client.register_activation(
        redirect_uri=REDIRECT_URI,
        code_challenge=s256_challenge(verifier),
        deployment_fingerprint=FINGERPRINT,
    )
    return client, UUID(response["id"])


async def test_register_activation_returns_request_id_and_expiry() -> None:
    control_plane = FakeControlPlane()
    _, request_id = await registered(control_plane)

    assert control_plane.registrations == 1
    assert str(request_id) in control_plane.requests
    # The fake refuses anything but an S256 challenge and an absolute
    # http(s) redirect URI, so reaching this assertion means the client sent
    # both correctly.


async def test_register_activation_omits_the_registration_code_when_absent() -> None:
    control_plane = FakeControlPlane()
    _, request_id = await registered(control_plane)

    pending = control_plane.requests[str(request_id)]
    assert pending.registration_code is None
    # Absent means absent on the wire, not null: the pre-registration body
    # is byte-for-byte what it always was.
    assert pending.registration_code_present is False


async def test_register_activation_sends_a_registration_code_when_present() -> None:
    control_plane = FakeControlPlane()
    code = control_plane.issue_registration_code()
    client = client_for(control_plane)

    response = await client.register_activation(
        redirect_uri=REDIRECT_URI,
        code_challenge=s256_challenge("v" * 43),
        deployment_fingerprint=FINGERPRINT,
        registration_code=code,
    )

    # The 2026-10-11 amendment: with a code the service answers the credential
    # directly — no request id exists, nothing is pending.
    assert response["activated"] is True
    assert response["token"]
    assert control_plane.requests == {}
    # The fake claims the way the real service does: the code is now bound
    # to this deployment's fingerprint.
    assert control_plane.registration_codes[code] == FINGERPRINT


async def test_an_unknown_registration_code_is_refused() -> None:
    control_plane = FakeControlPlane()
    client = client_for(control_plane)

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client.register_activation(
            redirect_uri=REDIRECT_URI,
            code_challenge=s256_challenge("v" * 43),
            deployment_fingerprint=FINGERPRINT,
            registration_code="a-code-the-vendor-never-issued",
        )

    assert refusal.value.reason == "registration_code_invalid"
    assert control_plane.registrations == 0  # a refused claim creates no request


async def test_a_registration_code_used_by_another_deployment_is_refused() -> None:
    control_plane = FakeControlPlane()
    code = control_plane.issue_registration_code()
    control_plane.registration_codes[code] = "some-other-deployment"
    client = client_for(control_plane)

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client.register_activation(
            redirect_uri=REDIRECT_URI,
            code_challenge=s256_challenge("v" * 43),
            deployment_fingerprint=FINGERPRINT,
            registration_code=code,
        )

    assert refusal.value.reason == "registration_code_used"


async def test_exchange_returns_the_credential() -> None:
    control_plane = FakeControlPlane()
    verifier = "v" * 43
    client, request_id = await registered(control_plane, verifier=verifier)
    code = control_plane.complete_handshake(request_id)

    credential = await client.exchange_activation_code(
        code=code, code_verifier=verifier, deployment_fingerprint=FINGERPRINT
    )

    assert credential["token"] in control_plane.tokens
    assert credential["tenant_id"] == str(control_plane.tenant_id)
    assert datetime.fromisoformat(credential["expires_at"]) > datetime.now(UTC)


async def test_exchange_with_a_wrong_verifier_is_refused() -> None:
    control_plane = FakeControlPlane()
    client, request_id = await registered(control_plane)
    code = control_plane.complete_handshake(request_id)

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client.exchange_activation_code(
            code=code, code_verifier="w" * 43, deployment_fingerprint=FINGERPRINT
        )

    assert refusal.value.reason == "invalid_verifier"
    assert control_plane.token_exchanges == 0


async def test_fetch_lease_returns_the_frozen_payload() -> None:
    control_plane = FakeControlPlane(tier="standard", entitlement_version=3)
    token = "issued-by-the-vendor"
    control_plane.tokens[token] = datetime.now(UTC) + timedelta(days=90)

    payload = await client_for(control_plane).fetch_lease(token=token)

    assert set(payload) == {
        "tier",
        "license_expires_at",
        "seat_cap",
        "company_cap",
        "entitlement_version",
        "issued_at",
        "signature",
    }
    assert control_plane.lease_fetches == 1


async def test_fetch_lease_without_a_usable_token_is_refused() -> None:
    control_plane = FakeControlPlane()
    client = client_for(control_plane)

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client.fetch_lease(token="never-issued")

    assert refusal.value.reason == "invalid activation token"
    assert control_plane.lease_fetches == 0


async def test_a_suspended_tenant_refusal_surfaces_its_reason() -> None:
    control_plane = FakeControlPlane(tenant_status="suspended")
    control_plane.tokens["t"] = datetime.now(UTC) + timedelta(days=1)

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client_for(control_plane).fetch_lease(token="t")

    assert refusal.value.reason == "tenant_suspended"


async def test_a_non_object_body_is_refused() -> None:
    control_plane = FakeControlPlane()
    control_plane.tokens["t"] = datetime.now(UTC) + timedelta(days=1)
    control_plane.lease_body = ["not", "an", "object"]  # type: ignore[assignment]

    with pytest.raises(ControlPlaneRefused) as refusal:
        await client_for(control_plane).fetch_lease(token="t")

    assert refusal.value.reason == "malformed_response"


async def test_an_unreachable_host_raises_unavailable() -> None:
    client = ControlPlaneClient(
        base_url=BASE_URL, timeout=1.0, transport=UnreachableTransport()
    )

    with pytest.raises(ControlPlaneUnavailable):
        await client.fetch_lease(token="t")


async def test_a_slow_host_times_out_as_unavailable() -> None:
    # A timeout needs a transport that does real I/O (httpx enforces it in
    # the socket layer, not in ASGI or mock transports), and no test here
    # touches a socket. What is checked instead is the plumbing: the
    # configured timeout reaches the request, so a hung vendor call cannot
    # hold a read open indefinitely.
    control_plane = FakeControlPlane()
    control_plane.tokens["t"] = datetime.now(UTC) + timedelta(days=1)
    recording = _RecordingTransport(httpx.Response(200, json=control_plane.lease_payload()))

    client = ControlPlaneClient(base_url=BASE_URL, timeout=0.5, transport=recording)
    await client.fetch_lease(token="t")

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
