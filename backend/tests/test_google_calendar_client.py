"""Wire tests for the Google Calendar client (slice M8; ADR-0013).

The real client — real httpx, real URLs, real form bodies and JSON — against
``tests/fake_google_calendar.py``, which mirrors Google's OAuth token endpoint
and the Calendar API v3 surface the push uses: ``events.insert``/``patch``/
``delete`` on the target calendar, the ``iCalUID`` reconciliation lookup, and
the calendar-list read. What is exercised is what a drifted API, a revoked
grant, or an outage does to the push sweep and the probe: the refresh-token
exchange, create/update-by-content, delete, the per-leg probe (ruling R5), and
the typed error family (``app.adapters.calendar.errors``) the Task 3 worker
catches without importing httpx. No test touches the network.

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
from app.adapters.calendar.google_client import GoogleCalendarClient
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
from tests.fake_google_calendar import FakeGoogleCalendar, UnreachableGoogleCalendar

CLIENT_ID = "fake-gcal-client-id"
CLIENT_SECRET = "fake-gcal-secret"
REFRESH_TOKEN = "the-refresh-token"
CALENDAR_ID = "deadlines@tcsp.example"

EVENT = IcsEvent(
    uid="nar1-62280001-2026",
    date=date(2026, 11, 30),
    summary="NAR1 — Acme Ltd (62280001)",
    description="Annual return due. Filing window opens 2026-09-01.",
    alarms=(IcsAlarm(days_before=30, description="NAR1 due soon"),),
)


def credential(**overrides: str) -> ProviderCredential:
    values = {
        "provider": CalendarProviderName.GOOGLE,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "calendar_id": CALENDAR_ID,
    }
    values.update(overrides)
    return ProviderCredential(**values)  # type: ignore[arg-type]


def client_for(
    google: FakeGoogleCalendar, cred: ProviderCredential | None = None
) -> GoogleCalendarClient:
    """The real client pointed at the fake provider, in-process."""
    return GoogleCalendarClient(
        cred or credential(), timeout=1.0, transport=httpx.ASGITransport(app=google.app)
    )


def legs_by_name(report: ProbeReport) -> dict[str, ProbeLeg]:
    return {leg.name: leg for leg in report.legs}


def test_the_client_satisfies_the_calendar_provider_protocol() -> None:
    client = GoogleCalendarClient(credential(), timeout=1.0)
    provider: CalendarProvider = client  # mypy asserts the structural match
    assert provider is client


async def test_upserting_creates_the_event_and_exchanges_the_refresh_token_once() -> None:
    google = FakeGoogleCalendar()
    client = client_for(google)

    event_id = await client.upsert_event(EVENT)

    # The fake refuses any token request whose form does not carry this
    # client_id/client_secret and grant_type=refresh_token, so arriving here
    # means the exchange was well formed. The access token is cached for the
    # client's lifetime: a second call reuses it rather than re-asking.
    second_id = await client.upsert_event(replace(EVENT, uid="nar1-62280001-2027"))
    assert second_id != event_id
    assert google.refreshes == 1
    stored = google.events[event_id]
    assert stored["iCalUID"] == EVENT.uid
    assert stored["summary"] == EVENT.summary
    assert stored["description"] == EVENT.description
    assert stored["start"] == {"date": "2026-11-30"}
    assert stored["end"] == {"date": "2026-12-01"}  # exclusive, the API's own rule
    assert stored["reminders"]["overrides"] == [{"method": "popup", "minutes": 40_320}]
    assert google.event_creates == 2
    assert google.event_updates == 0


async def test_reminder_minutes_are_clamped_at_the_apis_four_week_cap() -> None:
    # The API's documented override range is 0-40320 minutes. The client must
    # guarantee its wire shape stays inside it: the API would 400 anything
    # longer, and this module reads a 400 as transient — an unclamped alarm
    # would be a write the sweep retried forever. Twenty-eight days and under
    # pass through unchanged; the floor is zero.
    google = FakeGoogleCalendar()
    client = client_for(google)
    event = replace(
        EVENT,
        alarms=(
            IcsAlarm(days_before=45, description="beyond the cap"),
            IcsAlarm(days_before=30, description="NAR1 due soon"),
            IcsAlarm(days_before=15, description="fortnight"),
            IcsAlarm(days_before=7, description="a week"),
            IcsAlarm(days_before=0, description="day of"),
        ),
    )

    event_id = await client.upsert_event(event)

    overrides = google.events[event_id]["reminders"]["overrides"]
    assert [entry["minutes"] for entry in overrides] == [40_320, 40_320, 21_600, 10_080, 0]


async def test_upserting_again_updates_in_place_and_keeps_the_provider_event_id() -> None:
    google = FakeGoogleCalendar()
    client = client_for(google)

    created = await client.upsert_event(EVENT)
    corrected = replace(EVENT, summary="NAR1 — Acme Ltd (62280001), corrected")
    updated = await client.upsert_event(corrected)

    assert updated == created  # the push state keeps this id; it must not move
    assert google.event_creates == 1
    assert google.event_updates == 1
    assert google.uid_lookups == [EVENT.uid, EVENT.uid]
    assert google.events[created]["summary"] == "NAR1 — Acme Ltd (62280001), corrected"


async def test_delete_removes_the_event_and_tolerates_one_already_gone() -> None:
    google = FakeGoogleCalendar()
    client = client_for(google)

    event_id = await client.upsert_event(EVENT)
    await client.delete_event(event_id)
    assert event_id not in google.events
    assert google.event_deletes == 1

    # A disappeared-obligation delete that races a provider-side removal is
    # still a delete: the sweep forgets the row either way.
    await client.delete_event("gcal-event-999")
    assert google.delete_attempts == 2
    assert google.event_deletes == 1


async def test_the_probe_reports_each_leg_and_leaves_no_probe_event_behind() -> None:
    google = FakeGoogleCalendar()
    client = client_for(google)
    await client.upsert_event(EVENT)
    assert google.refreshes == 1

    report = await client.probe()

    # The probe proves a live refresh (ruling R5), not a cached token.
    assert google.refreshes == 2
    assert report.passed
    assert [leg.name for leg in report.legs] == [
        TOKEN_REFRESH,
        CALENDAR_LIST,
        PROBE_WRITE,
        PROBE_DELETE,
    ]
    assert all(leg.ok for leg in report.legs)
    assert google.calendar_list_reads == 1
    assert google.event_creates == 2  # one deadline + the probe event
    assert google.event_deletes == 1  # the probe event only
    assert len(google.events) == 1  # the deadline remains, the probe event is gone


async def test_a_probe_with_a_dead_refresh_token_fails_the_first_leg_and_skips_the_rest() -> None:
    google = FakeGoogleCalendar(invalid_refresh_tokens={REFRESH_TOKEN})
    report = await client_for(google).probe()

    assert not report.passed
    legs = legs_by_name(report)
    assert legs[TOKEN_REFRESH].ok is False
    assert legs[TOKEN_REFRESH].detail == "invalid_grant"
    for name in (CALENDAR_LIST, PROBE_WRITE, PROBE_DELETE):
        assert legs[name].ok is False
        assert legs[name].detail == "skipped"
    assert google.calendar_list_reads == 0
    assert google.event_creates == 0


async def test_a_probe_whose_write_leg_fails_reports_it_and_skips_only_the_delete() -> None:
    google = FakeGoogleCalendar(events_status=500)
    report = await client_for(google).probe()

    assert not report.passed
    legs = legs_by_name(report)
    assert legs[TOKEN_REFRESH].ok is True
    assert legs[CALENDAR_LIST].ok is True
    assert legs[PROBE_WRITE].ok is False
    assert legs[PROBE_WRITE].detail == "backendError"
    assert legs[PROBE_DELETE].ok is False
    assert legs[PROBE_DELETE].detail == "skipped"


async def test_a_revoked_refresh_token_is_a_typed_auth_failure_not_a_crash() -> None:
    google = FakeGoogleCalendar(invalid_refresh_tokens={REFRESH_TOKEN})

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(google).upsert_event(EVENT)

    assert failure.value.reason == "invalid_grant"


async def test_a_wrong_client_secret_is_a_typed_auth_failure() -> None:
    google = FakeGoogleCalendar()

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(google, credential(client_secret="wrong-secret")).upsert_event(EVENT)

    assert failure.value.reason == "invalid_client"


async def test_a_server_error_is_a_typed_transient_failure() -> None:
    google = FakeGoogleCalendar(events_status=500)

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client_for(google).upsert_event(EVENT)

    assert failure.value.status == 500


async def test_an_unreachable_provider_is_a_transient_failure_without_a_status() -> None:
    client = GoogleCalendarClient(credential(), timeout=1.0, transport=UnreachableGoogleCalendar())

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client.upsert_event(EVENT)

    assert failure.value.status is None


async def test_a_token_response_without_an_access_token_is_a_transient_failure() -> None:
    google = FakeGoogleCalendar(token_body={})

    with pytest.raises(CalendarProviderUnavailable) as failure:
        await client_for(google).upsert_event(EVENT)

    assert failure.value.reason == "malformed_token_response"


async def test_no_error_carries_the_provider_body_or_the_credential() -> None:
    # The fake marks every refusal body with ``marker``; a client that echoed
    # the body (or the request's own secret) would be caught here.
    google = FakeGoogleCalendar(
        marker="provider-body-marker", invalid_refresh_tokens={REFRESH_TOKEN}
    )

    with pytest.raises(CalendarAuthFailed) as failure:
        await client_for(google).upsert_event(EVENT)

    rendered = f"{failure.value!r} {failure.value}"
    assert "provider-body-marker" not in rendered
    assert CLIENT_SECRET not in rendered
