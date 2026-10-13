"""Wire tests for the Gmail client (ADR-0005; Phase 3 mailbox slice).

The real client — real httpx, real URLs, real form bodies and JSON — against
``tests/fake_google.py``, which mirrors Google's token endpoint and the Gmail
API. What is exercised is what a drifted API, a revoked grant or an outage
does to the deployment: refused calls, unreachable hosts, timeouts, and
bodies that are not the shape expected. No test touches the network.

The *parsing* of these responses lives in ``app.domain.mail`` and is pinned
in ``test_mail_domain.py``; these stop at the transport boundary: the client
delivers a JSON object or a typed error, never a message (or a log line)
carrying token material.
"""

import base64
import hashlib
from datetime import timedelta

import httpx
import pytest

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.adapters.mail.gmail_client import GmailClient
from tests.fake_google import FakeGoogle, UnreachableGoogle

REDIRECT_URI = "http://localhost:8000/api/v1/mail/oauth/callback"
CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
VERIFIER = "v" * 43
NOW_EXPIRES_IN = 3599


def client_for(google: FakeGoogle, *, timeout: float = 1.0) -> GmailClient:
    """The real client pointed at the fake provider, in-process."""
    return GmailClient(timeout=timeout, transport=httpx.ASGITransport(app=google.app))


def challenge_for(verifier: str) -> str:
    """The challenge computed independently of the application's own helper."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def issue_tokens(google: FakeGoogle) -> dict[str, object]:
    """Run one successful consent exchange and return the raw token body."""
    code = google.authorize(challenge_for(VERIFIER))
    return await client_for(google).exchange_code(
        code=code,
        code_verifier=VERIFIER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
    )


async def test_exchanging_the_code_passes_the_verifier_and_returns_the_tokens() -> None:
    google = FakeGoogle()
    code = google.authorize(challenge_for(VERIFIER))

    body = await client_for(google).exchange_code(
        code=code,
        code_verifier=VERIFIER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
    )

    # The fake refuses anything but a form-encoded body carrying this client's
    # credentials, the registered redirect and a verifier matching the
    # challenge, so reaching these assertions means all of that was sent.
    assert google.token_exchanges == 1
    assert google.codes[code].consumed is True
    assert body["access_token"] in google.access_tokens
    assert body["refresh_token"] in google.refresh_tokens
    assert body["expires_in"] == NOW_EXPIRES_IN


async def test_an_authorization_code_is_single_use() -> None:
    google = FakeGoogle()
    code = google.authorize(challenge_for(VERIFIER))
    client = client_for(google)
    await client.exchange_code(
        code=code,
        code_verifier=VERIFIER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
    )

    with pytest.raises(MailProviderRefused) as refusal:
        await client.exchange_code(
            code=code,
            code_verifier=VERIFIER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        )

    assert refusal.value.reason == "invalid_grant"
    assert google.token_exchanges == 1


async def test_a_wrong_verifier_is_refused() -> None:
    google = FakeGoogle()
    code = google.authorize(challenge_for(VERIFIER))

    with pytest.raises(MailProviderRefused) as refusal:
        await client_for(google).exchange_code(
            code=code,
            code_verifier="w" * 43,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        )

    assert refusal.value.reason == "invalid_grant"
    assert google.token_exchanges == 0


async def test_a_refusal_carries_the_code_and_never_the_body() -> None:
    # Every refusal body the fake sends echoes its marker in
    # ``error_description``: if the client surfaced the body, the marker
    # would travel with the error.
    google = FakeGoogle(marker="provider-body-marker")
    code = google.authorize(challenge_for(VERIFIER))

    with pytest.raises(MailProviderRefused) as refusal:
        await client_for(google).exchange_code(
            code=code,
            code_verifier=VERIFIER,
            client_id=CLIENT_ID,
            client_secret="wrong-secret",
            redirect_uri=REDIRECT_URI,
        )

    assert refusal.value.reason == "invalid_client"
    assert "provider-body-marker" not in str(refusal.value)
    assert "provider-body-marker" not in repr(refusal.value)
    assert "wrong-secret" not in str(refusal.value)


async def test_refreshing_returns_a_new_access_token_and_keeps_the_grant() -> None:
    google = FakeGoogle()
    first = await issue_tokens(google)
    refresh_token = str(first["refresh_token"])

    body = await client_for(google).refresh_access_token(
        refresh_token=refresh_token,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )

    assert google.refreshes == 1
    assert body["access_token"] != first["access_token"]
    assert body["access_token"] in google.access_tokens
    # Google does not re-issue the refresh token on a refresh: the grant is
    # still the one the consent screen minted.
    assert "refresh_token" not in body


async def test_a_revoked_grant_is_refused() -> None:
    google = FakeGoogle()
    tokens = await issue_tokens(google)
    refresh_token = str(tokens["refresh_token"])
    google.revoked_refresh_tokens.add(refresh_token)

    with pytest.raises(MailProviderRefused) as refusal:
        await client_for(google).refresh_access_token(
            refresh_token=refresh_token,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

    assert refusal.value.reason == "invalid_grant"
    assert google.refreshes == 0


async def test_the_profile_read_reports_the_mailbox_and_its_size() -> None:
    google = FakeGoogle(address="ai-filings@tcsp.example", messages_total=7)
    body = await client_for(google).fetch_profile(access_token=_any_token(google))

    assert google.profile_fetches == 1
    assert body["emailAddress"] == "ai-filings@tcsp.example"
    assert body["messagesTotal"] == 7


async def test_an_unusable_access_token_is_a_typed_refusal() -> None:
    # The Gmail API refuses an unknown or expired token with its own error
    # envelope — an object, unlike the token endpoint's — and the client
    # narrows both to one bounded reason.
    google = FakeGoogle()

    with pytest.raises(MailProviderRefused) as refusal:
        await client_for(google).fetch_profile(access_token="never-issued")

    assert refusal.value.reason == "UNAUTHENTICATED"
    assert google.profile_fetches == 0


async def test_the_message_list_asks_for_the_newest_messages() -> None:
    google = FakeGoogle()
    for index in range(4):
        google.with_message(
            subject=f"message {index}", sender="a@b", date="Mon, 22 Sep 2026 09:31:00 +0800"
        )
    google.messages_total = 4
    token = _any_token(google)

    body = await client_for(google).list_message_ids(access_token=token, max_results=2)

    assert google.list_fetches == 1
    ids = [entry["id"] for entry in body["messages"]]
    assert ids == ["msg-4", "msg-3"]


async def test_the_metadata_read_asks_for_metadata_only() -> None:
    google = FakeGoogle()
    message_id = google.with_message(
        subject="NAR1 confirmation", sender="Director <d@client.example>", date="Mon, 22 Sep 2026"
    )

    body = await client_for(google).fetch_message_metadata(
        access_token=_any_token(google), message_id=message_id
    )

    # ``format=metadata`` plus the header names is the whole reason the probe
    # can never receive a message body, whatever the parser does. The
    # addressing and thread headers are among them: the attribution decision
    # (ADR-0019 §1) is made from the envelope, before any body is fetched.
    assert google.message_format == "metadata"
    assert sorted(google.message_headers_requested) == [
        "Auto-Submitted",
        "Cc",
        "Date",
        "From",
        "In-Reply-To",
        "Message-ID",
        "References",
        "Return-Path",
        "Subject",
        "To",
    ]
    assert google.message_fetches == [message_id]
    assert body["id"] == message_id


async def test_the_message_list_can_ask_for_a_window() -> None:
    google = FakeGoogle()
    google.with_message(subject="older", sender="a@b", date="Mon, 22 Sep 2026")
    google.with_message(subject="newer", sender="a@b", date="Tue, 23 Sep 2026")
    # A cursor at the older message's own timestamp: the operator is exclusive,
    # so the window is everything after it — exactly what the cycle asks for.
    cursor = google.received_base
    token = _any_token(google)

    body = await client_for(google).list_message_ids(
        access_token=token, max_results=50, query=f"after:{int(cursor.timestamp())}"
    )

    # The cursor's window is what the fake's search operator saw.
    assert google.list_queries == [f"after:{int(cursor.timestamp())}"]
    assert [entry["id"] for entry in body["messages"]] == ["msg-2"]


async def test_the_raw_read_returns_the_whole_message() -> None:
    google = FakeGoogle()
    message_id = google.with_message(
        subject="NAR1 confirmation",
        sender="Director <d@client.example>",
        date="Mon, 22 Sep 2026",
        attachments=[("signed.pdf", "application/pdf", b"%PDF-1.7 signed")],
    )

    body = await client_for(google).fetch_message_raw(
        access_token=_any_token(google), message_id=message_id
    )

    # ``format=raw`` is what makes the stored object the message the mailbox
    # holds, attachments included; the bytes are decoded by the domain parser.
    assert google.message_format == "raw"
    assert google.raw_fetches == [message_id]
    assert google.message_headers_requested == []
    assert isinstance(body["raw"], str)
    assert body["internalDate"] == str(
        int((google.received_base + timedelta(minutes=0)).timestamp() * 1000)
    )


async def test_an_unreachable_provider_raises_unavailable() -> None:
    client = GmailClient(timeout=1.0, transport=UnreachableGoogle())

    with pytest.raises(MailProviderUnavailable):
        await client.fetch_profile(access_token="t")


async def test_the_configured_timeout_reaches_the_request() -> None:
    # httpx enforces the timeout in the socket layer, which no test here
    # touches; what is checked is the plumbing — the configured timeout
    # reaches every request, so a hung provider cannot hold a request open
    # indefinitely.
    recording = _RecordingTransport(
        httpx.Response(200, json={"emailAddress": "a@b", "messagesTotal": 0})
    )
    client = GmailClient(timeout=0.5, transport=recording)

    await client.fetch_profile(access_token="t")

    assert recording.timeouts == [{"connect": 0.5, "read": 0.5, "write": 0.5, "pool": 0.5}]


def _any_token(google: FakeGoogle) -> str:
    """One access token the fake will accept (a consent exchange without the round trip)."""
    token = f"{google.marker}-access-direct"
    google.access_tokens[token] = "consent"
    return token


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
