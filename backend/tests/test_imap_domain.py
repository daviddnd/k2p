"""Pure rules for the IMAP read fallback (ADR-0005 path A; Phase 3 P3-1).

The half of the fallback that is not a socket: the identity a message is
recorded under (its ``Message-ID``, or the UIDVALIDITY-namespaced fallback for
one that carries none), the day an IMAP ``SINCE`` search is written in, and the
parser that turns one ``FETCH`` answer — its header block and its
``INTERNALDATE`` — into the envelope the ingestion cycle consumes.

The parsers are the strict kind the module's provider parsers already are: a
fetch without a usable receipt time is refused rather than guessed at
(``MailResponseInvalid``), because that value is the ingestion cursor. No I/O
and no framework (ADR-0006); the same rules are pinned through the real client
in ``test_imap_client.py`` and against PostgreSQL in
``test_mail_ingest_imap_scenarios_db.py``.
"""

from datetime import UTC, datetime

import pytest

from app.domain.mail import (
    MAX_MESSAGE_ID_CHARS,
    MailResponseInvalid,
    bare_message_id,
    imap_since_day,
    imap_uid_message_id,
    message_identity,
    normalized_message_id,
    parse_imap_envelope,
)

UID = 42
UIDVALIDITY = 1234
HEADERS = (
    b"Message-ID: <20260920.0931.7@ai-filings.tcsp.example>\r\n"
    b"From: CHAN TAI MAN <director@client.example>\r\n"
    b"Subject: Re: Annual Return Confirmation\r\n"
    b"Date: Tue, 23 Sep 2026 09:31:00 +0800\r\n"
    b"\r\n"
)


def envelope(
    *,
    headers: bytes = HEADERS,
    internal_date: str = "20-Sep-2026 09:00:00 +0000",
    uid: int = UID,
    uidvalidity: int = UIDVALIDITY,
):
    return parse_imap_envelope(
        headers=headers, internal_date=internal_date, uid=uid, uidvalidity=uidvalidity
    )


# --- The envelope --------------------------------------------------------


def test_the_envelope_carries_the_headers_and_the_servers_receipt_time() -> None:
    metadata = envelope()

    assert metadata.message_id == "<20260920.0931.7@ai-filings.tcsp.example>"
    assert metadata.sender == "CHAN TAI MAN <director@client.example>"
    assert metadata.subject == "Re: Annual Return Confirmation"
    assert metadata.date == "Tue, 23 Sep 2026 09:31:00 +0800"
    # IMAP hands a receipt time the server recorded, which is what the cursor
    # is derived from in both legs.
    assert metadata.received_at == datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def test_the_envelope_keeps_non_utc_receipt_times_instant() -> None:
    metadata = envelope(internal_date="20-Sep-2026 09:00:00 +0800")

    assert metadata.received_at == datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    assert metadata.received_at.tzinfo is not None


def test_a_message_without_a_message_id_falls_back_to_its_uid() -> None:
    # Never a raise: a mailbox can hold a message with no ``Message-ID``, and
    # the id it is recorded under has to be one a re-fetch recognizes.
    metadata = envelope(headers=b"Subject: no id here\r\n\r\n")

    assert metadata.message_id == "imap-uid:1234:42"
    assert metadata.subject == "no id here"


def test_a_blank_message_id_header_falls_back_too() -> None:
    metadata = envelope(headers=b"Message-ID:   \r\nFrom: a@b.example\r\n\r\n")

    assert metadata.message_id == "imap-uid:1234:42"
    assert metadata.sender == "a@b.example"


def test_a_folded_message_id_is_read_as_one_value() -> None:
    # Header folding is legal and a raw header block carries it folded; the
    # identity must be the same string an unfolded header would give — and so
    # must the header's own id, which is the anchor a chain is addressed by.
    metadata = envelope(
        headers=b"Message-ID: <folded\r\n @ai-filings.tcsp.example>\r\n\r\n"
    )

    assert metadata.message_id == "<folded@ai-filings.tcsp.example>"
    assert metadata.rfc_message_id == "<folded@ai-filings.tcsp.example>"


def test_a_fetch_without_a_usable_receipt_time_is_refused() -> None:
    # The receipt time is the cursor; there is no honest way to guess one.
    for internal_date in ("", "yesterday", "20-Sep-2026 09:00:00"):
        with pytest.raises(MailResponseInvalid):
            envelope(internal_date=internal_date)


def test_a_header_block_that_is_not_headers_is_an_envelope_without_them() -> None:
    # A fetch whose literal is not a header block yields no header values and
    # no identity of its own — the fallback id is what it has, and nothing
    # raises: the message either resolves or does not, exactly as a headerless
    # message does.
    metadata = envelope(headers=b"not a header block at all")

    assert metadata.message_id == "imap-uid:1234:42"
    assert metadata.subject is None
    assert metadata.sender is None


# --- The identity, as both legs speak it --------------------------------


def test_the_stored_identity_keeps_the_headers_own_angle_brackets() -> None:
    # The SMTP send leg records the ``Message-ID`` header's value for a
    # message no provider gave an id for; the inbound fallback must use the
    # same string, or a message the deployment sent and later read back would
    # be two identities.
    assert normalized_message_id("<a@b.example>") == "<a@b.example>"
    assert normalized_message_id("  <a@b.example>  ") == "<a@b.example>"
    assert normalized_message_id("<a\r\n @b.example>") == "<a@b.example>"


def test_a_missing_or_blank_message_id_normalizes_to_nothing() -> None:
    assert normalized_message_id(None) is None
    assert normalized_message_id("") is None
    assert normalized_message_id("   ") is None


def test_the_search_form_of_an_identity_drops_its_angle_brackets() -> None:
    # A header search matches the header's text, and the stored value carries
    # the brackets; the search asks for the bare id, exactly as the Gmail
    # leg's ``rfc822msgid:`` lookup does.
    assert bare_message_id("<a@b.example>") == "a@b.example"
    assert bare_message_id(" <a@b.example> ") == "a@b.example"


def test_the_fallback_identity_is_namespaced_by_the_mailboxs_uidvalidity() -> None:
    # A UID is only meaningful inside one incarnation of a mailbox: the id has
    # to say which one, or an old id could collide with a different message.
    assert imap_uid_message_id(uidvalidity=1234, uid=42) == "imap-uid:1234:42"
    assert imap_uid_message_id(uidvalidity=2, uid=42) != imap_uid_message_id(
        uidvalidity=1, uid=42
    )


def test_the_fallback_identity_is_what_an_unusable_header_falls_back_to() -> None:
    # One rule, three ways to miss it: no header, a header claiming the
    # reserved namespace, and one too long for the narrowest column that
    # carries an identity. Each lands on the message's own bounded, searchable
    # UID id — never on a third namespace, and never on the header's value.
    assert message_identity(None, uidvalidity=1234, uid=42) == "imap-uid:1234:42"
    assert message_identity("   ", uidvalidity=1234, uid=42) == "imap-uid:1234:42"
    assert message_identity("imap-uid:9999:7", uidvalidity=1234, uid=42) == "imap-uid:1234:42"
    assert message_identity("imap-uid:not-a-uid", uidvalidity=1234, uid=42) == "imap-uid:1234:42"
    assert message_identity("x" * 129, uidvalidity=1234, uid=42) == "imap-uid:1234:42"


def test_a_message_id_may_not_claim_the_fallback_namespace() -> None:
    # ``imap-uid:`` is this module's own namespace for a message that has no
    # usable ``Message-ID``, and the client reads it back as a UID reference:
    # a header dressed as one would point at this message, at a different one
    # (a matching UIDVALIDITY), or at nothing at all (another incarnation's) —
    # so a header that claims it is not an identity at all.
    for claimed in (
        f"imap-uid:{UIDVALIDITY}:{UID}",
        "imap-uid:9999:7",
        "imap-uid:",
    ):
        metadata = envelope(headers=f"Message-ID: {claimed}\r\n\r\n".encode())

        assert metadata.message_id == f"imap-uid:{UIDVALIDITY}:{UID}"

    # Bracket-wrapped, the value is an ordinary id again — nothing starts with
    # the prefix once ``<`` does — and it round-trips through the search like
    # any other.
    wrapped = envelope(headers=b"Message-ID: <imap-uid:9999:7>\r\n\r\n")

    assert wrapped.message_id == "<imap-uid:9999:7>"


def test_an_identity_longer_than_the_narrowest_column_falls_back_to_the_uid() -> None:
    # 128 characters is the width of ``quarantined_messages.mailbox_message_id``
    # (``communications`` allows 320): an identity a table cannot hold is an
    # identity that cannot be recorded, and a cycle that cannot record a
    # refused message never advances its cursor.
    at_the_bound = "<" + "a" * 126 + ">"
    too_long = "<" + "a" * 127 + ">"
    assert len(at_the_bound) == 128
    assert len(too_long) == 129

    kept = envelope(headers=f"Message-ID: {at_the_bound}\r\n\r\n".encode())
    fell_back = envelope(headers=f"Message-ID: {too_long}\r\n\r\n".encode())

    assert kept.message_id == at_the_bound
    assert fell_back.message_id == f"imap-uid:{UIDVALIDITY}:{UID}"


def test_every_identity_the_rule_mints_fits_the_narrowest_column() -> None:
    # Whatever a sender writes, the id this deployment records is bounded.
    for header in (None, "imap-uid:1:2", "x" * 5_000, "<" + "y" * 2_000 + "@example>"):
        identity = message_identity(header, uidvalidity=1234, uid=42)
        assert len(identity) <= MAX_MESSAGE_ID_CHARS


# --- The search day ------------------------------------------------------


def test_the_search_day_is_written_in_imaps_own_english_abbreviations() -> None:
    # ``strftime("%b")`` is locale-dependent; a search string the server
    # cannot parse is a cycle that reads nothing.
    assert imap_since_day(datetime(2026, 9, 5, 13, 0, tzinfo=UTC)) == "05-Sep-2026"
    assert imap_since_day(datetime(2026, 1, 31, 23, 0, tzinfo=UTC)) == "31-Jan-2026"
    assert imap_since_day(datetime(2026, 12, 1, 0, 0, tzinfo=UTC)) == "01-Dec-2026"
