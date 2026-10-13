"""The SMTP leg: the same mailbox, with an app password (ADR-0005 path A).

ADR-0005's fallback for a TCSP without a provider API, and the pilot path:
SMTP to the TCSP's own mailbox with an application password, no OAuth client,
no Google verification. One send per call — connect, upgrade, authenticate,
hand over the message, quit.

**The credential is deployment configuration, never a row.** The address and
the app password come from the environment (``AICS_MAIL_ADDRESS``,
``AICS_MAIL_APP_PASSWORD``, ``AICS_SMTP_HOST``/``PORT``) under ADR-0014's
custody rule for the same reason the Gmail refresh token lives in a 0600 file:
a credential in the database travels in every dump and replica. Nothing here
formats a password, an address or a message body into an error — a raised
error names the call and a provider code, and nothing else.

**The upgrade is not optional.** A server that does not advertise ``STARTTLS``
is refused before any credential is offered, because continuing in the clear is
exactly the silent downgrade a mail-relay attacker would arrange. TLS is
verified with ``ssl.create_default_context`` — certificate chain *and*
hostname — through the ``tls_context`` seam (a test replaces it with a context
that trusts its own fixture certificate; production gets the verifying
default).

**The message is handed over as bytes.** The stored ``message/rfc822`` draft is
what goes on the wire, so what the client's mailbox files is what the records
registry holds, ``Message-ID`` included.

The failure classification is the domain's (``app.domain.mail_send``) and the
wire's own knowledge decides it: a connection that never came up, a TLS or
authentication step that failed, and a 4xx reply are *transient* — nothing was
accepted and another attempt can work; a 5xx reply is *permanent* —
``smtplib``'s own rule that 5xx is a refusal the server will repeat; and a
connection **lost around the message transfer** is *unconfirmed*, because the
server may already have taken the message. That last one is the SMTP leg's
stated residual: it has no Sent lookup to reconcile with, so a later attempt
can duplicate, and the RFC 5322 ``Message-ID`` is what makes the duplicate
detectable in the mailbox (contract §5.1.11).

A **partial** refusal is a send that happened: ``smtplib`` reports it as a
return value rather than an error when some recipients were accepted, the
providers' own bounce reporting is what tells staff about the rest, and calling
it a failure would make the sender send the letter again to everybody.
"""

import asyncio
import contextlib
import smtplib
import ssl
from collections.abc import Sequence

from app.adapters.mail.errors import MailDeliveryFailed
from app.domain.mail_send import DeliveryFailure

# What the socket waits for one step of the conversation. The client's whole
# send is bounded by a few of these; a provider that is merely slow must not
# hold a request open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 15.0


def tls_context() -> ssl.SSLContext:
    """The context the SMTP leg verifies its server with (the seam tests replace).

    ``create_default_context`` checks the certificate chain **and** the
    hostname; nothing here weakens either, so a deployment cannot be silently
    pointed at an unverified relay. A test replaces this with a context that
    trusts ``tests/fixtures/smtp-tls``, which is the only way to exercise the
    handshake without reaching a real provider.
    """
    return ssl.create_default_context()


class SmtpClient:
    """SMTP over the standard library, one message per call.

    ``smtplib`` is blocking, so the whole conversation runs in a worker thread
    (``asyncio.to_thread``) and the event loop is free while a client's letter
    is handed over. The socket timeout is the standard library's own, so a
    stalled server ends the attempt instead of holding it.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout

    async def send_message(
        self, *, sender: str, recipients: Sequence[str], raw: bytes
    ) -> None:
        """Hand one composed message to the provider's submission service.

        Raises ``MailDeliveryFailed`` with the classification the module
        docstring states; returns ``None`` when the server accepted the
        message for at least one recipient.
        """
        try:
            # The thread's own socket timeout ends a stalled conversation; this
            # outer bound is the backstop for the thread itself (a DNS lookup
            # that ignores the socket timeout, for instance).
            async with asyncio.timeout(self._timeout * 2):
                await asyncio.to_thread(
                    self._send_blocking,
                    sender=sender,
                    recipients=list(recipients),
                    raw=raw,
                )
        except TimeoutError as exc:
            raise MailDeliveryFailed(
                failure=DeliveryFailure.UNCONFIRMED, reason="timed_out"
            ) from exc

    def _send_blocking(self, *, sender: str, recipients: Sequence[str], raw: bytes) -> None:
        """The whole conversation, on one connection, in the blocking world."""
        try:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        except (OSError, smtplib.SMTPException) as exc:
            # The connection never came up: nothing was transmitted, so this is
            # a retry rather than a lost message.
            raise MailDeliveryFailed(
                failure=DeliveryFailure.TRANSIENT,
                reason=f"unavailable:{type(exc).__name__}",
            ) from exc
        try:
            self._upgrade_and_authenticate(client)
            self._hand_over(client, sender=sender, recipients=recipients, raw=raw)
        finally:
            with contextlib.suppress(OSError, smtplib.SMTPException):
                client.quit()

    def _upgrade_and_authenticate(self, client: smtplib.SMTP) -> None:
        """EHLO, STARTTLS, EHLO, AUTH — or a typed failure.

        Everything in this phase happens before the message, so every way it
        can fail is *transient* except a server that refuses protocol steps in
        a way it will refuse again (a 5xx reply, and the missing-STARTTLS
        case, which is a permanent refusal of this deployment's policy).
        """
        try:
            client.ehlo()
            if not client.has_extn("starttls"):
                # Fail closed: a plaintext session must never carry the
                # app password or a client's letter.
                raise MailDeliveryFailed(
                    failure=DeliveryFailure.PERMANENT, reason="starttls_unavailable"
                )
            client.starttls(context=tls_context())
            client.ehlo()
            client.login(self._username, self._password)
        except MailDeliveryFailed:
            raise
        except smtplib.SMTPResponseException as exc:
            # A reply code from the server: 5xx is a refusal it will repeat,
            # 4xx a condition that can clear (this is where a rejected
            # credential lands, as 535).
            raise MailDeliveryFailed(
                failure=_failure_for_code(exc.smtp_code), reason=f"smtp_{exc.smtp_code}"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # A handshake or connection that failed without a code. Nothing has
            # been sent, so the attempt can be made again.
            raise MailDeliveryFailed(
                failure=DeliveryFailure.TRANSIENT,
                reason=f"tls_refused:{type(exc).__name__}",
            ) from exc

    def _hand_over(
        self, client: smtplib.SMTP, *, sender: str, recipients: Sequence[str], raw: bytes
    ) -> None:
        """MAIL FROM, RCPT TO, DATA — or a typed failure.

        The distinction that matters here is whether the message may have been
        accepted: a reply code says it was not (and 5xx says it never will be),
        while a connection lost in the middle of the transfer says nothing.
        """
        try:
            client.sendmail(sender, list(recipients), raw)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            raise MailDeliveryFailed(
                failure=_failure_for_code(min(codes) if codes else None),
                reason=f"smtp_{min(codes)}" if codes else "recipients_refused",
            ) from exc
        except smtplib.SMTPResponseException as exc:
            raise MailDeliveryFailed(
                failure=_failure_for_code(exc.smtp_code), reason=f"smtp_{exc.smtp_code}"
            ) from exc
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            # The connection went away around the transfer: the server may
            # have accepted the message before it did. This is the one answer
            # that must not be guessed, and it is the SMTP leg's stated
            # residual — there is no Sent lookup to reconcile with.
            raise MailDeliveryFailed(
                failure=DeliveryFailure.UNCONFIRMED,
                reason=f"disconnected:{type(exc).__name__}",
            ) from exc
        except smtplib.SMTPException as exc:
            raise MailDeliveryFailed(
                failure=DeliveryFailure.UNCONFIRMED,
                reason=f"protocol_error:{type(exc).__name__}",
            ) from exc


def _failure_for_code(code: int | None) -> DeliveryFailure:
    """SMTP's own rule: 4xx is a condition that may clear, 5xx is a refusal.

    ``None`` — an error with no code at all in the phase that has no message
    in flight — is transient: the caller's retry is cheap and nothing was
    accepted.
    """
    if code is None or 400 <= code < 500:
        return DeliveryFailure.TRANSIENT
    return DeliveryFailure.PERMANENT
