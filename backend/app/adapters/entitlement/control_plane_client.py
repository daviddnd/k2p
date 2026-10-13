"""The vendor control plane's HTTP API, as the execution plane calls it.

Three calls, one per step of ADR-0016: register an activation request,
exchange the browser's one-time code (with the PKCE verifier) for this
deployment's credential, and read the entitlement lease. Every response body
comes back as raw JSON — validating the frozen payload shape is a domain
rule (``app.domain.entitlement.parse_lease_payload``), not a transport
concern — and every failure is one of two typed errors, so callers decide
what a refusal means without parsing strings.

This module deliberately imports nothing from ``app.*``. It is the wire, and
the integration test that drives it against the real vendor service loads it
standalone beside that service: both deployables name their package ``app``
and cannot be imported into one interpreter (see
``backend/tests/control_plane_driver.py``). Keeping the client free of
package-internal imports is what keeps that test possible.

Never retried here: a failed refresh is the read path's business (it keeps
the cached lease and records the reason), and a failed activation is the
administrator's (they retry). Neither wants a hidden second attempt.
"""

from typing import Any

import httpx

# The refusal reason is stored in the lease cache's ``last_error``; keep it
# short enough for the column and free of anything the vendor echoed.
_MAX_REASON_LENGTH = 120


class ControlPlaneUnavailable(Exception):
    """The vendor service could not be reached or did not answer in time."""


class ControlPlaneRefused(Exception):
    """The vendor service answered with a refusal, or with no usable body.

    ``reason`` is the vendor's ``detail`` when it sent one, else a code
    derived from the status — never the body itself. A vendor that sends the
    object form (``{"code", "reason"}``, as the pack routes do) narrows to
    ``code:reason``; ``code`` is that leading code alone, for callers that
    branch on the kind of refusal rather than report it.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.code = reason.split(":", 1)[0]
        super().__init__(reason)


class ControlPlaneClient:
    """The control plane's API over HTTP.

    ``transport`` exists for tests (``httpx.ASGITransport`` against a fake or
    the real service in-process); production constructs the client with the
    deployment's ``AICS_CP_BASE_URL`` and timeout and no transport.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def register_activation(
        self,
        *,
        redirect_uri: str,
        code_challenge: str,
        deployment_fingerprint: str,
        registration_code: str | None = None,
    ) -> dict[str, Any]:
        """POST /activation/requests — this deployment's activation intent.

        ``registration_code`` is the claim code a self-service signup
        delivered; presented, it travels with the request so the vendor can
        bind the registration to this deployment's fingerprint. Omitted, it
        is omitted from the body — the request is byte-for-byte what it was
        before claim codes existed.
        """
        body: dict[str, Any] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "deployment_fingerprint": deployment_fingerprint,
        }
        if registration_code is not None:
            body["registration_code"] = registration_code
        return await self._request("POST", "/api/v1/activation/requests", json_body=body)

    async def exchange_activation_code(
        self,
        *,
        code: str,
        code_verifier: str,
        deployment_fingerprint: str,
    ) -> dict[str, Any]:
        """POST /activation/token — the one-time code plus the PKCE verifier.

        The code alone is worthless: it is consumed on first redemption and
        only redeemable with the verifier that never left this deployment.
        """
        return await self._request(
            "POST",
            "/api/v1/activation/token",
            json_body={
                "code": code,
                "code_verifier": code_verifier,
                "deployment_fingerprint": deployment_fingerprint,
            },
        )

    async def fetch_lease(self, *, token: str) -> dict[str, Any]:
        """GET /entitlement — one lease refresh, as the frozen payload body."""
        return await self._request(
            "GET",
            "/api/v1/entitlement",
            headers={"Authorization": f"Bearer {token}"},
        )

    async def fetch_commands(self, *, token: str, envelope_version: int) -> list[dict[str, Any]]:
        """GET /commands — one poll, and the declaration of the version we speak.

        The declared version is what lets the vendor decide whether it may
        enqueue at all (contract §5.2's version policy); the returned
        envelopes are the ones this deployment has not answered yet, so a
        second poll before a report returns the same commands again.
        """
        body = await self._request(
            "GET",
            "/api/v1/commands",
            params={"envelope_version": envelope_version},
            headers={"Authorization": f"Bearer {token}"},
        )
        commands = body.get("commands")
        if not isinstance(commands, list):
            raise ControlPlaneRefused("malformed_response")
        return [item for item in commands if isinstance(item, dict)]

    async def fetch_pack(
        self, *, token: str, pack_type: str, version: int | None = None
    ) -> dict[str, Any]:
        """GET /packs/{pack_type} — one licensed pack release.

        The latest release when ``version`` is omitted. A type the vendor has
        never published answers 404, which the caller reports as
        ``not_published`` rather than as a failure: an empty catalog is a
        normal state, not an outage.
        """
        return await self._request(
            "GET",
            f"/api/v1/packs/{pack_type}",
            params=None if version is None else {"version": version},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def submit_command_result(
        self, *, token: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /commands/results — one outcome, idempotent by command id."""
        return await self._request(
            "POST",
            "/api/v1/commands/results",
            json_body=result,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, transport=self._transport
        ) as client:
            try:
                response = await client.request(
                    method, path, json=json_body, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                # Transport-level failure: outage, DNS, TLS, timeout. The
                # message carries no credential — neither the URL nor the
                # headers are included.
                raise ControlPlaneUnavailable(f"{method} {path}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise ControlPlaneRefused(_refusal_reason(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise ControlPlaneRefused("malformed_response") from exc
        if not isinstance(body, dict):
            raise ControlPlaneRefused("malformed_response")
        return body


def _refusal_reason(response: httpx.Response) -> str:
    """A short reason for a non-2xx answer: the vendor's ``detail``, or its status.

    The vendor sends a plain string for the lease and the command channel, and
    a ``{"code", "reason"}`` object for the pack routes (a refusal that a
    deployment acts on has a machine-readable reason). Both narrow to one
    short code; anything else falls back to the status.
    """
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        code = detail.get("code")
        reason = detail.get("reason")
        if isinstance(code, str) and code:
            detail = f"{code}:{reason}" if isinstance(reason, str) and reason else code
        else:
            detail = None
    if not isinstance(detail, str) or not detail:
        detail = f"http_{response.status_code}"
    return detail[:_MAX_REASON_LENGTH]
