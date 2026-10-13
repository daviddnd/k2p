"""Domain tests for the calendar push (scope §2.4; ADR-0013 item 3, 2026-10-03
amendment; slice M8).

Pure, no database and no HTTP: the push/fallback decision per tenant state,
the push-set derivation over the feed's projection entries (the projection
itself is ``app.domain.calendar_sync``'s — the push reuses it, never forks
it), and the content-hash rule that makes a sweep idempotent by content.
"""

from dataclasses import replace
from datetime import date
from uuid import UUID

from app.domain.calendar_push import (
    CalendarProviderName,
    PushEntryState,
    SyncMode,
    content_hash,
    decide_sync,
    derive_push_plan,
)
from app.domain.ics import IcsAlarm, IcsEvent

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")

GOOGLE_CREDENTIAL = {
    "provider": CalendarProviderName.GOOGLE,
    "client_id": "google-client",
    "client_secret": "google-secret",
    "refresh_token": "google-refresh",
    "calendar_id": "primary",
}

MICROSOFT_CREDENTIAL = {
    "provider": CalendarProviderName.MICROSOFT,
    "client_id": "microsoft-client",
    "client_secret": "microsoft-secret",
    "refresh_token": "microsoft-refresh",
    "calendar_id": "events-calendar",
    "directory_tenant_id": "microsoft-directory",
}


def make_event(**overrides: object) -> IcsEvent:
    values: dict[str, object] = {
        "date": date(2026, 4, 12),
        "uid": f"nar1_due-{COMPANY_ID}-2026@aics.compliance.local",
        "summary": "NAR1 annual return due: Acme Limited",
        "description": "NAR1 annual return due for Acme Limited (BRN 12345678).",
        "alarms": (
            IcsAlarm(days_before=30, description="30 days before the due date."),
            IcsAlarm(days_before=15, description="15 days before the due date."),
            IcsAlarm(days_before=7, description="7 days before the due date."),
        ),
    }
    values.update(overrides)
    return IcsEvent(**values)  # type: ignore[arg-type]


def make_credential(**overrides: object):
    from app.domain.calendar_push import ProviderCredential

    values: dict[str, object] = {
        **GOOGLE_CREDENTIAL,
        "probe_passed": False,
    }
    values.update(overrides)
    return ProviderCredential(**values)  # type: ignore[arg-type]


def state_for(event: IcsEvent, *, provider_event_id: str = "evt-1") -> PushEntryState:
    return PushEntryState(
        uid=event.uid, provider_event_id=provider_event_id, content_hash=content_hash(event)
    )


# --- The push/fallback decision ---


def test_no_credential_decides_the_feed() -> None:
    decision = decide_sync(None)
    assert decision.mode is SyncMode.FEED
    assert decision.provider is None
    assert decision.probe_passed is False
    assert decision.pushes is False


def test_a_credential_decides_push_and_carries_the_provider_and_probe_flag() -> None:
    unprobed = decide_sync(make_credential())
    assert unprobed.mode is SyncMode.PUSH
    assert unprobed.provider is CalendarProviderName.GOOGLE
    assert unprobed.probe_passed is False
    assert unprobed.pushes is False

    probed = decide_sync(make_credential(probe_passed=True))
    assert probed.mode is SyncMode.PUSH
    assert probed.probe_passed is True
    assert probed.pushes is True


def test_a_microsoft_credential_names_its_directory_tenant() -> None:
    decision = decide_sync(make_credential(**MICROSOFT_CREDENTIAL, probe_passed=True))
    assert decision.provider is CalendarProviderName.MICROSOFT
    assert decision.pushes is True


# --- The content hash ---


def test_the_content_hash_is_deterministic() -> None:
    assert content_hash(make_event()) == content_hash(make_event())
    assert len(content_hash(make_event())) == 64
    int(content_hash(make_event()), 16)  # a hex digest


def test_the_hash_changes_with_every_canonical_field() -> None:
    baseline = content_hash(make_event())
    changed = [
        make_event(uid="nar1_due-other-2026@aics.compliance.local"),
        make_event(date=date(2026, 4, 20)),
        make_event(summary="NAR1 annual return due: Beta Limited"),
        make_event(description="a different description"),
        make_event(alarms=make_event().alarms[1:]),
        make_event(
            alarms=tuple(replace(alarm, days_before=14) for alarm in make_event().alarms)
        ),
    ]
    assert all(content_hash(event) != baseline for event in changed)


# --- The push-set derivation ---


def test_new_entries_are_created() -> None:
    event = make_event()
    plan = derive_push_plan(pushed=[event], retained=[], existing={})
    assert plan.created == (event,)
    assert plan.updated == ()
    assert plan.unchanged == ()
    assert plan.deleted == ()


def test_an_entry_with_unchanged_content_is_not_pushed_again() -> None:
    event = make_event()
    plan = derive_push_plan(
        pushed=[event], retained=[], existing={event.uid: state_for(event)}
    )
    assert plan.created == ()
    assert plan.updated == ()
    assert plan.unchanged == (event.uid,)
    assert plan.deleted == ()


def test_a_changed_entry_is_updated_in_place() -> None:
    """Same UID, new content: the provider updates the event in place."""
    event = make_event()
    stale = PushEntryState(
        uid=event.uid,
        provider_event_id="evt-1",
        content_hash=content_hash(make_event(summary="the old text")),
    )
    plan = derive_push_plan(pushed=[event], retained=[], existing={event.uid: stale})
    assert plan.created == ()
    assert plan.updated == (event,)
    assert plan.unchanged == ()
    assert plan.deleted == ()


def test_a_disappeared_obligation_is_marked_for_delete_with_its_provider_event() -> None:
    gone = PushEntryState(
        uid="nar1_due-gone@aics.compliance.local",
        provider_event_id="evt-gone",
        content_hash="hash",
    )
    plan = derive_push_plan(pushed=[], retained=[], existing={gone.uid: gone})
    assert plan.created == ()
    assert plan.updated == ()
    assert plan.unchanged == ()
    assert plan.deleted == (gone,)


def test_a_retained_entry_is_neither_pushed_nor_deleted() -> None:
    """A gate-denied company's existing event persists (ADR-0012): its entry is
    carried as retained, so it is absent from every set of the plan."""
    event = make_event()
    existing = {event.uid: state_for(event)}
    plan = derive_push_plan(pushed=[], retained=[event], existing=existing)
    assert plan.created == ()
    assert plan.updated == ()
    assert plan.unchanged == ()
    assert plan.deleted == ()


def test_the_plan_is_sorted_by_uid_for_determinism() -> None:
    first = make_event(uid="nar1_due-a@aics.compliance.local")
    second = make_event(uid="nar1_due-b@aics.compliance.local")
    plan = derive_push_plan(pushed=[second, first], retained=[], existing={})
    assert plan.created == (first, second)
