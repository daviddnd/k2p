"""A faithful stand-in for Google's token endpoint and the Calendar API v3.

The shapes here are copied from Google's documentation: the token endpoint's
form-encoded refresh grant and its ``access_token``/``expires_in`` answer (and
the flat ``{"error": "invalid_grant"}`` refusal — the same shape the Gmail
fake mirrors), and the Calendar API surface the push uses — ``events.list``
with the ``iCalUID`` reconciliation lookup, ``events.insert``, ``events.patch``,
``events.delete`` on ``/calendar/v3/calendars/{calendarId}/events``, and the
``users/me/calendarList`` read. Events live in a store keyed by the provider's
own event id, the way the push state holds them; the ``iCalUID`` is what the
client's upsert searches by. A client that satisfies this fake therefore
satisfies the real endpoints; only a real Google account remains untested by
it.

No socket: the fake is a small ASGI app driven through
``httpx.ASGITransport``, so the suite stays hermetic. Its levers stage what a
pushing deployment meets: a refused refresh (``invalid_refresh_tokens``), a
server error (``events_status``/``calendar_list_status``), a token endpoint
that answers without an access token (``token_body``), and the marker every
refusal body echoes so a test can prove the client never surfaces one.
"""

import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN_PATH = "/token"
CALENDAR_V3 = "/calendar/v3"
CALENDAR_LIST_PATH = f"{CALENDAR_V3}/users/me/calendarList"


class UnreachableGoogleCalendar(httpx.AsyncBaseTransport):
    """A transport that fails the way a dead host does — without a socket."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("google calendar is unreachable", request=request)


@dataclass
class FakeGoogleCalendar:
    """Google's token endpoint and the Calendar API, as one mutable object."""

    client_id: str = "fake-gcal-client-id"
    client_secret: str = "fake-gcal-secret"
    calendar_id: str = "deadlines@tcsp.example"
    calendar_name: str = "K2P Deadlines"
    # Every token the fake mints carries this, so a test can search for it
    # anywhere (a database, a log) and prove it did not travel.
    marker: str = "fake-gcal-token"
    expires_in: int = 3599

    # Levers.
    # A token endpoint refusal (any non-200 answers a flat invalid_grant).
    token_status: int = 200
    # A body in place of the real one: the drift case, a 200 with no
    # access_token in it.
    token_body: dict[str, Any] | None = None
    events_status: int = 200
    calendar_list_status: int = 200
    # Refresh tokens the fake treats as revoked or unknown.
    invalid_refresh_tokens: set[str] = field(default_factory=set)

    # What the fake holds and was asked to do (so a test can assert a client
    # did not re-ask, or did).
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: dict[str, str] = field(default_factory=dict)
    refreshes: int = 0
    event_creates: int = 0
    event_updates: int = 0
    event_deletes: int = 0
    # Every authenticated DELETE, refused or not: the delete leg's idempotency
    # is about a missing event answering success, and a counter that only
    # moved on success could not show the attempt happened.
    delete_attempts: int = 0
    calendar_list_reads: int = 0
    uid_lookups: list[str] = field(default_factory=list)
    app: FastAPI = field(init=False)

    def __post_init__(self) -> None:
        self.app = _build_app(self)


def _build_app(google: FakeGoogleCalendar) -> FastAPI:
    app = FastAPI()
    events_path = f"{CALENDAR_V3}/calendars/{{calendar_id}}/events"

    @app.post(TOKEN_PATH)
    async def token(request: Request) -> JSONResponse:
        form = _form(await request.body())
        if google.token_status != 200:
            return _token_error(google, "invalid_grant")
        if form.get("grant_type") != "refresh_token":
            return _token_error(google, "invalid_request")
        if form.get("client_id") != google.client_id:
            return _token_error(google, "invalid_client")
        if form.get("client_secret") != google.client_secret:
            return _token_error(google, "invalid_client")
        refresh_token = form.get("refresh_token", "")
        if refresh_token in google.invalid_refresh_tokens:
            return _token_error(google, "invalid_grant")
        if google.token_body is not None:
            return JSONResponse(google.token_body)
        google.refreshes += 1
        access_token = f"{google.marker}-access-{secrets.token_urlsafe(8)}"
        google.access_tokens[access_token] = refresh_token
        return JSONResponse(
            {
                "access_token": access_token,
                "expires_in": google.expires_in,
                "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/calendar",
            }
        )

    @app.get(CALENDAR_LIST_PATH)
    async def calendar_list(request: Request) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        if google.calendar_list_status != 200:
            return _api_error(google, google.calendar_list_status, "backendError")
        google.calendar_list_reads += 1
        return JSONResponse(
            {
                "items": [
                    {
                        "id": google.calendar_id,
                        "summary": google.calendar_name,
                        "accessRole": "owner",
                        "timeZone": "Asia/Hong_Kong",
                    }
                ]
            }
        )

    @app.get(events_path)
    async def list_events(request: Request, calendar_id: str) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(google, calendar_id)
        if missing is not None:
            return missing
        if google.events_status != 200:
            return _api_error(google, google.events_status, "backendError")
        uid = request.query_params.get("iCalUID")
        if uid is not None:
            # The product's only events.list call is the reconciliation
            # lookup; any other parameter combination is a client bug.
            if set(request.query_params) - {"iCalUID"}:
                return _api_error(google, 400, "invalidArgument")
            google.uid_lookups.append(uid)
            return JSONResponse(
                {
                    "items": [
                        dict(stored)
                        for stored in google.events.values()
                        if stored.get("iCalUID") == uid
                    ]
                }
            )
        return JSONResponse({"items": [dict(stored) for stored in google.events.values()]})

    @app.post(events_path)
    async def create_event(request: Request, calendar_id: str) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(google, calendar_id)
        if missing is not None:
            return missing
        if google.events_status != 200:
            return _api_error(google, google.events_status, "backendError")
        body = _json_object(await request.body())
        event_id = f"gcal-event-{len(google.events) + 1}"
        stored = dict(body)
        stored["id"] = event_id
        google.events[event_id] = stored
        google.event_creates += 1
        return JSONResponse(dict(stored))

    @app.patch(f"{events_path}/{{event_id}}")
    async def update_event(
        request: Request, calendar_id: str, event_id: str
    ) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(google, calendar_id)
        if missing is not None:
            return missing
        if google.events_status != 200:
            return _api_error(google, google.events_status, "backendError")
        stored = google.events.get(event_id)
        if stored is None:
            return _api_error(google, 404, "notFound")
        stored.update(_json_object(await request.body()))
        google.event_updates += 1
        return JSONResponse(dict(stored))

    @app.delete(f"{events_path}/{{event_id}}")
    async def delete_event(
        request: Request, calendar_id: str, event_id: str
    ) -> JSONResponse:
        refusal = _authenticate(google, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(google, calendar_id)
        if missing is not None:
            return missing
        if google.events_status != 200:
            return _api_error(google, google.events_status, "backendError")
        google.delete_attempts += 1
        if event_id not in google.events:
            return _api_error(google, 404, "notFound")
        del google.events[event_id]
        google.event_deletes += 1
        return JSONResponse({}, status_code=204)

    return app


def _calendar_refusal(google: FakeGoogleCalendar, calendar_id: str) -> JSONResponse | None:
    if calendar_id == google.calendar_id:
        return None
    return _api_error(google, 404, "notFound")


def _authenticate(google: FakeGoogleCalendar, request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token not in google.access_tokens:
        return _api_error(google, 401, "authError")
    return None


def _token_error(google: FakeGoogleCalendar, code: str) -> JSONResponse:
    """The token endpoint's flat error body, echoing the marker a test searches for.

    Real Google describes its refusals in ``error_description``; the fake puts
    its marker there so a client that echoed the body would be caught by a
    test rather than by a leak.
    """
    return JSONResponse(
        {"error": code, "error_description": f"refused by the fake ({google.marker})"},
        status_code=400,
    )


def _api_error(google: FakeGoogleCalendar, status: int, reason: str) -> JSONResponse:
    """The Calendar API's error envelope (nested, unlike the token endpoint's)."""
    return JSONResponse(
        {
            "error": {
                "code": status,
                "message": f"refused by the fake ({google.marker})",
                "errors": [{"reason": reason, "message": f"refused by the fake ({google.marker})"}],
            }
        },
        status_code=status,
    )


def _form(body: bytes) -> dict[str, str]:
    """The form-encoded token request as a dict (``+`` decodes to a space)."""
    return dict(parse_qsl(body.decode("utf-8")))


def _json_object(body: bytes) -> dict[str, Any]:
    """A request JSON body that must be an object, empty when absent."""
    if not body:
        return {}
    parsed: Any = json.loads(body)
    return parsed if isinstance(parsed, dict) else {}
