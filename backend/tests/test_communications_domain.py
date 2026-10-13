"""Domain rules for one stored inbound message (ADR-0005; data-model §2.9).

Pure: what a stored ``.eml`` says — its sender, its attachment parts with
their declared types and decoded bytes — and how an address is normalised for
attribution. Body *text* is read for one purpose only, and coarsely: the
multi-company resolver needs the sentence that names a company, so
``message_text`` returns the message's plain-text body, collapsed and capped,
and an HTML-only message returns nothing at all. Reading what a reply *means*
is the reply parser's slice, and it works from the stored raw message.

The messages here are built with the standard library's own MIME writer, so
what the parser is decoding is a real message, not a hand-rolled approximation
of one.
"""

from email.message import EmailMessage

from app.domain.communications import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_MESSAGE_BYTES_READ,
    MAX_MESSAGE_TEXT_CHARS,
    addresses_in_header,
    message_text,
    normalized_address,
    parse_inbound_message,
    sender_address,
)


def message(
    *,
    sender: str | None = "Director <director@client.example>",
    subject: str | None = "NAR1 confirmation",
    body: str = "Everything is unchanged.",
    attachments: list[tuple[str, str, bytes]] | None = None,
    payload: str | None = None,
) -> bytes:
    """One RFC 822 message, the way a mail client would write it."""
    if payload is not None:
        return payload.encode("utf-8")
    built = EmailMessage()
    if sender is not None:
        built["From"] = sender
    if subject is not None:
        built["Subject"] = subject
    built["To"] = "ai-filings@tcsp.example"
    built.set_content(body)
    for filename, content_type, data in attachments or []:
        maintype, _, subtype = content_type.partition("/")
        built.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return built.as_bytes()


def test_a_simple_message_reports_its_sender_and_subject() -> None:
    parsed = parse_inbound_message(message())

    assert parsed.sender == "director@client.example"
    assert parsed.subject == "NAR1 confirmation"
    assert parsed.attachments == ()


def test_the_body_is_not_an_attachment() -> None:
    parsed = parse_inbound_message(message(body="One line of answer."))

    assert parsed.attachments == ()


def test_an_attachment_comes_back_with_its_type_and_bytes() -> None:
    parsed = parse_inbound_message(
        message(attachments=[("signed.pdf", "application/pdf", b"%PDF-1.7 signed")])
    )

    assert len(parsed.attachments) == 1
    attachment = parsed.attachments[0]
    assert attachment.filename == "signed.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.data == b"%PDF-1.7 signed"


def test_several_attachments_keep_their_order() -> None:
    parsed = parse_inbound_message(
        message(
            attachments=[
                ("one.pdf", "application/pdf", b"%PDF-1.7 one"),
                ("two.png", "image/png", b"\x89PNG\r\n\x1a\ntwo"),
            ]
        )
    )

    assert [attachment.filename for attachment in parsed.attachments] == ["one.pdf", "two.png"]
    assert [attachment.content_type for attachment in parsed.attachments] == [
        "application/pdf",
        "image/png",
    ]


def test_a_message_with_more_parts_than_the_cap_keeps_the_first_ones() -> None:
    attachments = [
        (f"file-{index}.pdf", "application/pdf", b"%PDF-1.7 " + str(index).encode())
        for index in range(MAX_ATTACHMENTS_PER_MESSAGE + 5)
    ]

    parsed = parse_inbound_message(message(attachments=attachments))

    assert len(parsed.attachments) == MAX_ATTACHMENTS_PER_MESSAGE
    assert parsed.attachments[0].filename == "file-0.pdf"
    # The count is what lets the caller report the rest instead of dropping it.
    assert parsed.attachment_count == MAX_ATTACHMENTS_PER_MESSAGE + 5


def test_a_message_within_the_cap_counts_every_attachment() -> None:
    parsed = parse_inbound_message(
        message(attachments=[("a.pdf", "application/pdf", b"%PDF-1.7 a")])
    )

    assert parsed.attachment_count == len(parsed.attachments) == 1
    assert parse_inbound_message(message()).attachment_count == 0


def test_a_text_only_message_needs_no_attachments_to_parse() -> None:
    parsed = parse_inbound_message(message(sender="a@b", subject=None))

    assert parsed.sender == "a@b"
    assert parsed.subject is None


def test_a_body_the_sender_mangled_is_still_a_message() -> None:
    # Not a well-formed message: no headers at all. It parses to nothing
    # rather than raising, because the stored bytes are the record.
    parsed = parse_inbound_message(b"just some text, no headers\n")

    assert parsed.sender is None
    assert parsed.subject is None
    assert parsed.attachments == ()


def test_a_nested_message_part_is_not_an_attachment() -> None:
    inner = EmailMessage()
    inner["From"] = "someone@client.example"
    inner["Subject"] = "forwarded"
    inner.set_content("forwarded body")
    outer = EmailMessage()
    outer["From"] = "Director <director@client.example>"
    outer["Subject"] = "Fwd: NAR1"
    outer.set_content("see attached")
    outer.add_attachment(inner, filename="forwarded.eml")

    parsed = parse_inbound_message(outer.as_bytes())

    # The forwarded message is inside the stored raw message and the scanner
    # reads those bytes; it is not decoded into a second stored document.
    assert parsed.sender == "director@client.example"
    assert parsed.attachments == ()


def test_sender_address_reads_the_bare_address() -> None:
    assert sender_address("Director <d@client.example>") == "d@client.example"
    assert sender_address("d@client.example") == "d@client.example"
    assert sender_address('"Chan, Tai Man" <chan@client.example>') == "chan@client.example"
    assert sender_address("") is None
    assert sender_address(None) is None


def test_a_normalised_address_is_the_comparison_form() -> None:
    assert normalized_address("  Director@Client.Example ") == "director@client.example"


def test_addresses_in_header_reads_every_address_in_its_comparison_form() -> None:
    # One header can carry several addresses (the boss-on-copy case the
    # attribution rule reads), and each is the bare, case-folded form the
    # registered channels compare in.
    assert addresses_in_header("Wu Tai Man <wu@client.example>, director@client.example") == (
        "wu@client.example",
        "director@client.example",
    )
    # The first occurrence wins and a repeat in another case does not become a
    # second entry; an empty entry does not become one either.
    assert addresses_in_header(
        "WU@CLIENT.EXAMPLE, wu@client.example,, Ops <ops@client.example>"
    ) == ("wu@client.example", "ops@client.example")
    # Folding whitespace is not part of an address.
    assert addresses_in_header("Boss <boss@client.example>,\r\n assistant@client.example") == (
        "boss@client.example",
        "assistant@client.example",
    )


def test_addresses_in_header_is_empty_when_no_address_is_written() -> None:
    assert addresses_in_header("") == ()
    assert addresses_in_header("undisclosed-recipients:;") == ()


def test_message_text_reads_the_plain_body() -> None:
    assert message_text(message(body="One line of answer.")) == "One line of answer."


def test_message_text_collapses_whitespace() -> None:
    assert message_text(message(body="  Line one.\n\n\tLine   two.  ")) == "Line one. Line two."


def test_an_html_only_message_has_no_text() -> None:
    # Converting HTML is the parser's job, not this rule's: a message with no
    # text/plain part has no text here, and an unresolvable message
    # quarantines, which is the safe outcome.
    html = EmailMessage()
    html["From"] = "Director <director@client.example>"
    html["Subject"] = "NAR1 confirmation"
    html.set_content("<p>Everything is unchanged.</p>", subtype="html")

    assert message_text(html.as_bytes()) == ""


def test_message_text_is_capped() -> None:
    # The cap's value is pinned here rather than read from the module under
    # test: raising it is a decision somebody makes on purpose, in a test,
    # and not something this assertion can quietly follow.
    assert MAX_MESSAGE_TEXT_CHARS == 8_000

    text = message_text(message(body="x" * (MAX_MESSAGE_TEXT_CHARS + 500)))

    assert len(text) == 8_000


def test_a_message_with_no_body_has_no_text() -> None:
    headers_only = EmailMessage()
    headers_only["From"] = "Director <director@client.example>"
    headers_only["Subject"] = "NAR1 confirmation"

    assert message_text(b"") == ""
    assert message_text(headers_only.as_bytes()) == ""


def test_a_text_attachment_is_not_the_message_body() -> None:
    raw = message(
        body="See attached.",
        attachments=[("notes.txt", "text/plain", b"BRN 12345678")],
    )

    assert message_text(raw) == "See attached."


def test_a_message_cut_by_the_read_bound_has_no_text() -> None:
    # The read bound fails closed: a message whose text/plain part sits past
    # it has no text here, so it resolves nothing and quarantines rather than
    # being read at a cost the sender chooses.
    raw = (
        b"From: Director <director@client.example>\r\n"
        b"Subject: NAR1 confirmation\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="big.bin"\r\n'
        b"\r\n" + b"Q" * (MAX_MESSAGE_BYTES_READ + 1024) + b"\r\n"
        b"--B\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"\r\n"
        b"Bright Lantern Design Limited confirmed.\r\n"
        b"--B--\r\n"
    )

    assert message_text(raw) == ""


def test_a_body_with_an_unreadable_charset_has_no_text() -> None:
    raw = (
        b"From: Director <director@client.example>\r\n"
        b"Subject: NAR1 confirmation\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="x-not-a-charset"\r\n'
        b"\r\n"
        b"BRN 12345678\r\n"
    )

    assert message_text(raw) == ""
