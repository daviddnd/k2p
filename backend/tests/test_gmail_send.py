"""The Gmail send leg, and the reconciliation lookup that guards it.

Ruling 3 and ADR-0011 §1 meet here: a message may leave the deployment only
once intent is durable, and a repeated attempt must not produce a second copy
of a client's letter. The Gmail half of that is the RFC 5322 ``Message-ID``
the M3 generator writes — it is the idempotency anchor, and
``find_sent_message_id`` is the lookup that makes it usable
(``gmail.readonly`` already covers ``in:sent``).

What the wire tests pin: the send request carries the message's own bytes as
Google takes them (base64url, no padding); a refusal is classified by its
status — a rate limit or a server error may succeed later, everything else
will not; a transport error around a send is **unconfirmed**, never a silent
"did not send"; and a lookup that finds the message answers its provider id
while a query Gmail would not understand finds nothing.
"""

from email.message import EmailMessage
from email.policy import SMTP

import httpx
import pytest

from app.adapters.mail.errors import (
    MailDeliveryFailed,
    MailProviderRefused,
    MailProviderUnavailable,
)
from app.adapters.mail.gmail_client import GmailClient
from app.domain.mail_send import DeliveryFailure
from tests.fake_google import FakeGoogle, UnreachableGoogle

MESSAGE_ID = "<20260925090000.abcdef@tcsp.example>"


def draft_bytes(*, message_id: str = MESSAGE_ID) -> bytes:
    """A byte-complete RFC 5322 message, the way the M3 generator composes one."""
    message = EmailMessage(policy=SMTP)
    message["From"] = "ai-filings@tcsp.example"
    message["To"] = "director@brightlantern.example.hk"
    message["Subject"] = "[Bright Lantern Design Limited — CR 1234567] Annual Return Confirmation"
    message["Message-ID"] = message_id
    message.set_content("Dear Sir or Madam\n")
    return message.as_bytes()


def client_for(google: FakeGoogle, *, timeout: float = 1.0) -> GmailClient:
    return GmailClient(timeout=timeout, transport=httpx.ASGITransport(app=google.app))


def any_token(google: FakeGoogle) -> str:
    token = f"{google.marker}-access-direct"
    google.access_tokens[token] = "consent"
    return token


async def test_a_send_carries_the_messages_own_bytes_and_files_it_in_sent() -> None:
    google = FakeGoogle()
    raw = draft_bytes()

    answer = await client_for(google).send_message(access_token=any_token(google), raw=raw)

    # The request carries exactly the stored bytes: what the provider files is
    # what the records registry holds, so the Message-ID the reconcile looks
    # for is the one the mailbox carries.
    assert google.sent_raw == [raw]
    assert answer["id"] == "sent-1"
    assert answer["threadId"] == "thread-sent-1"
    assert answer["labelIds"] == ["SENT"]


async def test_the_sent_message_is_findable_by_its_message_id() -> None:
    google = FakeGoogle()
    raw = draft_bytes()
    await client_for(google).send_message(access_token=any_token(google), raw=raw)

    found = await client_for(google).find_sent_message_id(
        access_token=any_token(google), message_id=MESSAGE_ID
    )

    assert found == "sent-1"
    # Gmail indexes the id without the header's angle brackets.
    assert google.list_queries[-1] == "in:sent rfc822msgid:20260925090000.abcdef@tcsp.example"


async def test_a_lookup_that_finds_nothing_is_not_a_guess() -> None:
    """The negative answer is what authorises the resend: nothing in Sent
    carries this Message-ID."""
    google = FakeGoogle()
    google.with_message(subject="something else", sender="a@b.example", date="Mon, 22 Sep 2026")

    found = await client_for(google).find_sent_message_id(
        access_token=any_token(google), message_id=MESSAGE_ID
    )

    assert found is None


async def test_a_lookup_matches_the_exact_message_id_not_any_of_them() -> None:
    """Another message's id must not answer for this one — the anchor is the
    whole header value."""
    google = FakeGoogle()
    google.file_sent(draft_bytes(message_id="<other@tcsp.example>"))

    found = await client_for(google).find_sent_message_id(
        access_token=any_token(google), message_id=MESSAGE_ID
    )

    assert found is None


async def test_a_spent_access_token_is_a_refusal_not_a_lost_message() -> None:
    """A 401 files nothing, so it must not read as unconfirmed: the sender
    would then pause on an outcome that never happened."""
    google = FakeGoogle()

    with pytest.raises(MailDeliveryFailed) as caught:
        await client_for(google).send_message(access_token="not-a-token", raw=draft_bytes())

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "UNAUTHENTICATED"
    assert google.sends == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, DeliveryFailure.TRANSIENT),
        (500, DeliveryFailure.TRANSIENT),
        (503, DeliveryFailure.TRANSIENT),
        (400, DeliveryFailure.PERMANENT),
        (403, DeliveryFailure.PERMANENT),
    ],
)
async def test_a_refusal_is_classified_by_what_another_attempt_would_do(
    status: int, expected: DeliveryFailure
) -> None:
    google = FakeGoogle()
    google.send_status = status

    with pytest.raises(MailDeliveryFailed) as caught:
        await client_for(google).send_message(access_token=any_token(google), raw=draft_bytes())

    assert caught.value.failure is expected
    assert google.sends == 0  # a refused send files nothing


async def test_an_unflushed_refusal_with_no_status_is_permanent() -> None:
    """A refusal whose body could not be read has no retry signal; treating it
    as transient would retry a call this deployment cannot make."""
    transport = _RefusingTransport(
        httpx.Response(403, content=b"not json at all", headers={"content-type": "text/html"})
    )
    client = GmailClient(timeout=1.0, transport=transport)

    with pytest.raises(MailDeliveryFailed) as caught:
        await client.send_message(access_token="t", raw=draft_bytes())

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "http_403"


async def test_an_unreachable_provider_leaves_the_outcome_unknown() -> None:
    """The send may or may not have been accepted — the one answer the sender
    must not invent."""
    client = GmailClient(timeout=1.0, transport=UnreachableGoogle())

    with pytest.raises(MailDeliveryFailed) as caught:
        await client.send_message(access_token="t", raw=draft_bytes())

    assert caught.value.failure is DeliveryFailure.UNCONFIRMED
    assert caught.value.reason == "transport_error"


async def test_the_ambiguous_providers_answer_is_never_a_lost_message() -> None:
    """The case the outbox exists for: the provider filed the message and the
    transport died before the answer. The client reports *unconfirmed* — and
    the message is really there, which is what the reconcile then finds."""
    google = FakeGoogle()
    google.ambiguous_send = True
    raw = draft_bytes()

    with pytest.raises(MailDeliveryFailed) as caught:
        await client_for(google).send_message(access_token=any_token(google), raw=raw)

    assert caught.value.failure is DeliveryFailure.UNCONFIRMED
    assert google.sent_raw == [raw]
    found = await client_for(google).find_sent_message_id(
        access_token=any_token(google), message_id=MESSAGE_ID
    )
    assert found == "sent-1"


async def test_a_read_still_raises_the_read_vocabulary() -> None:
    """The send's classification did not change what a read reports: an
    unreachable provider is still ``MailProviderUnavailable``, and a read that
    Google refuses is still ``MailProviderRefused`` with its own code — the
    delivery vocabulary lives on the send path alone."""
    client = GmailClient(timeout=1.0, transport=UnreachableGoogle())

    with pytest.raises(MailProviderUnavailable):
        await client.fetch_profile(access_token="t")

    google = FakeGoogle()
    with pytest.raises(MailProviderRefused) as caught:
        await client_for(google).fetch_profile(access_token="not-a-token")
    assert caught.value.reason == "UNAUTHENTICATED"


class _RefusingTransport(httpx.AsyncBaseTransport):
    """A provider that always answers with one fixed refusal."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._response
