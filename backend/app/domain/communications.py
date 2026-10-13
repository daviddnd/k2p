"""Inbound client communication: the Communication record and the mail that
becomes one (ADR-0005; data-model §2.9).

A ``Communication`` is the immutable record of one exchange with a client —
a confirmation request, a reminder, a reply, a reconfirmation, a
clarification. This slice writes the inbound email half of it: one row per
message received on the dedicated mailbox, carrying what the message is
(direction, channel), where it came from (``mailbox_message_id``,
``thread_id``, sender), when the provider received it, and the stored raw
message (``raw_document_id``). Language and the fact checklist are null until
the reply-parser slice fills them; they are not guessed here.

This module is the pure half:

- the **vocabularies** — ``Direction``, ``Channel``, ``CommunicationKind``
  (§2.9's kinds, migration ``0027``, widened ``0033``), and the
  ``IngestOutcome`` a cycle reports per message;
- **decoding one stored message**: headers, the sender address the
  attribution rule matches on, and the attachment parts with their declared
  types and decoded bytes. MIME is decoded with the standard library
  (``email``), which is pure; the provider's wire shapes have their own
  parsers in ``app.domain.mail``;
- **the message text**, and no more of it than the rules that read it need:
  ``message_text`` returns the plain-text body collapsed and capped, which is
  what the multi-company resolver reads (``app.domain.reply_resolver``), and
  ``first_body_line`` returns the body's first non-empty line with its line
  structure kept, which is what the confirmation grammar reads
  (``app.domain.attribution.is_confirmation``). What a reply *means* — its
  language, its confirmations, the changes it reports — is the reply parser's
  slice, and the parser works from the stored raw message.

Pure and framework-free per ADR-0006.
"""

from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from enum import StrEnum

# The cap on attachment parts a message may contribute as stored documents. A
# MIME body can hold thousands of tiny parts, and a cycle that tries to store
# all of them is a cycle a sender can stall. Parts beyond the cap are counted
# but not decoded or stored — the raw message holds them, and the ingestion
# cycle reports the excess rather than dropping it silently.
MAX_ATTACHMENTS_PER_MESSAGE = 50

# The cap on the message *text* handed on. The stored message is already
# bounded (the document write path's 25 MiB), but a body decodes to at most
# that much text, and every later step reads this string: a sender must not
# decide how much of it a cycle holds and scans. A client reply runs to a few
# hundred characters and one carrying its quoted history to a few kilobytes,
# so eight thousand is more than an order of magnitude past what resolving a
# company out of it needs; a message whose company reference sits beyond the
# cut resolves nothing and quarantines, which is the safe outcome.
MAX_MESSAGE_TEXT_CHARS = 8_000

# The message bytes that are parsed at all. Parsing MIME is where the memory
# goes — the standard library's feeder builds the body several times over
# while it reads, measured at roughly eight times the input — and the
# multi-company path may hand this function bytes no size check has seen
# (``MAX_UPLOAD_BYTES`` is enforced when a message is *stored*, and a message
# that resolves late is fetched from the provider, not read from the store).
# So the input is bounded as well as the output: eight bytes per capped
# character is the widest UTF-8 character (four bytes) with as much again for
# MIME structure, which leaves the character cap, not this one, binding for
# any body whose text could still fit in it. A message large enough to be cut
# here finds no ``text/plain`` part, or only part of one, and resolves
# nothing — the quarantine outcome, which is the safe one.
MAX_MESSAGE_BYTES_READ = MAX_MESSAGE_TEXT_CHARS * 8

# The cap on the body prefix ``first_body_line`` scans for its line. The first
# non-empty line of a reply is a short instruction — the confirm act is one
# word — so the bound only decides when a body opens with blank padding or a
# very long first line: a line that does not begin within this prefix reports
# none, and the message is processed ordinarily. Bounded because the scan must
# not walk an arbitrary body merely because the byte cap let it through.
MAX_FIRST_LINE_CHARS = 1_000


class Direction(StrEnum):
    """§2.9 ``Communication.direction``.

    Both halves are written now: the ingestion cycle records ``inbound``
    messages, and the sender records ``outbound`` ones when a confirmation
    request's message is accepted (contract §5.1.11). The record is one row per
    **exchange**, so a direction is a fact about who moved first, not a
    different kind of row.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Channel(StrEnum):
    """§2.9 ``Communication.channel``; v1.0 is email only (§2.5 — WhatsApp and
    WeChat are deferred, and the vocabulary grows when they land)."""

    EMAIL = "email"


class CommunicationKind(StrEnum):
    """§2.9 ``Communication.kind`` — the exchange's role in its thread
    (contract §5.1.12, migrations ``0027`` and ``0033``).

    Only two kinds are written by v1.0's mail paths: the sender records an
    outbound confirmation request, and the ingestion cycle records an inbound
    reply. ``reminder`` is written by the reminder sweep (§5.1.12);
    ``reconfirmation`` and ``clarification`` are the reply parser's later
    vocabulary (M5b). ``attribution_ask`` and ``attribution_confirmation``
    are the attribution exchange's (ADR-0019 §1, migration ``0033``): the
    outbound registration/confirmation ask, and the registered channel's
    confirming reply — the authorising act, recorded like any exchange.
    Named here because the DB CHECK carries the full set.
    """

    CONFIRMATION_REQUEST = "confirmation_request"
    REMINDER = "reminder"
    REPLY = "reply"
    RECONFIRMATION = "reconfirmation"
    CLARIFICATION = "clarification"
    ATTRIBUTION_ASK = "attribution_ask"
    ATTRIBUTION_CONFIRMATION = "attribution_confirmation"


class IngestOutcome(StrEnum):
    """What one cycle decided about one message (the report's vocabulary).

    Exactly one of these holds; ``reason`` carries the detail the outcome
    alone does not — the gate's ``GatingReason`` for ``denied``, the refusal
    code for ``failed``. A message the cycle did not ingest stays in the
    mailbox, which is where the TCSP's own copy lives (ADR-0005) and where
    staff handle it — the §2.5 quarantine record for a refused message is the
    ingestion cycle's to write (``app.application.mail_ingest``), not this
    module's. The ``awaiting_*`` values are the attribution cases' own — the
    two ADR-0019 §1 awaits and the clarification loop's
    ``awaiting_clarification``: the message's evidence (the thread, or the
    unthreaded classes' registration shape) opened an attribution case and
    asks rather than quarantines — nothing was ingested, but nothing was
    refused either, and the case id rides the outcome. ``attribution_confirmed`` is
    the act that ends an await: a registered channel's confirming reply, which
    is stored as the authorising exchange and processes the originals it
    authorises.
    """

    INGESTED = "ingested"
    # Already recorded under this mailbox message id (idempotency).
    SKIPPED_KNOWN = "skipped_known"
    # Sent by this deployment's own mailbox (an outbound message, not a reply).
    SELF_SENT = "self_sent"
    # No registered contact channel matches the sender.
    UNATTRIBUTED_SENDER = "unattributed_sender"
    # The sender is registered against more than one company: which one the
    # message is about is the resolver's decision
    # (``app.domain.reply_resolver``), and it is never a guess.
    AMBIGUOUS_SENDER = "ambiguous_sender"
    # The §5.1 automation gate refused ``email_ingestion`` for the company.
    DENIED = "denied"
    # The message itself could not be ingested (a provider read failed, or the
    # raw message failed validation).
    FAILED = "failed"
    # ADR-0019 §1's two awaits: the threaded message opened an attribution
    # case and nothing was read or stored. ``awaiting_registered_channel`` —
    # no registered channel of the chain's company is on the message, so the
    # ask goes back to the sender; ``awaiting_confirmation`` — a registered
    # channel is among the To/Cc addresses and is asked to confirm.
    AWAITING_REGISTERED_CHANNEL = "awaiting_registered_channel"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    # The multi-company split (workstream ③, ADR-0019 §3): the message named
    # several of the sender's own companies decisively (E1) or its
    # clarification answer named several (E2), and the parent row the split
    # opened now carries the division — the parent id rides the outcome, and
    # ``reason`` carries a park or failure code when the division did not
    # complete. Nothing of the message is ingested on this outcome: the
    # per-company slices land on the work items.
    SPLIT = "split"
    # The clarification loop's await (workstream ②, ruling D1): an unthreaded
    # message from a sender registered for several companies, none of which
    # the resolver matched, opened a company-less case that asks which company
    # the message concerns — nothing was read or stored, the same posture as
    # the two §1 awaits, and the case id rides the outcome.
    AWAITING_CLARIFICATION = "awaiting_clarification"
    # ADR-0019 §1's confirming act (ruling R5): a registered channel of the
    # chain's company answered the confirmation ask, so the message is stored
    # as the authorising exchange (``attribution_confirmation``) and the cases
    # it authorises are processed. It is not an ingestion like the others: the
    # message authorises, and the originals it authorises are what lands.
    ATTRIBUTION_CONFIRMED = "attribution_confirmed"


class AttachmentDisposition(StrEnum):
    """What became of one attachment part.

    ``refused`` means the attachment is not stored at all: its declared type
    is outside the registry's allowed set, its bytes do not match the
    declaration, or it is over the per-file cap. It stays in the mailbox
    (ADR-0005), and the refusal is reported and audited by code.
    """

    STORED = "stored"
    REFUSED = "refused"


@dataclass(frozen=True)
class MessageAttachment:
    """One attachment part: what it claims to be, and its decoded bytes."""

    filename: str | None
    content_type: str
    data: bytes


@dataclass(frozen=True)
class InboundMessage:
    """One stored message, decoded far enough to classify and file it.

    ``attachments`` holds at most ``MAX_ATTACHMENTS_PER_MESSAGE`` parts;
    ``attachment_count`` is how many the message actually carried, so a caller
    can report what it did not store instead of dropping it silently.
    """

    sender: str | None
    subject: str | None
    attachments: tuple[MessageAttachment, ...]
    attachment_count: int


def parse_inbound_message(raw: bytes) -> InboundMessage:
    """Decode a stored message: its sender, subject, and attachment parts.

    Everything is decoded permissively — a malformed message is still a
    message the client sent, and the record of it is the point. A part whose
    payload cannot be decoded to bytes (a nested ``message/rfc822``
    attachment, a body the sender mangled) simply does not appear as an
    attachment: it is still inside the stored raw message, which is what the
    scanner and, later, the parser read. Parts beyond the per-message cap are
    counted but not decoded — decoding is the cost the cap exists to bound,
    and walking the remaining headers is not.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw)
    attachments: list[MessageAttachment] = []
    attachment_count = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename is None:
            continue
        attachment_count += 1
        if len(attachments) >= MAX_ATTACHMENTS_PER_MESSAGE:
            continue
        payload = _decoded_payload(part)
        if payload is None:
            continue
        attachments.append(
            MessageAttachment(
                filename=filename,
                content_type=part.get_content_type().lower(),
                data=payload,
            )
        )
    return InboundMessage(
        sender=sender_address(_header(message, "From")),
        subject=_header(message, "Subject"),
        attachments=tuple(attachments),
        attachment_count=attachment_count,
    )


def message_text(raw: bytes) -> str:
    """A stored message's plain-text body, whitespace-collapsed and capped.

    This is what the multi-company resolver reads — it looks for the company's
    number or its registered name in the text (``app.domain.reply_resolver``)
    — so the result is deliberately coarse: every whitespace run becomes a
    single space, and the text stops at ``MAX_MESSAGE_TEXT_CHARS``. The
    collapse is this reader's own point, and it is why the confirmation
    grammar reads the body through ``first_body_line`` instead:
    ``is_confirmation``'s rule is the first non-empty *line*, and a collapse
    that removes the newlines erases the very structure the rule reads.

    Reading is bounded on both ends (``MAX_MESSAGE_BYTES_READ`` in,
    ``MAX_MESSAGE_TEXT_CHARS`` out), and a message cut by either bound
    resolves nothing: it quarantines, and staff see it.
    """
    content = _plain_body(raw)
    if content is None:
        return ""
    # Only a bounded prefix of the body is ever looked at, and it is cut
    # before collapsing: the work then costs what the cap says rather than
    # what the sender sent, where collapsing the whole body first would
    # allocate an object per word of it. A cut in the middle of a word is
    # safe — a partial digit run and a partial name both match nothing.
    return " ".join(content[:MAX_MESSAGE_TEXT_CHARS].split())


def first_body_line(raw: bytes, *, limit: int = MAX_FIRST_LINE_CHARS) -> str:
    """A stored message's first non-empty body line, line structure kept.

    The confirmation act's reader (``app.domain.attribution.is_confirmation``,
    ruling R5): the grammar is the body's **first non-empty line** — leading
    blank lines skipped, the signature and quoted history below ignored — and
    that rule needs the line breaks ``message_text`` collapses away. The body
    is the same read (``_plain_body``), what counts as an empty line is the
    grammar's own (a line of whitespace is not a line), and the line is
    returned as the sender wrote it: the rule's normalization (NFKC,
    case-folding, stripping) is the rule's, not this reader's.

    Bounded on both ends like ``message_text``: ``MAX_MESSAGE_BYTES_READ`` of
    input, ``limit`` of the body scanned. A body whose first non-empty line
    does not begin within the prefix reports ``""`` — the act is not read,
    and the message is processed ordinarily — and so does a message with no
    plain-text body.
    """
    content = _plain_body(raw)
    if content is None:
        return ""
    for line in content[:limit].splitlines():
        if line.strip():
            return line
    return ""


def sender_address(from_header: str | None) -> str | None:
    """The bare address inside a ``From`` header (``Director <d@x>`` → ``d@x``).

    The address is the identity, not the display name: attribution matches it
    against registered contact channels, and a display name is whatever the
    sender typed.
    """
    if not from_header:
        return None
    for _, address in getaddresses([from_header]):
        if address.strip():
            return address.strip()
    return None


def normalized_address(address: str) -> str:
    """The comparison form of an address: trimmed and case-folded.

    Mail addresses are matched case-insensitively in practice, and the stored
    contact-channel identifier keeps whatever the staff member typed.
    """
    return address.strip().casefold()


def addresses_in_header(header_value: str) -> tuple[str, ...]:
    """The bare addresses in a ``To``/``Cc`` header, deduplicated in order.

    One header can carry several addresses — the boss-on-copy case the
    attribution rule reads (ADR-0019 §1) — and the same address may appear
    more than once, in any casing. Each address is reduced to its comparison
    form (``normalized_address``) and the first occurrence wins, so the order
    the header wrote is kept while a repeat does not become a second entry;
    an entry carrying no address (a trailing comma, a display name alone) is
    dropped rather than kept as an empty string.
    """
    addresses: list[str] = []
    seen: set[str] = set()
    for _, address in getaddresses([header_value]):
        normalized = normalized_address(address)
        if normalized and normalized not in seen:
            seen.add(normalized)
            addresses.append(normalized)
    return tuple(addresses)


def _plain_body(raw: bytes) -> str | None:
    """The message's decoded plain-text body, or ``None`` when it has none.

    The one body read ``message_text`` and ``first_body_line`` share, so the
    two readers' decode rules — and their bounds — cannot drift apart. The
    body is the standard library's own: the ``text/plain`` part alone, so an
    attached ``.txt`` is not the body and neither is a forwarded message's
    inner text. A message with no ``text/plain`` body — an HTML-only one — is
    ``None`` rather than a converted approximation: converting markup belongs
    with the parser that has to read the whole message, and the callers' safe
    outcomes (a message nothing attributes quarantines, an unreadable
    confirmation is not an act) need no approximation. A body the standard
    library cannot decode at all is ``None`` too — a declared charset it does
    not know raises ``LookupError`` — while bytes that are invalid *within* a
    charset it does know are not a failure: the text handler decodes with
    ``errors="replace"``, so they arrive as U+FFFD characters and match
    nothing.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw[:MAX_MESSAGE_BYTES_READ])
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        return None
    try:
        content = body.get_content()
    except (LookupError, ValueError):
        return None
    return content if isinstance(content, str) else None


def _header(message: Message, name: str) -> str | None:
    """A header's decoded text, or ``None`` when it is absent or unusable."""
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decoded_payload(part: Message) -> bytes | None:
    """A part's decoded bytes, or ``None`` when the part does not decode."""
    try:
        payload = part.get_payload(decode=True)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, bytes) else None
