"""The dedicated mailbox connection, deployment side (ADR-0005; Phase 3).

The product talks to clients from ONE mailbox the TCSP owns — never from
staff inboxes — through the provider's own API where one exists, and Gmail's
is the first. This module is the pure half of connecting it: what the consent
URL must ask for, what makes the browser's return this deployment's own
attempt, when a stored access token has to be renewed, how a mailbox other
than the configured one is refused, and the strict parsers for the provider
responses that carry a credential.

It also owns the two read legs' shared rules, which are identities rather
than transports (ADR-0005's SMTP/IMAP fallback): the ``Message-ID`` a message
is recorded under when it is a usable identity — the same string the SMTP
send leg stamps, angle brackets kept — and the UIDVALIDITY-namespaced id it
falls back to otherwise, whether the header is absent, claims the fallback's
own namespace, or is longer than the column that carries it
(``message_identity``); the envelope the IMAP leg's ``FETCH`` hands over,
parsed as strictly as the Gmail leg's JSON; and the day an IMAP ``SINCE``
search is written in, where the calendar's own English abbreviations are
spelled out rather than left to the machine's locale.

Two custody rules are stated here so no caller has to remember them:

- the tokens are secrets under ADR-0014 — ``access_token``, ``refresh_token``
  and the pending attempt's ``state``/``verifier`` are ``repr=False``, never
  formatted into a message, never stored in the database;
- the consent URL asks for exactly the two Gmail scopes this product needs
  (send, read) and nothing else: Gmail scopes are *restricted* scopes, so
  anything not asked for is also not justified to Google's reviewers.

The PKCE material is the activation handshake's own helpers
(``app.domain.activation``): RFC 7636 is provider-agnostic, and a second
implementation of a cryptographic detail is one too many.

Pure and framework-free per ADR-0006: stdlib hashing, randomness, base64,
time, and URL encoding only.
"""

import base64
import binascii
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from hmac import compare_digest
from urllib.parse import urlencode
from uuid import UUID

from app.domain.communications import addresses_in_header

# Gmail's two scopes, and deliberately nothing else (scope §2, ADR-0005).
# Both are *restricted* scopes: the product sends from the mailbox and reads
# what clients sent, so these two are the whole need.
GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

# The state token is the callback's own authority: 32 bytes is unguessable
# and is what makes a browser returning a borrowed code useless.
_STATE_BYTES = 32

# How long an administrator's browser has to complete the consent screen.
# Short enough that an abandoned attempt stops working, long enough to sign
# in with a second factor.
PENDING_TTL_SECONDS = 600

# An access token is renewed once it is within this of expiring: a probe's
# calls must not race the expiry they were checked against.
TOKEN_REFRESH_LEEWAY_SECONDS = 60

# The margin a read window opens before the cursor, on both legs. A provider's
# search is index-based rather than exact and two messages can share a second,
# so the next read asks for everything after the cursor *minus this*: the
# message at the boundary is listed again and costs one id check, which is
# cheaper than a message nothing ever lists.
CURSOR_OVERLAP_SECONDS = 300

# The IMAP fallback's own identity for a message that carries no usable
# ``Message-ID``. Namespaced by the mailbox's UIDVALIDITY because a UID is
# only meaningful inside one incarnation of a mailbox: if the server presents
# a new one, an id from the old incarnation names a message that no longer
# exists rather than one that now bears that UID. A header may never claim
# this namespace (``message_identity``).
IMAP_UID_PREFIX = "imap-uid:"

# The longest identity a message may be recorded under: the narrowest column
# that carries one — ``quarantined_messages.mailbox_message_id``, 128
# characters (``communications`` allows 320). A longer header is not an
# identity: the write would be refused, and a cycle that cannot record a
# message it refused never advances its cursor.
MAX_MESSAGE_ID_CHARS = 128

# The longest ``Message-ID`` token that may be an attribution anchor: the
# ``attribution_cases.original_rfc_message_id`` column is 320 characters
# (ADR-0019 §1). A longer token is not an anchor — the case's write would be
# refused with a ``DataError`` no path answers, and a cycle that cannot
# record what it examined never advances its cursor — so ``_first_message_id``
# reports none for it, exactly as ``message_identity`` falls back for a header
# a narrower identity column cannot hold.
MAX_RFC_ANCHOR_CHARS = 320

# The calendar IMAP's ``SINCE`` searches are written in. Spelled out rather
# than taken from ``strftime("%b")``, which answers in the machine's locale —
# a search string the server cannot parse is a cycle that reads nothing.
_IMAP_MONTHS = (
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


@dataclass(frozen=True)
class MessageListing:
    """One read window: the ids a cycle will examine, and the search behind it.

    ``message_ids`` is oldest first — the order a cycle examines in, so its
    cursor only ever moves forward over messages it finished — whichever leg
    answered, and whichever order that leg's API returns. ``query`` is the
    provider's own expression for the window (Gmail's ``after:<epoch>``, IMAP's
    ``SINCE <day>``, or the leg's own "everything" for a mailbox this
    deployment has never read), reported so an operator can see what was read.
    """

    message_ids: tuple[str, ...]
    query: str | None


class MailResponseInvalid(Exception):
    """A provider response is not the shape the wire contract fixes.

    ``reason`` names the field, never its value: a response body carries
    token material, so a refusal is recorded and rendered without echoing
    what arrived.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PendingMailConnection:
    """A consent attempt an administrator started that the browser has not finished.

    ``state`` and ``verifier`` are the anti-forgery halves: the state is what
    the callback compares, and the verifier is the PKCE secret that makes the
    one-time code useless to anyone who steals it. The two local ids record
    who to attribute the finished connection to — the callback is
    unauthenticated by necessity, so the attribution comes from here.
    """

    state: str = field(repr=False)
    verifier: str = field(repr=False)
    local_tenant_id: UUID
    local_user_id: UUID
    expires_at: datetime

    def is_expired(self, *, now: datetime) -> bool:
        return now > self.expires_at


@dataclass(frozen=True)
class MailboxCredential:
    """The mailbox this deployment is connected to, and the tokens for it.

    One credential, one mailbox (ADR-0005): the address is kept beside the
    tokens so the operator's view can name the mailbox without a provider
    call, and so the address guard's decision is recorded rather than
    implied. ``scopes`` is what the provider *granted*, which is not always
    what was asked for — a user may uncheck one on the consent screen — and
    the difference is visible in the status response.
    """

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    address: str
    scopes: tuple[str, ...]
    connected_at: datetime
    token_expires_at: datetime

    def needs_refresh(self, *, now: datetime, leeway_seconds: int) -> bool:
        return now >= self.token_expires_at - timedelta(seconds=leeway_seconds)


@dataclass(frozen=True)
class MailConnectionState:
    """The deployment's mailbox state, as one credential file holds it.

    An empty state (``MailConnectionState()``) is a deployment that has never
    connected a mailbox.
    """

    credential: MailboxCredential | None = None
    pending: PendingMailConnection | None = None

    @property
    def is_connected(self) -> bool:
        return self.credential is not None


@dataclass(frozen=True)
class TokenSet:
    """One token response: the credential's rotatable half."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    scopes: tuple[str, ...] | None
    expires_in: int


@dataclass(frozen=True)
class MailboxProfile:
    """The mailbox profile: the address the guard checks, and its size."""

    address: str
    messages_total: int


@dataclass(frozen=True)
class MessageMetadata:
    """One message's envelope: its headers, and never its body.

    The ``date`` header is carried verbatim (RFC 2822, as Google returns
    it): parsing a mail date is a rendering concern, and this shape is what
    the probe exists to show — that the mailbox is readable, not what is in
    it. ``received_at`` is Google's own ``internalDate``, the moment the
    mailbox accepted the message; the ingestion cursor is derived from it.
    ``thread_id`` is the provider's thread, which is how the reply parser
    will join a message to the request it answers.

    The attribution slice (ADR-0019 §1) decides from this envelope before any
    body is read, so the addressing and thread headers are part of it:
    ``recipients_to``/``recipients_cc`` are the addresses in their comparison
    form (``app.domain.communications.addresses_in_header``),
    ``in_reply_to``/``references`` are the message-id tokens a thread join
    matches on (``message_id_tokens``), and ``auto_submitted``/``return_path``
    are the two anti-abuse headers, stored verbatim. ``message_id`` is the
    identity the message is *recorded* under — the provider's id on the Gmail
    leg, the ``Message-ID`` or the UID fallback on the IMAP one — while
    ``rfc_message_id`` is the ``Message-ID`` header's own first token, when it
    carries one usable as an anchor (``MAX_RFC_ANCHOR_CHARS``), which is the
    anchor a chain is addressed by.
    """

    message_id: str
    subject: str | None
    sender: str | None
    date: str | None
    received_at: datetime
    thread_id: str | None = None
    recipients_to: tuple[str, ...] = ()
    recipients_cc: tuple[str, ...] = ()
    in_reply_to: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    rfc_message_id: str | None = None
    auto_submitted: str | None = None
    return_path: str | None = None


def new_state_token() -> str:
    """A fresh ``state`` for one consent attempt."""
    return secrets.token_urlsafe(_STATE_BYTES)


def state_matches(*, expected: str, received: str | None) -> bool:
    """Whether the browser returned the state this attempt minted (constant time).

    Compared as bytes: ``compare_digest`` refuses two strings holding non-ASCII
    characters, and the received value is whatever a caller put in a URL — a
    comparison that raised on it would turn a hostile query string into a 500.
    """
    if not received:
        return False
    return compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


def consent_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    """Google's consent URL for the dedicated mailbox.

    ``access_type=offline`` and ``prompt=consent`` are both required rather
    than cosmetic: Google issues a refresh token on the *first* consent only
    unless the prompt is forced, and a connection that cannot be refreshed
    stops working within the hour — including a re-connection after a
    disconnect, which is exactly when the operator expects it to work.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


def parse_token_response(raw: Mapping[str, object]) -> TokenSet:
    """The tokens from Google's token endpoint (consent exchange or refresh).

    A refresh response carries no refresh token — Google returns one on the
    consent exchange — so its absence is not an error here; the caller that
    *needs* one (the connection) checks for it.
    """
    access_token = raw.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MailResponseInvalid("missing_field:access_token")
    refresh_token = raw.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise MailResponseInvalid("invalid_field:refresh_token")
    expires_in = raw.get("expires_in")
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
        raise MailResponseInvalid("invalid_field:expires_in")
    scope = raw.get("scope")
    if scope is None:
        scopes = None
    elif isinstance(scope, str):
        scopes = tuple(scope.split())
    else:
        raise MailResponseInvalid("invalid_field:scope")
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token or None,
        scopes=scopes,
        expires_in=expires_in,
    )


def parse_profile_response(raw: Mapping[str, object]) -> MailboxProfile:
    """The profile from ``users.getProfile``: the address, and the total count."""
    address = raw.get("emailAddress")
    if not isinstance(address, str) or not address:
        raise MailResponseInvalid("missing_field:emailAddress")
    messages_total = raw.get("messagesTotal")
    if not isinstance(messages_total, int) or isinstance(messages_total, bool):
        raise MailResponseInvalid("invalid_field:messagesTotal")
    return MailboxProfile(address=address, messages_total=messages_total)


def parse_messages_list(raw: Mapping[str, object]) -> list[str]:
    """The message ids from ``users.messages.list``.

    An absent ``messages`` key is an empty mailbox, not an error — a fresh
    dedicated mailbox is the state a pilot starts in. A present entry that is
    not a message object is refused: silently skipping it would hide a
    drifted response behind a short list.
    """
    messages = raw.get("messages")
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise MailResponseInvalid("invalid_field:messages")
    ids: list[str] = []
    for entry in messages:
        if not isinstance(entry, Mapping):
            raise MailResponseInvalid("invalid_field:messages")
        message_id = entry.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise MailResponseInvalid("invalid_field:messages")
        ids.append(message_id)
    return ids


def parse_message_metadata(raw: Mapping[str, object]) -> MessageMetadata:
    """One message's envelope from ``users.messages.get`` (metadata format).

    Only the headers the probe asks Google for are read out — the addressing
    and thread headers the attribution decision is made from included;
    everything else in the body — the snippet, the payload parts — stops here
    and is discarded with the response that carried it.
    """
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise MailResponseInvalid("missing_field:id")
    payload = raw.get("payload")
    headers = payload.get("headers") if isinstance(payload, Mapping) else None
    if not isinstance(headers, list):
        raise MailResponseInvalid("invalid_field:payload.headers")
    found: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, Mapping):
            raise MailResponseInvalid("invalid_field:payload.headers")
        name = header.get("name")
        value = header.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise MailResponseInvalid("invalid_field:payload.headers")
        found.setdefault(name.casefold(), value)
    return MessageMetadata(
        message_id=message_id,
        subject=found.get("subject"),
        sender=found.get("from"),
        date=found.get("date"),
        received_at=_epoch_milliseconds(raw.get("internalDate")),
        thread_id=_optional_text(raw.get("threadId")),
        recipients_to=addresses_in_header(found.get("to", "")),
        recipients_cc=addresses_in_header(found.get("cc", "")),
        in_reply_to=message_id_tokens(found.get("in-reply-to", "")),
        references=message_id_tokens(found.get("references", "")),
        rfc_message_id=_first_message_id(found.get("message-id")),
        auto_submitted=found.get("auto-submitted"),
        return_path=found.get("return-path"),
    )


def parse_raw_message_response(raw: Mapping[str, object]) -> bytes:
    """The message bytes from ``users.messages.get`` with ``format=raw``.

    Gmail hands back the whole RFC 822 message — headers, body, and the
    MIME-encoded attachments — as base64url without padding. These are the
    bytes the ingestion slice scans and stores; nothing here re-encodes or
    reshapes them, so the stored object is the message the mailbox holds.
    """
    encoded = raw.get("raw")
    if not isinstance(encoded, str) or not encoded:
        raise MailResponseInvalid("missing_field:raw")
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError) as exc:
        raise MailResponseInvalid("invalid_field:raw") from exc


def _epoch_milliseconds(value: object) -> datetime:
    """Google's millisecond epoch timestamp as an aware UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MailResponseInvalid("missing_field:internalDate")
    try:
        millis = int(value)
    except ValueError as exc:
        raise MailResponseInvalid("invalid_field:internalDate") from exc
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def parse_imap_envelope(
    *, headers: bytes, internal_date: str, uid: int, uidvalidity: int
) -> MessageMetadata:
    """One message's envelope from an IMAP ``FETCH``.

    The IMAP analogue of ``parse_message_metadata``: the header block a
    ``BODY.PEEK[HEADER.FIELDS (…)]`` fetch returns is the envelope, and the
    server's own ``INTERNALDATE`` is the receipt time the cursor is derived
    from. ``thread_id`` is ``None`` — IMAP has no thread of its own, and the
    reply join reads the message's own ``In-Reply-To``/``References`` fields
    (below) rather than a value invented here.

    The identity is the ``Message-ID`` as both legs speak it, by way of
    ``message_identity``'s rule: the header is used only when it is a usable
    identity (no ``imap-uid:`` prefix, no longer than
    ``MAX_MESSAGE_ID_CHARS``), and otherwise the message is identified by
    ``imap_uid_message_id`` — never a raise: a message with no usable id is a
    message the cycle still has to be able to recognize on a re-fetch.

    The header block is read **raw** (the standard library's permissive
    policy): the newer policy's own parsers would rewrite a ``Date`` and, on a
    folded ``Message-ID``, keep only the fragment before the fold — and this
    parser's job is the value the header carries. Each value is unfolded to
    one line; the Message-ID is folded solid, because an id cannot contain a
    space, so the recorded identity and the header's own ``rfc_message_id``
    agree on a folded header.
    """
    found: dict[str, str] = {}
    for name, value in BytesParser().parsebytes(headers).items():
        found.setdefault(name.casefold(), _unfolded(value))
    return MessageMetadata(
        message_id=message_identity(
            found.get("message-id"), uidvalidity=uidvalidity, uid=uid
        ),
        subject=found.get("subject"),
        sender=found.get("from"),
        date=found.get("date"),
        received_at=_internal_date(internal_date),
        thread_id=None,
        recipients_to=addresses_in_header(found.get("to", "")),
        recipients_cc=addresses_in_header(found.get("cc", "")),
        in_reply_to=message_id_tokens(found.get("in-reply-to", "")),
        references=message_id_tokens(found.get("references", "")),
        rfc_message_id=_first_message_id(found.get("message-id")),
        auto_submitted=found.get("auto-submitted"),
        return_path=found.get("return-path"),
    )


# One RFC 5322 message id as it appears in ``In-Reply-To``/``References``: the
# angle-bracketed token, extracted with the id's own delimiters so the token a
# join stores matches the one the sender stored.
_MESSAGE_ID_TOKEN = re.compile(r"<[^<>\s]+>")


def message_id_tokens(*values: str) -> tuple[str, ...]:
    """The ``<...>`` message-id tokens of one or more header values, in order.

    A thread header (``In-Reply-To``, ``References``) carries message ids as
    angle-bracketed tokens among whatever else a sender wrote. This is the one
    rule that says what a token is, so the ids an envelope reports and the ids
    a thread join matches on are the same strings: each value is scanned for
    tokens and each value contributes its own, in argument order. Whitespace
    between two tokens (a folded header) belongs to neither, and anything that
    is not a token — a bare id, a bracketed phrase carrying a space — is left
    out. The rule previously lived, private, in the ingestion join
    (``app.application.mail_ingest``); it lives here because the envelope
    parsers extract tokens from the same headers.
    """
    tokens: list[str] = []
    for value in values:
        tokens.extend(_MESSAGE_ID_TOKEN.findall(value))
    return tuple(tokens)


def normalized_message_id(value: str | None) -> str | None:
    """An RFC 5322 ``Message-ID`` as both legs record it.

    The header's value with its folding removed — an id cannot contain a space,
    so the fold's whitespace is dropped rather than kept — and its angle
    brackets left on, because that is the string the **SMTP send leg** records
    for a message no provider gave an id for (``mail_send._message_id_of``
    reads the ``Message-ID`` the generator stamps). One idiom, so a message
    this deployment sent and one it later read back from a corporate mailbox
    are the same identity rather than two.

    ``None`` for anything that is not a value at all — an absent header, or
    one that is blank — and never a raise: the caller has a fallback id for
    exactly that case. This is the *shape* of the value; whether it may be
    used as an **identity** is ``message_identity``'s rule.
    """
    if value is None:
        return None
    return "".join(value.split()) or None


def message_identity(header: str | None, *, uidvalidity: int, uid: int) -> str:
    """The id one IMAP message is recorded under: its header's, or its UID's.

    The header's ``Message-ID`` is used **only when it is a usable identity**:
    it must not claim the reserved ``imap-uid:`` namespace, and it must be at
    most ``MAX_MESSAGE_ID_CHARS`` characters. Either way, the alternative is
    the UID fallback — deterministic, bounded, and resolvable by the client
    without a search, because it names its own UID — and never a third
    namespace invented here.

    - **The reserved prefix.** ``imap-uid:`` is this module's own name for a
      message whose header is unusable, and the client reads a value in it
      back as a *UID reference*: a header dressed as one would name this
      message if the numbers happen to match the mailbox, a **different**
      message if they do not, and nothing at all if they name another
      incarnation's UIDVALIDITY — a listed id that cannot be read is a cycle
      that never advances. A header is a stranger's claim to a namespace it
      does not own, so a value that opens with it is not an identity.
    - **The length bound.** The narrowest column that carries an identity is
      ``quarantined_messages.mailbox_message_id``, 128 characters
      (``communications`` allows 320). An identity a table cannot hold is an
      identity that cannot be recorded, and a cycle that cannot record a
      message it refused never advances its cursor: the header falls back
      instead of failing the write.

    In every other case — an absent header, a blank one, one reserved, one
    over-long — the identity is ``imap_uid_message_id``.
    """
    normalized = normalized_message_id(header)
    if (
        normalized is None
        or normalized.startswith(IMAP_UID_PREFIX)
        or len(normalized) > MAX_MESSAGE_ID_CHARS
    ):
        return imap_uid_message_id(uidvalidity=uidvalidity, uid=uid)
    return normalized


def bare_message_id(message_id: str) -> str:
    """The same id as a *search* wants it: without its angle brackets.

    Both legs search by ``Message-ID`` — Gmail as ``rfc822msgid:``, IMAP as a
    ``HEADER`` criterion — and both match on the header's text, where the
    stored value carries the brackets.
    """
    return message_id.strip().strip("<>").strip()


def imap_uid_message_id(*, uidvalidity: int, uid: int) -> str:
    """The id a message without a usable ``Message-ID`` is recorded under.

    Deterministic, so a re-fetch of the same message recognizes it;
    namespaced by the mailbox's UIDVALIDITY, so an id from a previous
    incarnation of the mailbox can never collide with a different message;
    bounded (whatever a sender wrote), so every column that carries an
    identity can hold it; and resolvable by the client without a search,
    because it names its own UID. ``message_identity`` decides when a header
    is usable; this is what the message gets when it is not.
    """
    return f"{IMAP_UID_PREFIX}{uidvalidity}:{uid}"


def imap_since_day(value: datetime) -> str:
    """The day an IMAP ``SINCE`` search is written for: ``05-Sep-2026``."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


def _unfolded(value: str) -> str:
    """One header value on one line: its folding whitespace collapsed to a space."""
    return " ".join(value.split())


def _internal_date(value: str) -> datetime:
    """An IMAP ``INTERNALDATE`` as an aware datetime.

    The format is fixed (``20-Sep-2026 09:00:00 +0000``) and the zone is part
    of it, so a value without one is refused rather than assumed to be UTC: a
    guessed zone shifts the cursor by hours, and the cursor is what decides
    which messages a cycle ever reads.
    """
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise MailResponseInvalid("invalid_field:internalDate") from exc
    if parsed is None or parsed.tzinfo is None:
        raise MailResponseInvalid("invalid_field:internalDate")
    return parsed


def _optional_text(value: object) -> str | None:
    """A field that may be absent, but may not be the wrong type."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailResponseInvalid("invalid_field:threadId")
    return value or None


def _first_message_id(value: str | None) -> str | None:
    """A ``Message-ID`` header's own first token, when it is a usable anchor.

    Folded solid before the token is read — ``normalized_message_id``'s own
    rule, an id cannot contain a space — so a folded header yields the same
    token an unfolded one would. A header carrying no token at all (absent,
    blank, or something that is not an id) has none to report, and neither
    has a token longer than ``MAX_RFC_ANCHOR_CHARS``: the token is the anchor
    a case is addressed by (``attribution_cases.original_rfc_message_id``),
    and one its column cannot hold is not an anchor.
    """
    tokens = message_id_tokens(normalized_message_id(value) or "")
    if not tokens:
        return None
    token = tokens[0]
    return token if len(token) <= MAX_RFC_ANCHOR_CHARS else None


def address_matches(*, expected: str, actual: str) -> bool:
    """Whether the consented mailbox is the configured one (normalized).

    The guard exists because consent is granted by whoever is signed in to
    the browser, which is not necessarily the mailbox this deployment was
    configured for; connecting the wrong mailbox would send clients' mail
    from an account nobody chose. Case and surrounding space are the only
    normalizations — anything cleverer would start accepting addresses the
    operator did not type.
    """
    return bool(actual) and expected.strip().casefold() == actual.strip().casefold()
