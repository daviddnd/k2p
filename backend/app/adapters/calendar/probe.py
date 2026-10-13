"""The probe's shared shape (ruling R5; the ``/mail/probe`` precedent, extended).

The probe is honest work: it refreshes the token, reads the calendar list,
writes a probe event, and deletes it — proving *write* permission, which a
read-only probe cannot. Each leg reports its own outcome, so the route (slice
M8's Task 3) can render exactly where a failing credential broke. Both
adapters run the same four legs in the same order and use this module's names,
so the per-provider reports are directly comparable.

A leg that was never attempted — because an earlier leg failed — is reported
as not passed with the detail ``skipped``: the probe names what it did not do
rather than staying silent about it.
"""

from dataclasses import dataclass

# The legs, in the order the probe runs them. The names are the wire
# vocabulary the probe route renders, one entry per leg.
TOKEN_REFRESH = "token_refresh"
CALENDAR_LIST = "calendar_list"
PROBE_WRITE = "probe_write"
PROBE_DELETE = "probe_delete"

LEG_NAMES: tuple[str, ...] = (TOKEN_REFRESH, CALENDAR_LIST, PROBE_WRITE, PROBE_DELETE)

# What a leg that never ran carries: it did not pass, and it was not attempted.
SKIPPED = "skipped"


@dataclass(frozen=True)
class ProbeLeg:
    """One probe leg's outcome: its name, whether it passed, and a short detail.

    ``detail`` is a bounded reason code on failure (the typed error's code,
    or ``skipped``) and ``None`` on success.
    """

    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ProbeReport:
    """Every leg's outcome, in order. ``passed`` is all four legs passing."""

    legs: tuple[ProbeLeg, ...]

    @property
    def passed(self) -> bool:
        return all(leg.ok for leg in self.legs)


def skipped_legs(after: int) -> tuple[ProbeLeg, ...]:
    """The legs that never ran once leg ``after`` (a LEG_NAMES index) failed."""
    return tuple(ProbeLeg(name=name, ok=False, detail=SKIPPED) for name in LEG_NAMES[after:])
