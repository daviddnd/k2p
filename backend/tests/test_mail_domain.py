"""Pure rules for the dedicated mailbox connection (ADR-0005; Phase 3).

What the consent URL must carry, what counts as the same attempt coming back,
when the access token has to be renewed, how a different mailbox is refused,
and the strict parsers for the provider responses that hand this deployment
its credential. No I/O and no framework (ADR-0006); the same rules are pinned
again through the real routes in ``test_api_mail.py`` and against PostgreSQL
in ``test_mail_scenarios_db.py``.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import pytest

from app.domain.mail import (
    GMAIL_SCOPES,
    MailboxCredential,
    MailResponseInvalid,
    PendingMailConnection,
    address_matches,
    consent_url,
    message_id_tokens,
    new_state_token,
    parse_message_metadata,
    parse_messages_list,
    parse_profile_response,
    parse_token_response,
    state_matches,
)

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
CLIENT_ID = "1234.apps.googleusercontent.com"
REDIRECT_URI = "http://localhost:8000/api/v1/mail/oauth/callback"
VERIFIER = "v" * 43


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def s256_challenge(verifier: str) -> str:
    """The challenge computed independently of the application's own helper."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --- The consent URL -----------------------------------------------------


def test_the_consent_url_asks_for_exactly_the_two_gmail_scopes() -> None:
    url = consent_url(
        client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="st", code_challenge="ch"
    )

    assert urlsplit(url).netloc == "accounts.google.com"
    assert query_of(url)["scope"].split() == list(GMAIL_SCOPES)
    assert GMAIL_SCOPES == (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    )


def test_the_consent_url_carries_the_client_redirect_state_and_challenge() -> None:
    url = consent_url(
        client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="st", code_challenge="ch"
    )

    query = query_of(url)
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["response_type"] == "code"
    assert query["state"] == "st"
    assert query["code_challenge"] == "ch"
    assert query["code_challenge_method"] == "S256"


def test_the_consent_url_asks_for_a_refresh_token_every_time() -> None:
    # Google issues a refresh token on the first consent only unless the
    # prompt is forced: a re-connection (after a disconnect) must still come
    # back with one, and a long-lived mailbox needs one to work at all.
    query = query_of(
        consent_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="st", code_challenge="ch"
        )
    )

    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"


def test_the_challenge_matches_the_verifier_it_was_minted_from() -> None:
    from app.domain.activation import code_challenge_for, new_code_verifier

    verifier = new_code_verifier()
    query = query_of(
        consent_url(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            state="st",
            code_challenge=code_challenge_for(verifier),
        )
    )

    assert query["code_challenge"] == s256_challenge(verifier)
    assert 43 <= len(verifier) <= 128


def test_each_attempt_gets_an_unpredictable_state() -> None:
    states = {new_state_token() for _ in range(50)}

    assert len(states) == 50
    assert all(len(state) >= 32 for state in states)


# --- The pending attempt -------------------------------------------------


def pending(**overrides: object) -> PendingMailConnection:
    values: dict[str, object] = {
        "state": "state-1",
        "verifier": VERIFIER,
        "local_tenant_id": uuid4(),
        "local_user_id": uuid4(),
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(overrides)
    return PendingMailConnection(**values)  # type: ignore[arg-type]


def test_state_matches_is_exact_and_refuses_what_is_missing() -> None:
    assert state_matches(expected="state-1", received="state-1") is True
    assert state_matches(expected="state-1", received="state-2") is False
    assert state_matches(expected="state-1", received=None) is False
    assert state_matches(expected="state-1", received="") is False
    # A query string may hold anything: comparing must answer false, not raise.
    assert state_matches(expected="state-1", received="狀態") is False


def test_a_pending_attempt_expires_at_its_deadline() -> None:
    attempt = pending(expires_at=NOW + timedelta(minutes=10))

    assert attempt.is_expired(now=NOW) is False
    assert attempt.is_expired(now=NOW + timedelta(minutes=10)) is False
    assert attempt.is_expired(now=NOW + timedelta(minutes=10, seconds=1)) is True


# --- Token lifetime ------------------------------------------------------


def credential(**overrides: object) -> MailboxCredential:
    values: dict[str, object] = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "address": "ai-filings@tcsp.example",
        "scopes": GMAIL_SCOPES,
        "connected_at": NOW,
        "token_expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return MailboxCredential(**values)  # type: ignore[arg-type]


def test_the_access_token_is_renewed_before_it_expires() -> None:
    stored = credential(token_expires_at=NOW + timedelta(hours=1))

    assert stored.needs_refresh(now=NOW, leeway_seconds=60) is False
    # Inside the leeway the token is treated as stale: the probe's calls must
    # not race the expiry.
    assert stored.needs_refresh(now=NOW + timedelta(minutes=59, seconds=30), leeway_seconds=60)
    assert stored.needs_refresh(now=NOW + timedelta(hours=1), leeway_seconds=60) is True


def test_a_credential_never_renders_its_tokens() -> None:
    stored = credential()

    assert "access-1" not in repr(stored)
    assert "refresh-1" not in repr(stored)


# --- Provider response parsing ------------------------------------------


def test_parse_token_response_reads_the_granted_scopes() -> None:
    parsed = parse_token_response(
        {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 3599,
            "scope": " ".join(GMAIL_SCOPES),
            "token_type": "Bearer",
        }
    )

    assert parsed.access_token == "access-1"
    assert parsed.refresh_token == "refresh-1"
    assert parsed.scopes == GMAIL_SCOPES
    assert parsed.expires_in == 3599
    assert "access-1" not in repr(parsed)


def test_parse_token_response_allows_a_refresh_response_without_a_refresh_token() -> None:
    # Google returns a refresh token on the consent exchange; a *refresh*
    # call returns only a new access token, and that is not an error.
    parsed = parse_token_response({"access_token": "access-2", "expires_in": 3599})

    assert parsed.refresh_token is None
    assert parsed.scopes is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"access_token": "", "expires_in": 3599},
        {"access_token": "access-1", "expires_in": "soon"},
        {"access_token": "access-1"},
        {"access_token": "access-1", "expires_in": 3599, "scope": 7},
    ],
)
def test_parse_token_response_refuses_a_body_it_cannot_trust(body: dict[str, object]) -> None:
    with pytest.raises(MailResponseInvalid) as refused:
        parse_token_response(body)

    assert isinstance(refused.value.reason, str) and refused.value.reason


def test_parse_profile_requires_an_address() -> None:
    profile = parse_profile_response(
        {"emailAddress": "ai-filings@tcsp.example", "messagesTotal": 12}
    )
    assert profile.address == "ai-filings@tcsp.example"
    assert profile.messages_total == 12

    # A response with no address cannot pass the guard, so it is refused
    # rather than stored as an empty mailbox identity.
    with pytest.raises(MailResponseInvalid):
        parse_profile_response({"messagesTotal": 12})
    with pytest.raises(MailResponseInvalid):
        parse_profile_response({"emailAddress": "x@y", "messagesTotal": "many"})


def test_parse_messages_list_tolerates_an_empty_mailbox() -> None:
    assert parse_messages_list({}) == []
    assert parse_messages_list({"resultSizeEstimate": 0}) == []


def test_parse_messages_list_refuses_a_malformed_entry() -> None:
    with pytest.raises(MailResponseInvalid):
        parse_messages_list({"messages": [{"id": "m1"}, {"threadId": "t2"}]})


def test_parse_message_metadata_picks_the_envelope_headers() -> None:
    metadata = parse_message_metadata(
        {
            "id": "18f2",
            "threadId": "18f0",
            "internalDate": "1790112000000",
            "snippet": "this must never leave the parser",
            "payload": {
                "headers": [
                    {"name": "Delivered-To", "value": "ai-filings@tcsp.example"},
                    {"name": "Subject", "value": "NAR1 confirmation"},
                    {"name": "From", "value": "Director <director@client.example>"},
                    {"name": "Date", "value": "Mon, 22 Sep 2026 09:31:00 +0800"},
                    {
                        "name": "To",
                        "value": "Wu Tai Man <wu@client.example>, director@client.example",
                    },
                    {"name": "Cc", "value": "Ops <ops@client.example>"},
                    {"name": "Message-ID", "value": "<reply-1@client.example>"},
                    {"name": "In-Reply-To", "value": "<filed-1@tcsp.example>"},
                    {
                        "name": "References",
                        "value": "<filed-0@tcsp.example> <filed-1@tcsp.example>",
                    },
                    {"name": "Auto-Submitted", "value": "auto-generated"},
                    {"name": "Return-Path", "value": "<>"},
                ]
            },
        }
    )

    assert metadata.message_id == "18f2"
    assert metadata.thread_id == "18f0"
    assert metadata.subject == "NAR1 confirmation"
    assert metadata.sender == "Director <director@client.example>"
    assert metadata.date == "Mon, 22 Sep 2026 09:31:00 +0800"
    # The provider's own receipt time, and the one field the ingestion cursor
    # is derived from.
    assert metadata.received_at == datetime.fromtimestamp(1790112000, tz=UTC)
    # The addressing headers in their comparison form, first occurrence first.
    assert metadata.recipients_to == ("wu@client.example", "director@client.example")
    assert metadata.recipients_cc == ("ops@client.example",)
    # The header's own id beside the provider id the message is recorded under,
    # and the thread headers as the angle-bracketed tokens a join matches on.
    assert metadata.rfc_message_id == "<reply-1@client.example>"
    assert metadata.in_reply_to == ("<filed-1@tcsp.example>",)
    assert metadata.references == ("<filed-0@tcsp.example>", "<filed-1@tcsp.example>")
    # The anti-abuse headers verbatim, empty envelope sender included.
    assert metadata.auto_submitted == "auto-generated"
    assert metadata.return_path == "<>"


def test_parse_message_metadata_requires_the_receipt_time() -> None:
    # Everything the ingestion cycle does with a message is ordered by when
    # the mailbox received it; a response without one is not usable.
    with pytest.raises(MailResponseInvalid):
        parse_message_metadata({"id": "18f2", "payload": {"headers": []}})


def test_parse_message_metadata_leaves_absent_headers_empty() -> None:
    metadata = parse_message_metadata(
        {"id": "18f2", "internalDate": "1790112000000", "payload": {"headers": []}}
    )

    assert metadata.subject is None
    assert metadata.sender is None
    assert metadata.date is None
    assert metadata.recipients_to == ()
    assert metadata.recipients_cc == ()
    assert metadata.in_reply_to == ()
    assert metadata.references == ()
    assert metadata.rfc_message_id is None
    assert metadata.auto_submitted is None
    assert metadata.return_path is None

    with pytest.raises(MailResponseInvalid):
        parse_message_metadata({"payload": {"headers": []}})


# --- The thread headers --------------------------------------------------


def test_message_id_tokens_extract_the_angle_bracketed_ids_in_order() -> None:
    assert message_id_tokens("<a@client.example>") == ("<a@client.example>",)
    assert message_id_tokens("<first@tcsp.example> <second@tcsp.example>") == (
        "<first@tcsp.example>",
        "<second@tcsp.example>",
    )
    # Some clients quote a reference; the token is the id inside the quotes.
    assert message_id_tokens('"<quoted@client.example>"') == ("<quoted@client.example>",)
    # Folding whitespace between two ids belongs to neither.
    assert message_id_tokens("<first@client.example>\r\n\t<second@client.example>") == (
        "<first@client.example>",
        "<second@client.example>",
    )
    # Each value contributes its own tokens, in argument order.
    assert message_id_tokens("<a@client.example>", "<b@client.example>") == (
        "<a@client.example>",
        "<b@client.example>",
    )


def test_message_id_tokens_keep_only_what_is_a_token() -> None:
    assert message_id_tokens() == ()
    assert message_id_tokens("") == ()
    assert message_id_tokens("no ids in this header") == ()
    # A bare id, and brackets whose text carries whitespace, are not tokens:
    # the rule is the id's own delimiters, not any angle brackets at all.
    assert message_id_tokens("a@client.example") == ()
    assert message_id_tokens("<not an id>") == ()


# --- The address guard ---------------------------------------------------


def test_the_address_guard_compares_normalized_addresses() -> None:
    assert address_matches(expected="AI-Filings@TCSP.example", actual="ai-filings@tcsp.example")
    assert address_matches(expected=" ai-filings@tcsp.example ", actual="ai-filings@tcsp.example")
    assert not address_matches(expected="ai-filings@tcsp.example", actual="someone@gmail.com")
    assert not address_matches(expected="ai-filings@tcsp.example", actual="")


def test_the_state_token_is_not_guessable_and_is_secret_per_attempt() -> None:
    # The callback's only local authority is that the browser returns the
    # value this process minted: two states must never collide, and the
    # repr must not carry it (a credential-shaped secret, ADR-0014).
    first, second = new_state_token(), new_state_token()

    assert first != second
    assert len(first) >= 32
    assert first not in repr(pending(state=first))
