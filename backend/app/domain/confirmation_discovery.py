"""Confirmation-discovery domain rules (scope §2.5; the discovery slice).

The discovery cycle asks, per company, the one question the client-actions
panel answers per period: **has this period's data-confirmation ask date
arrived, and has the client been asked?** The vocabulary and the selection
rule live here, pure and framework-free (ADR-0006):

- The outcome vocabulary — ``generated | refused | skipped`` — is what one
  cycle reports per company it examined; a refusal is never a write, only the
  trigger's own typed exception caught and named (the application module owns
  that mapping).
- ``already_requested`` is the one skip reason, and it is the panel's own
  done-signal: any ``confirmation_requests`` row for ``(company, year)`` —
  draft, queued, sent or superseded — counts as "asked" (R2). Liveness is
  deliberately not the criterion: a superseded row is history that still
  answers the question.
- Eligibility is exactly three facts — the period's data-confirmation date
  (the anniversary minus the configured offset, ``app.domain.milestones``),
  the day the cycle runs, and whether any request row exists for the pair.
  The cycle attempts the **earliest** eligible candidate year per company per
  run; a period whose ask date has not arrived is not eligible whatever its
  year, and an asked period is not eligible whatever its date.
- The candidate years are the panel's own range, ``as_of.year - 1`` through
  ``as_of.year + 2`` — the same four periods the client-actions panel reads
  (``app.application.service_renewals.pending_actions``). One range, stated
  once, so the two derivations cannot drift on their bounds (R9 pins the
  agreement by test, not by shared code).
"""

from collections.abc import Mapping, Set
from datetime import date
from enum import StrEnum


class DiscoveryOutcome(StrEnum):
    """What the cycle decided about one company (the report's vocabulary)."""

    GENERATED = "generated"
    REFUSED = "refused"
    SKIPPED = "skipped"


class DiscoverySkip(StrEnum):
    """Why an arrived period owed nothing, when the outcome is ``skipped``."""

    ALREADY_REQUESTED = "already_requested"


def candidate_years(as_of: date) -> range:
    """The periods the cycle examines: the panel's own four-year range."""
    return range(as_of.year - 1, as_of.year + 3)


def is_eligible(data_confirmation_date: date, as_of: date, has_request: bool) -> bool:
    """One (company, year) is due when its ask date has arrived and no request
    row exists for the pair — the whole eligibility rule, three facts."""
    return data_confirmation_date <= as_of and not has_request


def earliest_eligible_year(
    data_confirmation_by_year: Mapping[int, date],
    requested_years: Set[int],
    as_of: date,
) -> int | None:
    """The earliest candidate year that is due, or ``None`` when every arrived
    candidate year already has a request row (or none has arrived)."""
    for year in candidate_years(as_of):
        if is_eligible(data_confirmation_by_year[year], as_of, year in requested_years):
            return year
    return None


def earliest_arrived_year(
    data_confirmation_by_year: Mapping[int, date], as_of: date
) -> int | None:
    """The earliest candidate year whose ask date has arrived, requests ignored.

    The application uses this only to name the period behind a skip: when no
    year is eligible but one has arrived and is asked, the company was asked
    about its earliest arrived period — ``already_requested`` names it.
    """
    for year in candidate_years(as_of):
        if data_confirmation_by_year[year] <= as_of:
            return year
    return None


__all__ = [
    "DiscoveryOutcome",
    "DiscoverySkip",
    "candidate_years",
    "earliest_arrived_year",
    "earliest_eligible_year",
    "is_eligible",
]
