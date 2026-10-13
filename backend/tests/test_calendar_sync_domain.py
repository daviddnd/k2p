"""Domain tests for the calendar-sync projection and the iCalendar writer
(scope §2.4; ADR-0013 item 3).

No database and no HTTP: both halves are pure, and the format properties
asserted here are the ones that break real feeds — CRLF line endings,
75-octet folding of bilingual text, TEXT escaping, all-day dates, alarms —
walked structurally rather than pattern-matched from one rendered string.
"""

import hashlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from app.domain.calendar_sync import (
    UID_DOMAIN,
    Deadline,
    UnsupportedObligationError,
    event_uid,
    generate_feed_token,
    hash_feed_token,
    project_deadline,
    risk_phrase,
)
from app.domain.compliance import ComplianceEventKind, risk_band
from app.domain.ics import (
    LINE_OCTET_LIMIT,
    IcsAlarm,
    IcsEvent,
    IcsProperty,
    escape_text,
    fold_line,
    render_calendar,
)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_COMPANY_ID = UUID("22222222-2222-2222-2222-222222222222")
DUE_DATE = date(2026, 4, 12)
PRODID = "-//ai-com-sec//test//EN"
DTSTAMP = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)

# 33 characters, three octets each before the fold limit is even reached.
BILINGUAL_NAME = "陳大文會計師事務所有限公司" * 3


def make_deadline(**overrides: object) -> Deadline:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "br_number": "12345678",
        "company_name": "Acme Limited",
        "kind": ComplianceEventKind.NAR1_DUE,
        "due_date": DUE_DATE,
        "period": 2026,
        "band": risk_band(30),
    }
    values.update(overrides)
    return Deadline(**values)  # type: ignore[arg-type]


def fold_groups(body: str) -> list[tuple[str, list[bytes]]]:
    """Each logical line of the body with the physical lines it was written as."""
    groups: list[tuple[str, list[bytes]]] = []
    for physical in physical_lines(body):
        text = physical.decode("utf-8")
        # A physical line starts with a space only when the writer folded.
        if text.startswith(" ") and groups:
            logical, written = groups[-1]
            groups[-1] = (logical + text[1:], [*written, physical])
        else:
            groups.append((text, [physical]))
    return groups


def logical_lines(body: str) -> list[str]:
    """The body's logical content lines, folds rejoined; CRLF enforced."""
    assert body.endswith("\r\n")
    for physical in body.split("\r\n")[:-1]:
        assert "\n" not in physical and "\r" not in physical
    return [logical for logical, _ in fold_groups(body)]


def physical_lines(body: str) -> list[bytes]:
    """The body's physical lines as bytes — the octet limit is about bytes."""
    encoded = body.encode("utf-8")
    assert encoded.endswith(b"\r\n")
    return encoded.split(b"\r\n")[:-1]


def components(lines: list[str]) -> list[list[str]]:
    """Every component as its own list of logical lines, nesting flattened."""
    found: list[list[str]] = []
    stack: list[list[str]] = []
    for line in lines:
        if line.startswith("BEGIN:"):
            stack.append([line])
        elif line.startswith("END:"):
            assert stack, f"END without BEGIN: {line}"
            block = stack.pop()
            block.append(line)
            found.append(block)
        else:
            assert stack, f"property outside any component: {line}"
            stack[-1].append(line)
    assert stack == [], "unclosed component"
    return found


def properties_of(block: list[str]) -> dict[str, str]:
    """One component's properties by name (parameters dropped)."""
    return {
        line.split(":", 1)[0].split(";", 1)[0]: line
        for line in block
        if not line.startswith(("BEGIN:", "END:"))
    }


def render(events: list[IcsEvent], *, properties: list[IcsProperty] | None = None) -> str:
    return render_calendar(
        prodid=PRODID,
        dtstamp=DTSTAMP,
        events=events,
        properties=[] if properties is None else properties,
    )


# --- The UID rule ---


def test_uid_is_deterministic_for_the_same_input() -> None:
    assert project_deadline(make_deadline()) == project_deadline(make_deadline())


def test_uid_names_the_company_obligation_and_period() -> None:
    assert (
        event_uid(company_id=COMPANY_ID, kind=ComplianceEventKind.NAR1_DUE, period=2026)
        == f"nar1_due-{COMPANY_ID}-2026@{UID_DOMAIN}"
    )


def test_a_moved_due_date_keeps_the_uid_and_moves_the_entry() -> None:
    """An anniversary correction must move the subscriber's entry, not add one."""
    original = project_deadline(make_deadline(due_date=date(2026, 4, 12)))
    corrected = project_deadline(make_deadline(due_date=date(2026, 4, 20)))
    assert corrected.uid == original.uid
    assert corrected.date == date(2026, 4, 20)


def test_the_next_period_is_a_new_entry_and_a_sibling_company_is_its_own() -> None:
    this_year = event_uid(
        company_id=COMPANY_ID, kind=ComplianceEventKind.NAR1_DUE, period=2026
    )
    next_year = event_uid(
        company_id=COMPANY_ID, kind=ComplianceEventKind.NAR1_DUE, period=2027
    )
    sibling = event_uid(
        company_id=OTHER_COMPANY_ID, kind=ComplianceEventKind.NAR1_DUE, period=2026
    )
    assert len({this_year, next_year, sibling}) == 3


# --- The entry's text and alarms ---


def test_the_entry_names_the_company_and_the_obligation() -> None:
    event = project_deadline(make_deadline())
    assert event.summary == "NAR1 annual return due: Acme Limited"
    assert "BRN 12345678" in event.description
    assert "Cap. 622 s.662" in event.description
    assert "green (30-day alert)" in event.description


def test_the_entry_falls_back_to_the_brn_when_no_name_is_held() -> None:
    event = project_deadline(make_deadline(company_name=None))
    assert event.summary == "NAR1 annual return due: 12345678"


def test_the_entry_carries_the_three_alert_alarms() -> None:
    event = project_deadline(make_deadline())
    assert [alarm.days_before for alarm in event.alarms] == [30, 15, 7]
    assert all("Acme Limited" in alarm.description for alarm in event.alarms)


def test_the_entry_carries_the_risk_band_as_of_the_fetch() -> None:
    amber = project_deadline(make_deadline(band=risk_band(10)))
    overdue = project_deadline(make_deadline(band=risk_band(-3)))
    assert "amber (15-day alert)" in amber.description
    assert "red (past due)" in overdue.description


def test_projection_refuses_an_obligation_kind_it_does_not_know() -> None:
    with pytest.raises(UnsupportedObligationError) as failure:
        project_deadline(make_deadline(kind=ComplianceEventKind.CHANGE_WINDOW_15D))
    assert failure.value.kind is ComplianceEventKind.CHANGE_WINDOW_15D


@pytest.mark.parametrize(
    ("days_remaining", "phrase"),
    [
        (60, "green"),
        (30, "green (30-day alert)"),
        (15, "amber (15-day alert)"),
        (7, "red (7-day alert)"),
        (-1, "red (past due)"),
    ],
)
def test_risk_phrase_covers_every_band(days_remaining: int, phrase: str) -> None:
    assert risk_phrase(risk_band(days_remaining)) == phrase


# --- The feed token ---


def test_feed_tokens_are_generated_secrets_stored_only_as_digests() -> None:
    token = generate_feed_token()
    assert token != generate_feed_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", token)
    assert hash_feed_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in hash_feed_token(token)


# --- The iCalendar writer ---


def test_every_line_ends_with_crlf() -> None:
    body = render([project_deadline(make_deadline())])
    assert body.count("\n") == body.count("\r\n")
    assert body.count("\r") == body.count("\r\n")
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.endswith("END:VCALENDAR\r\n")


def test_the_calendar_declares_its_version_and_prodid() -> None:
    lines = logical_lines(render([]))
    assert lines == ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "END:VCALENDAR"]


def test_an_event_is_structurally_complete() -> None:
    event = project_deadline(make_deadline())
    blocks = components(logical_lines(render([event])))
    kinds = [block[0][len("BEGIN:") :] for block in blocks]
    assert kinds.count("VCALENDAR") == 1
    assert kinds.count("VEVENT") == 1
    assert kinds.count("VALARM") == 3
    event_block = next(block for block in blocks if block[0] == "BEGIN:VEVENT")
    assert event_block[-1] == "END:VEVENT"
    properties = properties_of(event_block)
    assert set(properties) == {"UID", "DTSTAMP", "DTSTART", "SUMMARY", "DESCRIPTION"}
    assert properties["UID"] == f"UID:{event.uid}"
    assert properties["DTSTART"] == "DTSTART;VALUE=DATE:20260412"
    assert properties["SUMMARY"] == "SUMMARY:NAR1 annual return due: Acme Limited"
    assert properties["DTSTAMP"] == "DTSTAMP:20260301T093000Z"


def test_alarm_blocks_are_nested_inside_their_event() -> None:
    lines = logical_lines(render([project_deadline(make_deadline())]))
    event_start = lines.index("BEGIN:VEVENT")
    event_end = lines.index("END:VEVENT")
    alarms = [
        index
        for index, line in enumerate(lines)
        if line in ("BEGIN:VALARM", "END:VALARM", "ACTION:DISPLAY", "TRIGGER:-P30D")
    ]
    assert alarms == sorted(alarms)
    assert all(event_start < index < event_end for index in alarms)
    assert [line for line in lines if line.startswith("TRIGGER:")] == [
        "TRIGGER:-P30D",
        "TRIGGER:-P15D",
        "TRIGGER:-P7D",
    ]
    alarm_blocks = [block for block in components(lines) if block[0] == "BEGIN:VALARM"]
    assert [properties_of(block)["DESCRIPTION"] for block in alarm_blocks] == [
        "DESCRIPTION:NAR1 annual return due: Acme Limited — 30 days before the due date.",
        "DESCRIPTION:NAR1 annual return due: Acme Limited — 15 days before the due date.",
        "DESCRIPTION:NAR1 annual return due: Acme Limited — 7 days before the due date.",
    ]


def test_bilingual_text_folds_at_75_octets_without_splitting_a_character() -> None:
    event = project_deadline(make_deadline(company_name=BILINGUAL_NAME))
    body = render([event])

    physical = physical_lines(body)
    assert all(len(line) <= LINE_OCTET_LIMIT for line in physical)
    # A byte-slicing fold would leave a physical line that is not valid
    # UTF-8; a character-counting fold would leave one over the octet limit.
    assert all(line.decode("utf-8") for line in physical)

    written = {logical: lines for logical, lines in fold_groups(body)}
    summary = f"SUMMARY:{escape_text(event.summary)}"
    assert summary in written
    summary_physical = written[summary]
    assert len(summary_physical) > 1, "the long summary should have folded"
    # The rename rejoins to the original text, and the first line used its
    # octet budget to the character boundary nearest the limit.
    assert b"".join(
        [summary_physical[0], *[line[1:] for line in summary_physical[1:]]]
    ).decode("utf-8") == summary
    assert LINE_OCTET_LIMIT - 3 < len(summary_physical[0]) <= LINE_OCTET_LIMIT
    assert all(line.startswith(b" ") for line in summary_physical[1:])


def test_a_line_that_already_fits_is_not_folded() -> None:
    assert fold_line("SUMMARY:short") == ["SUMMARY:short"]
    assert fold_line("A" * LINE_OCTET_LIMIT) == ["A" * LINE_OCTET_LIMIT]
    assert fold_line("陳" * 25) == ["陳" * 25]  # exactly 75 octets
    assert fold_line("陳" * 26) == ["陳" * 25, " 陳"]


def test_folding_an_ascii_line_splits_at_the_octet_limit() -> None:
    segments = fold_line("X" * 200)
    assert [len(segment.encode("utf-8")) for segment in segments] == [75, 75, 52]
    assert all(segment.startswith(" ") for segment in segments[1:])
    assert "".join([segments[0], *[segment[1:] for segment in segments[1:]]]) == "X" * 200


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("Acme, Limited", "Acme\\, Limited"),
        ("Acme; (HK)", "Acme\\; (HK)"),
        ("Acme \\ Branch", "Acme \\\\ Branch"),
        ("Acme\r\nLimited", "Acme\\nLimited"),
        ("Acme\nLimited", "Acme\\nLimited"),
        ("Acme\rLimited", "Acme\\nLimited"),
    ],
)
def test_text_values_escape_the_delimiters(raw: str, escaped: str) -> None:
    assert escape_text(raw) == escaped


def test_escaped_text_survives_into_the_body() -> None:
    event = project_deadline(
        make_deadline(company_name="Acme, Limited; (HK) \\ Branch\nSecond Line")
    )
    body = render([event])
    assert all(line.decode("utf-8") for line in physical_lines(body))
    summary = next(line for line in logical_lines(body) if line.startswith("SUMMARY:"))
    assert "\\," in summary
    assert "\\;" in summary
    assert "\\\\" in summary
    assert "\\n" in summary
    assert summary.endswith("Second Line")


def test_calendar_level_properties_are_rendered_and_escaped() -> None:
    value = f"{COMPANY_ID}:refused, twice"
    lines = logical_lines(
        render([], properties=[IcsProperty(name="X-AICS-NOT-GENERATED", value=value)])
    )
    assert f"X-AICS-NOT-GENERATED:{COMPANY_ID}:refused\\, twice" in lines
    # Calendar-level properties sit between the header and the close, and
    # the raw comma separator never reaches the body.
    assert lines.index(f"X-AICS-NOT-GENERATED:{COMPANY_ID}:refused\\, twice") == 3
    assert not any("refused, twice" in line for line in lines)


def test_two_deadlines_are_two_blocks_with_their_own_alarms() -> None:
    events = [
        project_deadline(make_deadline()),
        project_deadline(make_deadline(period=2027, due_date=date(2027, 4, 12))),
    ]
    blocks = components(logical_lines(render(events)))
    events_blocks = [block for block in blocks if block[0] == "BEGIN:VEVENT"]
    assert len(events_blocks) == 2
    assert len({properties_of(block)["UID"] for block in events_blocks}) == 2
    assert len([block for block in blocks if block[0] == "BEGIN:VALARM"]) == 6


def test_an_empty_calendar_is_still_a_valid_calendar() -> None:
    assert render([]) == (
        "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "END:VCALENDAR"])
        + "\r\n"
    )


def test_alarms_are_optional_on_an_event() -> None:
    bare = IcsEvent(date=DUE_DATE, uid="uid@domain", summary="s", description="d")
    assert "BEGIN:VALARM" not in render([bare])
    alarmed = replace(bare, alarms=(IcsAlarm(days_before=1, description="a day before"),))
    assert "TRIGGER:-P1D" in render([alarmed])
