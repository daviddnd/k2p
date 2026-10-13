"""A faithful stand-in for Microsoft's token endpoint and the Graph calendar API.

The shapes here are copied from Microsoft's documentation: the v2.0 token
endpoint's form-encoded refresh grant and its ``access_token``/``expires_in``
answer (and the flat ``{"error": "invalid_grant"}`` refusal), the delegated
``/me/calendars`` list, and the target calendar's events surface — ``GET``
with the ``$filter=iCalUID eq '…'`` reconciliation lookup, ``POST`` to create,
``PATCH``/``DELETE`` on ``/me/calendars/{id}/events/{event_id}``. Events live
in a store keyed by the provider's own event id, the way the push state holds
them; the ``iCalUID`` is what the client's upsert searches by. A client that
satisfies this fake therefore satisfies the real endpoints; only a real M365
tenant remains untested by it.

No socket: the fake is a small ASGI app driven through
``httpx.ASGITransport``, so the suite stays hermetic. Its levers stage what a
pushing deployment meets: a refused refresh (``invalid_refresh_tokens``), a
server error (``events_status``/``calendars_status``), a token endpoint that
answers without an access token (``token_body``), and the marker every refusal
body echoes so a test can prove the client never surfaces one.
"""

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN_PATH = "/{tenant_id}/oauth2/v2.0/token"
GRAPH_V1 = "/v1.0"
ME_CALENDARS = f"{GRAPH_V1}/me/calendars"

_GRANTED_SCOPE = "offline_access https://graph.microsoft.com/Calendars.ReadWrite"


class UnreachableGraph(httpx.AsyncBaseTransport):
    """A transport that fails the way a dead host does — without a socket."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("graph is unreachable", request=request)


@dataclass
class FakeMicrosoftGraph:
    """Microsoft's token endpoint and the Graph calendar surface, as one object."""

    directory_tenant_id: str = "fake-directory-tenant"
    client_id: str = "fake-graph-client-id"
    client_secret: str = "fake-graph-secret"
    calendar_id: str = "target-calendar-001"
    calendar_name: str = "K2P Deadlines"
    # Every token the fake mints carries this, so a test can search for it
    # anywhere (a database, a log) and prove it did not travel.
    marker: str = "fake-graph-token"
    expires_in: int = 3599

    # Levers.
    # A token endpoint refusal (any non-200 answers a flat invalid_grant).
    token_status: int = 200
    # A body in place of the real one: the drift case, a 200 with no
    # access_token in it.
    token_body: dict[str, Any] | None = None
    events_status: int = 200
    calendars_status: int = 200
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


def _build_app(graph: FakeMicrosoftGraph) -> FastAPI:
    app = FastAPI()

    @app.post(TOKEN_PATH)
    async def token(tenant_id: str, request: Request) -> JSONResponse:
        form = _form(await request.body())
        if tenant_id != graph.directory_tenant_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if graph.token_status != 200:
            return _token_error(graph, "invalid_grant")
        if form.get("grant_type") != "refresh_token":
            return _token_error(graph, "invalid_request")
        if form.get("client_id") != graph.client_id:
            return _token_error(graph, "invalid_client")
        if form.get("client_secret") != graph.client_secret:
            return _token_error(graph, "invalid_client")
        refresh_token = form.get("refresh_token", "")
        if refresh_token in graph.invalid_refresh_tokens:
            return _token_error(graph, "invalid_grant")
        if graph.token_body is not None:
            return JSONResponse(graph.token_body)
        graph.refreshes += 1
        access_token = f"{graph.marker}-access-{secrets.token_urlsafe(8)}"
        graph.access_tokens[access_token] = refresh_token
        return JSONResponse(
            {
                "access_token": access_token,
                "expires_in": graph.expires_in,
                "token_type": "Bearer",
                "scope": _GRANTED_SCOPE,
            }
        )

    @app.get(ME_CALENDARS)
    async def calendars(request: Request) -> JSONResponse:
        refusal = _authenticate(graph, request)
        if refusal is not None:
            return refusal
        if graph.calendars_status != 200:
            return _api_error(graph, graph.calendars_status, "generalException")
        graph.calendar_list_reads += 1
        return JSONResponse(
            {
                "value": [
                    {
                        "id": graph.calendar_id,
                        "name": graph.calendar_name,
                        "canEdit": True,
                        "isDefaultCalendar": False,
                    }
                ]
            }
        )

    @app.get(f"{ME_CALENDARS}/{{calendar_id}}/events")
    async def list_events(request: Request, calendar_id: str) -> JSONResponse:
        refusal = _authenticate(graph, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(graph, calendar_id)
        if missing is not None:
            return missing
        if graph.events_status != 200:
            return _api_error(graph, graph.events_status, "generalException")
        uid = _filter_uid(request.query_params.get("$filter"))
        if uid is None and request.query_params.get("$filter"):
            return _api_error(graph, 400, "invalidRequest")
        if uid is not None:
            graph.uid_lookups.append(uid)
            return JSONResponse(
                {
                    "value": [
                        dict(stored)
                        for stored in graph.events.values()
                        if stored.get("iCalUID") == uid
                    ]
                }
            )
        return JSONResponse({"value": [dict(stored) for stored in graph.events.values()]})

    @app.post(f"{ME_CALENDARS}/{{calendar_id}}/events")
    async def create_event(request: Request, calendar_id: str) -> JSONResponse:
        refusal = _authenticate(graph, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(graph, calendar_id)
        if missing is not None:
            return missing
        if graph.events_status != 200:
            return _api_error(graph, graph.events_status, "generalException")
        body = json_object(await request.body())
        event_id = f"graph-event-{len(graph.events) + 1}"
        stored = dict(body)
        stored["id"] = event_id
        graph.events[event_id] = stored
        graph.event_creates += 1
        return JSONResponse(dict(stored), status_code=201)

    @app.patch(f"{ME_CALENDARS}/{{calendar_id}}/events/{{event_id}}")
    async def update_event(request: Request, calendar_id: str, event_id: str) -> JSONResponse:
        refusal = _authenticate(graph, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(graph, calendar_id)
        if missing is not None:
            return missing
        if graph.events_status != 200:
            return _api_error(graph, graph.events_status, "generalException")
        stored = graph.events.get(event_id)
        if stored is None:
            return _api_error(graph, 404, "ErrorItemNotFound")
        stored.update(json_object(await request.body()))
        graph.event_updates += 1
        return JSONResponse(dict(stored))

    @app.delete(f"{ME_CALENDARS}/{{calendar_id}}/events/{{event_id}}")
    async def delete_event(request: Request, calendar_id: str, event_id: str) -> JSONResponse:
        refusal = _authenticate(graph, request)
        if refusal is not None:
            return refusal
        missing = _calendar_refusal(graph, calendar_id)
        if missing is not None:
            return missing
        if graph.events_status != 200:
            return _api_error(graph, graph.events_status, "generalException")
        graph.delete_attempts += 1
        if event_id not in graph.events:
            return _api_error(graph, 404, "ErrorItemNotFound")
        del graph.events[event_id]
        graph.event_deletes += 1
        return JSONResponse({}, status_code=204)

    return app


def _filter_uid(expression: str | None) -> str | None:
    """The ``iCalUID eq '…'`` filter, as the client's reconciliation sends it.

    An absent filter means "list everything"; an unparseable one is a client
    bug, and the fake refuses it rather than silently answering about the
    wrong events.
    """
    if expression is None:
        return None
    match = re.fullmatch(r"\s*iCalUID eq '([^']*)'\s*", expression)
    return match.group(1) if match is not None else None


def _calendar_refusal(graph: FakeMicrosoftGraph, calendar_id: str) -> JSONResponse | None:
    if calendar_id == graph.calendar_id:
        return None
    return _api_error(graph, 404, "ErrorItemNotFound")


def _authenticate(graph: FakeMicrosoftGraph, request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token not in graph.access_tokens:
        return _api_error(graph, 401, "InvalidAuthenticationToken")
    return None


def _token_error(graph: FakeMicrosoftGraph, code: str) -> JSONResponse:
    """The token endpoint's flat error body, echoing the marker a test searches for.

    Real Microsoft describes its refusals in ``error_description``; the fake
    puts its marker there so a client that echoed the body would be caught by
    a test rather than by a leak.
    """
    return JSONResponse(
        {"error": code, "error_description": f"refused by the fake ({graph.marker})"},
        status_code=400,
    )


def _api_error(graph: FakeMicrosoftGraph, status: int, code: str) -> JSONResponse:
    """The Graph API's error envelope (nested, unlike the token endpoint's)."""
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": f"refused by the fake ({graph.marker})",
            }
        },
        status_code=status,
    )


def _form(body: bytes) -> dict[str, str]:
    """The form-encoded token request as a dict (``+`` decodes to a space)."""
    return dict(parse_qsl(body.decode("utf-8")))


def json_object(body: bytes) -> dict[str, Any]:
    """A request JSON body that must be an object, empty when absent."""
    if not body:
        return {}
    parsed: Any = json.loads(body)
    return parsed if isinstance(parsed, dict) else {}
