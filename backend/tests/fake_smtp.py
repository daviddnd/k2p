"""A minimal SMTP server, on loopback, that speaks the real protocol.

The SMTP leg is the ADR-0005 fallback for a TCSP without a provider API, and
the only way to verify a client that uses the standard library's ``smtplib``
is to put a server on the other end of a socket. This one is deliberately
small — greeting, ESMTP greeting, ``STARTTLS``, ``AUTH``, ``MAIL FROM``,
``RCPT TO``, ``DATA``, ``QUIT`` — and deliberately faithful in the parts that
matter: it upgrades the connection with a real TLS handshake before it accepts
a credential, it reads the message until the end-of-data marker and
un-dot-stuffs it, and it answers with real reply codes.

The certificate under ``tests/fixtures/smtp-tls/`` is **test-only key
material**: a self-signed pair for ``localhost``, generated for this fake and
used by nothing else. It protects nothing — it exists so the handshake, the
authentication and the message transfer are exercised for real instead of
being stubbed out — and the client trusts it through the ``tls_context`` seam
its own module documents. Nothing here is reachable off the machine: the
server binds ``127.0.0.1`` on an ephemeral port.

The levers stage what a deployment meets: a server that does not offer
``STARTTLS`` at all (the upgrade is not optional — a client that continued
anyway would be silently downgraded), a wrong app password, a temporary (4xx)
and a permanent (5xx) refusal, and the one that matters most: **the connection
dropped after the message body, before the answer**. That is the ambiguity the
outbox's SMTP residual is about — the server has the message and the
deployment does not know — and it is why the client reports ``unconfirmed``.
"""

import base64
import contextlib
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "smtp-tls"
CERTIFICATE = FIXTURES / "server-cert.pem"
PRIVATE_KEY = FIXTURES / "server-key.pem"

CRLF = b"\r\n"
END_OF_DATA = b"\r\n.\r\n"


@dataclass
class FakeMessage:
    """One message the server accepted, as the wire carried it."""

    sender: str
    recipients: list[str]
    data: bytes


@dataclass
class FakeSmtpServer:
    """An SMTP server on loopback with the levers a sending test needs."""

    username: str = "ai-filings@tcsp.example"
    password: str = "app-password"
    # OFF means the server advertises no STARTTLS; the client must refuse to
    # continue rather than hand a credential to a plaintext session.
    advertise_starttls: bool = True
    # Reply codes: ``data_status`` answers the message body, and
    # ``recipient_status`` the RCPT command (a 5xx refuses every recipient, a
    # 4xx defers them — the two classifications the client has to tell apart).
    data_status: int = 250
    recipient_status: int = 250
    # The ambiguous case: the body is read and then the socket is closed
    # without an answer, so the server holds what the client cannot confirm.
    drop_after_data: bool = False
    # A server that never answers at all (the client's own timeout is what ends
    # it).
    silent: bool = False

    # What the fake saw.
    sessions: int = 0
    authenticated: int = 0
    starttls_sessions: int = 0
    commands: list[str] = field(default_factory=list)
    messages: list[FakeMessage] = field(default_factory=list)
    error: str | None = None

    _listener: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    # --- lifecycle -------------------------------------------------------

    def __enter__(self) -> "FakeSmtpServer":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self) -> "FakeSmtpServer":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.2)
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._listener is not None:
            self._listener.close()
        self._thread = None
        self._listener = None

    @property
    def port(self) -> int:
        assert self._listener is not None, "the server is not running"
        return int(self._listener.getsockname()[1])

    # --- the server loop -------------------------------------------------

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:  # the listener was closed under us
                return
            self.sessions += 1
            try:
                self._session(connection)
            except (OSError, ssl.SSLError) as exc:
                # A client that hung up is not this fake's failure; the
                # session is over either way and the tests assert on what
                # was accepted before it did.
                self.error = f"{type(exc).__name__}"
            finally:
                with contextlib.suppress(OSError):
                    connection.close()

    def _session(self, connection: socket.socket) -> None:
        connection.settimeout(5)
        stream = _Stream(connection)
        if self.silent:
            # Read until the client gives up; no greeting, no answer.
            with contextlib.suppress(OSError):
                while connection.recv(4096):
                    pass
            return
        stream.send("220 fake ESMTP ready")
        sender = ""
        recipients: list[str] = []
        authenticated = False
        while True:
            line = stream.readline()
            if line is None:
                return
            command, _, argument = line.partition(" ")
            command = command.upper()
            self.commands.append(f"{command} {argument}" if argument else command)
            if command in {"EHLO", "HELO"}:
                features = ["PIPELINING"]
                if self.advertise_starttls and not stream.encrypted:
                    features.append("STARTTLS")
                if stream.encrypted or not self.advertise_starttls:
                    features.append("AUTH PLAIN LOGIN")
                stream.send_many(
                    f"250-fake greets {argument}",
                    *[f"250-{feature}" for feature in features],
                    "250 SIZE 25000000",
                )
            elif command == "STARTTLS":
                if not self.advertise_starttls:
                    stream.send("502 command not implemented")
                    continue
                stream.send("220 ready to start TLS")
                stream.upgrade()
                self.starttls_sessions += 1
            elif command == "AUTH":
                authenticated = self._authenticate(stream, argument)
                if authenticated:
                    self.authenticated += 1
            elif command == "MAIL":
                sender = _address(argument)
                stream.send("250 sender ok")
            elif command == "RCPT":
                if 200 <= self.recipient_status < 300:
                    recipients.append(_address(argument))
                    stream.send(f"{self.recipient_status} recipient ok")
                else:
                    stream.send(f"{self.recipient_status} recipient refused")
            elif command == "DATA":
                stream.send("354 end data with <CRLF>.<CRLF>")
                body = stream.read_until(END_OF_DATA)
                if body is None:
                    return
                self.messages.append(
                    FakeMessage(
                        sender=sender, recipients=list(recipients), data=_unstuff(body)
                    )
                )
                if self.drop_after_data:
                    return  # the message is here; the client never hears so
                stream.send(f"{self.data_status} message accepted")
            elif command == "RSET":
                sender, recipients = "", []
                stream.send("250 reset")
            elif command == "NOOP":
                stream.send("250 ok")
            elif command == "QUIT":
                stream.send("221 bye")
                return
            else:
                stream.send("502 command not implemented")

    def _authenticate(self, stream: "_Stream", argument: str) -> bool:
        mechanism, _, payload = argument.partition(" ")
        mechanism = mechanism.upper()
        if mechanism == "PLAIN":
            decoded = _b64(payload or (stream.readline() or ""))
            # ``[authzid] NUL authcid NUL passwd``: the authorization identity
            # is optional, and ``smtplib`` sends it empty, so two separators
            # are the ordinary case rather than a malformed one.
            fields = [] if decoded is None else decoded.split("\x00")
            if len(fields) == 3:
                _, username, password = fields
            elif len(fields) == 2:
                username, password = fields
            else:
                stream.send("501 malformed authentication")
                return False
            return self._accept(stream, username, password)
        if mechanism == "LOGIN":
            # The client may carry the username as the mechanism's initial
            # response instead of waiting for the prompt.
            username = _b64(payload) if payload else None
            if username is None:
                stream.send("334 VXNlcm5hbWU6")
                username = _b64(stream.readline() or "")
            stream.send("334 UGFzc3dvcmQ6")
            password = _b64(stream.readline() or "")
            if username is None or password is None:
                stream.send("501 malformed authentication")
                return False
            return self._accept(stream, username, password)
        stream.send("504 mechanism not supported")
        return False

    def _accept(self, stream: "_Stream", username: str, password: str) -> bool:
        if username == self.username and password == self.password:
            stream.send("235 authentication successful")
            return True
        stream.send("535 authentication failed")
        return False


class _Stream:
    """One SMTP session's byte stream, plaintext or upgraded."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = b""
        self.encrypted = False
        self._reader = connection.makefile("rb")

    def send(self, line: str) -> None:
        self._connection.sendall(line.encode("utf-8") + CRLF)

    def send_many(self, *lines: str) -> None:
        for line in lines:
            self.send(line)

    def readline(self) -> str | None:
        raw = self._reader.readline()
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def read_until(self, marker: bytes) -> bytes | None:
        """Read up to and including ``marker`` (the body's end-of-data line).

        ``read1`` rather than ``read``: the buffered reader's ``read`` blocks
        until it has the whole count, which would hang on the last partial
        chunk of a message; ``read1`` returns what has arrived.
        """
        chunks = [self._buffer]
        self._buffer = b""
        while True:
            raw = self._reader.read1(4096)
            if not raw:
                return None
            chunks.append(raw)
            joined = b"".join(chunks)
            index = joined.find(marker)
            if index != -1:
                self._buffer = joined[index + len(marker) :]
                # The CRLF before the terminator's dot belongs to the message
                # — ``smtplib`` sends ``…body… CRLF . CRLF`` — so it is put
                # back: the bytes this fake holds are the bytes that were
                # written, not a version of them with the last line ending
                # eaten by the framing.
                return joined[:index] + CRLF

    def upgrade(self) -> None:
        """Wrap the socket, server side, with the fixture's certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(CERTIFICATE), keyfile=str(PRIVATE_KEY))
        self._reader.close()
        self._connection = context.wrap_socket(self._connection, server_side=True)
        self._reader = self._connection.makefile("rb")
        self.encrypted = True


def _address(argument: str) -> str:
    """The bare address out of ``FROM:<a@b>`` / ``TO:<a@b> SIZE=37``.

    ESMTP parameters follow the address (``SIZE``, ``BODY``), so the address is
    taken out of its angle brackets when it has them rather than by splitting
    on the colon.
    """
    value = argument.split(":", 1)[-1].strip()
    if value.startswith("<"):
        return value[1:].split(">", 1)[0]
    return value.split()[0] if value else ""


def _unstuff(body: bytes) -> bytes:
    """Undo SMTP's dot-stuffing and hand back the message as it was written."""
    text = body.replace(b"\r\n..", b"\r\n.")
    if text.startswith(b".."):
        text = text[1:]
    return text


def _b64(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return None


def trusting_context() -> ssl.SSLContext:
    """A client context that trusts the fixture certificate, and nothing else.

    ``check_hostname`` stays on: the fake is reached as ``localhost`` and its
    certificate carries that name (and ``127.0.0.1``) as a subject alternative
    name, so the verification the production context performs is really
    exercised rather than switched off.
    """
    return ssl.create_default_context(cafile=str(CERTIFICATE))
