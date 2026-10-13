"""IMAP over the standard library, with the app password (ADR-0005 path A).

The read half of ADR-0005's fallback: a TCSP whose mailbox has no provider API
to call — corporate Exchange, self-hosted mail — points the ingestion cycle at
the same dedicated mailbox over IMAP, with the address and the app password the
SMTP leg sends with. Three reads, one per step of the cycle: the window's
message ids, one message's envelope, and one message's bytes.

**Read-only, by two mechanisms.** The mailbox is opened ``EXAMINE`` (never
``SELECT``) *and* every body read is ``BODY.PEEK[…]``: the mailbox is the
TCSP's own and its staff keep direct access to it (ADR-0005), so nothing this
module does may mark a message seen, move it, or delete it. ``LOGOUT`` closes
the conversation — deliberately not ``CLOSE``, which expunges messages flagged
``\\Deleted``.

**The identity is the envelope's.** A window is selected in two steps because
IMAP's ``SINCE`` is day-granular and compares the internal date *disregarding
time and timezone*: the search starts at the day of the cursor less the
overlap margin both legs read with — written in the cursor's own zone, which
is the zone ``INTERNALDATE`` carries, so the comparison stays in the mailbox's
own calendar — and the window's lower edge is then enforced exactly against
each candidate's ``INTERNALDATE``. That edge is the cursor's own instant,
**inclusive**: the message at the boundary — one that shares a second with the
newest message a cycle examined — is listed again, which is the whole point of
the margin; a margin-wide *band* of already-recorded mail in the selection
would meanwhile eat the per-cycle bound and let a busy mailbox starve it.
Each candidate's id is the ``Message-ID`` header as both legs speak it
(``app.domain.mail.message_identity``) — used only when the header is a usable
identity, and otherwise the UIDVALIDITY-namespaced fallback — resolved while
the window is built, because the recorded id is what makes a re-fetch
idempotent. A read *by* id resolves it back to a UID with one header search
(or directly, for a fallback id), and a fallback id from another incarnation
of the mailbox resolves to nothing rather than to whatever now bears that UID.

**One conversation per call.** Each read connects, logs in, selects read-only,
does its commands and logs out — the shape the SMTP leg and the scanner have,
so no read depends on what an earlier one left open, at the price of a login
per read. Per *cycle* the price is one login per message read plus one for the
window; the cycle's own bound (``AICS_MAIL_INGEST_MAX_MESSAGES``) caps how many
that is. Nothing is cached between calls, deliberately: an adapter that
remembers a UID for an id would have to answer what it does when the mailbox
moved on. The window's own cost is the search's granularity: a cycle reads the
envelope of every candidate the day-granular search returns until it has filled
the bound, so the work one cycle does is bounded by the bound plus the day's
messages the search could not exclude — worth knowing, and cheap in the
dedicated mailbox this product reads.

The connection is built by one module-level factory (``connect``), which the
suite replaces with the fake in ``tests/fake_imap.py``; **TLS is the transport
on every port** (``IMAP4_SSL``, verified through ``tls_context``), a
cleartext connection exists only behind the development-only
``AICS_IMAP_ALLOW_PLAINTEXT``, and a server whose only protocol is cleartext is
refused by name (``plaintext_not_allowed``) before any credential is offered.
Every refusal is one of the mail adapters' two typed errors
(``app.adapters.mail.errors``), carrying a bounded code — the server's own
bracketed response code where it sent one, never its text, and never the
password, the address or a message.
"""

import asyncio
import contextlib
import imaplib
import re
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.domain.mail import (
    CURSOR_OVERLAP_SECONDS,
    IMAP_UID_PREFIX,
    MailResponseInvalid,
    MessageListing,
    MessageMetadata,
    bare_message_id,
    imap_since_day,
    parse_imap_envelope,
)

# The mailbox the product reads — the dedicated one, never a staff inbox.
MAILBOX = "INBOX"

# The port IMAPS conventionally lives on. TLS is what this leg speaks on *any*
# port (see ``connect``), so this is documentation rather than a switch.
TLS_PORT = 993

# OpenSSL's own reason code for "the peer answered the TLS ClientHello with a
# non-TLS record" — a server speaking cleartext IMAP. It is a stable code,
# carried in ``SSLError.reason`` by the C layer and, for an error rebuilt from
# its message, in the text itself.
_CLEARTEXT_REASON = "WRONG_VERSION_NUMBER"

# What one envelope read asks for: the receipt time the cursor needs and the
# headers attribution, the resolver and the record read. The addressing and
# thread headers are part of that envelope — the attribution decision
# (ADR-0019 §1) is made from them before any body is fetched. The list is the
# whole of the request: a fetch asks for nothing it does not name.
HEADER_FIELDS = (
    "MESSAGE-ID FROM SUBJECT DATE TO CC IN-REPLY-TO REFERENCES "
    "AUTO-SUBMITTED RETURN-PATH"
)
_ENVELOPE_FETCH = f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
_RAW_FETCH = "(BODY.PEEK[])"

_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]*)"')
_RESPONSE_CODE = re.compile(r"\[([A-Z][A-Z0-9_-]*)\]")
_MAX_REASON_LENGTH = 120


def connect(
    *, host: str, port: int, timeout: float, allow_plaintext: bool
) -> imaplib.IMAP4:
    """The connection this leg opens (the one factory the suite replaces).

    **TLS is the transport, on every port.** ``connect`` returns ``IMAP4_SSL``
    verified through ``tls_context`` whatever the port says — the port is not a
    security decision, and the rule this replaced (993 encrypted, anything else
    plain-text) made a mistyped or corporate port a silent cleartext session
    carrying the app password and a client's mail. A plaintext connection
    exists only behind ``AICS_IMAP_ALLOW_PLAINTEXT``, a development-only
    setting for a local server: never a deployment, never a mailbox with client
    data. A server that answers the TLS attempt in cleartext is refused by name
    (``plaintext_not_allowed``, in ``_open``) before any credential is offered.
    """
    if allow_plaintext:
        return imaplib.IMAP4(host, port, timeout=timeout)
    return imaplib.IMAP4_SSL(host, port, ssl_context=tls_context(), timeout=timeout)


def tls_context() -> ssl.SSLContext:
    """The context IMAPS verifies its server with (the seam tests replace).

    ``create_default_context`` checks the certificate chain **and** the
    hostname; nothing here weakens either, so a deployment cannot be silently
    pointed at an unverified server — the SMTP leg's own rule.
    """
    return ssl.create_default_context()


class ImapClient:
    """The mailbox over IMAP, holding its credential and no connection.

    ``username`` is the mailbox address and ``password`` the app password the
    SMTP leg authenticates with — the same pair, from the environment
    (``AICS_MAIL_ADDRESS`` / ``AICS_MAIL_APP_PASSWORD``), never the database
    (ADR-0014) — which is also why ``address`` is the account: it is what the
    cycle's self-sent rule compares against.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: float,
        allow_plaintext: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        # Stated by every caller rather than defaulted: it decides whether the
        # app password may cross the wire in the clear (``settings.
        # imap_allow_plaintext``, development only).
        self._allow_plaintext = allow_plaintext

    @property
    def address(self) -> str:
        """The mailbox this leg reads — the account it authenticates as."""
        return self._username

    async def list_message_ids(
        self, *, cursor: datetime | None, max_results: int
    ) -> MessageListing:
        """The window's message ids, oldest first, and the search behind it.

        ``cursor`` is the tenant's read position — the newest receipt time a
        cycle examined — or ``None`` for a mailbox this deployment has never
        read: the first read's reach is the bound, exactly as the provider
        leg's is, and its newest messages are the ones it takes.
        """
        return await self._conversation(
            lambda connection, uidvalidity: self._window(
                connection,
                uidvalidity=uidvalidity,
                cursor=cursor,
                max_results=max_results,
            )
        )

    async def fetch_message_metadata(self, *, message_id: str) -> MessageMetadata:
        """One message's envelope — its headers and receipt time, never a body."""
        return await self._conversation(
            lambda connection, uidvalidity: self._envelope(
                connection, uidvalidity=uidvalidity, identity=message_id
            )
        )

    async def fetch_message_raw(self, *, message_id: str) -> bytes:
        """One message's bytes, exactly as the mailbox holds them."""
        return await self._conversation(
            lambda connection, uidvalidity: self._raw(
                connection, uidvalidity=uidvalidity, identity=message_id
            )
        )

    # --- One conversation ------------------------------------------------

    async def _conversation[Result](
        self, operation: Callable[[imaplib.IMAP4, int], Result]
    ) -> Result:
        """Run one whole conversation off the event loop, or fail typed.

        ``imaplib`` is blocking, so the conversation runs in a worker thread;
        the socket timeout is the standard library's own and the outer bound is
        the backstop for the thread itself (a DNS lookup that ignores the
        socket timeout, for instance) — the SMTP leg's own two bounds.
        """
        try:
            async with asyncio.timeout(self._timeout * 2):
                return await asyncio.to_thread(self._blocking, operation)
        except TimeoutError as exc:
            raise MailProviderUnavailable("timed_out") from exc

    def _blocking[Result](self, operation: Callable[[imaplib.IMAP4, int], Result]) -> Result:
        connection = self._open()
        try:
            self._login(connection)
            uidvalidity = self._open_mailbox(connection)
            return operation(connection, uidvalidity)
        except imaplib.IMAP4.abort as exc:
            # The connection went away — EOF, an unexpected line, a socket
            # error. Nothing was answered, so this is a leg that could not be
            # reached rather than one that refused.
            raise MailProviderUnavailable(f"unavailable:{type(exc).__name__}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProviderRefused(_code_of(exc)) from exc
        except OSError as exc:
            raise MailProviderUnavailable(f"unavailable:{type(exc).__name__}") from exc
        finally:
            self._logout(connection)

    def _open(self) -> imaplib.IMAP4:
        """Connect, or refuse: the transport is TLS unless a deployment opted out.

        A server that answers the TLS attempt in cleartext is **refused by
        name** (``plaintext_not_allowed``) rather than reported as an outage:
        it is reachable and it is not one this leg will carry a credential to,
        and the two answers send an operator to different remedies. Every other
        failure — an unverifiable certificate, a timeout, DNS — keeps the
        reachability answer, with the exception's class name as the diagnosis
        and none of its text.
        """
        try:
            return connect(
                host=self._host,
                port=self._port,
                timeout=self._timeout,
                allow_plaintext=self._allow_plaintext,
            )
        except ssl.SSLError as exc:
            if _speaks_cleartext(exc):
                raise MailProviderRefused("plaintext_not_allowed") from exc
            raise MailProviderUnavailable(f"unavailable:{type(exc).__name__}") from exc
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailProviderUnavailable(f"unavailable:{type(exc).__name__}") from exc

    def _login(self, connection: imaplib.IMAP4) -> None:
        self._checked(*connection.login(self._username, self._password))

    def _open_mailbox(self, connection: imaplib.IMAP4) -> int:
        """Open the mailbox read-only and read the UIDVALIDITY it reports.

        ``EXAMINE`` is one of the two mechanisms that keep this leg from
        changing the mailbox; the UIDVALIDITY is the namespace a fallback
        identity is only meaningful inside, so a server that does not report it
        is a server whose messages cannot be named safely.
        """
        self._checked(*connection.select(MAILBOX, readonly=True))
        reported = connection.response("UIDVALIDITY")[1][0]
        if reported is None:
            raise MailResponseInvalid("missing_field:uidvalidity")
        try:
            return int(reported)
        except (TypeError, ValueError) as exc:
            raise MailResponseInvalid("invalid_field:uidvalidity") from exc

    def _logout(self, connection: imaplib.IMAP4) -> None:
        """End the conversation without expunging anything."""
        with contextlib.suppress(OSError, imaplib.IMAP4.error):
            connection.logout()

    # --- The window ------------------------------------------------------

    def _window(
        self,
        connection: imaplib.IMAP4,
        *,
        uidvalidity: int,
        cursor: datetime | None,
        max_results: int,
    ) -> MessageListing:
        """The window the cursor defines, oldest first, bounded.

        Two steps, because IMAP's search cannot express the window exactly:
        ``SINCE`` is day-granular and disregards the time of day, so the search
        asks for the cursor's own day (less the overlap margin both legs read
        with — the day ``INTERNALDATE`` is written in is the cursor's own zone,
        which is the mailbox's calendar) and each candidate's ``INTERNALDATE``
        then decides the window exactly.

        The lower edge is the cursor's own instant, **inclusive**: a message
        sharing a second with the newest examined one is listed again, which is
        what the overlap is for. It is not the overlapped instant, and that is
        deliberate — a margin-wide band of already-recorded mail inside the
        selection would be taken by the bound ahead of the new messages, and a
        mailbox busier than ``max_results`` per overlap would never advance.
        """
        since = None if cursor is None else cursor - timedelta(seconds=CURSOR_OVERLAP_SECONDS)
        criteria = ("ALL",) if since is None else ("SINCE", imap_since_day(since))
        status, data = connection.uid("SEARCH", *criteria)
        self._checked(status, data)
        uids = _uids_of(data)
        if max_results <= 0 or not uids:
            return MessageListing(message_ids=(), query=" ".join(criteria))
        if cursor is None:
            # No cursor: the bound decides how far back this deployment
            # reaches, and what it reaches for is the newest mail.
            return MessageListing(
                message_ids=tuple(
                    self._envelope_for_uid(connection, uid=uid, uidvalidity=uidvalidity).message_id
                    for uid in uids[-max_results:]
                ),
                query=" ".join(criteria),
            )
        chosen: list[str] = []
        for uid in uids:
            # UIDs ascend, so this walks the mailbox's own order: the window's
            # oldest messages are the ones a bounded cycle finishes first, and
            # what is left is still above the cursor when the next one asks.
            envelope = self._envelope_for_uid(connection, uid=uid, uidvalidity=uidvalidity)
            if envelope.received_at < cursor:
                continue  # the day the search could not address, trimmed here
            chosen.append(envelope.message_id)
            if len(chosen) >= max_results:
                break
        return MessageListing(message_ids=tuple(chosen), query=" ".join(criteria))

    # --- One message -----------------------------------------------------

    def _envelope(
        self, connection: imaplib.IMAP4, *, uidvalidity: int, identity: str
    ) -> MessageMetadata:
        uid = self._uid_for(connection, identity=identity, uidvalidity=uidvalidity)
        return self._envelope_for_uid(connection, uid=uid, uidvalidity=uidvalidity)

    def _envelope_for_uid(
        self, connection: imaplib.IMAP4, *, uid: int, uidvalidity: int
    ) -> MessageMetadata:
        status, data = connection.uid("FETCH", str(uid), _ENVELOPE_FETCH)
        self._checked(status, data)
        prefix, headers = _literal_of(data)
        reported = _INTERNALDATE.search(prefix)
        if reported is None:
            raise MailProviderRefused("malformed_response")
        return parse_imap_envelope(
            headers=headers,
            internal_date=reported.group(1).decode("ascii", "replace"),
            uid=uid,
            uidvalidity=uidvalidity,
        )

    def _raw(self, connection: imaplib.IMAP4, *, uidvalidity: int, identity: str) -> bytes:
        uid = self._uid_for(connection, identity=identity, uidvalidity=uidvalidity)
        status, data = connection.uid("FETCH", str(uid), _RAW_FETCH)
        self._checked(status, data)
        return _literal_of(data)[1]

    def _uid_for(self, connection: imaplib.IMAP4, *, identity: str, uidvalidity: int) -> int:
        """The UID one recorded identity names, in *this* mailbox.

        The reserved ``imap-uid:`` namespace is never searched for as header
        text — a value in it is this deployment's own reference or it names
        nothing: a UID is only meaningful inside one UIDVALIDITY, so an id
        minted under another incarnation resolves to nothing rather than to
        whatever now bears that UID, and a value that does not parse at all
        (which no header may produce — ``app.domain.mail.message_identity``)
        is the same answer. Anything else is a ``Message-ID``, found by a
        header search on its bare form — the same form the provider leg's
        ``rfc822msgid:`` lookup takes.
        """
        if identity.startswith(IMAP_UID_PREFIX):
            fallback = _fallback_uid(identity)
            if fallback is None or fallback[0] != uidvalidity:
                raise MailProviderRefused("not_found")
            return fallback[1]
        status, data = connection.uid(
            "SEARCH", "HEADER", "Message-ID", _quoted(bare_message_id(identity))
        )
        self._checked(status, data)
        uids = _uids_of(data)
        if not uids:
            # Deleted, moved, or never in this mailbox: the retry surface's
            # dismissal is the answer for a message the mailbox no longer has.
            raise MailProviderRefused("not_found")
        return uids[0]

    # --- The answer's own status -----------------------------------------

    @staticmethod
    def _checked(status: str, data: object) -> None:
        """Refuse an answer that is not ``OK``, keeping the server's own code.

        ``imaplib`` raises for a ``BAD`` line and hands ``NO`` back as the
        return value, so both roads to a refusal are read here: a server that
        will not do what was asked is a refusal with a usable connection, not
        an outage.
        """
        if status != "OK":
            raise MailProviderRefused(_code_of(data))


def _uids_of(data: list[Any]) -> list[int]:
    """The UIDs a SEARCH answer names, ascending — a mailbox's own order."""
    numbers: list[int] = []
    for item in data:
        if item is None:
            continue
        text = item.decode("ascii", "replace") if isinstance(item, bytes) else str(item)
        numbers.extend(int(token) for token in text.split() if token.isdigit())
    return sorted(numbers)


def _literal_of(data: list[Any]) -> tuple[bytes, bytes]:
    """The prefix and the literal bytes one FETCH answer carries.

    ``imaplib`` hands a literal back as the tuple the server sent: the response
    line up to the literal's length, then the bytes themselves. An answer
    without one — an empty ``OK`` line, say — is not a message.
    """
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            prefix, literal = item
            if isinstance(prefix, bytes) and isinstance(literal, bytes):
                return prefix, literal
    raise MailProviderRefused("malformed_response")


def _speaks_cleartext(error: ssl.SSLError) -> bool:
    """Whether a failed TLS attempt failed because the peer answered in cleartext.

    OpenSSL reports a peer that sent a non-TLS greeting as
    ``WRONG_VERSION_NUMBER`` (it read the server's plaintext banner as a record
    header). That is a server whose only protocol on this port is cleartext —
    the case the policy refuses — and it is read from the structured reason
    first and from the message text second, because a rebuilt error carries the
    code only in its text.
    """
    reason = getattr(error, "reason", None) or ""
    return _CLEARTEXT_REASON in reason or _CLEARTEXT_REASON in str(error)


def _fallback_uid(identity: str) -> tuple[int, int] | None:
    """The validity and UID a reserved-namespace identity names, if it parses.

    Called only for a value that opens with ``IMAP_UID_PREFIX`` (the caller
    checks): what follows must be the two numbers the id is made of, or the
    value names nothing.
    """
    validity, _, uid = identity[len(IMAP_UID_PREFIX) :].partition(":")
    if not validity.isdigit() or not uid.isdigit():
        return None
    return int(validity), int(uid)


def _quoted(value: str) -> str:
    """An IMAP quoted string: the value with ``\\`` and ``"`` escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _code_of(value: object) -> str:
    """The server's own bracketed response code, or this leg's bounded code.

    IMAP answers a refusal as ``NO [CODE] free text``. Only the code travels:
    the text can echo the account and the command, and nothing in this package
    is allowed to carry what a server said.
    """
    match = _RESPONSE_CODE.search(str(value))
    if match is None:
        return "imap_refused"
    return match.group(1)[:_MAX_REASON_LENGTH]
