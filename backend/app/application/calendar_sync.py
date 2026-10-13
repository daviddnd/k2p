"""Calendar feed and API-push orchestration (scope §2.4; ADR-0013 item 3
and its 2026-10-03 amendment; ADR-0012 tiers; contract §5.1).

Two delivery paths, one projection, one write path.

**The pairing (the 2026-10-03 founder rulings).** Every tenant has the
``.ics` capability-URL feed; a tenant that registers its own Google or
Microsoft app additionally gets the push, the better path — events land in
the TCSP's own calendar directly, updated in place. No provider credential
→ the feed; a credential whose probe has not passed → still the feed,
because the fail-safe default is the path every tier keeps. A lapsed §5.1
gate stops a company's pushes and never deletes its provider events
(ADR-0012's "already-synced entries persist"): the company is omitted with
the gate's own reason, exactly as the feed omits it.

**The credential.** ``create_credential`` / ``list_credentials`` /
``revoke_credential`` own the per-tenant capability the TCSP subscribes
with. The token exists in plaintext exactly once — in the response to the
create call — and only its SHA-256 digest is stored
(``app.domain.calendar_sync.hash_feed_token``); at most one credential is
active at a time, so rotation is explicit: revoke, then create. Both
mutations emit their audit event on the same session (ADR-0009), and
neither ever puts the token in one.

**The provider credential (the push's own).** ``save_provider_credential``
/ ``remove_provider_credential`` own the Google or Microsoft app
registration the push authenticates with, held in the ADR-0014 file store
(``app.adapters.calendar.credential_store``) — never the database, never a
log line, never an audit entry's details. Create and revoke are custody
mutations: each emits its audit event on the request's session, naming the
credential's id and provider metadata only. ``probe_provider`` is ruling
R5's honest probe — refresh the token, list the target calendar, write a
probe event, delete it — and records the outcome on the credential's
``probe_passed`` flag either way: a credential that probed green once and
now fails goes back to the feed, which is the fail-safe default.

**The feed.** ``build_feed`` resolves the token to a tenant, gates every
company in that tenant through the §5.1 gate, and projects the V1 read
model's deadlines (``app.application.compliance``) into calendar entries.
It adds no deadline rule of its own: a company dormant on its due date
produces no entry, and a company whose type has no established rule is
carried as a refusal rather than a guessed date — exactly as the read model
reports them.

**The push.** ``sweep_pushes`` runs the same projection and the same gate
for the tenant's provider calendar, and reconciles it against the stored
push state (``app.adapters.persistence.calendar_push``) by content hash
(ruling R3): an entry is pushed only when it is new or its content changed,
a disappeared obligation's provider event is deleted once and its row
forgotten, and an unchanged projection pushes nothing at all. The provider
itself is a seam — ``CalendarProvider`` below — implemented by the Google
and Microsoft adapters (slice M8's Task 2) and by fakes in the suites; the
one place an adapter is constructed is ``build_provider`` below, so the
probe route and the push worker share a single seam the suites stub.

**Triggers (ruling R4).** Calendar-relevant writes — a change event's
``apply``, a statics correction that moves a due date — call
``request_push_sweep``, which enqueues the tenant on an in-process queue
the push worker drains; the worker's interval is the reconciliation
backstop. The queue exists only while the loop runs, so a deployment that
opts out (the default) pays nothing, and the feed is unaffected either way.

Gating is per company because coverage is per company (ADR-0013 item 4): a
company whose assignment or package does not cover ``calendar_sync`` is
simply absent from the feed, the way the dashboard flags it. The
deployment-level leg surfaces as the same gate's
``tier_capability_missing``: when *no* company in the portfolio is covered,
the feed refuses with the first company's reason instead of serving an
empty calendar that would read as "no deadlines". A tenant with no
companies has nothing to deny and gets an empty calendar.

**Degradation (ADR-0012).** Entries already synced into the TCSP's own
calendar persist: that is what a feed does once it stops serving — the
client keeps the events it last fetched, the URL starts answering 403, and
nothing in this deployment deletes anything — and it is the push's rule for
a gate-denied company, whose provider events and push rows are carried as
*retained* and left untouched. The in-app calendar read is
ungated and keeps showing every deadline, so a lapsed lease costs the
TCSP the sync, never the information.

Services flush but never commit — the API session dependency commits at the
request edge.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.calendar.credential_store import CalendarCredentialStore, StoredCredential
from app.adapters.calendar.google_client import GoogleCalendarClient
from app.adapters.calendar.microsoft_client import MicrosoftGraphClient
from app.adapters.calendar.probe import ProbeReport
from app.adapters.persistence.calendar_push import CalendarPushEntry
from app.adapters.persistence.calendar_sync import CalendarFeedCredential
from app.adapters.persistence.models import Company
from app.application import audit, compliance, service_packages
from app.core.config import APP_VERSION, settings
from app.domain.audit import AuditAction
from app.domain.calendar_push import (
    CalendarProviderName,
    ProviderCredential,
    PushEntryState,
    SyncMode,
    content_hash,
    decide_sync,
    derive_push_plan,
)
from app.domain.calendar_sync import (
    Deadline,
    generate_feed_token,
    hash_feed_token,
    project_deadline,
)
from app.domain.compliance import RiskBand
from app.domain.ics import IcsEvent, IcsProperty, render_calendar
from app.domain.rbac import ActorContext, Role
from app.domain.service_packages import (
    AutomationGateDecision,
    AutomationType,
    GatingReason,
)

# The feed's window: today through the next 365 days — a 366-day range, the
# read model's own maximum. A full annual cycle is the smallest window that
# shows every company's current NAR1 obligation exactly once; past-dated
# deadlines are deliberately not re-served, because a subscriber's client
# keeps the entries it already has and the in-app calendar is the read for
# overdue state.
FEED_HORIZON_DAYS = 365

PRODID = f"-//ai-com-sec//compliance calendar {APP_VERSION}//EN"

# The feed's own refusal property: a company with no deadline, and why. An
# `X-` property is the only place an `.ics` can carry it, and carrying it is
# the point — the feed refuses rather than guessing a date.
NOT_GENERATED_PROPERTY = "X-AICS-NOT-GENERATED"

# The feed has no staff actor, so it reads with the scope an Administrator
# has: the tenant's whole portfolio. This is the read model's single
# scoping rule applied to a capability that belongs to the tenant, not to a
# staffer — the token's creation is Administrator-only for the same reason.
_TENANT_SCOPE = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


class FeedCredentialNotFoundError(Exception):
    """No credential with this id exists in the tenant (cross-tenant → unknown)."""


class ActiveFeedCredentialExistsError(Exception):
    """The tenant already has an active credential; revoke it before creating one."""


class InvalidFeedTokenError(Exception):
    """The presented token is unknown or revoked — the feed answers 401."""


class ProviderCredentialNotConnectedError(Exception):
    """No provider credential is stored for the tenant — the probe answers 409."""


class AutomationDeniedError(Exception):
    """The §5.1 gate denied calendar sync for every company in the portfolio.

    ``reason`` is the gate's own ``GatingReason`` — the same vocabulary the
    dashboard's "service not covered" flags carry, so an unlicensed
    deployment and one whose every client package lapsed are both named
    rather than implied.
    """

    def __init__(self, reason: GatingReason) -> None:
        self.reason = reason
        super().__init__(f"automation_denied: {reason.value}")


@dataclass(frozen=True)
class FeedCredentialView:
    """One credential's metadata — never its token."""

    id: UUID
    created_by: UUID | None
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class IssuedFeedCredential:
    """A credential and the one-time plaintext the create response returns."""

    credential: FeedCredentialView
    token: str


@dataclass(frozen=True)
class ProviderCredentialView:
    """One provider credential's metadata — the secrets are never fields."""

    credential_id: UUID
    provider: CalendarProviderName
    calendar_id: str
    probe_passed: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CompanyPushState:
    """One company's place in the push: pushing, feed, or the gate's refusal."""

    company_id: UUID
    state: str  # "pushing" | "feed" | "refused" — the vocabulary the feed owns
    reason: str | None  # the §5.1 gate's own reason when state is "refused"


@dataclass(frozen=True)
class TenantPushStatus:
    """The tenant's delivery path plus one per-company state.

    ``mode`` and ``probe_passed`` travel together deliberately: an unprobed
    credential decides ``push`` mode but pushes nothing, and nothing
    user-visible may read an unprobed credential as "pushing".
    """

    credential: ProviderCredentialView | None
    mode: SyncMode
    probe_passed: bool
    companies: tuple[CompanyPushState, ...]


@dataclass(frozen=True)
class FeedRefusal:
    """A covered company the feed could not generate a deadline for, and why."""

    company_id: UUID
    company_type: str
    reason: str


@dataclass(frozen=True)
class CalendarFeed:
    """The projected feed: window, entries, refusals."""

    as_of: date
    window_from: date
    window_to: date
    events: tuple[IcsEvent, ...]
    refusals: tuple[FeedRefusal, ...]


def render_feed(feed: CalendarFeed, *, dtstamp: datetime) -> str:
    """The feed as an iCalendar body (the writer is ``app.domain.ics``)."""
    return render_calendar(
        prodid=PRODID,
        dtstamp=dtstamp,
        events=feed.events,
        properties=[
            IcsProperty(
                name=NOT_GENERATED_PROPERTY,
                value=f"{refusal.company_id}:{refusal.reason}",
            )
            for refusal in feed.refusals
        ],
    )


def _view(credential: CalendarFeedCredential) -> FeedCredentialView:
    return FeedCredentialView(
        id=credential.id,
        created_by=credential.created_by,
        created_at=credential.created_at,
        revoked_at=credential.revoked_at,
    )


async def _credential(
    session: AsyncSession, *, tenant_id: UUID, credential_id: UUID
) -> CalendarFeedCredential:
    row = await session.scalar(
        select(CalendarFeedCredential).where(
            CalendarFeedCredential.id == credential_id,
            CalendarFeedCredential.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise FeedCredentialNotFoundError
    return row


async def create_credential(
    session: AsyncSession, *, tenant_id: UUID, actor_user_id: UUID | None
) -> IssuedFeedCredential:
    """Issue the tenant's feed credential and return the token once.

    The plaintext token is returned here and nowhere else: only its digest
    is stored, and no read path can reconstruct it. A tenant that already
    has an active credential gets ``ActiveFeedCredentialExistsError`` (the
    partial unique index is the backstop) — rotation is revoke-then-create.
    """
    existing = await session.scalar(
        select(CalendarFeedCredential.id).where(
            CalendarFeedCredential.tenant_id == tenant_id,
            CalendarFeedCredential.revoked_at.is_(None),
        )
    )
    if existing is not None:
        raise ActiveFeedCredentialExistsError
    token = generate_feed_token()
    credential = CalendarFeedCredential(
        tenant_id=tenant_id, token_hash=hash_feed_token(token), created_by=actor_user_id
    )
    session.add(credential)
    await session.flush()
    # created_at is a server default; read it back so the response carries
    # the stored instant rather than triggering a lazy load at serialization.
    await session.refresh(credential)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CALENDAR_FEED_CREATED,
        actor_user_id=actor_user_id,
        entity_type="calendar_feed_credential",
        entity_id=credential.id,
        # The digest is deliberately absent: an audit chain is a read surface.
        details={},
    )
    return IssuedFeedCredential(credential=_view(credential), token=token)


async def list_credentials(
    session: AsyncSession, *, tenant_id: UUID
) -> list[FeedCredentialView]:
    """Every credential the tenant has created, oldest first — metadata only."""
    rows = await session.scalars(
        select(CalendarFeedCredential)
        .where(CalendarFeedCredential.tenant_id == tenant_id)
        .order_by(CalendarFeedCredential.created_at)
    )
    return [_view(row) for row in rows.all()]


async def revoke_credential(
    session: AsyncSession, *, tenant_id: UUID, credential_id: UUID, actor_user_id: UUID | None
) -> FeedCredentialView:
    """Revoke one credential; the feed stops answering for it immediately.

    Idempotent: revoking an already-revoked credential changes nothing and
    writes no second audit entry (the first revocation is the event).
    """
    credential = await _credential(session, tenant_id=tenant_id, credential_id=credential_id)
    if credential.revoked_at is not None:
        return _view(credential)
    credential.revoked_at = datetime.now(UTC)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CALENDAR_FEED_REVOKED,
        actor_user_id=actor_user_id,
        entity_type="calendar_feed_credential",
        entity_id=credential.id,
        details={"revoked_at": credential.revoked_at},
    )
    return _view(credential)


async def build_feed(session: AsyncSession, *, token: str, as_of: date) -> CalendarFeed:
    """The feed for the tenant the token belongs to, or a refusal.

    Raises ``InvalidFeedTokenError`` for an unknown or revoked token (the
    credential is the only authorization this path has) and
    ``AutomationDeniedError`` when the §5.1 gate denies calendar sync for
    every company in the portfolio.
    """
    tenant_id = await _tenant_for_token(session, token=token)
    companies, decisions = await _portfolio_decisions(
        session, tenant_id=tenant_id, as_of=as_of
    )
    covered = {
        company.id
        for company in companies
        if decisions[company.id][AutomationType.CALENDAR_SYNC].allowed
    }
    if companies and not covered:
        reason = decisions[companies[0].id][AutomationType.CALENDAR_SYNC].reason
        assert reason is not None  # a denied decision always carries one
        raise AutomationDeniedError(reason)
    window_to, calendar = await _tenant_calendar(session, tenant_id=tenant_id, as_of=as_of)
    return CalendarFeed(
        as_of=as_of,
        window_from=as_of,
        window_to=window_to,
        events=tuple(
            project_deadline(_deadline(entry))
            for entry in calendar.entries
            if entry.company_id in covered
        ),
        refusals=tuple(
            FeedRefusal(
                company_id=item.company_id,
                company_type=item.company_type,
                reason=item.reason,
            )
            for item in calendar.not_generated
            if item.company_id in covered
        ),
    )


async def _portfolio_decisions(
    session: AsyncSession, *, tenant_id: UUID, as_of: date
) -> tuple[list[Company], dict[UUID, dict[AutomationType, AutomationGateDecision]]]:
    """The tenant's companies and one §5.1 calendar-sync decision per company.

    The decisions come from the same bulk read the dashboard serves — one
    gate answer per company in a few queries, not two per company — so the
    feed and the push cannot disagree about what the gate would say.
    """
    companies = list(
        (
            await session.scalars(
                select(Company)
                .where(Company.tenant_id == tenant_id)
                .order_by(Company.br_number)
            )
        ).all()
    )
    decisions = await service_packages.gating_decisions_for_companies(
        session, company_ids=[company.id for company in companies], as_of=as_of
    )
    return companies, decisions


async def _tenant_calendar(
    session: AsyncSession, *, tenant_id: UUID, as_of: date
) -> tuple[date, compliance.Calendar]:
    """The tenant's projected deadlines over the feed window (ungated).

    Gating filters *who is served or pushed*, never what the read model
    computes: both paths project from this one read, over the same window,
    so feed and push assert the same obligations.
    """
    window_to = as_of + timedelta(days=FEED_HORIZON_DAYS)
    calendar = await compliance.calendar(
        session,
        tenant_id=tenant_id,
        context=_TENANT_SCOPE,
        date_from=as_of,
        date_to=window_to,
        as_of=as_of,
    )
    return window_to, calendar


class CalendarProvider(Protocol):
    """The provider seam the push leg calls (slice M8's Task 2 implements it).

    Implemented structurally — the real Google and Microsoft adapters supply
    both methods, the suites a fake — so ``sweep_pushes`` never sees a
    concrete adapter. ``upsert_event`` creates or, when the provider already
    holds the UID's event, updates it in place, and returns the provider's
    event id; ``delete_event`` removes the event that id names.
    """

    async def upsert_event(self, event: IcsEvent) -> str: ...

    async def delete_event(self, provider_event_id: str) -> None: ...


class ProbingProvider(CalendarProvider, Protocol):
    """The seam the probe route calls: a provider plus ruling R5's probe."""

    async def probe(self) -> ProbeReport: ...


@dataclass(frozen=True)
class PushOmission:
    """A company the §5.1 gate denied, and the gate's own reason for it."""

    company_id: UUID
    reason: GatingReason


@dataclass(frozen=True)
class PushSweepReport:
    """What one sweep did and omitted, by obligation UID.

    ``mode`` records the tenant's delivery path as decided: the feed (no
    credential, or one whose probe never passed) means the sweep did nothing
    and the remaining fields are empty.
    """

    mode: SyncMode
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[str, ...]
    omissions: tuple[PushOmission, ...]


def _credential_store() -> CalendarCredentialStore:
    """The provider-credential store at the configured custody directory."""
    return CalendarCredentialStore(settings.calendar_credential_dir)


async def sweep_pushes(
    session: AsyncSession, *, tenant_id: UUID, as_of: date, provider: CalendarProvider
) -> PushSweepReport:
    """Reconcile the tenant's provider calendar with the current projection.

    The push/fallback decision comes first: with no credential — or one
    whose probe never passed — the tenant stays on the feed and the sweep is
    a no-op that touches neither the provider nor the push-state table.
    Otherwise the sweep gates every company exactly as the feed does, pushes
    the covered companies' entries through ``provider`` only where the
    content hash changed or the obligation is new, deletes a disappeared
    obligation's provider event once and forgets its row, and reports every
    gate-denied company with the gate's own reason.

    A denied company's existing events are *retained* (ADR-0012): its
    entries still project, so they are excluded from the delete set without
    being upserted. A delete whose provider call fails propagates before the
    row is removed, so the next sweep retries it — the row is forgotten only
    once the provider event is gone.

    Flushes but never commits — the caller's session dependency commits at
    the request edge.
    """
    decision = decide_sync(_credential_store().load(tenant_id))
    if not decision.pushes:
        return PushSweepReport(
            mode=decision.mode,
            created=(),
            updated=(),
            unchanged=(),
            deleted=(),
            omissions=(),
        )
    companies, decisions = await _portfolio_decisions(
        session, tenant_id=tenant_id, as_of=as_of
    )
    covered = {
        company.id
        for company in companies
        if decisions[company.id][AutomationType.CALENDAR_SYNC].allowed
    }
    omitted: list[PushOmission] = []
    for company in companies:
        if company.id in covered:
            continue
        reason = decisions[company.id][AutomationType.CALENDAR_SYNC].reason
        assert reason is not None  # a denied decision always carries one
        omitted.append(PushOmission(company_id=company.id, reason=reason))
    _, calendar = await _tenant_calendar(session, tenant_id=tenant_id, as_of=as_of)
    pushed = [
        project_deadline(_deadline(entry))
        for entry in calendar.entries
        if entry.company_id in covered
    ]
    retained = [
        project_deadline(_deadline(entry))
        for entry in calendar.entries
        if entry.company_id not in covered
    ]
    rows = list(
        (
            await session.scalars(
                select(CalendarPushEntry).where(CalendarPushEntry.tenant_id == tenant_id)
            )
        ).all()
    )
    row_by_uid = {row.uid: row for row in rows}
    plan = derive_push_plan(
        pushed=pushed,
        retained=retained,
        existing={
            row.uid: PushEntryState(
                uid=row.uid,
                provider_event_id=row.provider_event_id,
                content_hash=row.content_hash,
            )
            for row in rows
        },
    )
    # Deterministic order (the plan sorts by UID): a sweep replays the same
    # provider calls in the same sequence run over run.
    for event in (*plan.created, *plan.updated):
        event_id = await provider.upsert_event(event)
        hash_value = content_hash(event)
        row = row_by_uid.get(event.uid)
        if row is None:
            session.add(
                CalendarPushEntry(
                    tenant_id=tenant_id,
                    uid=event.uid,
                    provider_event_id=event_id,
                    content_hash=hash_value,
                )
            )
        else:
            row.provider_event_id = event_id
            row.content_hash = hash_value
    for state in plan.deleted:
        await provider.delete_event(state.provider_event_id)
        await session.delete(row_by_uid[state.uid])
    await session.flush()
    return PushSweepReport(
        mode=decision.mode,
        created=tuple(event.uid for event in plan.created),
        updated=tuple(event.uid for event in plan.updated),
        unchanged=plan.unchanged,
        deleted=tuple(state.uid for state in plan.deleted),
        omissions=tuple(omitted),
    )


def _provider_view(stored: StoredCredential) -> ProviderCredentialView:
    return ProviderCredentialView(
        credential_id=stored.credential_id,
        provider=stored.credential.provider,
        calendar_id=stored.credential.calendar_id,
        probe_passed=stored.credential.probe_passed,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


def provider_credential(tenant_id: UUID) -> ProviderCredentialView | None:
    """The tenant's provider-credential metadata, or ``None`` when none is stored."""
    stored = _credential_store().load_stored(tenant_id)
    return None if stored is None else _provider_view(stored)


async def save_provider_credential(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    credential: ProviderCredential,
) -> ProviderCredentialView:
    """Store (or replace) the tenant's provider credential and audit the custody.

    The file write is the custody mutation; the audit entry on the request's
    session names the credential's id and provider metadata — never the
    client secret, never the refresh token (ADR-0014). Replacing keeps the
    document's id and ``created_at`` (see the store), so the audit chain's
    identity is stable across a rotation.
    """
    stored = _credential_store().save(tenant_id, credential)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CALENDAR_PROVIDER_CONNECTED,
        actor_user_id=actor_user_id,
        entity_type="calendar_provider_credential",
        entity_id=stored.credential_id,
        details={"provider": credential.provider.value, "calendar_id": credential.calendar_id},
    )
    return _provider_view(stored)


async def remove_provider_credential(
    session: AsyncSession, *, tenant_id: UUID, actor_user_id: UUID | None
) -> ProviderCredentialView | None:
    """Forget the tenant's provider credential; the audit chain records it.

    Idempotent, the mailbox disconnect's rule: no credential means nothing
    to record and no audit entry is written — the first disconnect is the
    event. The tenant falls back to the feed; nothing already pushed is
    deleted (ADR-0012).
    """
    store = _credential_store()
    stored = store.load_stored(tenant_id)
    if stored is None:
        return None
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CALENDAR_PROVIDER_DISCONNECTED,
        actor_user_id=actor_user_id,
        entity_type="calendar_provider_credential",
        entity_id=stored.credential_id,
        details={
            "provider": stored.credential.provider.value,
            "calendar_id": stored.credential.calendar_id,
        },
    )
    store.clear(tenant_id)
    return _provider_view(stored)


def build_provider(credential: ProviderCredential) -> ProbingProvider:
    """The one place a provider adapter is constructed.

    The probe route and the push worker both call here, so the suites stub
    a single seam; the sweep itself receives the result through the
    ``CalendarProvider`` protocol and never sees a concrete adapter.
    """
    timeout = settings.calendar_request_timeout_seconds
    if credential.provider is CalendarProviderName.MICROSOFT:
        return MicrosoftGraphClient(credential, timeout=timeout)
    return GoogleCalendarClient(credential, timeout=timeout)


async def probe_provider(tenant_id: UUID) -> tuple[ProviderCredentialView, ProbeReport]:
    """Ruling R5's honest probe: run every leg, record the flag either way.

    Refreshes the token, lists the target calendar, writes a probe event
    and deletes it, reporting each leg's own outcome. On success the
    credential's ``probe_passed`` flag is set; a failed leg clears it, so a
    credential whose grant lapsed goes back to the feed — the fail-safe
    default. Raises ``ProviderCredentialNotConnectedError`` when no
    credential is stored. The token never appears in the report, a log line
    or an audit entry.
    """
    store = _credential_store()
    stored = store.load_stored(tenant_id)
    if stored is None:
        raise ProviderCredentialNotConnectedError
    report = await build_provider(stored.credential).probe()
    updated = store.save(tenant_id, replace(stored.credential, probe_passed=report.passed))
    return _provider_view(updated), report


async def tenant_push_status(session: AsyncSession, *, tenant_id: UUID) -> TenantPushStatus:
    """The push/fallback pairing per company: mode, probe flag, gate reasons.

    The tenant-level facts come from ``decide_sync`` over the stored
    credential; each company is then classified exactly as the sweep would
    classify it — covered companies are ``pushing`` when the tenant pushes
    and ``feed`` otherwise, gate-denied companies are ``refused`` with the
    gate's own reason. ``probe_passed`` travels beside ``mode`` so an
    unprobed credential is never presented as pushing.
    """
    decision = decide_sync(_credential_store().load(tenant_id))
    companies, decisions = await _portfolio_decisions(
        session, tenant_id=tenant_id, as_of=date.today()
    )
    states = []
    for company in companies:
        gate = decisions[company.id][AutomationType.CALENDAR_SYNC]
        if not gate.allowed:
            reason = gate.reason
            assert reason is not None  # a denied decision always carries one
            states.append(
                CompanyPushState(company_id=company.id, state="refused", reason=reason.value)
            )
        else:
            states.append(
                CompanyPushState(
                    company_id=company.id,
                    state="pushing" if decision.pushes else "feed",
                    reason=None,
                )
            )
    return TenantPushStatus(
        credential=provider_credential(tenant_id),
        mode=decision.mode,
        probe_passed=decision.probe_passed,
        companies=tuple(states),
    )


class _FeedModeProvider:
    """The never-called provider a feed-mode sweep receives.

    ``sweep_pushes`` decides push-vs-feed from the credential before it
    touches the provider, so a tenant the decision keeps on the feed needs
    no adapter at all — but the sweep's signature wants one. If this is ever
    called, the decision and the sweep disagreed: that is a bug, and it
    must not masquerade as a provider failure.
    """

    async def upsert_event(self, event: IcsEvent) -> str:
        raise RuntimeError("a feed-mode sweep never calls the provider")

    async def delete_event(self, provider_event_id: str) -> None:
        raise RuntimeError("a feed-mode sweep never calls the provider")


async def sweep_tenant_pushes(
    session: AsyncSession, *, tenant_id: UUID, as_of: date
) -> PushSweepReport:
    """The push worker's cycle: decide from the credential, build the adapter
    only for a tenant the push actually fires for, and sweep.

    Provider failures (``CalendarAuthFailed`` / ``CalendarProviderUnavailable``)
    propagate to the worker, which books them per cycle and backs off — the
    push state is left untouched for the next sweep, exactly as the sweep's
    own rule says.
    """
    credential = _credential_store().load(tenant_id)
    if credential is not None and decide_sync(credential).pushes:
        provider: CalendarProvider = build_provider(credential)
    else:
        provider = _FeedModeProvider()
    return await sweep_pushes(session, tenant_id=tenant_id, as_of=as_of, provider=provider)


# Ruling R4's write-time triggers: an in-process queue of tenant ids, created
# by the push worker's start and drained by its cycles. A deployment that
# runs no loop has no queue, and ``request_push_sweep`` is then a no-op —
# the feed never needed a loop and neither does a deployment that opts out.
_push_triggers: asyncio.Queue[UUID] | None = None


def begin_push_triggers() -> asyncio.Queue[UUID]:
    """Open the trigger queue; the push worker's start is the only caller."""
    global _push_triggers
    if _push_triggers is None:
        _push_triggers = asyncio.Queue()
    return _push_triggers


def end_push_triggers() -> None:
    """Close the trigger queue; the push worker's stop is the only caller."""
    global _push_triggers
    _push_triggers = None


def request_push_sweep(tenant_id: UUID) -> None:
    """Ask the push worker to sweep this tenant at its next cycle.

    Called once from each calendar-relevant write seam (a change event's
    ``apply``, a statics correction that moves a due date). With no loop
    running there is no queue and nothing to do — the interval backstop is
    a deployment choice, off by default.
    """
    queue = _push_triggers
    if queue is not None:
        queue.put_nowait(tenant_id)


async def _tenant_for_token(session: AsyncSession, *, token: str) -> UUID:
    """The token's tenant, or ``InvalidFeedTokenError``.

    Lookup is by digest; a revoked credential is indistinguishable from an
    unknown one, so revocation (and a token that never existed) answers the
    same 401.
    """
    tenant_id = await session.scalar(
        select(CalendarFeedCredential.tenant_id).where(
            CalendarFeedCredential.token_hash == hash_feed_token(token),
            CalendarFeedCredential.revoked_at.is_(None),
        )
    )
    if tenant_id is None:
        raise InvalidFeedTokenError
    return tenant_id


def _deadline(entry: compliance.CalendarEntry) -> Deadline:
    """The read model's entry as the projection's input.

    The API shape flattens the risk band into four fields; the projection
    takes the band back as the domain value ``risk_band`` produced.
    """
    return Deadline(
        company_id=entry.company_id,
        br_number=entry.br_number,
        company_name=entry.company_name,
        kind=entry.kind,
        due_date=entry.due_date,
        period=entry.anniversary_year,
        band=RiskBand(
            days_remaining=entry.days_remaining,
            alert=entry.alert,
            level=entry.risk,
            overdue=entry.overdue,
        ),
    )
