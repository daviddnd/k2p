"""Connecting and using the dedicated mailbox (ADR-0005; Phase 3).

One mailbox, one connection, three administrative operations, one read, and
the **reader** every ingestion pass acquires:

- ``begin`` is what the Administrator's POST does: mint the ``state`` and the
  PKCE verifier, leave the pending attempt on disk, and return the consent
  URL the browser opens. The verifier exists nowhere else.
- ``complete`` is what the browser's return hits; the API layer passes it the
  code and the state and nothing else. It is **unauthenticated by necessity**
  — the browser arrives from Google carrying no session — so its authority is
  the pending attempt: the state must be the one this deployment minted, the
  attempt must not have expired, and the code is redeemable only with the
  verifier that never left this process. Success writes the credential
  (0600 file, never the database) and **consumes the attempt**, so the same
  callback cannot be replayed.
- ``disconnect`` forgets the credential and any pending attempt. The tokens
  are dropped rather than revoked at Google: the TCSP's own Google account
  page is where a grant is revoked, and the deployment's copy is what this
  operation removes.
- ``probe`` is the proof the read path works: the mailbox profile and the
  metadata of the newest messages, nothing else. It renews the access token
  when it is stale, and a renewal that Google refuses is a typed error the
  caller reports — never a 500.
- ``reader`` is what the ingestion cycle and the §2.5 quarantine retry read
  with, and it applies ADR-0005's precedence exactly as the sending slice
  applies it to a send: **a connected Gmail credential means the provider
  API; otherwise the configured IMAP settings mean the fallback; otherwise the
  deployment has no mailbox to read**. It is the one place that decision is
  made for reading, so the cycle and the retry cannot drift apart on it — and
  a credential whose grant can no longer be renewed is reported, never
  silently fallen back from.

The connection writes are audited (ADR-0009) against the administrator who
started the attempt, because the callback has no actor of its own and the
attempt records who it was. A routine access-token renewal is not an audit
event: no tenant data changed, and the chain is the tenant's record of its
compliance data (the same rule that keeps lease refreshes out of it). No
token, code, state, verifier, app password, or message is ever written to an
audit entry, a log, or the database.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.credential_store import MailCredentialStore
from app.adapters.mail.errors import MailProviderRefused
from app.adapters.mail.gmail_client import GmailClient
from app.adapters.mail.imap_client import ImapClient
from app.application import audit
from app.core.config import settings
from app.domain.activation import code_challenge_for, new_code_verifier
from app.domain.audit import AuditAction
from app.domain.mail import (
    CURSOR_OVERLAP_SECONDS,
    GMAIL_SCOPES,
    PENDING_TTL_SECONDS,
    TOKEN_REFRESH_LEEWAY_SECONDS,
    MailboxCredential,
    MailboxProfile,
    MailConnectionState,
    MailResponseInvalid,
    MessageListing,
    MessageMetadata,
    PendingMailConnection,
    address_matches,
    consent_url,
    new_state_token,
    parse_message_metadata,
    parse_messages_list,
    parse_profile_response,
    parse_raw_message_response,
    parse_token_response,
    state_matches,
)

# How many of the newest messages the probe describes. A handful is the
# point: the reviewer sees that the mailbox is readable, not a mailbox.
PROBE_MESSAGE_COUNT = 5


class MailNotConfigured(Exception):
    """This deployment has no Google OAuth client or no expected mailbox."""


class MailConnectionRefused(Exception):
    """The callback had nothing usable to complete, or Google refused it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MailNotConnected(Exception):
    """There is no mailbox credential to read with."""


class MailRefreshFailed(Exception):
    """The stored grant can no longer be exchanged for an access token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MailProbeRefused(Exception):
    """The provider refused a read even with a fresh access token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ConnectStart:
    """Where to send the administrator's browser, and when the attempt dies."""

    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True)
class ConnectionView:
    """The operator's view of the connection — never the tokens."""

    connected: bool
    address: str | None
    scopes: list[str]
    connected_at: datetime | None
    token_expires_at: datetime | None


@dataclass(frozen=True)
class ProbeResult:
    """The mailbox profile and the newest messages' metadata, nothing else."""

    profile: MailboxProfile
    messages: list[MessageMetadata]


def client() -> GmailClient:
    """The Gmail client for this deployment (the seam tests replace)."""
    return GmailClient(timeout=settings.mail_request_timeout_seconds)


class MailboxReader(Protocol):
    """The mailbox reads one ingestion pass consumes, whichever leg provides them.

    The surface is the ingestion cycle's own — the ids to examine (oldest
    first), one message's envelope, one message's bytes — so the cycle's
    per-message order is the same code whichever leg read the mailbox. Each
    implementation parses its provider's own wire shape into the domain's
    (``app.domain.mail``), which is why the cycle never sees a JSON body or a
    header block.
    """

    @property
    def address(self) -> str:
        """The mailbox read: what the self-sent rule compares and the records name."""
        ...

    async def list_message_ids(
        self, *, cursor: datetime | None, max_results: int
    ) -> MessageListing:
        """The window's message ids, oldest first, and the search behind them.

        ``cursor`` is the tenant's read position — the newest receipt time a
        cycle examined — or ``None`` for a mailbox this deployment has never
        read; ``max_results`` is the per-cycle bound, which the API applies to
        the newest matches (see each leg for which end of the window it takes).
        """
        ...

    async def fetch_message_metadata(self, *, message_id: str) -> MessageMetadata:
        """One message's envelope, by its recorded id — never its body."""
        ...

    async def fetch_message_raw(self, *, message_id: str) -> bytes:
        """One message's bytes, by its recorded id, exactly as the mailbox holds them."""
        ...


@dataclass(frozen=True)
class GmailMailboxReader:
    """The provider leg: a connected mailbox credential (ADR-0005 primary)."""

    credential: MailboxCredential
    client: GmailClient

    @property
    def address(self) -> str:
        return self.credential.address

    async def list_message_ids(
        self, *, cursor: datetime | None, max_results: int
    ) -> MessageListing:
        """The newest ``max_results`` messages of the window, oldest first.

        The window opens the overlap margin before the cursor, because Gmail's
        search is index-based rather than exact; the bound is the API's own,
        and it takes the newest matches. The cursor then advances over exactly
        what the cycle examined, and a message the bound did not reach is below
        the new cursor — the limitation contract §5.1.7 states.
        """
        query = None if cursor is None else _after(cursor)
        answer = await self.client.list_message_ids(
            access_token=self.credential.access_token,
            max_results=max_results,
            query=query,
        )
        # The API answers newest first; the surface is oldest first.
        return MessageListing(message_ids=tuple(reversed(parse_messages_list(answer))), query=query)

    async def fetch_message_metadata(self, *, message_id: str) -> MessageMetadata:
        return parse_message_metadata(
            await self.client.fetch_message_metadata(
                access_token=self.credential.access_token, message_id=message_id
            )
        )

    async def fetch_message_raw(self, *, message_id: str) -> bytes:
        return parse_raw_message_response(
            await self.client.fetch_message_raw(
                access_token=self.credential.access_token, message_id=message_id
            )
        )


def _after(cursor: datetime) -> str:
    """Gmail's window operator: everything strictly after the cursor's own instant.

    Gmail's search is an index rather than an exact filter, and the operator
    takes seconds, so the window opens the overlap margin before the cursor —
    the message at the boundary is listed again and costs one id check.
    """
    return f"after:{int(cursor.timestamp()) - CURSOR_OVERLAP_SECONDS}"


async def reader(*, now: datetime | None = None) -> MailboxReader:
    """Which leg reads the mailbox — the send path's one rule, applied to reads.

    A connected Gmail credential means the provider API; otherwise the
    configured IMAP settings (``AICS_IMAP_HOST``/``PORT``, with
    ``AICS_MAIL_ADDRESS`` and ``AICS_MAIL_APP_PASSWORD`` as the account and its
    app password — the same pair the SMTP leg sends with) mean the fallback;
    otherwise this deployment has no mailbox to read and ``MailNotConnected``
    is raised, which the ingestion trigger answers as its own 409
    ``not_connected``. There is deliberately no "choose a leg" setting.

    A credential whose grant can no longer be renewed is **not** a silent
    fallback to IMAP: the failure is reported (``MailRefreshFailed``) so an
    operator sees that the connection they made is broken — the rule the send
    path states for the same decision.

    The IMAP leg is built with ``settings.imap_allow_plaintext``, which is
    false everywhere but a development machine: with it false the leg speaks
    TLS on whatever port is configured, and a server whose only protocol is
    cleartext is refused (``plaintext_not_allowed``) rather than carrying the
    app password in the clear.
    """
    at = now if now is not None else datetime.now(UTC)
    try:
        credential = await usable_credential(now=at)
    except MailNotConnected:
        credential = None
    if credential is not None:
        return GmailMailboxReader(credential=credential, client=client())
    host = settings.imap_host.strip()
    address = settings.mail_address.strip()
    password = settings.mail_app_password.strip()
    if host and address and password:
        return ImapClient(
            host=host,
            port=settings.imap_port,
            username=address,
            password=password,
            timeout=settings.mail_request_timeout_seconds,
            allow_plaintext=settings.imap_allow_plaintext,
        )
    raise MailNotConnected


def is_configured() -> bool:
    """Whether the deployment can run a consent flow at all.

    All three values are required, and the expected mailbox is not optional:
    it is what the address guard compares against, so a deployment without
    one refuses the flow rather than consenting for whichever account the
    browser happens to be signed in to.
    """
    return bool(
        settings.google_client_id
        and settings.google_client_secret
        and settings.mail_address
    )


async def begin(*, local_tenant_id: UUID, local_user_id: UUID) -> ConnectStart:
    """Register a consent attempt and return the URL to open.

    Replaces any pending attempt: one connection flow at a time is what a
    single-mailbox deployment needs, and the newest attempt is the one the
    administrator is looking at.
    """
    if not is_configured():
        raise MailNotConfigured
    store = _store()
    state = store.load()
    minted_state = new_state_token()
    verifier = new_code_verifier()
    expires_at = datetime.now(UTC) + timedelta(seconds=PENDING_TTL_SECONDS)
    store.save(
        replace(
            state,
            pending=PendingMailConnection(
                state=minted_state,
                verifier=verifier,
                local_tenant_id=local_tenant_id,
                local_user_id=local_user_id,
                expires_at=expires_at,
            ),
        )
    )
    return ConnectStart(
        authorization_url=consent_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            state=minted_state,
            code_challenge=code_challenge_for(verifier),
        ),
        expires_at=expires_at,
    )


async def complete(
    session: AsyncSession, *, code: str, state: str | None, now: datetime | None = None
) -> MailboxCredential:
    """Redeem the browser's code and store the mailbox credential.

    Raises ``MailConnectionRefused`` for every local refusal (no attempt, an
    expired one, a mismatched state, a refused exchange, a mailbox that is
    not the configured one) and lets ``MailProviderUnavailable`` propagate:
    an unreachable Google is not a refusal, and nothing local changed. The
    attempt is consumed only by a success, which is what makes the callback
    single-use — the code itself is single-use at Google whatever happens
    here.
    """
    at = now if now is not None else datetime.now(UTC)
    store = _store()
    connection = store.load()
    pending = connection.pending
    if pending is None:
        raise MailConnectionRefused("no_pending_connection")
    if pending.is_expired(now=at):
        store.save(replace(connection, pending=None))
        raise MailConnectionRefused("connection_expired")
    if not state_matches(expected=pending.state, received=state):
        raise MailConnectionRefused("state_mismatch")
    try:
        raw = await client().exchange_code(
            code=code,
            code_verifier=pending.verifier,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=settings.google_redirect_uri,
        )
    except MailProviderRefused as exc:
        raise MailConnectionRefused(f"code_refused:{exc.reason}") from exc
    try:
        tokens = parse_token_response(raw)
    except MailResponseInvalid as exc:
        raise MailConnectionRefused(f"credentials_invalid:{exc.reason}") from exc
    if tokens.refresh_token is None:
        # Without a refresh token the connection would stop working within the
        # hour, and the consent URL asks for offline access precisely so this
        # cannot happen silently.
        raise MailConnectionRefused("credentials_invalid:refresh_token_missing")
    profile = await _fetch_profile(access_token=tokens.access_token, refusal=MailConnectionRefused)
    if not address_matches(expected=settings.mail_address, actual=profile.address):
        raise MailConnectionRefused("address_mismatch")
    credential = MailboxCredential(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        address=profile.address,
        scopes=tokens.scopes if tokens.scopes is not None else GMAIL_SCOPES,
        connected_at=at,
        token_expires_at=at + timedelta(seconds=tokens.expires_in),
    )
    await audit.record_event(
        session,
        tenant_id=pending.local_tenant_id,
        action=AuditAction.MAIL_CONNECTION_CREATED,
        actor_user_id=pending.local_user_id,
        entity_type="mail_connection",
        details={"address": credential.address, "scopes": list(credential.scopes)},
    )
    # The credential file is the last write: it is what makes the deployment
    # connected, and the attempt goes with it — a consumed attempt cannot be
    # replayed.
    store.save(MailConnectionState(credential=credential))
    return credential


def status() -> ConnectionView:
    """The connection as the operator reads it, from local state only.

    Deliberately no provider call: this view answers during an outage and
    costs nothing, and the probe is what checks the mailbox itself.
    """
    credential = _store().load().credential
    if credential is None:
        return ConnectionView(
            connected=False, address=None, scopes=[], connected_at=None, token_expires_at=None
        )
    return ConnectionView(
        connected=True,
        address=credential.address,
        scopes=list(credential.scopes),
        connected_at=credential.connected_at,
        token_expires_at=credential.token_expires_at,
    )


async def disconnect(
    session: AsyncSession, *, local_tenant_id: UUID, local_user_id: UUID
) -> ConnectionView:
    """Forget the credential and any pending attempt; audit the disconnection.

    Idempotent: nothing connected means nothing to record, so an operator
    draining a flow in two tabs does not write a second audit entry.
    """
    store = _store()
    connection = store.load()
    if connection.credential is None and connection.pending is None:
        return status()
    address = connection.credential.address if connection.credential is not None else None
    await audit.record_event(
        session,
        tenant_id=local_tenant_id,
        action=AuditAction.MAIL_CONNECTION_REMOVED,
        actor_user_id=local_user_id,
        entity_type="mail_connection",
        details={"address": address},
    )
    store.clear()
    return status()


async def usable_credential(*, now: datetime | None = None) -> MailboxCredential:
    """The connected mailbox's credential, with an access token that works.

    The single place a stale access token is renewed and written back, so the
    probe and the ingestion cycle cannot drift apart on when to renew. A
    refused renewal is the typed ``MailRefreshFailed`` (the grant is gone, the
    mailbox has to be connected again) and ``MailNotConnected`` means there is
    no mailbox at all. A renewal writes no audit entry: no tenant data
    changed.
    """
    at = now if now is not None else datetime.now(UTC)
    store = _store()
    connection = store.load()
    credential = connection.credential
    if credential is None:
        raise MailNotConnected
    if credential.needs_refresh(now=at, leeway_seconds=TOKEN_REFRESH_LEEWAY_SECONDS):
        # The renewed credential is written back with the rest of the state, so
        # a connection flow the administrator starts meanwhile keeps its attempt.
        credential = await _renew(credential=credential, now=at)
        store.save(replace(connection, credential=credential))
    return credential


async def probe(*, now: datetime | None = None) -> ProbeResult:
    """Read the mailbox profile and the newest messages' metadata.

    The access token is renewed first when it is stale, so a probe does not
    fail on an hour-old consent; a renewal Google refuses is a typed error
    (``MailRefreshFailed``) because it means the grant is gone, not that the
    request was wrong.
    """
    credential = await usable_credential(now=now)
    profile = await _fetch_profile(access_token=credential.access_token, refusal=MailProbeRefused)
    try:
        listing = await client().list_message_ids(
            access_token=credential.access_token, max_results=PROBE_MESSAGE_COUNT
        )
    except MailProviderRefused as exc:
        raise MailProbeRefused(exc.reason) from exc
    messages: list[MessageMetadata] = []
    for message_id in _parse(MailProbeRefused, parse_messages_list, listing):
        try:
            raw = await client().fetch_message_metadata(
                access_token=credential.access_token, message_id=message_id
            )
        except MailProviderRefused as exc:
            raise MailProbeRefused(exc.reason) from exc
        messages.append(_parse(MailProbeRefused, parse_message_metadata, raw))
    return ProbeResult(profile=profile, messages=messages)


async def _renew(*, credential: MailboxCredential, now: datetime) -> MailboxCredential:
    """Trade the refresh token for a new access token; the caller stores the result."""
    try:
        raw = await client().refresh_access_token(
            refresh_token=credential.refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
    except MailProviderRefused as exc:
        raise MailRefreshFailed(exc.reason) from exc
    try:
        tokens = parse_token_response(raw)
    except MailResponseInvalid as exc:
        raise MailRefreshFailed(f"invalid_response:{exc.reason}") from exc
    return replace(
        credential,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token or credential.refresh_token,
        scopes=tokens.scopes if tokens.scopes is not None else credential.scopes,
        token_expires_at=now + timedelta(seconds=tokens.expires_in),
    )


async def _fetch_profile(
    *, access_token: str, refusal: Callable[[str], Exception]
) -> MailboxProfile:
    """The mailbox profile, with provider refusals narrowed to the caller's error."""
    try:
        raw = await client().fetch_profile(access_token=access_token)
    except MailProviderRefused as exc:
        raise refusal(exc.reason) from exc
    return _parse(refusal, parse_profile_response, raw)


def _parse[Result](
    refusal: Callable[[str], Exception],
    parser: Callable[[Mapping[str, object]], Result],
    raw: dict[str, object],
) -> Result:
    """Run a domain parser, narrowing a malformed response to this layer's error."""
    try:
        return parser(raw)
    except MailResponseInvalid as exc:
        raise refusal(f"invalid_response:{exc.reason}") from exc


def _store() -> MailCredentialStore:
    return MailCredentialStore(settings.mail_credential_file)
