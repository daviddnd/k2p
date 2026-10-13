"""A faithful stand-in for Google's token endpoint and the Gmail API.

The shapes here are copied from Google's OAuth 2.0 documentation and the
Gmail API reference: the token endpoint's form-encoded request and its
``access_token``/``expires_in``/``refresh_token``/``scope`` response (and the
flat ``{"error": "invalid_grant"}`` refusal), the ``users.messages.list`` and
``users.getProfile`` bodies, ``users.messages.get`` for both formats the
product uses — ``metadata``, whose ``payload.headers`` array is all the probe
reads, and ``raw``, which carries the whole RFC 822 message base64url-encoded
— and ``users.messages.send``, whose request carries the message the same way
and whose answer is the provider id and thread the API files it under.

Messages are stored once and rendered per format, the way the real API does:
a message holds its headers, its ``internalDate``, its ``labelIds`` and its
raw bytes (built here with the standard library's own MIME writer, so a
multipart body with attachments is a real multipart body). A client that asked
for ``metadata`` receives no body, and one that asked for ``raw`` receives no
parsed parts; the metadata rendering serves exactly the headers the request
named (``metadataHeaders``), as the real format does — a client that forgot to
name one receives an envelope without it, which is what keeps the header list
testable. **A send files the message in the mailbox** — a stored entry
with the ``SENT`` label, exactly as Gmail does — which is what makes the
reconciliation lookup testable: the sender asks ``in:sent rfc822msgid:<id>``
before repeating an attempt, and this fake answers that query the way the real
search does (a query the product would never send matches nothing rather than
everything).

The levers stage what a sending deployment meets: a provider that refuses the
send (``send_status``), and the case the outbox exists for — ``ambiguous_send``
filing the message and *then* failing the transport, so the message exists in
the mailbox while the deployment never heard an answer.

The PKCE check is the same S256 verification the real service performs, and
the fake only knows the *challenge* — because that is all Google knows:
``authorize`` stands in for the consent screen a browser would have opened
(accepting the challenge the deployment put in the consent URL and minting
the one-time code), and the token endpoint then requires the verifier that
never left the deployment. A client that satisfies this fake therefore
satisfies the real endpoints; only a real browser and a real Google account
remain untested by it.

No socket: the fake is a small ASGI app driven through
``httpx.ASGITransport``, so the suite stays hermetic.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN_PATH = "/token"
GMAIL_V1 = "/gmail/v1/users/me"

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# What this product asks for, copied from Google's scope list rather than from
# the application, so the fake would catch a client that asked for something
# else. Google echoes the granted set in the token response.
_GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly"
)

# The fake's clock: messages without an explicit ``received_at`` land here,
# one minute apart, so ordering and cursors are deterministic.
DEFAULT_RECEIVED_BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class UnreachableGoogle(httpx.AsyncBaseTransport):
    """A transport that fails the way a dead host does — without a socket."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("google is unreachable", request=request)


@dataclass
class _IssuedCode:
    challenge: str
    consumed: bool = False


@dataclass
class FakeGoogle:
    """Google's token endpoint and the Gmail API, as one mutable object."""

    client_id: str = "fake-client-id.apps.googleusercontent.com"
    client_secret: str = "fake-client-secret"
    redirect_uri: str = "http://localhost:8000/api/v1/mail/oauth/callback"
    address: str = "ai-filings@tcsp.example"
    messages_total: int = 0
    received_base: datetime = DEFAULT_RECEIVED_BASE
    # Every token the fake mints carries this, so a test can search for it
    # anywhere (a database, a log) and prove it did not travel.
    marker: str = "fake-google-token"
    expires_in: int = 3599
    granted_scope: str = _GMAIL_SCOPES
    # The messages, oldest first.
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Levers.
    token_status: int = 200
    profile_status: int = 200
    messages_status: int = 200
    # A refusal from ``users.messages.send``: the message is *not* filed, and
    # the API answers this status (429 and 5xx are the retryable ones).
    send_status: int = 200
    # The crash case: the message IS filed in Sent, and then the transport
    # fails, so the provider holds what the deployment never heard about.
    ambiguous_send: bool = False
    # A 200 whose body is not the shape ``users.messages.list`` promises: the
    # lever for a drifted response, which the client's parser refuses.
    list_body: dict[str, Any] | None = None
    # The provider's own id for the next sent message (deterministic when a
    # test needs to assert the recorded id).
    sent_message_prefix: str = "sent"

    # What the fake was asked to do.
    token_exchanges: int = 0
    refreshes: int = 0
    profile_fetches: int = 0
    message_fetches: list[str] = field(default_factory=list)
    raw_fetches: list[str] = field(default_factory=list)
    list_fetches: int = 0
    # Every authenticated ``messages.list`` call, refused or not: the send
    # path's reconciliation is a lookup whose *refusals* are the interesting
    # part, and a counter that only moved on success could not show a retry.
    list_attempts: int = 0
    message_format: str | None = None
    message_headers_requested: list[str] = field(default_factory=list)
    list_queries: list[str | None] = field(default_factory=list)
    sends: int = 0
    sent_raw: list[bytes] = field(default_factory=list)
    revoked_refresh_tokens: set[str] = field(default_factory=set)

    codes: dict[str, _IssuedCode] = field(default_factory=dict)
    access_tokens: dict[str, str] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    app: FastAPI = field(init=False)

    def __post_init__(self) -> None:
        self.app = _build_app(self)

    def authorize(self, challenge: str) -> str:
        """Stand in for the consent screen: accept a challenge, mint a code."""
        code = f"authorization-code-{secrets.token_urlsafe(8)}"
        self.codes[code] = _IssuedCode(challenge=challenge)
        return code

    def accept_access_token(self, access_token: str) -> None:
        """Accept a token the fake did not mint (a credential a test planted)."""
        self.access_tokens[access_token] = "consent"

    def with_message(
        self,
        *,
        subject: str | None,
        sender: str | None,
        date: str | None,
        to: str | None = None,
        cc: str | None = None,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        auto_submitted: str | None = None,
        return_path: str | None = None,
        body: str | None = None,
        attachments: list[tuple[str, str, bytes]] | None = None,
        received_at: datetime | None = None,
        raw: bytes | None = None,
    ) -> str:
        """Add one message (an ``.eml`` the fake can serve raw) and return its id.

        ``to`` defaults to the mailbox's own address — the way a client's mail
        reaches the dedicated mailbox — and the addressing and thread headers
        the attribution decision (ADR-0019 §1) reads are served only when the
        fixture names them, so a header's absence is a case a test can stage.
        ``raw`` overrides the built message — the lever a test uses to hand the
        client bytes that are not a message at all.
        """
        envelope = _envelope_headers(
            subject=subject,
            sender=sender,
            date=date,
            to=to if to is not None else self.address,
            cc=cc,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            auto_submitted=auto_submitted,
            return_path=return_path,
        )
        raw = raw if raw is not None else _build_raw(
            envelope=envelope,
            body=body if body is not None else "body of the next message",
            attachments=attachments or [],
        )
        return self._store(
            id_prefix="msg",
            raw=raw,
            label_ids=["INBOX"],
            headers=envelope,
            received_at=received_at,
        )

    def file_sent(self, raw: bytes) -> str:
        """File one outbound message in Sent, the way ``messages.send`` does.

        The message is stored with its own raw bytes, so the metadata read that
        follows a reconciliation lookup answers with the same headers the
        message carries — which is what makes the sender's thread lookup real.
        """
        parsed = message_from_bytes(raw, policy=default_policy)
        return self._store(
            id_prefix=self.sent_message_prefix,
            raw=raw,
            label_ids=["SENT"],
            headers=_envelope_headers(
                subject=parsed.get("Subject"),
                sender=parsed.get("From"),
                date=parsed.get("Date"),
                to=parsed.get("To"),
                cc=parsed.get("Cc"),
                message_id=parsed.get("Message-ID"),
                in_reply_to=parsed.get("In-Reply-To"),
                references=parsed.get("References"),
                auto_submitted=parsed.get("Auto-Submitted"),
                return_path=parsed.get("Return-Path"),
            ),
            received_at=None,
        )

    def _store(
        self,
        *,
        id_prefix: str,
        raw: bytes,
        label_ids: list[str],
        headers: tuple[tuple[str, str | None], ...],
        received_at: datetime | None,
    ) -> str:
        """Store one message and return the id the API answers with."""
        message_id = f"{id_prefix}-{len(self.messages) + 1}"
        received = received_at or self.received_base + timedelta(minutes=len(self.messages))
        self.messages.append(
            {
                "id": message_id,
                "threadId": f"thread-{message_id}",
                "labelIds": label_ids,
                "snippet": f"body of {message_id} — never part of the probe",
                "internalDate": str(int(received.timestamp() * 1000)),
                "raw": _encode_raw(raw),
                "sizeEstimate": len(raw),
                "headers": [
                    {"name": name, "value": value}
                    for name, value in headers
                    if value is not None
                ],
            }
        )
        self.messages_total += 1
        return message_id

    def raw_of(self, message_id: str) -> bytes:
        """The bytes the fake serves for one message (what a client would store)."""
        for stored in self.messages:
            if stored["id"] == message_id:
                encoded = str(stored["raw"])
                return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        raise KeyError(message_id)


def _envelope_headers(
    *,
    subject: str | None,
    sender: str | None,
    date: str | None,
    to: str | None,
    cc: str | None,
    message_id: str | None,
    in_reply_to: str | None,
    references: str | None,
    auto_submitted: str | None,
    return_path: str | None,
) -> tuple[tuple[str, str | None], ...]:
    """One message's envelope headers, as a fixture stages them.

    One list, so what the built raw message carries and what the metadata
    rendering serves are the same headers — the fake's two views of a message
    cannot disagree.
    """
    return (
        ("Subject", subject),
        ("From", sender),
        ("Date", date),
        ("To", to),
        ("Cc", cc),
        ("Message-ID", message_id),
        ("In-Reply-To", in_reply_to),
        ("References", references),
        ("Auto-Submitted", auto_submitted),
        ("Return-Path", return_path),
    )


def _build_raw(
    *,
    envelope: tuple[tuple[str, str | None], ...],
    body: str,
    attachments: list[tuple[str, str, bytes]],
) -> bytes:
    """One RFC 822 message, built the way a mail client would."""
    message = EmailMessage()
    for name, value in envelope:
        if value is not None:
            message[name] = value
    message.set_content(body)
    for filename, content_type, data in attachments:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def _encode_raw(raw: bytes) -> str:
    """Base64url without padding — the way the API carries a raw message."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _received_millis(stored: dict[str, Any]) -> int:
    return int(stored["internalDate"])


def _build_app(google: FakeGoogle) -> FastAPI:
    app = FastAPI()

    @app.post(TOKEN_PATH)
    async def token(request: Request) -> JSONResponse:
        form = _form(await request.body())
        if google.token_status != 200:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "refused"}, status_code=400
            )
        if form.get("client_id") != google.client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("client_secret") != google.client_secret:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return _exchange_code(google, form)
        if grant_type == "refresh_token":
            return _refresh(google, form)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    @app.get(f"{GMAIL_V1}/profile")
    async def profile(request: Request) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        if google.profile_status != 200:
            return _api_error(google, google.profile_status)
        google.profile_fetches += 1
        return JSONResponse(
            {
                "emailAddress": google.address,
                "messagesTotal": google.messages_total,
                "threadsTotal": google.messages_total,
                "historyId": "1234",
            }
        )

    @app.get(f"{GMAIL_V1}/messages")
    async def messages(request: Request) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        google.list_attempts += 1
        if google.messages_status != 200:
            return _api_error(google, google.messages_status)
        google.list_fetches += 1
        if google.list_body is not None:
            return JSONResponse(google.list_body)
        query = request.query_params.get("q")
        google.list_queries.append(query)
        matching = [stored for stored in google.messages if _matches_query(stored, query)]
        requested = request.query_params.get("maxResults")
        limit = int(requested) if requested is not None else len(matching)
        newest = list(reversed(matching))[:limit]
        return JSONResponse(
            {
                "messages": [
                    {"id": message["id"], "threadId": message["threadId"]} for message in newest
                ],
                "resultSizeEstimate": len(newest),
            }
        )

    @app.get(f"{GMAIL_V1}/messages/{{message_id}}")
    async def message(request: Request, message_id: str) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        google.message_format = request.query_params.get("format")
        google.message_headers_requested = request.query_params.getlist("metadataHeaders")
        for stored in google.messages:
            if stored["id"] != message_id:
                continue
            google.message_fetches.append(message_id)
            if google.message_format == "raw":
                google.raw_fetches.append(message_id)
            return JSONResponse(_rendered(google, stored))
        return _api_error(google, 404)

    @app.post(f"{GMAIL_V1}/messages/send")
    async def send(request: Request) -> JSONResponse:
        """File the message in Sent and answer with its provider id.

        A refusal (``send_status``) files nothing. The ambiguous lever files
        the message and *then* fails the transport: the provider holds what
        the deployment never heard an answer about, which is the case the
        outbox's reconciliation exists for.
        """
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        body = json.loads(await request.body() or b"{}")
        raw = _decode_raw(str(body.get("raw", "")))
        if google.send_status != 200:
            return _api_error(google, google.send_status)
        message_id = google.file_sent(raw)
        google.sends += 1
        google.sent_raw.append(raw)
        if google.ambiguous_send:
            raise httpx.ReadError(
                "the fake filed the message and then lost the connection",
                request=httpx.Request("POST", "https://gmail.googleapis.com"),
            )
        return JSONResponse(
            {"id": message_id, "threadId": f"thread-{message_id}", "labelIds": ["SENT"]}
        )

    return app


def _matches_query(stored: dict[str, Any], query: str | None) -> bool:
    """Gmail's search operators, as far as this product uses them.

    ``after:<epoch seconds>`` is the ingestion cursor's window; ``in:sent`` and
    ``rfc822msgid:<id>`` are the reconciliation lookup's. Anything else the
    product might send would be a change to the wire contract, so an
    unrecognised expression matches nothing rather than everything.
    """
    if query is None or not query.strip():
        return True
    matched = True
    for term in query.split():
        if term.startswith("after:"):
            try:
                after = int(term[len("after:") :])
            except ValueError:
                return False
            matched = matched and _received_millis(stored) > after * 1000
        elif term == "in:sent":
            matched = matched and "SENT" in stored["labelIds"]
        elif term.startswith("rfc822msgid:"):
            wanted = term[len("rfc822msgid:") :].strip("<>")
            matched = matched and _message_id_of(stored) == wanted
        else:
            return False
    return matched


def _message_id_of(stored: dict[str, Any]) -> str | None:
    """The RFC 5322 ``Message-ID`` of one stored message, as the search carries it.

    Unwrapped both ways: the header is parsed off the stored bytes (so the
    fake answers about the message it really holds) and the angle brackets are
    stripped, which is the form ``rfc822msgid:`` takes.
    """
    encoded = str(stored["raw"])
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    value = message_from_bytes(raw, policy=default_policy).get("Message-ID")
    return None if value is None else str(value).strip().strip("<>")


def _decode_raw(encoded: str) -> bytes:
    """The base64url message a ``send`` request carries, decoded."""
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def _rendered(google: FakeGoogle, stored: dict[str, Any]) -> dict[str, Any]:
    """One message in the shape the requested format returns.

    ``metadata`` carries only the headers the request named
    (``metadataHeaders``), as the real metadata format does — a client that
    forgot to ask for one receives an envelope without it, which is what keeps
    the header list testable. ``raw`` carries the bytes, and anything else
    (``full`` or no format at all) gets the whole stored entry — the bodies
    included, so a client that asked for more than this product asks for is
    handed a full-looking body.
    """
    if google.message_format == "metadata":
        wanted = [name.casefold() for name in google.message_headers_requested]
        return {
            "id": stored["id"],
            "threadId": stored["threadId"],
            "labelIds": stored["labelIds"],
            "snippet": stored["snippet"],
            "internalDate": stored["internalDate"],
            "payload": {
                "headers": [
                    header
                    for header in stored["headers"]
                    if header["name"].casefold() in wanted
                ]
            },
        }
    if google.message_format == "raw":
        return {
            "id": stored["id"],
            "threadId": stored["threadId"],
            "labelIds": stored["labelIds"],
            "internalDate": stored["internalDate"],
            "sizeEstimate": stored["sizeEstimate"],
            "raw": stored["raw"],
        }
    return dict(stored)


def _exchange_code(google: FakeGoogle, form: dict[str, str]) -> JSONResponse:
    code = form.get("code", "")
    issued = google.codes.get(code)
    if issued is None or issued.consumed:
        return _token_error(google, "invalid_grant")
    if form.get("redirect_uri") != google.redirect_uri:
        return _token_error(google, "redirect_uri_mismatch")
    if not _verifier_matches(verifier=form.get("code_verifier", ""), challenge=issued.challenge):
        return _token_error(google, "invalid_grant")
    issued.consumed = True
    google.token_exchanges += 1
    return JSONResponse(
        {
            "access_token": _new_access_token(google),
            "expires_in": google.expires_in,
            "refresh_token": _new_refresh_token(google),
            "scope": google.granted_scope,
            "token_type": "Bearer",
        }
    )


def _refresh(google: FakeGoogle, form: dict[str, str]) -> JSONResponse:
    refresh_token = form.get("refresh_token", "")
    if refresh_token not in google.refresh_tokens or refresh_token in google.revoked_refresh_tokens:
        return _token_error(google, "invalid_grant")
    google.refreshes += 1
    return JSONResponse(
        {
            "access_token": _new_access_token(google),
            "expires_in": google.expires_in,
            "scope": google.granted_scope,
            "token_type": "Bearer",
        }
    )


def _new_access_token(google: FakeGoogle) -> str:
    token = f"{google.marker}-access-{secrets.token_urlsafe(8)}"
    google.access_tokens[token] = "consent"
    return token


def _new_refresh_token(google: FakeGoogle) -> str:
    token = f"{google.marker}-refresh-{secrets.token_urlsafe(8)}"
    google.refresh_tokens.add(token)
    return token


def _authenticate(google: FakeGoogle, request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token not in google.access_tokens:
        return _api_error(google, 401)
    return None


def _token_error(google: FakeGoogle, code: str) -> JSONResponse:
    """The token endpoint's flat error body, echoing the marker a test searches for.

    Real Google describes its refusals in ``error_description``; the fake puts
    its marker there so a client that echoed the body would be caught by a
    test rather than by a leak.
    """
    return JSONResponse(
        {"error": code, "error_description": f"refused by the fake ({google.marker})"},
        status_code=400,
    )


def _api_error(google: FakeGoogle, status: int) -> JSONResponse:
    """The Gmail API's error envelope (an object, unlike the token endpoint's)."""
    return JSONResponse(
        {
            "error": {
                "code": status,
                "message": f"refused by the fake ({google.marker})",
                "status": "UNAUTHENTICATED" if status == 401 else "FAILED_PRECONDITION",
            }
        },
        status_code=status,
    )


def _form(body: bytes) -> dict[str, str]:
    """The form-encoded token request as a dict (``+`` decodes to a space)."""
    return dict(parse_qsl(body.decode("utf-8")))


def _verifier_matches(*, verifier: str, challenge: str) -> bool:
    """The S256 check the real service performs (RFC 7636 §4.6, constant-time)."""
    if not 43 <= len(verifier) <= 128 or not all(c in _UNRESERVED for c in verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def consent_challenge(consent_url: str) -> str:
    """The PKCE challenge a browser would carry out of the consent URL."""
    return dict(parse_qsl(urlsplit(consent_url).query))["code_challenge"]
