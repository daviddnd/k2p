"""Calendar subscription and API-push endpoints (scope §2.4; ADR-0013 item 3
and its 2026-10-03 amendment; contract §5.1).

``GET /calendar/feed.ics`` is the subscription itself: a **capability URL**
the TCSP's calendar provider polls with no session of its own — which is
exactly why the token is a credential, why it is stored only as a digest,
and why revocation answers 401 immediately. The body names the portfolio's
companies and due dates; that is the point of the feed, and the TCSP chose
the provider it subscribes with. The response is never cached
(``Cache-Control: no-store``): a revoked feed must stop being served, not be
served from a cache until it expires.

``POST/GET /calendar/feed-credentials`` and
``POST /calendar/feed-credentials/{id}/revoke`` are the Administrator-only
management routes (the calendar-sync configuration is a tenant-level
setting with no per-company grant semantics, the same rule tenant config
follows). The token appears in the create response and nowhere else: the
list returns metadata only, and every mutation emits its audit event in the
same transaction (ADR-0009).

**The push half (slice M8).** ``POST/GET/DELETE /calendar/provider-credentials``
own the tenant's own Google or Microsoft app registration — the credential
the push authenticates with, stored in the ADR-0014 file store, never the
database. Create validates the shape (422 on a malformed body), revoke
deletes the file, and both emit their audit event on the request's session
naming the credential's id — never its secrets. The read is metadata only.
``POST /calendar/probe`` is ruling R5's honest probe: it refreshes the
token, lists the target calendar, writes a probe event and deletes it,
reporting each leg's outcome, and moves the credential's ``probe_passed``
flag to the answer — a failed leg takes a once-green credential back to the
feed, the fail-safe default. ``GET /calendar/push-status`` is the pairing
made visible per company: the tenant's ``mode`` from ``decide_sync`` with
``probe_passed`` beside it (an unprobed credential never reads as
"pushing"), and each company ``pushing``, on the ``feed``, or ``refused``
with the §5.1 gate's own reason. ``GET /calendar/onboarding-guide`` serves
the per-provider registration walk from one static source (ruling R6; the
Phase-6 e-Registry-helper pattern — static text, no LLM).

The feed carries whatever the read model computes — dormant companies
excluded, companies whose due-date rule is not established carried as an
``X-AICS-NOT-GENERATED`` refusal — and serves only companies whose package
covers ``calendar_sync`` (ADR-0013 item 4). See
``app.application.calendar_sync``.
"""

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import calendar_sync
from app.domain.calendar_onboarding import guide_for
from app.domain.calendar_push import CalendarProviderName, ProviderCredential, SyncMode
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1", tags=["calendar"])

NOT_CONNECTED = "not_connected"


class FeedCredentialResponse(BaseModel):
    """A credential's metadata. The token is deliberately not a field."""

    id: UUID
    created_by: UUID | None
    created_at: datetime
    revoked_at: datetime | None


class IssuedFeedCredentialResponse(FeedCredentialResponse):
    """The create response: the credential and its subscription URL, once."""

    subscription_url: str


def _credential_response(
    credential: calendar_sync.FeedCredentialView,
) -> FeedCredentialResponse:
    return FeedCredentialResponse(
        id=credential.id,
        created_by=credential.created_by,
        created_at=credential.created_at,
        revoked_at=credential.revoked_at,
    )


def _subscription_url(request: Request, token: str) -> str:
    """The capability URL, on the host this route was called on.

    That host is the deployment the TCSP reaches; the calendar provider has
    to reach the same one. Built through the route's own name so a mounted
    prefix cannot drift from the URL handed out.
    """
    return str(request.url_for("get_calendar_feed").include_query_params(token=token))


@router.post("/calendar/feed-credentials", status_code=201)
async def create_feed_credential(
    request: Request,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IssuedFeedCredentialResponse:
    """Issue the tenant's feed credential; the URL is returned here only.

    409 when an active credential already exists — rotation is revoke, then
    create, so "which URL is live?" always has one answer.
    """
    try:
        issued = await calendar_sync.create_credential(
            session, tenant_id=actor.tenant_id, actor_user_id=actor.user_id
        )
    except calendar_sync.ActiveFeedCredentialExistsError:
        raise HTTPException(
            status_code=409,
            detail="an active feed credential already exists; revoke it first",
        ) from None
    return IssuedFeedCredentialResponse(
        id=issued.credential.id,
        created_by=issued.credential.created_by,
        created_at=issued.credential.created_at,
        revoked_at=issued.credential.revoked_at,
        subscription_url=_subscription_url(request, issued.token),
    )


@router.get("/calendar/feed-credentials")
async def list_feed_credentials(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[FeedCredentialResponse]:
    """The tenant's credentials, current and revoked — metadata only."""
    credentials = await calendar_sync.list_credentials(session, tenant_id=actor.tenant_id)
    return [_credential_response(credential) for credential in credentials]


@router.post("/calendar/feed-credentials/{credential_id}/revoke")
async def revoke_feed_credential(
    credential_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FeedCredentialResponse:
    """Stop the feed answering for this credential (404 for unknown ids)."""
    try:
        credential = await calendar_sync.revoke_credential(
            session,
            tenant_id=actor.tenant_id,
            credential_id=credential_id,
            actor_user_id=actor.user_id,
        )
    except calendar_sync.FeedCredentialNotFoundError:
        raise HTTPException(status_code=404, detail="credential not found") from None
    return _credential_response(credential)


@router.get("/calendar/feed.ics")
async def get_calendar_feed(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str | None, Query()] = None,
) -> Response:
    """The `.ics` subscription for the tenant the token belongs to.

    No session of its own — the token is the whole authorization. 401 for a
    missing, unknown, or revoked token; 403 with the §5.1 gate's own reason
    when calendar sync is not licensed for any company in the portfolio.
    Never cached: revocation must bite on the next fetch.
    """
    if not token:
        raise HTTPException(status_code=401, detail="missing feed token")
    try:
        feed = await calendar_sync.build_feed(session, token=token, as_of=date.today())
    except calendar_sync.InvalidFeedTokenError:
        raise HTTPException(status_code=401, detail="unknown or revoked feed token") from None
    except calendar_sync.AutomationDeniedError as exc:
        raise HTTPException(
            status_code=403, detail=f"automation_denied: {exc.reason.value}"
        ) from None
    return Response(
        content=calendar_sync.render_feed(feed, dtstamp=datetime.now(UTC)),
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# --- The push half (slice M8) ------------------------------------------------


class CreateProviderCredentialRequest(BaseModel):
    """The provider credential as the Administrator pastes it.

    The shape the credential store expects, validated before anything is
    written: the provider is the supported-vocabulary enum, every value the
    store requires is a non-empty string, and ``directory_tenant_id`` is
    Microsoft's own (required there, refused for Google — a document that
    carries it under ``google`` would be sloppiness the sweep would carry).
    """

    provider: CalendarProviderName
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    directory_tenant_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _provider_shape(self) -> "CreateProviderCredentialRequest":
        if self.provider is CalendarProviderName.MICROSOFT and self.directory_tenant_id is None:
            raise ValueError("directory_tenant_id is required for a microsoft credential")
        if self.provider is CalendarProviderName.GOOGLE and self.directory_tenant_id is not None:
            raise ValueError("directory_tenant_id is microsoft-only")
        return self

    def to_credential(self) -> ProviderCredential:
        """The request as the domain credential (``probe_passed`` starts false)."""
        return ProviderCredential(
            provider=self.provider,
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=self.refresh_token,
            calendar_id=self.calendar_id,
            directory_tenant_id=self.directory_tenant_id,
        )


class ProviderCredentialResponse(BaseModel):
    """The provider credential's metadata; the secrets are deliberately not fields."""

    connected: bool
    credential_id: UUID | None
    provider: CalendarProviderName | None
    calendar_id: str | None
    probe_passed: bool
    created_at: datetime | None
    updated_at: datetime | None


def _provider_response(
    view: calendar_sync.ProviderCredentialView | None,
) -> ProviderCredentialResponse:
    return ProviderCredentialResponse(
        connected=view is not None,
        credential_id=None if view is None else view.credential_id,
        provider=None if view is None else view.provider,
        calendar_id=None if view is None else view.calendar_id,
        probe_passed=False if view is None else view.probe_passed,
        created_at=None if view is None else view.created_at,
        updated_at=None if view is None else view.updated_at,
    )


@router.post("/calendar/provider-credentials", status_code=201)
async def create_provider_credential(
    payload: CreateProviderCredentialRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderCredentialResponse:
    """Store the tenant's provider credential (create or replace); 422 on a
    malformed body, metadata only in the response.

    The client secret and refresh token are written to the mode-0600 file
    and appear nowhere else — not this response, not a log line, not the
    audit entry the write emits (ADR-0014). Replacing a stored credential
    keeps its identity and ``created_at``; the probe flag resets with the
    new secret, because an unprobed credential never pushes.
    """
    view = await calendar_sync.save_provider_credential(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        credential=payload.to_credential(),
    )
    return _provider_response(view)


@router.get("/calendar/provider-credentials")
async def get_provider_credential(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
) -> ProviderCredentialResponse:
    """The stored credential's metadata, or ``connected: false``.

    Never calls the provider, so it answers during an outage; the probe is
    what asks the provider. Never carries the secrets — the response's
    fields have no place for them.
    """
    return _provider_response(calendar_sync.provider_credential(actor.tenant_id))


@router.delete("/calendar/provider-credentials")
async def delete_provider_credential(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderCredentialResponse:
    """Forget the provider credential; the tenant falls back to the feed.

    Idempotent, the mailbox disconnect's rule: with no credential there is
    nothing to record and no audit entry. Nothing already pushed is deleted
    (ADR-0012); the reconciliation sweep simply stops seeing the credential.
    """
    await calendar_sync.remove_provider_credential(
        session, tenant_id=actor.tenant_id, actor_user_id=actor.user_id
    )
    return _provider_response(None)


class ProbeLegResponse(BaseModel):
    """One probe leg's outcome: its name, whether it passed, a bounded detail."""

    name: str
    ok: bool
    detail: str | None = None


class CalendarProbeResponse(BaseModel):
    """The probe's answer: every leg, and the flag the run left behind."""

    credential_id: UUID
    passed: bool
    legs: list[ProbeLegResponse]


@router.post("/calendar/probe")
async def probe_calendar_provider(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
) -> CalendarProbeResponse:
    """Ruling R5's honest probe against the stored credential.

    Refreshes the token, lists the target calendar, writes a probe event
    and deletes it — proving *write* permission, which a read-only probe
    cannot. Each leg reports its own outcome; a leg after a failed one is
    reported as ``skipped``, naming what the probe did not do. The run
    moves the credential's ``probe_passed`` flag to the answer. 409 while
    no credential is connected. The token never appears in the response.
    """
    try:
        view, report = await calendar_sync.probe_provider(actor.tenant_id)
    except calendar_sync.ProviderCredentialNotConnectedError:
        raise HTTPException(status_code=409, detail=NOT_CONNECTED) from None
    return CalendarProbeResponse(
        credential_id=view.credential_id,
        passed=report.passed,
        legs=[ProbeLegResponse(name=leg.name, ok=leg.ok, detail=leg.detail) for leg in report.legs],
    )


class CompanyPushStatusResponse(BaseModel):
    """One company's place in the pairing: ``pushing``, ``feed``, or ``refused``."""

    company_id: UUID
    state: str
    reason: str | None = None


class PushStatusResponse(BaseModel):
    """The tenant's delivery path plus one state per company.

    ``mode`` and ``probe_passed`` travel together so an unprobed credential
    is never presented as pushing — the feed stays the honest default.
    """

    credential: ProviderCredentialResponse
    mode: SyncMode
    probe_passed: bool
    companies: list[CompanyPushStatusResponse]


@router.get("/calendar/push-status")
async def get_push_status(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PushStatusResponse:
    """How each company's deadlines reach the calendar: pushed, fed, or refused.

    The classification is the sweep's own: the §5.1 gate decides per company
    with the vocabulary the feed serves, and the tenant's push/fallback
    decision comes from the stored credential. This route reads; it never
    writes push state and never calls the provider.
    """
    status = await calendar_sync.tenant_push_status(session, tenant_id=actor.tenant_id)
    return PushStatusResponse(
        credential=_provider_response(status.credential),
        mode=status.mode,
        probe_passed=status.probe_passed,
        companies=[
            CompanyPushStatusResponse(
                company_id=item.company_id, state=item.state, reason=item.reason
            )
            for item in status.companies
        ],
    )


class OnboardingGuideResponse(BaseModel):
    """One provider's static registration walk (ruling R6), Markdown text."""

    provider: CalendarProviderName
    guide: str


@router.get("/calendar/onboarding-guide")
async def get_onboarding_guide(
    provider: Annotated[CalendarProviderName, Query()],
    _: Annotated[ActorContext, Depends(require_admin())],
) -> OnboardingGuideResponse:
    """The step-by-step registration guide for one provider, from the static
    source (ruling R6) — no LLM, operator-facing English. 422 on a provider
    outside the supported vocabulary.
    """
    return OnboardingGuideResponse(provider=provider, guide=guide_for(provider))
