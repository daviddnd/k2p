"""One-way calendar-sync projection (scope §2.4; ADR-0013 item 3).

The V1 read model computes deadlines and their alert bands
(``app.domain.compliance``); this module turns one of those deadlines into
the calendar entry a subscriber's client consumes. The product decisions it
owns:

- **The UID is the obligation, not the due date.** ``{kind}-{company_id}-{period}@…``
  is deterministic from the company, the obligation, and the obligation's
  own period — for NAR1, the anniversary year the return falls in. A
  re-fetch therefore *updates* the entry a subscriber already holds instead
  of adding a second one, and a due date that moves (an anniversary-date
  correction) keeps its UID and moves in place. The period is in the key for
  the opposite reason: keying on (company, obligation) alone would let next
  year's return overwrite this year's entry on the subscriber's calendar.
- **The 30/15/7-day alerts are alarms on the deadline event**, not separate
  events: an alarm is what a calendar client is for, and three extra all-day
  events per company would multiply a subscriber's calendar with entries
  that say nothing new. The caveat is client variance, and it is real —
  several clients ignore alarms on all-day events, and the ones that honour
  them apply the user's own notification settings; the fallback for a client
  that drops them is the in-app calendar and dashboard, which are ungated in
  every tier.
- **Every deadline carries the full alert set**, not only the thresholds
  still ahead of it: a trigger whose instant is in the past simply never
  fires, so the set stays a pure function of the deadline rather than of
  the day it was fetched. What genuinely varies per fetch — the current risk
  band — goes in the description, where a change moves no entry's identity.

**Refusals, not guesses.** ``project_deadline`` raises for any obligation
kind whose calendar semantics are not established here — today every kind
but NAR1's due date. The 15-day change window shares the deadline rule but
takes its period from the ``ChangeEvent`` entity: the entity exists (Phase 4,
CE1), while this feed's projection of the window is not built — so the kind
is still refused rather than guessed.

**The feed credential is a capability URL.** The token below is what the
TCSP's calendar provider fetches with; it is generated once, and only its
digest is ever stored (``hash_feed_token``). Unsalted SHA-256 is right here
for the same reason it is right for the control plane's activation tokens
(ADR-0016 point 3): the value is 256 bits of generated entropy, not a
password, so there is nothing a salt or a slow hash would protect.

Pure and framework-free per ADR-0006: stdlib only.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.domain.compliance import ComplianceEventKind, RiskBand
from app.domain.ics import IcsAlarm, IcsEvent

# The UID's domain part is fixed rather than the deployment's hostname: a
# UID that changed with the deployment's address would re-create every entry
# on every subscriber's calendar.
UID_DOMAIN = "aics.compliance.local"

# Scope §2.4's alert thresholds, as alarms: days before the due date.
ALERT_DAYS_BEFORE = (30, 15, 7)

# 32 bytes of `secrets` entropy, URL-safe — the token travels in a query
# string a calendar provider fetches, so it must need no escaping.
TOKEN_BYTES = 32

_OBLIGATIONS: dict[ComplianceEventKind, tuple[str, str]] = {
    ComplianceEventKind.NAR1_DUE: (
        "NAR1 annual return due",
        "Statutory window: 42 days after the incorporation anniversary (Cap. 622 s.662).",
    )
}

_ALERT_LABELS: dict[ComplianceEventKind, str] = {
    ComplianceEventKind.ALERT_30D: "30-day alert",
    ComplianceEventKind.ALERT_15D: "15-day alert",
    ComplianceEventKind.ALERT_7D: "7-day alert",
}


class UnsupportedObligationError(ValueError):
    """The obligation kind has no established calendar projection.

    Carries the refused kind so a caller can name what it asked for; the
    feed never projects an entry whose meaning is uncertain.
    """

    def __init__(self, kind: object) -> None:
        self.kind = kind
        super().__init__(f"no established calendar projection for obligation kind {kind!r}")


@dataclass(frozen=True)
class Deadline:
    """One deadline as the read model sees it, plus its obligation period.

    ``period`` identifies *which* instance of the obligation this is — for
    NAR1, the anniversary year — not the date it currently falls on, which
    the period determines and which may move.
    """

    company_id: UUID
    br_number: str
    company_name: str | None
    kind: ComplianceEventKind
    due_date: date
    period: int
    band: RiskBand


def event_uid(*, company_id: UUID, kind: ComplianceEventKind, period: int) -> str:
    """The deterministic UID of one obligation instance (see the module note)."""
    return f"{kind.value}-{company_id}-{period}@{UID_DOMAIN}"


def project_deadline(deadline: Deadline) -> IcsEvent:
    """The calendar entry for one deadline: identity, date, text, alarms.

    Raises ``UnsupportedObligationError`` for a kind this projection does
    not know — an entry a subscriber cannot interpret is worse than none.
    """
    obligation = _OBLIGATIONS.get(deadline.kind)
    if obligation is None:
        raise UnsupportedObligationError(deadline.kind)
    label, note = obligation
    company = deadline.company_name or deadline.br_number
    return IcsEvent(
        date=deadline.due_date,
        uid=event_uid(
            company_id=deadline.company_id, kind=deadline.kind, period=deadline.period
        ),
        summary=f"{label}: {company}",
        description=(
            f"{label} for {company} (BRN {deadline.br_number}). {note} "
            f"Risk at fetch time: {risk_phrase(deadline.band)}."
        ),
        alarms=tuple(
            IcsAlarm(
                days_before=days,
                description=(
                    f"{label}: {company} — {days} days before the due date."
                ),
            )
            for days in ALERT_DAYS_BEFORE
        ),
    )


def risk_phrase(band: RiskBand) -> str:
    """The band as plain words for an event description.

    A past due date is nothing but past due; otherwise the phrase names the
    colour and the deepest threshold crossed. ``risk_band`` is the only
    producer of these bands on any path that reaches here, and it yields
    exactly the three alert kinds or ``None``.
    """
    if band.overdue:
        return "red (past due)"
    if band.alert is None:
        return band.level.value
    return f"{band.level.value} ({_ALERT_LABELS[band.alert]})"


def generate_feed_token() -> str:
    """A fresh feed token: the secret half of the capability URL."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_feed_token(token: str) -> str:
    """The stored form of a feed token (hex SHA-256) — never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
