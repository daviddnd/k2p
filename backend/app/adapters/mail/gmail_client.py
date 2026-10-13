"""Gmail's token endpoint and API, as the execution plane calls them.

Four kinds of call, one per step of the mailbox's life (ADR-0005): the consent
exchange (authorization code + PKCE verifier → tokens), the refresh that keeps
a connected mailbox usable after the access token expires, the reads the probe
and the ingestion cycle need — the profile, the newest message ids, one
message's metadata, and one message's raw bytes — and the **send** the release
path uses, with the Sent lookup that makes a repeated attempt safe. Every
response body comes back as raw JSON (validating the shape is a domain rule,
``app.domain.mail.parse_*``, not a transport concern) and every failure is
one of two read errors — ``MailProviderUnavailable`` and
``MailProviderRefused``, which the IMAP leg raises too and which live with the
send failure in ``app.adapters.mail.errors`` — so callers decide what a
refusal means without parsing strings.

Three rules matter here and are easy to get wrong:

- **The metadata read asks for metadata only.** ``format=metadata`` plus the
  ``metadataHeaders`` names is what makes it impossible for the probe to
  receive a message body. The ingestion cycle reads the envelope that way for
  every message and asks for the ``raw`` bytes only when something needs the
  words a body carries: an unregistered sender's message is never read at all
  — its authorisation is decided from the envelope (ADR-0019 §1) — a
  single-company message is read once it has passed the gate, and a
  multi-company message is read to decide which company it is about (and, if
  the decision refuses it, never stored — ``app.application.mail_ingest``
  states the rule).
- **A send is never a plain ``_request``.** Its failures must answer the one
  question a read's failures do not: whether the message left. A transport
  error around a send is *unconfirmed* (the provider may have taken it before
  the connection died), a refusal is read by its status — a rate limit or a
  server error may succeed later, anything else will not — and both travel as
  ``MailDeliveryFailed`` with the domain's ``DeliveryFailure``
  (``app.adapters.mail.errors`` states why the classification is here).
- **Refusals never echo the provider's body.** Google sends
  ``error_description``/``message`` strings beside the code; a body can carry
  a request's credentials, so only the code travels (`invalid_grant`,
  `UNAUTHENTICATED`), bounded and never the description.

Never retried here: a failed refresh is the probe's business (it reports the
typed error) and a failed consent exchange is the administrator's (they run
the flow again). Neither wants a hidden second attempt. A failed *send* is the
sender's business: this module reports what happened and the retry policy
lives in ``app.domain.mail_send``.
"""

import base64
from typing import Any

import httpx

from app.adapters.mail.errors import (
    MailDeliveryFailed,
    MailProviderRefused,
    MailProviderUnavailable,
)
from app.domain.mail import parse_messages_list
from app.domain.mail_send import DeliveryFailure

# Google's endpoints — fixed, not configuration: there is exactly one Google.
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE_URL = "https://gmail.googleapis.com"
PROFILE_PATH = "/gmail/v1/users/me/profile"
MESSAGES_PATH = "/gmail/v1/users/me/messages"
SEND_PATH = "/gmail/v1/users/me/messages/send"

# What the probe reads, and only that. The addressing and thread headers are
# part of the envelope the attribution decision (ADR-0019 §1) is made from: a
# message whose authorisation is not settled is decided on these, and its body
# is never requested.
MESSAGE_FORMAT = "metadata"
MESSAGE_HEADERS = (
    "Subject",
    "From",
    "Date",
    "To",
    "Cc",
    "In-Reply-To",
    "References",
    "Message-ID",
    "Auto-Submitted",
    "Return-Path",
)

# The ingestion cycle's read of the message itself. ``raw`` is the whole
# RFC 822 message exactly as the mailbox holds it — the object the records
# registry stores, not a re-rendering of it.
RAW_MESSAGE_FORMAT = "raw"

# The reconciliation lookup. Gmail indexes ``Message-ID`` as
# ``rfc822msgid:<id>``; ``in:sent`` scopes it to what this mailbox sent, which
# is where the message this deployment is about to repeat would be. A handful
# of results is plenty: the ids are unique, and the sender only needs to know
# whether the list is empty.
SENT_QUERY_TEMPLATE = "in:sent rfc822msgid:{message_id}"
SENT_SEARCH_MAX_RESULTS = 5

# Refusal reasons are recorded and rendered; keep them short and free of
# anything the provider echoed.
_MAX_REASON_LENGTH = 120


class GmailClient:
    """Gmail over HTTP, with no credential held between calls.

    ``transport`` exists for tests (``httpx.ASGITransport`` against a fake);
    production constructs the client with the deployment's mail timeout and
    no transport.
    """

    def __init__(
        self, *, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """POST /token — the browser's one-time code plus the PKCE verifier.

        The code alone is worthless: Google consumes it on first redemption,
        and it is redeemable only with the verifier that never left this
        deployment.
        """
        return await self._request(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )

    async def refresh_access_token(
        self, *, refresh_token: str, client_id: str, client_secret: str
    ) -> dict[str, Any]:
        """POST /token — a new access token for the grant the consent minted.

        ``invalid_grant`` here is the durable failure: the grant was revoked,
        expired, or belongs to another client, and the mailbox has to be
        connected again.
        """
        return await self._request(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

    async def fetch_profile(self, *, access_token: str) -> dict[str, Any]:
        """GET users/me/profile — the mailbox's own address and its size."""
        return await self._request(
            "GET", f"{GMAIL_API_BASE_URL}{PROFILE_PATH}", access_token=access_token
        )

    async def list_message_ids(
        self, *, access_token: str, max_results: int, query: str | None = None
    ) -> dict[str, Any]:
        """GET users/me/messages — message ids, newest first.

        ``query`` is Gmail's ``q`` search expression; the ingestion cycle uses
        it to ask only for messages newer than its cursor. ``max_results``
        bounds the answer, which is why the cycle advances its cursor over
        what it actually examined rather than over everything that arrived.
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if query is not None:
            params["q"] = query
        return await self._request(
            "GET",
            f"{GMAIL_API_BASE_URL}{MESSAGES_PATH}",
            access_token=access_token,
            params=params,
        )

    async def fetch_message_metadata(self, *, access_token: str, message_id: str) -> dict[str, Any]:
        """GET users/me/messages/{id} in metadata format — never a body."""
        return await self._request(
            "GET",
            f"{GMAIL_API_BASE_URL}{MESSAGES_PATH}/{message_id}",
            access_token=access_token,
            params={
                "format": MESSAGE_FORMAT,
                "metadataHeaders": list(MESSAGE_HEADERS),
            },
        )

    async def fetch_message_raw(self, *, access_token: str, message_id: str) -> dict[str, Any]:
        """GET users/me/messages/{id} in raw format — the whole message.

        The ingestion cycle and the §2.5 review surface's retry reach this —
        the retry runs the cycle's own per-message order. A single-company
        message is read once it has passed the gate; a multi-company message
        is read *before* the gate decides, to have the words that decide which
        company it is about. The bytes are scanned and stored only once a
        message is accepted, never logged and never returned by an API route.
        """
        return await self._request(
            "GET",
            f"{GMAIL_API_BASE_URL}{MESSAGES_PATH}/{message_id}",
            access_token=access_token,
            params={"format": RAW_MESSAGE_FORMAT},
        )

    async def send_message(self, *, access_token: str, raw: bytes) -> dict[str, Any]:
        """POST users/me/messages/send — the composed RFC 5322 message.

        ``raw`` is the stored draft's own bytes, base64url-encoded exactly as
        the API takes it, so what the provider stamps and files is what the
        records registry holds. The answer carries the provider's message id
        and the thread it was filed in, which is what the outbound
        ``Communication`` row records.

        Raises ``MailDeliveryFailed`` for every failure, classified as the
        module docstring states: a transport error is *unconfirmed* (Google
        may have taken the message before the connection died) and a refusal
        is read by its status.
        """
        try:
            return await self._request(
                "POST",
                f"{GMAIL_API_BASE_URL}{SEND_PATH}",
                access_token=access_token,
                json_body={"raw": _base64url(raw)},
            )
        except MailProviderUnavailable as exc:
            raise MailDeliveryFailed(
                failure=DeliveryFailure.UNCONFIRMED, reason="transport_error"
            ) from exc
        except MailProviderRefused as exc:
            raise MailDeliveryFailed(
                failure=_failure_for_status(exc.status), reason=exc.reason
            ) from exc

    async def find_sent_message_id(self, *, access_token: str, message_id: str) -> str | None:
        """The provider id of the message this deployment already sent, if any.

        The idempotency anchor is the draft's RFC 5322 ``Message-ID`` (the M3
        generator writes one in the mailbox's own domain), and Gmail indexes it
        as ``rfc822msgid:`` — **without** the header's angle brackets, which is
        why they are stripped here. The lookup is what makes a repeated attempt
        safe: the sender asks *before* sending, so a crash between the
        provider's accept and the row's record cannot produce a second copy of
        a client's letter. The ``gmail.readonly`` scope covers it; no new scope
        is asked for.
        """
        raw = await self.list_message_ids(
            access_token=access_token,
            max_results=SENT_SEARCH_MAX_RESULTS,
            query=SENT_QUERY_TEMPLATE.format(message_id=_bare_message_id(message_id)),
        )
        ids = parse_messages_list(raw)
        return ids[0] if ids else None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        headers = (
            {"Authorization": f"Bearer {access_token}"} if access_token is not None else None
        )
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.request(
                    method, url, data=form, json=json_body, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                # Transport-level failure: outage, DNS, TLS, timeout. The
                # message carries no credential — neither the URL's query nor
                # the headers nor the form are included.
                raise MailProviderUnavailable(
                    f"{method} {_path(url)}: {exc.__class__.__name__}"
                ) from exc
        if response.status_code >= 400:
            raise MailProviderRefused(
                _refusal_reason(response), status=response.status_code
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MailProviderRefused("malformed_response") from exc
        if not isinstance(body, dict):
            raise MailProviderRefused("malformed_response")
        return body


def _bare_message_id(message_id: str) -> str:
    """The ``Message-ID`` as Gmail's search wants it: without its angle brackets."""
    return message_id.strip().strip("<>")


def _base64url(raw: bytes) -> str:
    """The URL-safe, unpadded base64 the Gmail API carries a raw message in."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _failure_for_status(status: int | None) -> DeliveryFailure:
    """A refused send, as the retry policy reads it.

    A rate limit or a server-side error (429, 5xx) can succeed later, so it is
    *transient*; anything else the API answers for a send — a malformed
    request, an unauthenticated call, a scope that was never granted, a
    message id that cannot exist — will answer the same way next time, so it
    is *permanent* and ends the row rather than filling the chain with the
    same refusal. ``None`` (no status: the refusal had no readable body) is
    permanent for the same reason a 4xx is.
    """
    if status == 429 or (status is not None and 500 <= status < 600):
        return DeliveryFailure.TRANSIENT
    return DeliveryFailure.PERMANENT


def _refusal_reason(response: httpx.Response) -> str:
    """A short reason for a non-2xx answer: Google's error code, or its status.

    Google sends two shapes — the token endpoint's flat ``{"error": "…"}``
    and the API's ``{"error": {"status": "…"}}`` — and both narrow to a code;
    the accompanying ``error_description``/``message`` is deliberately
    dropped, because a body is not a safe thing to echo.
    """
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if isinstance(error, dict):
        error = error.get("status") or error.get("code")
    if isinstance(error, int):
        error = f"http_{error}"
    if not isinstance(error, str) or not error:
        error = f"http_{response.status_code}"
    return error[:_MAX_REASON_LENGTH]


def _path(url: str) -> str:
    """The URL without its query, for a failure message."""
    return url.split("?", 1)[0]
