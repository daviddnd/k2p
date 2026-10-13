"""Microsoft Graph's calendar surface, as the push leg calls it (slice M8).

Three kinds of call, one per step of the provider credential's life: the
refresh-token exchange that turns the stored grant into an access token, the
event writes the sweep reconciles (create, update in place by the provider
event id the push state holds, delete), and the **probe** (ruling R5) — the
honest proof of write permission that gates the push on: refresh the token,
read the calendar list, write a probe event, delete it, and report each leg's
outcome (``app.adapters.calendar.probe``).

**The surface.** Delegated, against the target calendar the credential names:
``/me/calendars/{calendar_id}/events`` for the writes (the target-calendar
form of the delegated ``/me/events`` API), ``/me/calendars`` for the probe's
list leg, and the tenant's v2.0 token endpoint for the exchange. The calendar
id travels in the path, URL-quoted; the obligation's UID travels as the event
``iCalUID``, which is how ``upsert_event`` finds an existing event to update
instead of duplicating it — the sweep's idempotency-by-content (ruling R3) is
reinforced on the wire.

**Failures** are the shared family in ``app.adapters.calendar.errors`` — the
worker catches the base class and never imports httpx: a refused token
exchange or a 401/403 is ``CalendarAuthFailed`` (durable: reconnect the app
registration), a transport error, rate limit, 5xx, or an unreadable body is
``CalendarProviderUnavailable`` (the next sweep may succeed). Refusals never
echo the provider's body — a body can carry the request's own credential, so
only a bounded code travels, the ``gmail_client`` rule.

Never retried here: a failed refresh is the probe's or the sweep's caller
business (each sweep constructs a client and refreshes at most once per
lifetime, caching the access token), and a failed write reports what happened
while the push state stays put for the next sweep. A delete of an event the
provider no longer holds answers success: the row is forgotten either way.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.adapters.calendar.errors import CalendarAuthFailed, CalendarProviderUnavailable
from app.adapters.calendar.probe import (
    CALENDAR_LIST,
    PROBE_DELETE,
    PROBE_WRITE,
    TOKEN_REFRESH,
    ProbeLeg,
    ProbeReport,
    skipped_legs,
)
from app.domain.calendar_push import CalendarProviderName, ProviderCredential
from app.domain.ics import IcsEvent

# Microsoft's endpoints — fixed, not configuration: there is exactly one Graph
# and one identity platform. The token endpoint is directory-scoped: the
# tenant id in the path is what the credential's ``directory_tenant_id`` is.
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
TOKEN_SCOPE = "offline_access https://graph.microsoft.com/Calendars.ReadWrite"
GRAPH_API_BASE_URL = "https://graph.microsoft.com/v1.0"
CALENDARS_PATH = "/me/calendars"
# What the probe's event says on the calendar. The parenthetical is the point:
# a probe event that lingers after a crash is self-describing, never mistaken
# for a client deadline.
PROBE_EVENT_SUMMARY = "ai-com-sec calendar probe (safe to delete)"

# Refusals are recorded and rendered; keep them short and free of anything the
# provider echoed.
_MAX_REASON_LENGTH = 120


class MicrosoftGraphClient:
    """Graph over HTTP, holding one provider credential and one access token.

    ``transport`` exists for tests (``httpx.ASGITransport`` against a fake);
    production constructs the client per sweep with the deployment's timeout
    and no transport. The access token is refreshed lazily on the first call
    and cached for the client's lifetime — one token exchange per sweep, and
    the probe forces a fresh exchange so it proves the grant still lives.
    """

    def __init__(
        self,
        credential: ProviderCredential,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if credential.provider is not CalendarProviderName.MICROSOFT:
            raise ValueError("credential.provider must be microsoft")
        if credential.directory_tenant_id is None:
            raise ValueError("a microsoft credential carries no directory_tenant_id")
        self._credential = credential
        self._timeout = timeout
        self._transport = transport
        self._access_token: str | None = None

    async def upsert_event(self, event: IcsEvent) -> str:
        """Create the obligation's event, or update it in place when it exists.

        The lookup is by ``iCalUID`` (the obligation-keyed UID the projection
        assigns): an existing event is patched with the new content and keeps
        its provider event id — the push state stores that id, and an id that
        moved on every sweep would defeat the reconciliation.
        """
        token = await self._token()
        existing_id = await self._find_event_id(token, event.uid)
        body = _event_body(event)
        if existing_id is None:
            created = await self._request(
                "POST",
                self._events_url(),
                access_token=token,
                json_body=body,
            )
            return _event_id(created)
        await self._request(
            "PATCH",
            f"{self._events_url()}/{quote(existing_id, safe='')}",
            access_token=token,
            json_body=body,
        )
        return existing_id

    async def delete_event(self, provider_event_id: str) -> None:
        """Delete the event that provider event id names.

        Idempotent: an event the provider no longer holds (deleted by hand, or
        by a racing sweep on another node) is the outcome this call exists to
        produce, so a 404 answers success rather than raising.
        """
        token = await self._token()
        try:
            await self._request(
                "DELETE",
                f"{self._events_url()}/{quote(provider_event_id, safe='')}",
                access_token=token,
            )
        except CalendarProviderUnavailable as failure:
            if failure.status == 404:
                return
            raise

    async def probe(self) -> ProbeReport:
        """The ruling-R5 probe: refresh, list the calendars, write, delete.

        Every leg reports its own outcome; a leg after a failed one is reported
        as ``skipped`` — the probe names what it did not do. A failed leg never
        raises: the report *is* the probe's answer.
        """
        self._access_token = None  # a probe proves a live exchange, not a cache
        legs: list[ProbeLeg] = []
        try:
            token = await self._refresh_token()
        except (CalendarAuthFailed, CalendarProviderUnavailable) as failure:
            return ProbeReport(
                (ProbeLeg(TOKEN_REFRESH, ok=False, detail=failure.reason), *skipped_legs(1))
            )
        self._access_token = token
        legs.append(ProbeLeg(TOKEN_REFRESH, ok=True))
        try:
            await self._request("GET", f"{GRAPH_API_BASE_URL}{CALENDARS_PATH}", access_token=token)
            legs.append(ProbeLeg(CALENDAR_LIST, ok=True))
        except (CalendarAuthFailed, CalendarProviderUnavailable) as failure:
            return ProbeReport(
                (*legs, ProbeLeg(CALENDAR_LIST, ok=False, detail=failure.reason), *skipped_legs(2))
            )
        try:
            created = await self._request(
                "POST",
                self._events_url(),
                access_token=token,
                json_body=_event_body(_probe_event()),
            )
            probe_event_id = _event_id(created)
            legs.append(ProbeLeg(PROBE_WRITE, ok=True))
        except (CalendarAuthFailed, CalendarProviderUnavailable) as failure:
            return ProbeReport(
                (*legs, ProbeLeg(PROBE_WRITE, ok=False, detail=failure.reason), *skipped_legs(3))
            )
        try:
            await self._request(
                "DELETE",
                f"{self._events_url()}/{quote(probe_event_id, safe='')}",
                access_token=token,
            )
            legs.append(ProbeLeg(PROBE_DELETE, ok=True))
        except (CalendarAuthFailed, CalendarProviderUnavailable) as failure:
            legs.append(ProbeLeg(PROBE_DELETE, ok=False, detail=failure.reason))
        return ProbeReport(tuple(legs))

    async def _token(self) -> str:
        """The cached access token, refreshing it once per client lifetime."""
        if self._access_token is None:
            self._access_token = await self._refresh_token()
        return self._access_token

    async def _refresh_token(self) -> str:
        """The refresh-token exchange, as the v2.0 endpoint specifies it.

        Any 4xx from the token endpoint is a credential problem — the grant is
        gone, the secret is wrong — so it reads as ``CalendarAuthFailed`` even
        though the same status on an API call would be transient. A missing
        ``access_token`` in a 2xx body is the drift case: transient, because
        nothing about the credential says it will refuse again.
        """
        url = TOKEN_URL_TEMPLATE.format(tenant_id=self._credential.directory_tenant_id)
        try:
            body = await self._request(
                "POST",
                url,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": self._credential.refresh_token,
                    "client_id": self._credential.client_id,
                    "client_secret": self._credential.client_secret,
                    "scope": TOKEN_SCOPE,
                },
            )
        except CalendarProviderUnavailable as failure:
            if failure.status is not None and 400 <= failure.status < 500:
                raise CalendarAuthFailed(failure.reason, status=failure.status) from failure
            raise
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarProviderUnavailable("malformed_token_response")
        return token

    async def _find_event_id(self, access_token: str, uid: str) -> str | None:
        """The provider event id of the event holding this iCalUID, if any."""
        listing = await self._request(
            "GET",
            self._events_url(),
            access_token=access_token,
            params={"$filter": f"iCalUID eq '{uid}'"},
        )
        events = listing.get("value")
        if not isinstance(events, list) or not events:
            return None
        return _event_id(events[0] if isinstance(events[0], dict) else {})

    def _events_url(self) -> str:
        """The target calendar's events endpoint, the calendar id URL-quoted."""
        calendar_id = quote(self._credential.calendar_id, safe="")
        return f"{GRAPH_API_BASE_URL}{CALENDARS_PATH}/{calendar_id}/events"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        headers = (
            {"Authorization": f"Bearer {access_token}"} if access_token is not None else None
        )
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.request(
                    method, url, data=form, json=json_body, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                # Transport-level failure: outage, DNS, TLS, timeout. The
                # message carries no credential — neither the URL's query nor
                # the headers nor the form are included.
                raise CalendarProviderUnavailable(
                    f"{method} {_path(url)}: {exc.__class__.__name__}"
                ) from exc
        if response.status_code >= 400:
            reason = _refusal_reason(response)
            if response.status_code in (401, 403):
                raise CalendarAuthFailed(reason, status=response.status_code)
            raise CalendarProviderUnavailable(reason, status=response.status_code)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise CalendarProviderUnavailable("malformed_response") from exc
        if not isinstance(body, dict):
            raise CalendarProviderUnavailable("malformed_response")
        return body


def _event_body(event: IcsEvent) -> dict[str, Any]:
    """The projection entry as a Graph event: an all-day single instance.

    Graph's all-day end is exclusive, so a one-day deadline ends at the next
    midnight — the same exclusive-end rule Google Calendar keeps. Alarms ride
    as ISO 8601 durations (``P30D``) in the provider's own reminder shape.
    Unlike Google, Graph documents no maximum reminder lead — the event
    resource's ``reminderMinutesBeforeStart`` is an unbounded Int32 and the
    ISO duration form carries no documented cap — so there is deliberately no
    client-side clamp here: no number is invented where the provider names
    none. If a live tenant's Outlook policy ever refuses a long lead, the
    typed failure the sweep surfaces is the signal to revisit.
    """
    day = event.date.isoformat()
    following = (event.date + timedelta(days=1)).isoformat()
    return {
        "subject": event.summary,
        "iCalUID": event.uid,
        "isAllDay": True,
        "start": {"dateTime": f"{day}T00:00:00Z"},
        "end": {"dateTime": f"{following}T00:00:00Z"},
        "body": {"contentType": "text", "content": event.description},
        "reminders": {
            "isReminderOn": bool(event.alarms),
            "remindersBeforeEvent": [f"P{alarm.days_before}D" for alarm in event.alarms],
        },
    }


def _probe_event() -> IcsEvent:
    """One unmistakable probe event; the UID is unique so a crashed probe's
    leftover can never collide with a later one."""
    return IcsEvent(
        uid=f"aics-probe-{uuid4().hex}",
        date=datetime.now(UTC).date(),
        summary=PROBE_EVENT_SUMMARY,
        description="A one-way-sync connectivity probe; this event deletes itself.",
    )


def _event_id(body: dict[str, Any]) -> str:
    """The provider's event id from a response body, or the drift error."""
    event_id = body.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise CalendarProviderUnavailable("malformed_response")
    return event_id


def _refusal_reason(response: httpx.Response) -> str:
    """A short reason for a non-2xx answer: the provider's code, or its status.

    The token endpoint refuses flat (``{"error": "invalid_grant"}``) and the
    API refuses nested (``{"error": {"code": "ErrorAccessDenied"}}``); both
    narrow to the code, and the description beside it is dropped — a body is
    not a safe thing to echo.
    """
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if isinstance(error, dict):
        error = error.get("code")
    if not isinstance(error, str) or not error:
        error = f"http_{response.status_code}"
    return error[:_MAX_REASON_LENGTH]


def _path(url: str) -> str:
    """The URL without its query, for a failure message."""
    return url.split("?", 1)[0]
