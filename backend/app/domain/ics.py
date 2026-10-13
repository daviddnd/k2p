"""iCalendar serialization for the subscription feed (RFC 5545), hand-rolled.

Why this module exists instead of a library: one-way calendar sync's generic
fallback is an `.ics` feed (scope §2.4; ADR-0013 item 3), and what the feed
needs from iCalendar is small — an all-day event per deadline, with alarms.
The two ways a hand-rolled writer breaks a real feed are mechanical, and
both are about *octets* rather than characters:

- **Folding.** RFC 5545 §3.1 caps a content line at 75 octets excluding the
  line break, and a fold may not split a character. This product's company
  names are bilingual, so the data reaches the writer as three-byte-per-
  character text: a writer that counts characters overshoots the limit, and
  one that slices the encoded bytes emits lines no parser can decode.
- **Escaping.** A comma in a company name is a value separator until it is
  escaped; so are semicolons, backslashes, and newlines.

The writer is deliberately format-only: it knows nothing about deadlines or
risk bands (``app.domain.calendar_sync`` projects those), and nothing about
clients — whether a given client honours alarms on all-day events, or
updates an event on a changed ``DTSTAMP``, is not something a writer can
fix and so is stated where the projection is decided, not here.

Pure and framework-free per ADR-0006: stdlib only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

# RFC 5545 §3.1: content lines are at most 75 octets excluding CRLF, and a
# continuation line's leading space is inside that budget.
LINE_OCTET_LIMIT = 75
CRLF = "\r\n"
_FOLD = " "


@dataclass(frozen=True)
class IcsAlarm:
    """One ``VALARM``: a display alarm ``days_before`` the event's start.

    For an all-day event the trigger is relative to the start of its day, so
    a client that honours it alerts at an hour its own settings choose.
    """

    days_before: int
    description: str


@dataclass(frozen=True)
class IcsEvent:
    """One all-day ``VEVENT``: a ``VALUE=DATE`` start, no end.

    All-day events are the right shape for statutory deadlines: they have no
    time of day, and the feed must not invent one.
    """

    date: date
    uid: str
    summary: str
    description: str
    alarms: tuple[IcsAlarm, ...] = ()


@dataclass(frozen=True)
class IcsProperty:
    """A calendar-level property — how the feed carries its own refusals."""

    name: str
    value: str


def escape_text(value: str) -> str:
    """``value`` as an iCalendar TEXT value (RFC 5545 §3.3.11).

    The backslash goes first, or the escapes this function introduces would
    themselves be escaped by the next replacement.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> list[str]:
    """The physical lines ``line`` folds into, each within the octet limit.

    A line that already fits is returned unchanged. Longer lines split on
    character boundaries: the first physical line holds at most 75 octets,
    and every continuation holds at most 74 plus its leading fold space.
    """
    segments: list[str] = []
    remaining = line
    while True:
        if len(remaining.encode("utf-8")) <= LINE_OCTET_LIMIT:
            segments.append(remaining)
            return segments
        cut = _octet_prefix_length(remaining, LINE_OCTET_LIMIT)
        segments.append(remaining[:cut])
        remaining = _FOLD + remaining[cut:]


def render_calendar(
    *,
    prodid: str,
    dtstamp: datetime,
    events: Sequence[IcsEvent],
    properties: Sequence[IcsProperty] = (),
) -> str:
    """The complete ``VCALENDAR`` body, CRLF-terminated on every line.

    ``dtstamp`` is the generation instant stamped on every event (the feed
    is regenerated per fetch, so one instant for the file is the honest
    value); a naive datetime is read as UTC, matching the execution plane's
    clock.
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}"]
    lines.extend(f"{prop.name}:{escape_text(prop.value)}" for prop in properties)
    for event in events:
        lines.extend(_event_lines(event, dtstamp))
    lines.append("END:VCALENDAR")
    return "".join(segment + CRLF for line in lines for segment in fold_line(line))


def _event_lines(event: IcsEvent, dtstamp: datetime) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{_utc_timestamp(dtstamp)}",
        f"DTSTART;VALUE=DATE:{event.date:%Y%m%d}",
        f"SUMMARY:{escape_text(event.summary)}",
        f"DESCRIPTION:{escape_text(event.description)}",
    ]
    for alarm in event.alarms:
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{escape_text(alarm.description)}",
                f"TRIGGER:-P{alarm.days_before}D",
                "END:VALARM",
            ]
        )
    lines.append("END:VEVENT")
    return lines


def _utc_timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _octet_prefix_length(text: str, limit: int) -> int:
    """How many leading characters of ``text`` fit in ``limit`` UTF-8 octets.

    Callers only fold lines that are already over the limit, and a single
    character is far smaller than it, so the result is always ≥ 1 — the
    fold loop cannot stall.
    """
    used = 0
    for index, character in enumerate(text):
        size = len(character.encode("utf-8"))
        if used + size > limit:
            return index
        used += size
    return len(text)
