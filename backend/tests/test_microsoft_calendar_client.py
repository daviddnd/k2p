"""Wire tests for the Microsoft Graph calendar client (slice M8; ADR-0013).

The real client — real httpx, real URLs, real form bodies and JSON — against
``tests/fake_microsoft_graph.py``, which mirrors the v2.0 token endpoint and
the delegated ``/me/calendars/{id}/events`` surface. What is exercised is what
a drifted API, a revoked grant, or an outage does to the push sweep and the
probe: the refresh-token exchange, create/update-by-content, delete, the
per-leg probe (ruling R5), and the typed error family (``app.adapters.calendar.
errors``) the Task 3 worker catches without importing httpx. No test touches
the network.

The ``CalendarProvider`` protocol (``app.application.calendar_sync``) is the
duck type the sweep calls; the suites assert a client instance satisfies it by
assigning one where the protocol is expected — mypy checks the structure, the
runtime suites check the behaviour.
"""

from dataclasses import replace
from datetime import date

import httpx
import pytest

from app.adapters.calendar.errors import CalendarAuthFailed, CalendarProviderUnavailable
from app.adapters.calendar.microsoft_client import MicrosoftGraphClient
from app.adapters.calendar.probe import (
    CALENDAR_LIST,
    PROBE_DELETE,
    PROBE_WRITE,
    TOKEN_REFRESH,
    ProbeLeg,
    ProbeReport,
)
from app.application.calendar_sync import CalendarProvider
from app.domain.calendar_push import CalendarProviderName, ProviderCredential
from app.domain.ics import IcsAlarm, IcsEvent
from tests.fake_microsoft_graph import FakeMicrosoftGraph, UnreachableGraph

CLIENT_ID = "fake-graph-client-id"
CLIENT_SECRET = "fake-graph-secret"
REFRESH_TOKEN = "the-refresh-token"
DIRECTORY_TENANT_ID = "fake-directory-tenant"
CALENDAR_ID = "target-calendar-001"

EVENT = IcsEvent(
    uid="nar1-62280001-2026",
    date=date(2026, 11, 30),
    summary="NAR1 — Acme Ltd (62280001)",
    description="Annual return due. Filing window opens 2026-09-01.",
    alarms=(IcsAlarm(days_before=30, description="NAR1 due soon"),),
)


def credential(**overrides: str) -> ProviderCredential:
    values = {
        "provider": CalendarProviderName.MICROSOFT,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "calendar_id": CALENDAR_ID,
        "directory_tenant_id": DIRECTORY_TENANT_ID,
    }
    values.update(overrides)
    return ProviderCredential(**values)  # type: ignore[arg-type]


def client_for(
    graph: FakeMicrosoftGraph, cred: ProviderCredential | None = None
) -> MicrosoftGraphClient:
    """The real client pointed at the fake provider, in-process."""
    return MicrosoftGraphClient(
        cred or credential(), timeout=1.0, transport=httpx.ASGITransport(app=graph.app)
    )


def legs_by_name(report: ProbeReport) -> dict[str, ProbeLeg]:
    return {leg.name: leg for leg in report.legs}


def test_the_client_satisfies_the_calendar_provider_protocol() -> None:
    client = MicrosoftGraphClient(credential(), timeout=1.0)
    provider: CalendarProvider = client  # mypy asserts the structural match
    assert provider is client


async def test_upserting_creates_the_event_and_exchanges_the_refresh_token_once() -> None:
    graph = FakeMicrosoftGraph()
    client = client_for(graph)

    event_id = await client.upsert_event(EVENT)

    # The fake refuses any token request whose form does not carry this
    # client_id/client_secret and grant_type=refresh_token, so arriving here
    # means the exchange was well formed. The access token is cached for the
    # client's lifetime: a second call reuses it rather than re-asking.
    second_id = await client.upsert_event(replace(EVENT, uid="nar1-62280001-2027"))
    assert second_id != event_id
    assert graph.refreshes == 1
    stored = graph.events[event_id]
    assert stored["iCalUID"] == EVENT.uid
    assert stored["subject"] == EVENT.summary
    assert stored["body"]["content"] == EVENT.description
    assert stored["isAllDay"] is True
    assert stored["start"] == {"dateTime": "2026-11-30T00:00:00Z"}
    assert stored["end"] == {"dateTime": "2026-12-01T00:00:00Z"}
    assert stored["reminders"]["remindersBeforeEvent"] == ["P30D"]
    assert graph.event_creates == 2
    assert graph.event_updates == 0


async def test_upserting_again_updates_in_place_and_keeps_the_provider_event_id() -> None:
    graph = FakeMicrosoftGraph()
    client = client_for(graph)

    created = await client.upsert_event(EVENT)
    corrected = replace(EVENT, summary="NAR1 — Acme Ltd (62280001), corrected")
    updated = await client.upsert_event(corrected)

    assert updated == created  # the push state keeps this id; it must not move
    assert graph.event_creates == 1
    assert graph.event_updates == 1
    assert graph.uid_lookups == [EVENT.uid, EVENT.uid]
    assert graph.events[created]["subject"] == "NAR1 — Acme Ltd (62280001), corrected"


async def test_delete_removes_the_event_and_tolerates_one_already_gone() -> None:
    graph = FakeMicrosoftGraph()
    client = client_for(graph)

    event_id = await client.upsert_event(EVENT)
    await client.delete_event(event_id)
    assert event_id not in graph.events
    assert graph.event_deletes == 1

    # A disappeared-obligation delete that races a provider-side removal is
    # still a delete: the sweep forgets the row either way.
    await client.delete_event("graph-event-999")
    assert graph.delete_attempts == 2
    assert graph.event_deletes == 1


async def test_the_probe_reports_each_leg_and_leaves_no_probe_event_behind() -> None:
    graph = FakeMicrosoftGraph()
    client = client_for(graph)
    await client.upsert_event(EVENT)
    assert graph.refreshes == 1

    report = await client.probe()

    # The probe proves a live refresh (ruling R5), not a cached token.
    assert graph.refreshes == 2
    assert report.passed
    assert [leg.name for leg in report.legs] == [
        TOKEN_REFRESH,
        CALENDAR_LIST,
        PROBE_WRITE,
        PROBE_DELETE,
    ]
    assert all(leg.ok for leg in report.legs)
    assert graph.calendar_list_reads == 1
    assert graph.event_creates == 2  # one deadline + the probe event
    assert graph.event_deletes == 1  # the probe event only
    assert len(graph.events) == 1  # the deadline remains, the probe event is gone


async def test_a_probe_with_a_dead_refresh_token_fails_the_first_leg_and_skips_the_rest() -> None:
    graph = FakeMicrosoftGraph(invalid_refresh_tokens={REFRESH_TOKEN})
    report = await client_for(graph).probe()

    assert not report.passed
    legs = legs_by_name(report)
    assert legs[TOKEN_REFRESH].ok is False
    assert legs[TOKEN_REFRESH].detail == "invalid_grant"
    for name in (CALENDAR_LIST, PROBE_WRITE, PROBE_DELETE):
        assert legs[name].ok is False
        assert legs[name].detail == "skipped"
    assert graph.calendar_list_reads == 0
    assert graph.event_creates == 0


async def test_a_probe_whose_write_leg_fails_reports_it_and_skips_only_the_delete() -> None:
    graph = FakeMicrosoftGraph(events_status=500)
    report = await client_for(graph).probe()

    assert not report.passed
    legs = legs_by_name(report)
    assert legs[TOKEN_REFRESH].ok is True
    assert legs[CALENDAR_LIST].ok is True
    assert legs[PROBE_WRITE].ok is False
    assert legs[PROBE_WRITE].detail == "generalException"
    assert legs[PROBE_DELETE].ok is False
    assert legs[PROBE_DELETE].detail == "skipped"


async def test_a_revoked_refresh_token_is_a_typed_auth_failure_not_a_crash() -> None:
    graph = FakeMicrosoftGraph(invalid_refresh_tokens={REFRESH_TOKEN})

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(graph).upsert_event(EVENT)

    assert failure.value.reason == "invalid_grant"


async def test_a_wrong_client_secret_is_a_typed_auth_failure() -> None:
    graph = FakeMicrosoftGraph()

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(graph, credential(client_secret="wrong-secret")).upsert_event(EVENT)

    assert failure.value.reason == "invalid_client"


async def test_a_server_error_is_a_typed_transient_failure() -> None:
    graph = FakeMicrosoftGraph(events_status=500)

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client_for(graph).upsert_event(EVENT)

    assert failure.value.status == 500


async def test_an_unreachable_provider_is_a_transient_failure_without_a_status() -> None:
    client = MicrosoftGraphClient(credential(), timeout=1.0, transport=UnreachableGraph())

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client.upsert_event(EVENT)

    assert failure.value.status is None


async def test_a_token_response_without_an_access_token_is_a_transient_failure() -> None:
    graph = FakeMicrosoftGraph(token_body={})

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client_for(graph).upsert_event(EVENT)

    assert failure.value.reason == "malformed_token_response"


async def test_no_error_carries_the_provider_body_or_the_credential() -> None:
    # The fake marks every refusal body with ``marker``; a client that echoed
    # the body (or the request's own secret) would be caught here.
    graph = FakeMicrosoftGraph(
        marker="provider-body-marker", invalid_refresh_tokens={REFRESH_TOKEN}
    )

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(graph).upsert_event(EVENT)

    rendered = f"{failure.value!r} {failure.value}"
    assert "provider-body-marker" not in rendered
    assert CLIENT_SECRET not in rendered
