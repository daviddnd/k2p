"""A faithful stand-in for the IMAP server the read fallback talks to.

The commands and shapes are copied from RFC 3501 rather than from the
application, so a client that satisfies this fake would satisfy a real server:
``LOGIN`` (the app-password form the fallback uses), ``EXAMINE`` — a read-only
select, whose ``* OK [UIDVALIDITY n]`` the client reads through
``untagged_responses`` exactly as ``imaplib`` files it — ``UID SEARCH`` with a
day-granular ``SINCE`` or a ``HEADER`` criterion, ``UID FETCH`` for one
message, and ``LOGOUT``.

Three properties make it refuse wrong usage rather than tolerate it:

- **Only the fetched fields come back.** A
  ``BODY.PEEK[HEADER.FIELDS (…)]`` fetch answers with exactly the named
  headers — the stored lines themselves, folding included, so the client's
  unfold rule is exercised — and a blank line, so a client that forgot to ask
  for ``Message-ID`` gets an envelope without one, as a real server would give
  it;
- **A body fetch without PEEK marks the message ``\\Seen``** (RFC 3501
  §6.4.5), which the fake records *and* refuses: the client must never ask for
  one, and a test asserting "no message was marked seen" is then a real
  detector rather than an accident;
- **A command that changes the mailbox is refused.** ``STORE``, ``EXPUNGE``,
  ``MOVE``, ``COPY``, ``APPEND``, ``CREATE`` and ``DELETE`` all raise, and are
  recorded besides. The fallback reads a mailbox it never writes.

A ``SEARCH`` answers with the matching UIDs ascending — the order a mailbox
assigns them — and ``FETCH`` in the shape ``imaplib`` parses: a
literal-carrying tuple whose first element is the response prefix (the UID and
the ``INTERNALDATE``) and whose second is the fetched bytes.

Refusals are ``imaplib``'s own exception types, because that is what the
adapter catches and what a real conversation raises.

No socket: the fake is one object a test drives directly, wired in place of the
adapter's connection factory (``tests/test_imap_client.py``). The mailbox and
its counters live on the server object, so a connection that is opened, used
and logged out leaves the record the test reads.
"""

import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from app.domain.mail import imap_since_day

# The mailbox the product opens, and the one this fake holds.
MAILBOX = "INBOX"

# The fake's clock for messages without an explicit receipt time: one minute
# apart, so ordering and cursors are deterministic.
DEFAULT_RECEIVED_BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

_HEADER_FIELDS = re.compile(r"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]", re.IGNORECASE)
_FULL_FETCH = re.compile(r"BODY\.PEEK\[\]", re.IGNORECASE)
_UNPEEKED_BODY = re.compile(r"BODY\[", re.IGNORECASE)
_WRITER_COMMANDS = ("STORE", "EXPUNGE", "MOVE", "COPY", "APPEND", "CREATE", "DELETE", "CLOSE")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass
class _Stored:
    """One message in the fake's mailbox."""

    uid: int
    raw: bytes
    received_at: datetime

    def fields(self, names: list[str]) -> bytes:
        """Only the named headers, as a real server's ``HEADER.FIELDS`` answer.

        The lines are the stored message's own — folding included, exactly the
        bytes a real server passes through — so the client's unfold rule is
        what the answer is tested against, and a field the client did not name
        is not in the answer at all.
        """
        wanted = [name.casefold() for name in names]
        lines = [
            f"{name}: {value}"
            for name, value in BytesParser().parsebytes(self.raw).items()
            if name.casefold() in wanted
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    def header(self, field: str) -> str:
        """One header's stored value, for a ``HEADER`` search's substring match."""
        value = BytesParser().parsebytes(self.raw).get(field)
        return "" if value is None else " ".join(str(value).split())


class FakeImap:
    """The server: a mailbox, a UIDVALIDITY, and every command it was asked."""

    def __init__(
        self,
        *,
        username: str = "ai-filings@tcsp.example",
        password: str = "app-password-marker",
        uidvalidity: int = 1234,
        received_base: datetime = DEFAULT_RECEIVED_BASE,
    ) -> None:
        self.username = username
        self.password = password
        self.uidvalidity = uidvalidity
        self.received_base = received_base
        self.messages: dict[int, _Stored] = {}

        # Levers, for the failures a test stages.
        self.search_status = "OK"
        self.fetch_status = "OK"
        self.malformed_fetch = False
        self.refuse_login = False
        self.internal_date_override: str | None = None

        # What the client did, in one place. The password is deliberately not
        # among the records: the login is recorded as the account, exactly as a
        # deployment's own record of a conversation would be.
        self.commands: list[str] = []
        self.logins: list[str] = []
        self.logouts = 0
        self.selects: list[tuple[str, bool]] = []
        self.searches: list[tuple[str, ...]] = []
        self.fetches: list[tuple[int, str]] = []
        self.seen: set[int] = set()
        self.writes: list[str] = []
        # Every factory call, with the transport the client asked for: a test
        # can assert that a deployment was not offered a cleartext connection.
        self.connections: list[tuple[str, int, bool]] = []

    # --- Staging ---------------------------------------------------------

    def add_message(
        self,
        *,
        subject: str | None = "A client reply",
        sender: str | None = "CHAN TAI MAN <director@client.example>",
        date: str | None = "Tue, 23 Sep 2026 09:31:00 +0800",
        body: str | None = "body of the next message",
        message_id: str | None = None,
        received_at: datetime | None = None,
        raw: bytes | None = None,
    ) -> int:
        """Add one message and return its UID (ascending, as a server assigns).

        ``message_id`` defaults to one unique to the message, the way a real
        sender's would be; ``None`` cannot mean "no header" without making the
        ordinary call ambiguous, so a message that carries none is staged with
        ``raw``.
        """
        uid = len(self.messages) + 1
        if raw is None:
            message = EmailMessage()
            message["Message-ID"] = (
                f"<imap-{uid}-at-the-fake@client.example>" if message_id is None else message_id
            )
            if subject is not None:
                message["Subject"] = subject
            if sender is not None:
                message["From"] = sender
            message["To"] = self.username
            if date is not None:
                message["Date"] = date
            message.set_content(body if body is not None else "")
            raw = message.as_bytes(policy=policy.SMTP)
        self.messages[uid] = _Stored(
            uid=uid,
            raw=raw,
            received_at=received_at or self.received_base + timedelta(minutes=uid - 1),
        )
        return uid

    def raw_of(self, uid: int) -> bytes:
        """The bytes the fake serves for one message (what a client would store)."""
        return self.messages[uid].raw

    def identity_of(self, uid: int) -> str:
        """The id a client has to read this message back under.

        Computed here from the stored bytes — the ``Message-ID`` header as both
        legs speak it (angle brackets kept), or the UIDVALIDITY-namespaced
        fallback — rather than from the client's own rule, so a test asserting
        an identity is not asserting the client's arithmetic back at it.

        The one part of the identity rule this helper mirrors is its *shape*:
        a header claiming the reserved ``imap-uid:`` namespace, or one longer
        than the narrowest column that carries an id, is not an identity
        either (``app.domain.mail.message_identity``). Tests that stage such a
        header assert the expected UID id literally, so the rule itself is
        pinned by the domain suite rather than by this helper.
        """
        value = self.messages[uid].header("Message-ID")
        folded = "".join(value.split())
        if not folded or folded.startswith("imap-uid:") or len(folded) > 128:
            return f"imap-uid:{self.uidvalidity}:{uid}"
        return folded

    # --- The connection factory ------------------------------------------

    def connect(
        self, *, host: str, port: int, timeout: float, allow_plaintext: bool
    ) -> "_Connection":
        """What the adapter's factory returns: one conversation with this server.

        The signature mirrors the real factory's — ``allow_plaintext``
        included, so a test can see which transport the client asked for — and
        a test wires this in with
        ``monkeypatch.setattr(imap_client, "connect", fake.connect)``. The fake
        opens no socket: it stands in for whatever the real factory would have
        returned, so nothing here is cleartext.
        """
        self.commands.append(f"CONNECT {host}:{port}")
        self.connections.append((host, port, allow_plaintext))
        return _Connection(self)


class _Connection:
    """One ``imaplib``-shaped conversation.

    Per-connection state (logged in, selected, the untagged responses a
    ``SELECT`` reported) lives here; the mailbox and every counter live on the
    server.
    """

    def __init__(self, server: FakeImap) -> None:
        self._server = server
        self.state = "NONAUTH"
        self.untagged_responses: dict[str, list[bytes]] = {}
        self.is_readonly = False

    # --- The commands the fallback uses ----------------------------------

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._server.commands.append("LOGIN")
        self._server.logins.append(user)
        if (
            self._server.refuse_login
            or user != self._server.username
            or password != self._server.password
        ):
            # ``imaplib`` raises with the server's own response line, which is
            # where a response code like ``AUTHENTICATIONFAILED`` lives.
            raise imaplib.IMAP4.error(b"NO [AUTHENTICATIONFAILED] refuse for the fake")
        self.state = "AUTH"
        return "OK", [b"logged in"]

    def select(self, mailbox: str = MAILBOX, readonly: bool = False) -> tuple[str, list[bytes]]:
        self._server.commands.append(f"{'EXAMINE' if readonly else 'SELECT'} {mailbox}")
        self._server.selects.append((mailbox, readonly))
        if mailbox != MAILBOX:
            return "NO", [b"no such mailbox"]
        self.untagged_responses = {
            "EXISTS": [str(len(self._server.messages)).encode()],
            "UIDVALIDITY": [str(self._server.uidvalidity).encode()],
        }
        self.is_readonly = readonly
        self.state = "SELECTED"
        return "OK", [str(len(self._server.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        """``imaplib``'s own reader: the value once, then nothing (it pops)."""
        data = self.untagged_responses.pop(code.upper(), None)
        return code.upper(), list(data) if data is not None else [None]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        command = command.upper()
        if command in _WRITER_COMMANDS:
            self._server.writes.append(f"{command} {' '.join(str(arg) for arg in args)}")
            raise imaplib.IMAP4.error(f"the fallback must never send {command}")
        if command == "SEARCH":
            return self._search(*args)
        if command == "FETCH":
            return self._fetch(*args)
        raise imaplib.IMAP4.error(f"unknown UID command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self._server.commands.append("LOGOUT")
        self._server.logouts += 1
        self.state = "LOGOUT"
        return "BYE", [b"bye"]

    # --- The two commands, faithfully ------------------------------------

    def _search(self, *criteria: str) -> tuple[str, list[bytes]]:
        self._server.commands.append(f"UID SEARCH {' '.join(criteria)}")
        self._server.searches.append(tuple(criteria))
        if self._server.search_status != "OK":
            return self._server.search_status, [b"search refused by the fake"]
        matched = self._matched(criteria)
        return "OK", [" ".join(str(uid) for uid in matched).encode()]

    def _matched(self, criteria: tuple[str, ...]) -> list[int]:
        """The UIDs a real server answers, for the criteria this product sends."""
        if not criteria:
            raise imaplib.IMAP4.error("SEARCH without criteria")
        head, *rest = criteria
        if head.upper() == "ALL":
            return sorted(self._server.messages)
        if head.upper() == "SINCE":
            # RFC 3501: the internal date, disregarding time and timezone.
            day = _as_date(rest[0])
            return sorted(
                uid
                for uid, stored in self._server.messages.items()
                if stored.received_at.date() >= day
            )
        if head.upper() == "HEADER" and len(rest) >= 2:
            field, wanted = rest[0], rest[1].strip('"')
            return sorted(
                uid
                for uid, stored in self._server.messages.items()
                if wanted.casefold() in stored.header(field).casefold()
            )
        raise imaplib.IMAP4.error(f"the product does not search by {' '.join(criteria)}")

    def _fetch(self, uid: str, spec: str) -> tuple[str, list[Any]]:
        if not str(uid).isdigit():
            raise imaplib.IMAP4.error(f"fetch needs a UID, not {uid!r}")
        number = int(uid)
        stored = self._server.messages.get(number)
        if stored is None:
            return "NO", [b"no such message"]
        if self._server.fetch_status != "OK":
            return self._server.fetch_status, [b"fetch refused by the fake"]
        if _UNPEEKED_BODY.search(spec):
            # A body fetch without PEEK: the real server sets \Seen on it, and
            # this client must never ask for one. Recorded *and* refused.
            self._server.seen.add(number)
            raise imaplib.IMAP4.error(f"fetch without PEEK would mark \\Seen: {spec}")
        self._server.commands.append(f"UID FETCH {number} {spec}")
        if self._server.malformed_fetch:
            return "OK", [b""]
        fields = _HEADER_FIELDS.search(spec)
        if fields is not None:
            body = stored.fields(fields.group(1).split())
            kind, label = "headers", f"BODY[HEADER.FIELDS ({fields.group(1)})]"
        elif _FULL_FETCH.search(spec):
            body, kind, label = stored.raw, "full", "BODY[]"
        else:
            raise imaplib.IMAP4.error(f"the product does not fetch {spec}")
        self._server.fetches.append((number, kind))
        internal_date = (
            self._server.internal_date_override
            if self._server.internal_date_override is not None
            else _internal_date(stored.received_at)
        )
        prefix = (
            f'{number} (UID {number} INTERNALDATE "{internal_date}" '
            f"{label} {{{len(body)}}}"
        ).encode()
        return "OK", [(prefix, body), b")"]


def _internal_date(value: datetime) -> str:
    """The server's own ``INTERNALDATE`` rendering of a receipt time."""
    return f"{imap_since_day(value)} {value:%H:%M:%S} {value:%z}"


def _as_date(value: str) -> date:
    """An IMAP ``SINCE`` day (``19-Sep-2026``) as a date, locale-free."""
    day, month, year = value.split("-")
    return date(int(year), _MONTHS.index(month) + 1, int(day))
