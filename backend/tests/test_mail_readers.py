"""Which leg reads the mailbox, and the surface both legs present (ADR-0005).

The ingestion seam's provider rule, mirroring the sending slice's: a connected
Gmail credential means the provider API; otherwise the configured IMAP
settings mean the fallback; otherwise the deployment has no mailbox to read.
The rule lives in one place (``app.application.mail.reader``) and both callers
— the ingestion cycle and the §2.5 quarantine retry — acquire their reader
through it, so neither can drift into a provider decision of its own.

Both legs are pinned here against their fakes (``tests/fake_google.py``,
``tests/fake_imap.py``), with no socket and no database: the same reads, in the
same order, returning the same domain shapes — which is what lets the cycle
above them be one code path. The full cycle over IMAP is
``test_mail_ingest_imap_scenarios_db.py``.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.mail import imap_client
from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.gmail_client import GmailClient
from app.application import mail
from app.core.config import settings
from app.domain.mail import GMAIL_SCOPES, MailboxCredential, MailConnectionState
from tests.fake_google import FakeGoogle
from tests.fake_imap import FakeImap

CLIENT_ID = "the-deployment-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "the-deployment-client-secret"
MAILBOX = "ai-filings@tcsp.example"
IMAP_HOST = "imap.tcsp.example"
IMAP_PASSWORD = "app-password-marker-never-logged"
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def configure_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> None:
    """The deployment's Google client, and the fake standing in for Google."""
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    google.client_id = CLIENT_ID
    google.client_secret = CLIENT_SECRET
    monkeypatch.setattr(
        mail,
        "client",
        lambda: GmailClient(timeout=1.0, transport=httpx.ASGITransport(app=google.app)),
    )


def configure_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeImap) -> None:
    """The mailbox and app password the fallback reads with — and the fake."""
    monkeypatch.setattr(settings, "imap_host", IMAP_HOST)
    monkeypatch.setattr(settings, "imap_port", imap_client.TLS_PORT)
    monkeypatch.setattr(settings, "mail_address", fake.username)
    monkeypatch.setattr(settings, "mail_app_password", fake.password)
    monkeypatch.setattr(imap_client, "connect", fake.connect)


def plant_credential(*, expires_at: datetime | None = None) -> MailboxCredential:
    """A credential as the completed consent flow would have left it."""
    credential = MailboxCredential(
        access_token="planted-access-token",
        refresh_token="planted-refresh-token",
        address=MAILBOX,
        scopes=GMAIL_SCOPES,
        connected_at=NOW,
        token_expires_at=expires_at or NOW + timedelta(hours=1),
    )
    MailCredentialStore(settings.mail_credential_file).save(
        MailConnectionState(credential=credential)
    )
    return credential


# --- The rule ------------------------------------------------------------


async def test_a_connected_credential_reads_through_the_provider_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = FakeGoogle(address=MAILBOX)
    configure_google(monkeypatch, google)
    # Both legs configured: the provider API wins, the rule the send path has.
    imap = FakeImap(username=MAILBOX, password=IMAP_PASSWORD)
    configure_imap(monkeypatch, imap)
    plant_credential()
    google.accept_access_token("planted-access-token")

    reader = await mail.reader(now=NOW)

    assert isinstance(reader, mail.GmailMailboxReader)
    assert reader.address == MAILBOX


async def test_the_configured_imap_settings_are_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeImap(username=MAILBOX, password=IMAP_PASSWORD)
    uid = fake.add_message(subject="Re: Annual Return Confirmation")
    configure_imap(monkeypatch, fake)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")

    reader = await mail.reader(now=NOW)

    assert isinstance(reader, imap_client.ImapClient)
    assert reader.address == MAILBOX
    # The fallback presents the same surface: a window oldest first, one
    # message's envelope, one message's bytes.
    listing = await reader.list_message_ids(cursor=None, max_results=50)
    assert listing.message_ids == (fake.identity_of(uid),)
    assert listing.query == "ALL"
    metadata = await reader.fetch_message_metadata(message_id=listing.message_ids[0])
    assert metadata.message_id == fake.identity_of(uid)
    assert metadata.subject == "Re: Annual Return Confirmation"
    assert await reader.fetch_message_raw(message_id=listing.message_ids[0]) == fake.raw_of(uid)


async def test_the_imap_leg_is_built_with_the_deployments_transport_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport is configuration, and its default is TLS: the reader
    passes the deployment's ``AICS_IMAP_ALLOW_PLAINTEXT`` through to the leg,
    so a cleartext connection needs an operator to ask for one (the
    development-only setting) — and a configured port never chooses it on its
    own."""
    fake = FakeImap(username=MAILBOX, password=IMAP_PASSWORD)
    fake.add_message()
    configure_imap(monkeypatch, fake)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")

    reader = await mail.reader(now=NOW)
    await reader.list_message_ids(cursor=None, max_results=10)

    assert fake.connections[-1] == (IMAP_HOST, imap_client.TLS_PORT, False)

    monkeypatch.setattr(settings, "imap_allow_plaintext", True)
    opted_in = await mail.reader(now=NOW)
    await opted_in.list_message_ids(cursor=None, max_results=10)

    assert fake.connections[-1] == (IMAP_HOST, imap_client.TLS_PORT, True)


async def test_neither_leg_is_a_deployment_with_no_mailbox_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("google_client_id", "google_client_secret", "mail_address",
                 "mail_app_password", "imap_host"):
        monkeypatch.setattr(settings, name, "")

    with pytest.raises(mail.MailNotConnected):
        await mail.reader(now=NOW)


@pytest.mark.parametrize("missing", ["mail_address", "mail_app_password"])
async def test_imap_settings_without_the_credential_pair_are_not_configured(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    # The same three values the SMTP leg requires, because it is the same
    # mailbox and the same app password: a host on its own is not a mailbox.
    fake = FakeImap(username=MAILBOX, password=IMAP_PASSWORD)
    configure_imap(monkeypatch, fake)
    monkeypatch.setattr(settings, missing, "")

    with pytest.raises(mail.MailNotConnected):
        await mail.reader(now=NOW)


async def test_a_broken_grant_is_reported_and_never_falls_back_to_imap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deployment has *two* legs configured, and the one an operator made is
    # broken: hiding that behind the fallback would leave them believing a
    # connection works. The send path states the same rule for SMTP.
    google = FakeGoogle(address=MAILBOX)
    google.token_status = 400
    configure_google(monkeypatch, google)
    imap = FakeImap(username=MAILBOX, password=IMAP_PASSWORD)
    configure_imap(monkeypatch, imap)
    plant_credential(expires_at=NOW - timedelta(minutes=5))

    with pytest.raises(mail.MailRefreshFailed) as failure:
        await mail.reader(now=NOW)

    assert failure.value.reason == "invalid_grant"
    assert google.refreshes == 0  # the grant was refused, so nothing was renewed
    assert imap.logins == []  # and the fallback was not read instead


# --- The provider leg's own surface --------------------------------------


async def test_the_provider_leg_reads_the_window_oldest_first_and_parses_its_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = FakeGoogle(address=MAILBOX)
    older = google.with_message(
        subject="Older",
        sender="Director <d@client.example>",
        date="Mon, 22 Sep 2026",
        received_at=NOW - timedelta(minutes=10),
    )
    newer = google.with_message(
        subject="Newer",
        sender="Director <d@client.example>",
        date="Tue, 23 Sep 2026",
        received_at=NOW - timedelta(minutes=5),
    )
    configure_google(monkeypatch, google)
    plant_credential()
    google.accept_access_token("planted-access-token")
    reader = await mail.reader(now=NOW)

    listing = await reader.list_message_ids(cursor=NOW - timedelta(minutes=25), max_results=50)

    # The API answers newest first; the surface a cycle consumes is oldest
    # first, whichever leg answered.
    assert listing.message_ids == (older, newer)
    assert listing.query is not None and listing.query.startswith("after:")
    assert google.list_queries == [listing.query]
    metadata = await reader.fetch_message_metadata(message_id=older)
    assert metadata.message_id == older
    assert metadata.subject == "Older"
    assert metadata.thread_id == f"thread-{older}"
    assert await reader.fetch_message_raw(message_id=older) == google.raw_of(older)
    # And the reads asked for their shapes only: metadata for the envelope,
    # raw for the bytes.
    assert google.message_format == "raw"
    assert google.raw_fetches == [older]


async def test_the_provider_envelope_carries_the_addressing_and_thread_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attribution decision reads the envelope and nothing else: who a
    # message was addressed to, and which message it answers, are the facts
    # the registration/confirmation flow is decided on before any body is read.
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Re: Annual Return Confirmation",
        sender="Director <director@client.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
        to="Wu Tai Man <wu@client.example>, director@client.example, WU@CLIENT.EXAMPLE",
        cc="Ops <ops@client.example>, ops@client.example",
        message_id="<reply-1@client.example>",
        in_reply_to="<filed-1@tcsp.example>",
        references="<filed-0@tcsp.example> <filed-1@tcsp.example>",
        auto_submitted="auto-generated",
        return_path="<>",
    )
    configure_google(monkeypatch, google)
    plant_credential()
    google.accept_access_token("planted-access-token")
    reader = await mail.reader(now=NOW)

    metadata = await reader.fetch_message_metadata(message_id=message_id)

    # Addresses in their comparison form, first occurrence first: display
    # names are dropped, and a repeat in another case is not a second entry.
    assert metadata.recipients_to == ("wu@client.example", "director@client.example")
    assert metadata.recipients_cc == ("ops@client.example",)
    # The header's own id beside the provider id the message is recorded
    # under, and the thread headers as the tokens a join matches on.
    assert metadata.rfc_message_id == "<reply-1@client.example>"
    assert metadata.message_id == message_id
    assert metadata.in_reply_to == ("<filed-1@tcsp.example>",)
    assert metadata.references == ("<filed-0@tcsp.example>", "<filed-1@tcsp.example>")
    # The anti-abuse headers verbatim: an auto-submitted message and an empty
    # envelope sender are the two the ask guard refuses on.
    assert metadata.auto_submitted == "auto-generated"
    assert metadata.return_path == "<>"


async def test_an_envelope_without_addressing_or_thread_headers_leaves_them_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Annual Return Confirmation",
        sender="Director <director@client.example>",
        date="Tue, 23 Sep 2026 09:31:00 +0800",
    )
    configure_google(monkeypatch, google)
    plant_credential()
    google.accept_access_token("planted-access-token")
    reader = await mail.reader(now=NOW)

    metadata = await reader.fetch_message_metadata(message_id=message_id)

    # To defaults to the mailbox's own address — the way a client's mail
    # reaches the dedicated mailbox — and everything the fixture did not set
    # has no value at all rather than an invented one.
    assert metadata.recipients_to == (MAILBOX,)
    assert metadata.recipients_cc == ()
    assert metadata.in_reply_to == ()
    assert metadata.references == ()
    assert metadata.rfc_message_id is None
    assert metadata.auto_submitted is None
    assert metadata.return_path is None


async def test_a_renewable_credential_is_renewed_before_the_cycle_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reader acquisition is where the token renewal happens (one place, so
    # the probe and the cycle cannot drift), and the renewed credential is what
    # the reads carry.
    google = FakeGoogle(address=MAILBOX)
    message_id = google.with_message(
        subject="Older", sender="a@client.example", date="Mon, 22 Sep 2026"
    )
    configure_google(monkeypatch, google)
    stale = plant_credential(expires_at=NOW + timedelta(seconds=5))
    google.access_tokens[stale.access_token] = "consent"
    google.refresh_tokens.add(stale.refresh_token)

    reader = await mail.reader(now=NOW)
    listing = await reader.list_message_ids(cursor=None, max_results=50)

    assert google.refreshes == 1
    assert listing.message_ids == (message_id,)
    stored = MailCredentialStore(settings.mail_credential_file).load().credential
    assert stored is not None
    assert stored.access_token != stale.access_token
    assert stored.refresh_token == stale.refresh_token
