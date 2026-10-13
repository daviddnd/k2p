"""The SMTP leg, against a real SMTP conversation on loopback.

`tests/fake_smtp.py` is a small server that speaks the protocol — a real
greeting, a real ESMTP feature list, a real TLS handshake with the test-only
certificate under ``tests/fixtures/smtp-tls``, real reply codes — and the
client is the standard library's ``smtplib`` driven by our adapter. So what is
exercised is the conversation itself: that the message arrives byte for byte,
that a credential is never offered before the upgrade, that a temporary refusal
and a permanent one are told apart, and that a connection lost around the
transfer is reported as *unconfirmed* rather than as either verdict.

The two properties worth naming:

- **The upgrade is required.** A server that advertises no ``STARTTLS`` is
  refused (permanently: this deployment's policy will refuse it again), and
  the fake shows no credential or message ever reached it.
- **A partial refusal is still a send.** ``smtplib`` returns the refused
  recipients instead of raising when some were accepted; treating that as a
  failure would send the whole letter again.
"""

import pytest

from app.adapters.mail import smtp_client
from app.adapters.mail.errors import MailDeliveryFailed
from app.adapters.mail.smtp_client import SmtpClient
from app.domain.mail_send import DeliveryFailure
from tests.fake_smtp import FakeSmtpServer, trusting_context

PASSWORD_MARKER = "app-password-never-logged"
MESSAGE_ID = "<20260925090000.abcdef@tcsp.example>"
SENDER = "ai-filings@tcsp.example"
RECIPIENTS = ["director@brightlantern.example.hk", "secretary@brightlantern.example.hk"]


def draft_bytes() -> bytes:
    """A byte-complete RFC 5322 draft, the way the M3 generator composes one.

    Written as the literal bytes the wire carries — CRLF line endings, a
    non-ASCII line and a line that starts with a dot (which the transport must
    stuff and the fake must un-stuff) — rather than round-tripped through a
    parser, so the comparison after the transfer is exact.
    """
    return (
        b"From: ai-filings@tcsp.example\r\n"
        b"To: director@brightlantern.example.hk, secretary@brightlantern.example.hk\r\n"
        b"Subject: [Bright Lantern Design Limited \xe2\x80\x94 CR 1234567] "
        b"Annual Return Confirmation\r\n"
        b"Date: Thu, 25 Sep 2026 09:00:00 +0800\r\n"
        b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n"
        b"\r\n"
        b"Dear Sir or Madam\r\n"
        b"\xe6\x95\xac\xe5\x95\x9f\xe8\x80\x85\r\n"
        b"\r\n"
        b". leading dot survives\r\n"
    )


def client_for(server: FakeSmtpServer, *, timeout: float = 5.0) -> SmtpClient:
    return SmtpClient(
        host="localhost",
        port=server.port,
        username=server.username,
        password=server.password,
        timeout=timeout,
    )


@pytest.fixture(autouse=True)
def tls_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the fixture certificate, and only it.

    The seam is the client's own ``tls_context`` factory; hostname checking
    stays on (the fixture certificate carries ``localhost``), so the client
    verifies its server exactly as a deployment would.
    """
    monkeypatch.setattr(smtp_client, "tls_context", trusting_context)


async def test_a_send_reaches_the_server_byte_for_byte() -> None:
    raw = draft_bytes()
    with FakeSmtpServer(password=PASSWORD_MARKER) as server:
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=raw
        )

    assert server.starttls_sessions == 1
    assert server.authenticated == 1
    assert len(server.messages) == 1
    received = server.messages[0]
    assert received.sender == SENDER
    assert received.recipients == RECIPIENTS
    # The bytes the mailbox files are the bytes the records registry holds,
    # dot-stuffing undone and the trailing line ending intact.
    assert received.data == raw
    assert b"Message-ID: " + MESSAGE_ID.encode() in received.data


async def test_the_credential_and_the_message_are_both_after_the_upgrade() -> None:
    with FakeSmtpServer(password=PASSWORD_MARKER) as server:
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    auth_index = next(i for i, line in enumerate(server.commands) if line.startswith("AUTH"))
    starttls_index = server.commands.index("STARTTLS")
    mail_index = next(i for i, line in enumerate(server.commands) if line.startswith("MAIL"))
    assert starttls_index < auth_index < mail_index


async def test_a_server_without_starttls_is_refused_and_told_nothing() -> None:
    """Fail closed: the app password never reaches a plaintext session."""
    with (
        FakeSmtpServer(advertise_starttls=False, password=PASSWORD_MARKER) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "starttls_unavailable"
    assert server.authenticated == 0
    assert server.messages == []
    assert all(not line.startswith("AUTH") for line in server.commands)


async def test_a_wrong_app_password_is_permanent_and_sends_nothing() -> None:
    with FakeSmtpServer() as server:
        client = SmtpClient(
            host="localhost",
            port=server.port,
            username=server.username,
            password="wrong-password",
            timeout=5.0,
        )
        with pytest.raises(MailDeliveryFailed) as caught:
            await client.send_message(sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes())

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "smtp_535"
    assert server.messages == []


async def test_an_unreachable_server_is_transient() -> None:
    """Nothing was transmitted, so this is a retry — not a lost message."""
    server = FakeSmtpServer().start()
    port = server.port
    server.stop()

    client = SmtpClient(
        host="localhost", port=port, username="u", password="p", timeout=2.0
    )
    with pytest.raises(MailDeliveryFailed) as caught:
        await client.send_message(sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes())

    assert caught.value.failure is DeliveryFailure.TRANSIENT
    assert caught.value.reason.startswith("unavailable:")


async def test_a_permanent_refusal_of_the_message_is_permanent_and_named() -> None:
    with (
        FakeSmtpServer(data_status=552) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
        sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "smtp_552"


async def test_a_deferred_message_is_transient() -> None:
    """A 4xx at DATA — greylisting, a full mailbox — can clear."""
    with (
        FakeSmtpServer(data_status=451) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
        sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    assert caught.value.failure is DeliveryFailure.TRANSIENT
    assert caught.value.reason == "smtp_451"


async def test_refused_recipients_take_their_own_code() -> None:
    with (
        FakeSmtpServer(recipient_status=550) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    assert caught.value.failure is DeliveryFailure.PERMANENT
    assert caught.value.reason == "smtp_550"
    assert server.messages == []


async def test_a_connection_lost_around_the_transfer_is_unconfirmed() -> None:
    """The residual the contract states: the server has the message and the
    client cannot know it. Reported as neither sent nor refused."""
    raw = draft_bytes()
    with (
        FakeSmtpServer(drop_after_data=True) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=raw
        )

    assert caught.value.failure is DeliveryFailure.UNCONFIRMED
    assert caught.value.reason.startswith("disconnected:")
    # The message is really there — which is why the contract says a duplicate
    # is possible and its Message-ID is what makes it detectable.
    assert [message.data for message in server.messages] == [raw]


async def test_a_stalled_server_ends_the_attempt() -> None:
    with (
        FakeSmtpServer(silent=True) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server, timeout=0.5).send_message(
        sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    assert caught.value.failure in {DeliveryFailure.TRANSIENT, DeliveryFailure.UNCONFIRMED}
    assert server.messages == []


async def test_no_error_carries_the_app_password_or_the_message_id() -> None:
    """Custody: a raised error is safe to log — a code and a reason, never a
    credential and never a fragment of the letter."""
    with (
        FakeSmtpServer(password=PASSWORD_MARKER, data_status=552) as server,
        pytest.raises(MailDeliveryFailed) as caught,
    ):
        await client_for(server).send_message(
            sender=SENDER, recipients=RECIPIENTS, raw=draft_bytes()
        )

    text = f"{caught.value} {caught.value.reason}"
    assert PASSWORD_MARKER not in text
    assert MESSAGE_ID not in text
    assert "Bright Lantern" not in text
