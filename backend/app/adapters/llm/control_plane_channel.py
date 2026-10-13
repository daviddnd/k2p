"""The inference channel: the execution plane's one call to the vendor
control plane's inference endpoint (M6.4; ADR-0014's custody split, the
Standard tier's transport).

One call, one POST: ``POST {base}/api/v1/inference`` with the deployment's
activation token as the bearer credential and the composed snippet as the
request envelope's ``snippet`` field (protocol version 1 — the one integer
each way, contract §5.2). The response body comes back as raw JSON —
validating the frozen envelope shape is a domain rule
(``app.domain.ai_gateway.response_envelope_violations``), not a transport
concern — and every failure is one of two typed errors, so the gateway
decides what a refusal means without parsing strings.

This module deliberately imports nothing from ``app.*``. It is the wire, and
the integration test that drives it against the real vendor service loads it
standalone beside that service: both deployables name their package ``app``
and cannot be imported into one interpreter (the same constraint
``app.adapters.entitlement.control_plane_client`` answers, whose shape this
client mirrors: base_url/timeout/transport constructor, typed errors, no
retries).

**Custody.** The bearer token is the deployment's credential (ADR-0014): it
travels only in the ``Authorization`` header and never appears in an error
string, a log line, or a stored row — error messages carry the method, the
path, and the exception's class name, nothing the vendor echoed. The snippet
is likewise never logged or embedded in an error; the refusal reason is the
vendor's bounded ``detail`` or the status code, never the body.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

# The refusal reason is written to the audit chain (bounded ``details.error``
# on a failed call); keep it short enough and free of anything the vendor
# echoed — the credential and the snippet ride the same wire.
_MAX_REASON_LENGTH = 120

# Only machine-readable reasons travel into the audit chain: snake_case
# tokens, optionally ``code:reason`` where both sides are tokens. A vendor's
# free-text ``detail`` can echo the bearer token or the snippet back (it just
# handled both), and the audit chain is append-only — so free text narrows
# to the status code instead of being carried. The lease client can afford
# to carry free text (its refusals name no client data); this channel cannot.
_SAFE_REASON = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*(?::[a-z0-9]+(?:_[a-z0-9]+)*)?")


class InferenceChannelUnavailable(Exception):
    """The vendor service could not be reached or did not answer in time.

    The message is ``{method} {path}: {exception class}`` — a diagnosis with
    no credential, no snippet, no vendor free text.
    """


class InferenceChannelRefused(Exception):
    """The vendor service answered with a refusal, or with no usable body.

    ``reason`` is the vendor's machine-readable detail when it sent one
    (``entitlement_denied``, or the object form narrowed to ``code:reason``),
    else a code derived from the status — never free text, which can echo
    the credential or the snippet back. Configuration refusals raised before
    anything is sent carry the bounded deployment-side codes
    (``malformed_base_url``, from the constructor's base-URL parse, and
    ``malformed_response``). ``code`` is the leading token alone, for
    callers that branch on the kind of refusal rather than report it.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.code = reason.split(":", 1)[0]
        super().__init__(reason)


@dataclass(frozen=True)
class ChannelResult:
    """The decoded response envelope of one call — structurally
    ``app.adapters.llm.port.TransportResult`` (a frozen dataclass with a
    single ``envelope`` field).

    The port's class is not imported here so this module stays
    standalone-loadable for the cross-deployable driver; duck typing is the
    whole point of the Protocol seam.
    """

    envelope: Mapping[str, Any]


class InferenceChannel:
    """The control plane's inference endpoint over HTTP.

    ``transport`` exists for tests (``httpx.ASGITransport`` against a fake
    or the real service in-process); production constructs the channel with
    the deployment's ``AICS_CP_BASE_URL``, timeout, and activation token,
    and no transport. One channel per call: the client holds the credential
    and no connection, exactly like the lease client.

    Never retried here: a failed inference call is the gateway's business —
    it books the audit entry (the snippet left) and raises; a hidden second
    attempt would double the leak surface the minimization bar exists to
    bound.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Parse the base URL at construction, fail closed: ``httpx.InvalidURL``
        # is not an ``HTTPError``, so the request path's taxonomy cannot catch
        # it — a malformed base URL is a configuration refusal with the
        # bounded ``malformed_base_url`` code (the control-plane provider's
        # precedent), never a raw httpx escape at call time.
        try:
            httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise InferenceChannelRefused("malformed_base_url") from exc
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    async def complete(
        self, *, task_type: str, language: str, snippet: str
    ) -> ChannelResult:
        """POST one snippet; return the decoded envelope or raise typed."""
        body = await self._request(
            "POST",
            "/api/v1/inference",
            json_body={
                "protocol_version": 1,
                "task_type": task_type,
                "language": language,
                "snippet": snippet,
            },
        )
        return ChannelResult(envelope=body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.InvalidURL as exc:
            # Defense in depth: the constructor parses the base URL, but a
            # URL that fails at request-build time maps to the same bounded
            # configuration refusal — nothing was sent.
            raise InferenceChannelRefused("malformed_base_url") from exc
        except httpx.HTTPError as exc:
            # Transport-level failure: outage, DNS, TLS, timeout. The
            # message carries no credential — neither the URL nor the
            # headers are included.
            raise InferenceChannelUnavailable(
                f"{method} {path}: {exc.__class__.__name__}"
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


def refusal_reason(response: httpx.Response) -> str:
    """A bounded reason for a non-2xx answer: the vendor's machine-readable
    code, or the status.

    The vendor's free text can echo the credential or the snippet back (it
    just handled both), and the reason is written into the append-only audit
    chain — so anything that is not a safe token shape narrows to
    ``http_<status>`` rather than being carried.
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
    detail = detail[:_MAX_REASON_LENGTH]
    if _SAFE_REASON.fullmatch(detail) is None:
        detail = f"http_{response.status_code}"
    return detail
