"""The IMAP read fallback's client, against a faithful fake server (ADR-0005).

The fallback ADR-0005 prescribes for a TCSP on corporate Exchange: the same
dedicated mailbox, read with the app password the SMTP leg sends with, over
``imaplib`` and one connection per call. ``tests/fake_imap.py`` answers the
commands the client sends — and refuses the ones it must never send — so this
file can pin what a real server would judge: the read-only select, header-only
envelope fetches, a full fetch only when the client asks for one, the
``SINCE``/``HEADER`` searches, and refusals that carry the server's own code
and never its text.

No socket and no database. The full ingestion cycle over this leg is
``test_mail_ingest_imap_scenarios_db.py``; the provider precedence that chooses
the leg is ``test_mail_readers.py``.
"""

import logging
import ssl
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.mail import imap_client
from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.domain.mail import MailResponseInvalid
from tests.fake_imap import FakeImap

HOST = "imap.tcsp.example"
PORT = 993
TIMEOUT = 5.0
MAILBOX = "ai-filings@tcsp.example"
PASSWORD = "app-password-marker-never-logged"
# The fake's clock, and the instant a staged cursor sits at.
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)


def client_for(fake: FakeImap, monkeypatch: pytest.MonkeyPatch, **overrides: object):
    """The real client, with the fake wired into its one connection factory.

    ``allow_plaintext`` is stated rather than defaulted because it decides
    whether a credential may cross the wire in the clear (the fake stands in
    for the connection itself, so nothing here is actually cleartext).
    """
    monkeypatch.setattr(imap_client, "connect", fake.connect)
    settings: dict[str, object] = {
        "host": HOST,
        "port": PORT,
        "username": MAILBOX,
        "password": fake.password,
        "timeout": TIMEOUT,
        "allow_plaintext": False,
    }
    settings.update(overrides)
    return imap_client.ImapClient(**settings)  # type: ignore[arg-type]


def mailbox(*, uidvalidity: int = 1234) -> FakeImap:
    return FakeImap(username=MAILBOX, password=PASSWORD, uidvalidity=uidvalidity)


# --- The window ----------------------------------------------------------


async def test_the_window_is_the_mailboxs_own_order_and_the_listing_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    for index in range(3):
        fake.add_message(subject=f"Reply {index}")
    client = client_for(fake, monkeypatch)

    listing = await client.list_message_ids(cursor=None, max_results=50)

    # Oldest first, oldest by UID (the mailbox's own order), and each id is the
    # envelope's ``Message-ID`` — what a re-fetch will recognize.
    assert listing.message_ids == tuple(fake.identity_of(uid) for uid in (1, 2, 3))
    assert listing.query == "ALL"
    # The mailbox is opened read-only (EXAMINE), and reading it left no mark:
    # no flag was set, nothing was written, nothing was expunged.
    assert fake.selects == [("INBOX", True)]
    assert fake.seen == set()
    assert fake.writes == []
    assert sorted(fake.messages) == [1, 2, 3]
    # One conversation per call, and the password never appears in the record.
    assert fake.logins == [MAILBOX]
    assert fake.logouts == 1
    assert PASSWORD not in " ".join(fake.commands)


async def test_the_first_read_is_bounded_to_the_newest_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before the first cursor the bound decides how far back a deployment
    # reaches — the same rule the provider leg's first cycle has — so the
    # oldest messages of a mailbox a deployment has just connected are not
    # what it ingests.
    fake = mailbox()
    for index in range(4):
        fake.add_message(subject=f"Reply {index}")
    client = client_for(fake, monkeypatch)

    listing = await client.list_message_ids(cursor=None, max_results=2)

    assert listing.message_ids == tuple(fake.identity_of(uid) for uid in (3, 4))
    assert fake.fetches == [(3, "headers"), (4, "headers")]


async def test_the_window_is_searched_by_day_and_trimmed_by_the_cursors_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    fake.add_message(subject="before the cursor", received_at=BASE + timedelta(minutes=1))
    fake.add_message(subject="exactly at the cursor", received_at=SINCE)
    fake.add_message(subject="inside the window", received_at=BASE + timedelta(minutes=45))
    client = client_for(fake, monkeypatch)

    listing = await client.list_message_ids(cursor=SINCE, max_results=50)

    # The window's lower edge is the cursor's own instant, *inclusive* — the
    # message that shares it (or shares a second with the newest message a
    # cycle examined) is listed again, which is what the overlap margin is for,
    # and the day's earlier messages are trimmed here rather than by the search.
    assert listing.message_ids == (fake.identity_of(2), fake.identity_of(3))
    # IMAP's SINCE compares the internal date disregarding time and timezone,
    # so the search asks for the day the cursor's own zone puts the margin in;
    # the instant comparison above is what makes the window exact.
    assert fake.searches == [("SINCE", "20-Sep-2026")]
    assert listing.query == "SINCE 20-Sep-2026"


async def test_a_burst_is_finished_on_later_cycles_rather_than_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cycle bounded below the size of its window takes the window's *oldest*
    # messages: the cursor then advances only over what it finished, and the
    # rest are still above it next time. (The first read's bound takes the
    # newest, above, because there is no cursor to leave anything above.)
    fake = mailbox()
    for index in range(4):
        fake.add_message(
            subject=f"Reply {index}", received_at=SINCE + timedelta(minutes=index + 1)
        )
    client = client_for(fake, monkeypatch)

    listing = await client.list_message_ids(cursor=SINCE, max_results=2)

    assert listing.message_ids == (fake.identity_of(1), fake.identity_of(2))


# --- One message ---------------------------------------------------------


async def test_the_envelope_read_asks_for_headers_and_never_a_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    # Staged as the wire bytes a mailbox holds: what comes back is what the
    # headers say, verbatim, not a rendering of them.
    uid = fake.add_message(
        raw=(
            b"Message-ID: <reply-1@client.example>\r\n"
            b"From: CHAN TAI MAN <director@client.example>\r\n"
            b"Subject: Re: Annual Return Confirmation\r\n"
            b"Date: Tue, 23 Sep 2026 09:31:00 +0800\r\n"
            b"\r\n"
            b"BODY-MARKER-4471\r\n"
        )
    )
    client = client_for(fake, monkeypatch)
    identity = fake.identity_of(uid)

    metadata = await client.fetch_message_metadata(message_id=identity)

    assert metadata.message_id == identity
    assert metadata.subject == "Re: Annual Return Confirmation"
    assert metadata.sender == "CHAN TAI MAN <director@client.example>"
    assert metadata.date == "Tue, 23 Sep 2026 09:31:00 +0800"
    assert metadata.received_at == fake.messages[uid].received_at
    # IMAP has no thread of its own: null is the honest answer, and the reply
    # parser's join reads the message's own headers when it lands.
    assert metadata.thread_id is None
    assert fake.fetches == [(uid, "headers")]
    # The identity was found by searching the header, without its brackets —
    # the form both legs' lookups take.
    assert fake.searches == [("HEADER", "Message-ID", f'"{identity.strip("<>")}"')]
    assert fake.seen == set()


async def test_the_envelope_carries_folded_addressing_and_thread_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attribution decision reads the envelope and nothing else, and a raw
    # header block carries its folding: every value has to arrive unfolded,
    # with the addresses and ids the join matches on intact.
    fake = mailbox()
    uid = fake.add_message(
        raw=(
            b"Message-ID: <reply-1@client.example>\r\n"
            b"From: CHAN TAI MAN <director@client.example>\r\n"
            b"To: Wu Tai Man <wu@client.example>,\r\n"
            b"\tdirector@client.example,\r\n"
            b" WU@CLIENT.EXAMPLE\r\n"
            b"Cc: Ops <ops@client.example>,\r\n"
            b" ops@client.example\r\n"
            b"In-Reply-To: <filed-1@tcsp.example>\r\n"
            b"References: <filed-0@tcsp.example>\r\n"
            b"\t<filed-1@tcsp.example>\r\n"
            b"Auto-Submitted: auto-generated\r\n"
            b"Return-Path: <>\r\n"
            b"Subject: Re: Annual Return Confirmation\r\n"
            b"Date: Tue, 23 Sep 2026 09:31:00 +0800\r\n"
            b"\r\n"
            b"BODY-MARKER-4471\r\n"
        )
    )
    client = client_for(fake, monkeypatch)

    metadata = await client.fetch_message_metadata(message_id=fake.identity_of(uid))

    # Addresses in their comparison form, first occurrence first: display
    # names are dropped, and a repeat in another case is not a second entry.
    assert metadata.recipients_to == ("wu@client.example", "director@client.example")
    assert metadata.recipients_cc == ("ops@client.example",)
    # The header's own id beside the identity the message is recorded under,
    # and the thread headers as the tokens a join matches on.
    assert metadata.rfc_message_id == "<reply-1@client.example>"
    assert metadata.in_reply_to == ("<filed-1@tcsp.example>",)
    assert metadata.references == ("<filed-0@tcsp.example>", "<filed-1@tcsp.example>")
    # The anti-abuse headers verbatim: an auto-submitted message and an empty
    # envelope sender are the two the ask guard refuses on.
    assert metadata.auto_submitted == "auto-generated"
    assert metadata.return_path == "<>"
    # The headers the envelope always carried are unchanged by the new ones.
    assert metadata.subject == "Re: Annual Return Confirmation"
    assert metadata.sender == "CHAN TAI MAN <director@client.example>"
    assert metadata.date == "Tue, 23 Sep 2026 09:31:00 +0800"
    assert fake.seen == set()


async def test_an_envelope_without_the_new_headers_leaves_them_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    uid = fake.add_message(
        raw=(
            b"From: CHAN TAI MAN <director@client.example>\r\n"
            b"Subject: Re: Annual Return Confirmation\r\n"
            b"\r\n"
        )
    )
    client = client_for(fake, monkeypatch)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    metadata = await client.fetch_message_metadata(message_id=identity)

    # A message carrying none of them has no value at all rather than an
    # invented one — its Message-ID included, so the header's own id is None
    # while the identity is the UID fallback.
    assert metadata.message_id == identity
    assert metadata.recipients_to == ()
    assert metadata.recipients_cc == ()
    assert metadata.in_reply_to == ()
    assert metadata.references == ()
    assert metadata.rfc_message_id is None
    assert metadata.auto_submitted is None
    assert metadata.return_path is None


async def test_the_raw_read_is_the_whole_message_the_mailbox_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    uid = fake.add_message(body="BODY-MARKER-4471")
    client = client_for(fake, monkeypatch)
    identity = fake.identity_of(uid)

    raw = await client.fetch_message_raw(message_id=identity)

    # Byte for byte what the mailbox holds: this is what the records registry
    # stores and what the attachment and resolver paths parse.
    assert raw == fake.raw_of(uid)
    assert b"BODY-MARKER-4471" in raw
    assert fake.fetches == [(uid, "full")]


async def test_a_message_with_no_message_id_is_read_by_its_fallback_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    uid = fake.add_message(raw=b"Subject: no id in this one\r\n\r\n")
    client = client_for(fake, monkeypatch)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    metadata = await client.fetch_message_metadata(message_id=identity)

    assert metadata.message_id == identity
    assert metadata.subject == "no id in this one"
    # The id names its own UID, so there is nothing to search for.
    assert fake.searches == []
    assert fake.fetches == [(uid, "headers")]


async def test_a_fallback_id_from_another_mailbox_incarnation_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A UID is only meaningful inside one UIDVALIDITY: using one from another
    # incarnation could fetch a different message that now bears that UID.
    fake = mailbox(uidvalidity=1234)
    fake.add_message()
    client = client_for(fake, monkeypatch)

    with pytest.raises(MailProviderRefused) as refusal:
        await client.fetch_message_metadata(message_id="imap-uid:9999:1")

    assert refusal.value.reason == "not_found"
    assert fake.fetches == []
    assert fake.searches == []


@pytest.mark.parametrize("identity", ["imap-uid:oops", "imap-uid:1234", "imap-uid::1"])
async def test_an_identity_in_the_reserved_namespace_must_name_this_mailbox(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    # The prefix names this module's own namespace, so it is never searched for
    # as header text: a value in it that does not parse names nothing.
    fake = mailbox(uidvalidity=1234)
    fake.add_message(raw=f"Message-ID: {identity}\r\n\r\n".encode())
    client = client_for(fake, monkeypatch)

    with pytest.raises(MailProviderRefused) as refusal:
        await client.fetch_message_metadata(message_id=identity)

    assert refusal.value.reason == "not_found"
    assert fake.searches == []
    assert fake.fetches == []


async def test_a_message_id_claiming_the_fallback_namespace_is_read_by_its_own_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The crafted header is not an identity: the message is listed under the
    # UID fallback, and a read by that id resolves directly to the message
    # itself — never to whatever UID the header names.
    fake = mailbox(uidvalidity=1234)
    uid = fake.add_message(
        raw=b"Message-ID: imap-uid:9999:7\r\nSubject: dressed as a fallback\r\n\r\n"
    )
    client = client_for(fake, monkeypatch)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    listing = await client.list_message_ids(cursor=None, max_results=10)

    assert listing.message_ids == (identity,)
    searches = list(fake.searches)
    metadata = await client.fetch_message_metadata(message_id=identity)
    assert metadata.message_id == identity
    assert metadata.subject == "dressed as a fallback"
    assert fake.searches == searches  # the id names its own UID: no header search
    assert fake.fetches == [(uid, "headers"), (uid, "headers")]
    assert await client.fetch_message_raw(message_id=identity) == fake.raw_of(uid)


async def test_an_over_long_message_id_is_read_by_its_uid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Longer than the narrowest column that carries an identity: the header is
    # not usable as one, so the message is listed and read under its own UID —
    # bounded, and resolvable without a search.
    fake = mailbox(uidvalidity=1234)
    long_id = "<" + "a" * 400 + "@client.example>"
    uid = fake.add_message(
        raw=f"Message-ID: {long_id}\r\nSubject: long one\r\n\r\n".encode()
    )
    client = client_for(fake, monkeypatch)
    identity = f"imap-uid:{fake.uidvalidity}:{uid}"

    listing = await client.list_message_ids(cursor=None, max_results=10)

    assert listing.message_ids == (identity,)
    searches = list(fake.searches)
    metadata = await client.fetch_message_metadata(message_id=identity)
    assert metadata.message_id == identity
    assert metadata.subject == "long one"
    assert fake.searches == searches  # no header search: the id names the UID
    assert long_id not in " ".join(fake.commands)


async def test_an_id_the_mailbox_no_longer_holds_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    fake.add_message(message_id="<still-here@client.example>")
    client = client_for(fake, monkeypatch)

    with pytest.raises(MailProviderRefused) as refusal:
        await client.fetch_message_metadata(message_id="<deleted@client.example>")

    assert refusal.value.reason == "not_found"
    assert fake.fetches == []


async def test_each_read_is_its_own_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One connection per call — the SMTP leg's and the scanner's own shape —
    # so a read never depends on what a previous one left open. It costs a
    # login per read, and that is the stated price of an adapter that holds no
    # state between calls.
    fake = mailbox()
    uid = fake.add_message()
    client = client_for(fake, monkeypatch)
    identity = fake.identity_of(uid)

    await client.list_message_ids(cursor=None, max_results=10)
    await client.fetch_message_metadata(message_id=identity)
    await client.fetch_message_raw(message_id=identity)

    assert fake.logins == [MAILBOX] * 3
    assert fake.logouts == 3
    # Every one of them selected the mailbox read-only.
    assert fake.selects == [("INBOX", True)] * 3


async def test_the_address_is_the_mailbox_the_leg_authenticates_as(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ingestion cycle's self-sent rule and its audit entries name the
    # mailbox this leg reads: the account it authenticates as.
    fake = mailbox()

    assert client_for(fake, monkeypatch).address == MAILBOX


# --- Refusals ------------------------------------------------------------


async def test_a_refused_login_carries_the_servers_code_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    client = client_for(fake, monkeypatch, password="the-wrong-password")

    with pytest.raises(MailProviderRefused) as refusal:
        await client.list_message_ids(cursor=None, max_results=10)

    # IMAP's refusals carry a machine-readable code beside free text: only the
    # code travels, because the text can echo the account and the request.
    assert refusal.value.reason == "AUTHENTICATIONFAILED"
    assert "the-wrong-password" not in str(refusal.value)
    assert "refuse for the fake" not in str(refusal.value)
    assert fake.logouts == 1  # the conversation was still closed


async def test_a_server_that_cannot_be_reached_is_unavailable_and_its_text_stays_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(
        *, host: str, port: int, timeout: float, allow_plaintext: bool
    ) -> object:
        raise OSError("no route to host: 10.0.0.1")

    monkeypatch.setattr(imap_client, "connect", unreachable)
    client = imap_client.ImapClient(
        host=HOST,
        port=PORT,
        username=MAILBOX,
        password=PASSWORD,
        timeout=TIMEOUT,
        allow_plaintext=False,
    )

    with pytest.raises(MailProviderUnavailable) as failure:
        await client.list_message_ids(cursor=None, max_results=10)

    assert str(failure.value) == "unavailable:OSError"
    assert "10.0.0.1" not in str(failure.value)


# --- The transport (final review, finding 2) ------------------------------


def test_tls_is_the_transport_on_every_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old rule — 993 encrypted, every other port plain-text — made a
    mistyped or corporate port a silent cleartext session carrying the app
    password and a client's mail. The port decides nothing now: every port gets
    ``IMAP4_SSL`` (verified through ``tls_context``), and a plain-text
    connection exists only behind the development-only setting."""
    chosen: list[tuple[str, int]] = []

    def record_ssl(host: str, port: int, *, ssl_context: object, timeout: float) -> object:
        chosen.append(("ssl", port))
        return object()

    def record_plain(host: str, port: int, *, timeout: float) -> object:
        chosen.append(("plain", port))
        return object()

    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", record_ssl)
    monkeypatch.setattr(imap_client.imaplib, "IMAP4", record_plain)

    for port in (993, 143, 1993, 8443):
        imap_client.connect(host=HOST, port=port, timeout=TIMEOUT, allow_plaintext=False)
    imap_client.connect(host=HOST, port=143, timeout=TIMEOUT, allow_plaintext=True)

    assert chosen == [
        ("ssl", 993),
        ("ssl", 143),
        ("ssl", 1993),
        ("ssl", 8443),
        ("plain", 143),  # the development-only branch, and only on request
    ]


async def test_a_server_that_only_speaks_cleartext_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server whose only protocol is cleartext is refused — with the policy
    named rather than an outage reported (the two send an operator to different
    remedies) — and the refusal happens at the connection, so the app password
    is never offered."""
    logins: list[str] = []

    def cleartext_server(
        *, host: str, port: int, timeout: float, allow_plaintext: bool
    ) -> object:
        # What OpenSSL raises when the peer answers the TLS ClientHello with a
        # plaintext banner: the server's own greeting read as a record.
        raise ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1016)")

    def record_login(self: object, connection: object) -> None:
        logins.append("login")

    monkeypatch.setattr(imap_client, "connect", cleartext_server)
    monkeypatch.setattr(imap_client.ImapClient, "_login", record_login)
    client = imap_client.ImapClient(
        host=HOST,
        port=PORT,
        username=MAILBOX,
        password=PASSWORD,
        timeout=TIMEOUT,
        allow_plaintext=False,
    )

    with pytest.raises(MailProviderRefused) as refusal:
        await client.list_message_ids(cursor=None, max_results=10)

    assert refusal.value.reason == "plaintext_not_allowed"
    assert logins == []  # no credential was ever presented
    assert PASSWORD not in str(refusal.value)


async def test_a_tls_failure_that_is_not_cleartext_keeps_its_own_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverifiable certificate is not a cleartext server: it is one this
    deployment could not verify, and that keeps the reachability answer (with
    the class name and nothing of the error's text)."""
    def bad_certificate(
        *, host: str, port: int, timeout: float, allow_plaintext: bool
    ) -> object:
        raise ssl.SSLError(
            1,
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate (_ssl.c:1016)",
        )

    monkeypatch.setattr(imap_client, "connect", bad_certificate)
    client = imap_client.ImapClient(
        host=HOST,
        port=PORT,
        username=MAILBOX,
        password=PASSWORD,
        timeout=TIMEOUT,
        allow_plaintext=False,
    )

    with pytest.raises(MailProviderUnavailable) as failure:
        await client.list_message_ids(cursor=None, max_results=10)

    assert str(failure.value) == "unavailable:SSLError"
    assert "self-signed" not in str(failure.value)


@pytest.mark.parametrize("status", ["NO", "BAD"])
async def test_a_search_or_fetch_the_server_refuses_is_a_bounded_refusal(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    fake = mailbox()
    uid = fake.add_message()
    client = client_for(fake, monkeypatch)
    identity = fake.identity_of(uid)
    fake.search_status = status
    fake.fetch_status = status

    with pytest.raises(MailProviderRefused) as search:
        await client.list_message_ids(cursor=None, max_results=10)
    with pytest.raises(MailProviderRefused) as fetch:
        await client.fetch_message_metadata(message_id=identity)

    assert search.value.reason == "imap_refused"
    assert fetch.value.reason == "imap_refused"
    assert "refused by the fake" not in str(fetch.value)


async def test_an_answer_that_is_not_a_fetch_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mailbox()
    uid = fake.add_message()
    client = client_for(fake, monkeypatch)
    fake.malformed_fetch = True

    with pytest.raises(MailProviderRefused) as refusal:
        await client.fetch_message_raw(message_id=fake.identity_of(uid))

    assert refusal.value.reason == "malformed_response"


async def test_a_receipt_time_the_parser_cannot_read_is_the_parsers_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The receipt time is the ingestion cursor: a server that answers with
    # something else stops the cycle rather than moving a cursor somewhere
    # arbitrary. It is the domain parser's refusal, as on the provider leg.
    fake = mailbox()
    uid = fake.add_message()
    client = client_for(fake, monkeypatch)
    fake.internal_date_override = "yesterday"

    with pytest.raises(MailResponseInvalid) as refusal:
        await client.fetch_message_metadata(message_id=fake.identity_of(uid))

    assert refusal.value.reason == "invalid_field:internalDate"


async def test_an_unreachable_mailbox_reaches_no_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = mailbox()
    client = client_for(fake, monkeypatch, password="the-wrong-password")

    with pytest.raises(MailProviderRefused):
        await client.list_message_ids(cursor=None, max_results=10)

    assert "the-wrong-password" not in caplog.text
