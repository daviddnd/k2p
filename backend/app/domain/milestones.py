"""Milestone-panel domain rules (data-model §2.9 ``TenantConfig``; scope §2.4, §2.15).

Every client-facing date this product prints is a configured number of days
**before the company's anniversary date** (its made-up date) — never CR's
statutory deadline. The founder's ruling of 2026-09-24: the 42 days CR allows
after the anniversary are the emergency margin ("這 42 天應該是緊急情況的
備用時間"), so under the shipped defaults a client is asked to reply 62 days
before the filing falls due, and no configuration of this block can print CR's
last day to a client. ``milestones`` therefore holds:

- ``continuation_request_days_before`` — when staff ask the client whether the
  engagement continues (scope §2.15's continuation ask; the sending slice is
  what will carry it, §2.5);
- ``data_confirmation_days_before`` — when the confirmation request is
  generated (``app.application.confirmation``);
- ``client_reply_days_before`` — the date the client is asked to reply by;
- ``signature_post_days_before`` — when the drafts go out for signature;
- ``minimum_reply_window_days`` — the floor under the reply-by date: a client
  is never asked to reply in less than this, however late the trigger runs;
- ``reminder_cadence_days`` — days **after a request is sent**, a different
  computation from the four above (it is relative to a send, and nothing sends
  yet); it lives in this block because the panel is one place, and it is
  validated exactly as the rest is;
- ``anchor`` — the closed one-value vocabulary naming what the offsets count
  back from. ``anniversary`` is the ruled anchor; a stored row naming another
  one is refused rather than arithmetic'd on.

The four milestone dates are **not data**: they are the anniversary and its
configured offsets, derived every time the panel is read, so a reconfiguration
applies at once to every period (``milestone_dates``). The *statutory* NAR1 due
date (``anniversary + 42``, ``app.domain.compliance``) is what the calendar,
the alerts and the dashboard still use, and it is the guard that
``app.domain.confirmation.reply_deadline`` refuses against — this block never
replaces it, it stays inside it.

Validation is full-replace at PUT time and re-run on every read of a stored
block (``validate_milestones``): ordering coherent, every day a positive integer
within bounds, the cadence strictly increasing, and the reply offset within
``NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days`` — the offset and the
minimum window are two numbers measured against the one statutory window this
product works inside, and the bound keeps them together within CR's 42 days.
The ordering is the chain the work follows —
``continuation_request_days_before > data_confirmation_days_before >
client_reply_days_before > signature_post_days_before`` — because every offset
is counted *back* from the anniversary, so a **larger** offset is an **earlier**
date, and each milestone's day is when the thing before it should have been done
by: the client's answer to the continuation ask is due by the data-confirmation
date, the confirmed data is due by the client's reply-by date, and the client's
reply is due by the date the documents go out for signature. A block whose
numbers did not fall that way would post documents for a signature before the
client had confirmed what they are signing (the shipped defaults did exactly
that once: 20 days back for the reply, 21 for the posting) and would show the
panel a client meeting the posting first.
It is deliberately **not** a promise about the generator: ``reply_deadline``'s
refusal depends on the day it runs and never on the offset, so a within-bound
configuration is still refused by a late trigger — the validator is the panel's
coherence rule, and the runtime judgement is the generator's.

Pure and framework-free per ADR-0006: the standard library and
``app.domain.compliance``'s own arithmetic, nothing else.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from app.domain.compliance import NAR1_FILING_WINDOW_DAYS, anniversary_in_year

# The anchor vocabulary. One value today: the company's anniversary (made-up)
# date, stored on the company and already the basis of the NAR1 due date.
ANCHOR_ANNIVERSARY = "anniversary"

# The bounds every configured day count is held to. An offset of a year or more
# is not a milestone of an annual period. The reply window is bounded so that
# the reply offset below always has a reachable value: the chain's own floor is
# two days — the client's reply offset must exceed the signature posting's,
# which is itself at least one — so a window of `NAR1_FILING_WINDOW_DAYS` or
# more would leave `NAR1_FILING_WINDOW_DAYS - window` below that floor and hand
# the tenant a refusal about an offset bound no coherent block can reach instead
# of about the window that caused it.
MAX_DAYS_BEFORE = 365
MIN_REPLY_WINDOW_DAYS = 1
MAX_REPLY_WINDOW_DAYS = NAR1_FILING_WINDOW_DAYS - 2
MAX_REMINDER_CADENCE_ENTRIES = 6

# The shipped defaults (founder-ruled 2026-09-24). They are the TCSP's numbers
# to change — the panel exists for exactly that — but these are what a tenant
# without a stored block reads and what the migration backfilled. They fall in
# the chain the rule enforces (60 > 30 > 20 > 14): the signature posting sits
# inside the client's reply-by date, so a tenant that never opens the panel
# still posts documents only after the client was asked to reply.
DEFAULT_CONTINUATION_REQUEST_DAYS_BEFORE = 60
DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE = 30
DEFAULT_CLIENT_REPLY_DAYS_BEFORE = 20
DEFAULT_SIGNATURE_POST_DAYS_BEFORE = 14
DEFAULT_MINIMUM_REPLY_WINDOW_DAYS = 7
DEFAULT_REMINDER_CADENCE_DAYS: tuple[int, ...] = (3, 7, 10)

_PREFIX = "milestones."
# The chain, most days before the anniversary first. Each field must be greater
# than the next, because a larger offset is an earlier date and each milestone's
# day is when the thing before it should have been done by (see the module
# docstring). One tuple, so the chain is stated once and every adjacent link is
# checked rather than the pairs someone remembered.
_ORDERING = (
    "continuation_request_days_before",
    "data_confirmation_days_before",
    "client_reply_days_before",
    "signature_post_days_before",
)


class MilestoneValidationError(ValueError):
    """One or more milestone-block rules failed.

    Carries every violation found (not just the first) so a PUT response can
    report them all; each message names the block and the field it is about
    (``milestones.<field>: …``) so the same list reads correctly whichever
    blocks of the request were at fault.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Milestones:
    """The tenant's milestone block (data-model §2.9 ``TenantConfig``).

    A value, built only through ``validate_milestones`` (the write path and
    every read of a stored block) or ``default_milestones`` — the fields are
    the validated ones, and ``as_dict`` is the stored and returned shape.
    """

    continuation_request_days_before: int
    data_confirmation_days_before: int
    client_reply_days_before: int
    signature_post_days_before: int
    minimum_reply_window_days: int
    reminder_cadence_days: tuple[int, ...]
    anchor: str

    def as_dict(self) -> dict[str, Any]:
        """The block as the JSON column stores it and the API returns it.

        A fixed key set in the declaration's order; the cadence is a list
        because that is what JSON carries, and ``validate_milestones`` accepts
        its own output unchanged (a test pins the round trip).
        """
        return {
            "continuation_request_days_before": self.continuation_request_days_before,
            "data_confirmation_days_before": self.data_confirmation_days_before,
            "client_reply_days_before": self.client_reply_days_before,
            "signature_post_days_before": self.signature_post_days_before,
            "minimum_reply_window_days": self.minimum_reply_window_days,
            "reminder_cadence_days": list(self.reminder_cadence_days),
            "anchor": self.anchor,
        }


class MilestoneKind(StrEnum):
    """The four client-facing milestones the block configures (§5.3).

    One value per offset field, named for what the client experiences — the
    continuation ask, the data confirmation, the reply the client gives, and
    the posting of the documents for signature. The *dates* they fall on are
    ``MilestoneDates``; this vocabulary is what the panel's read model keys its
    per-milestone rows on.
    """

    CONTINUATION_REQUEST = "continuation_request"
    DATA_CONFIRMATION = "data_confirmation"
    CLIENT_REPLY = "client_reply"
    SIGNATURE_POST = "signature_post"


@dataclass(frozen=True)
class MilestoneDates:
    """One anniversary period's milestones, as dates.

    Derived, never stored: the anniversary the offsets were counted back from,
    and the four dates. ``client_reply`` is the **nominal** date — the
    anniversary minus its offset — which is what the panel shows and marks
    ``overdue`` once it has passed; whether asking is still worth it is
    ``app.domain.confirmation.reply_deadline``'s judgement, made against the
    statutory deadline and the configured minimum window when a request is
    actually generated.
    """

    anniversary: date
    continuation_request: date
    data_confirmation: date
    client_reply: date
    signature_post: date

    def ordered(self) -> tuple[tuple[MilestoneKind, date], ...]:
        """The four dates with their kinds, in the order the client meets them.

        One place owns which date belongs to which milestone, so a reader of
        the panel read (``app.application.service_renewals.pending_actions``)
        cannot pair a kind with the wrong date.
        """
        return (
            (MilestoneKind.CONTINUATION_REQUEST, self.continuation_request),
            (MilestoneKind.DATA_CONFIRMATION, self.data_confirmation),
            (MilestoneKind.CLIENT_REPLY, self.client_reply),
            (MilestoneKind.SIGNATURE_POST, self.signature_post),
        )


def default_milestones() -> Milestones:
    """The shipped defaults (founder-ruled 2026-09-24), already validated."""
    return Milestones(
        continuation_request_days_before=DEFAULT_CONTINUATION_REQUEST_DAYS_BEFORE,
        data_confirmation_days_before=DEFAULT_DATA_CONFIRMATION_DAYS_BEFORE,
        client_reply_days_before=DEFAULT_CLIENT_REPLY_DAYS_BEFORE,
        signature_post_days_before=DEFAULT_SIGNATURE_POST_DAYS_BEFORE,
        minimum_reply_window_days=DEFAULT_MINIMUM_REPLY_WINDOW_DAYS,
        reminder_cadence_days=DEFAULT_REMINDER_CADENCE_DAYS,
        anchor=ANCHOR_ANNIVERSARY,
    )


def _integer(
    value: object, field: str, *, minimum: int, maximum: int, errors: list[str]
) -> int | None:
    """A day count read from a mapping, or a violation recorded in ``errors``.

    ``bool`` is refused rather than accepted as ``0``/``1``: a JSON ``true`` in
    a day-count field is a client that meant something else, and the vocabulary
    here is deliberately narrow.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{_PREFIX}{field}: must be an integer (got {type(value).__name__})")
        return None
    if not minimum <= value <= maximum:
        errors.append(f"{_PREFIX}{field}: must be between {minimum} and {maximum}")
        return None
    return value


def _cadence(value: object, *, errors: list[str]) -> tuple[int, ...] | None:
    """The reminder cadence read from a mapping, or violations recorded.

    Strictly increasing: two reminders on one day is one reminder, and the
    panel would show two. An empty cadence is valid — a tenant that wants no
    reminders is coherent, and the sending slice is what would honour it.
    """
    if value is None or isinstance(value, str) or not isinstance(value, Sequence):
        errors.append(f"{_PREFIX}reminder_cadence_days: must be a list of integers")
        return None
    if len(value) > MAX_REMINDER_CADENCE_ENTRIES:
        errors.append(
            f"{_PREFIX}reminder_cadence_days: must have at most "
            f"{MAX_REMINDER_CADENCE_ENTRIES} entries"
        )
        return None
    entries = [
        _integer(item, "reminder_cadence_days", minimum=1, maximum=MAX_DAYS_BEFORE, errors=errors)
        for item in value
    ]
    if any(entry is None for entry in entries):
        return None
    days = [entry for entry in entries if entry is not None]
    if any(later <= earlier for earlier, later in pairwise(days)):
        errors.append(f"{_PREFIX}reminder_cadence_days: must be strictly increasing")
        return None
    return tuple(days)


def validate_milestones(values: Mapping[str, Any]) -> Milestones:
    """The validated milestone block, or ``MilestoneValidationError`` listing
    every violation.

    ``values`` is either a PUT body's block or a stored JSON column: one
    validation for both, so a stored row and a request are held to the same
    rules. Unknown keys are ignored (the closed vocabulary is what is read, and
    a tenant cannot widen it by storing something else); a missing or mistyped
    one is a violation. Cross-field rules run only when their operands are
    sound — an out-of-bounds value would otherwise draw a second, misleading
    violation about a comparison it was never part of.
    """
    errors: list[str] = []
    continuation = _integer(
        values.get("continuation_request_days_before"),
        "continuation_request_days_before",
        minimum=1,
        maximum=MAX_DAYS_BEFORE,
        errors=errors,
    )
    data_confirmation = _integer(
        values.get("data_confirmation_days_before"),
        "data_confirmation_days_before",
        minimum=1,
        maximum=MAX_DAYS_BEFORE,
        errors=errors,
    )
    client_reply = _integer(
        values.get("client_reply_days_before"),
        "client_reply_days_before",
        minimum=1,
        maximum=MAX_DAYS_BEFORE,
        errors=errors,
    )
    signature_post = _integer(
        values.get("signature_post_days_before"),
        "signature_post_days_before",
        minimum=1,
        maximum=MAX_DAYS_BEFORE,
        errors=errors,
    )
    window = _integer(
        values.get("minimum_reply_window_days"),
        "minimum_reply_window_days",
        minimum=MIN_REPLY_WINDOW_DAYS,
        maximum=MAX_REPLY_WINDOW_DAYS,
        errors=errors,
    )
    cadence = _cadence(values.get("reminder_cadence_days"), errors=errors)
    anchor = values.get("anchor")
    if anchor != ANCHOR_ANNIVERSARY:
        errors.append(f"{_PREFIX}anchor: must be {ANCHOR_ANNIVERSARY!r}")

    if (
        continuation is not None
        and data_confirmation is not None
        and client_reply is not None
        and signature_post is not None
    ):
        # Every adjacent link of the chain the work follows, checked from one
        # tuple: a larger offset is an *earlier* date, and each milestone's day
        # is when the thing before it should have been done by — the
        # continuation ask by the data-confirmation date, the confirmed data by
        # the client's reply-by date, and the client's reply by the date the
        # documents go out for signature. The messages name the earlier field,
        # the one that has to move.
        days = {
            "continuation_request_days_before": continuation,
            "data_confirmation_days_before": data_confirmation,
            "client_reply_days_before": client_reply,
            "signature_post_days_before": signature_post,
        }
        for earlier, later in pairwise(_ORDERING):
            if days[earlier] <= days[later]:
                errors.append(f"{_PREFIX}{earlier}: must be greater than {later}")
    if client_reply is not None and window is not None:
        # The ruled bound, and what it is *not*. The client's date is counted
        # back from the anniversary and CR's deadline is 42 days after it, so
        # the reply offset and the minimum window are two numbers measured
        # against the one statutory window this product works inside: the offset
        # says how far before the anniversary the client's own date sits, the
        # window says how short an answer's runway may ever be, and the bound
        # keeps the two together within CR's `NAR1_FILING_WINDOW_DAYS` days.
        #
        # It is deliberately not a prediction of the generator. That refusal
        # depends on the day it runs (`as_of + window` reaching the deadline)
        # and never on the offset: a within-bound configuration is still refused
        # by a late trigger, and an offset past this bound would generate fine
        # on an early enough day. The runtime judgement belongs to
        # `app.domain.confirmation.reply_deadline`; this is the panel's
        # coherence rule. `MAX_REPLY_WINDOW_DAYS` keeps `bound` at or above the
        # chain's own two-day floor, so the message a tenant reads always names
        # an offset a coherent block can actually hold.
        bound = NAR1_FILING_WINDOW_DAYS - window
        if client_reply > bound:
            errors.append(
                f"{_PREFIX}client_reply_days_before: must not exceed {bound} "
                "(NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days)"
            )

    if errors:
        raise MilestoneValidationError(errors)
    assert continuation is not None and data_confirmation is not None
    assert client_reply is not None and signature_post is not None
    assert window is not None and cadence is not None
    return Milestones(
        continuation_request_days_before=continuation,
        data_confirmation_days_before=data_confirmation,
        client_reply_days_before=client_reply,
        signature_post_days_before=signature_post,
        minimum_reply_window_days=window,
        reminder_cadence_days=cadence,
        anchor=ANCHOR_ANNIVERSARY,
    )


def milestone_dates(anniversary_date: date, year: int, *, milestones: Milestones) -> MilestoneDates:
    """The anniversary falling in ``year`` and its four configured milestones.

    The anniversary's month and day are the company's made-up date, clamped the
    way the statutory rule clamps it (Feb 29 → Feb 28 in a non-leap year,
    contract §5.1.1) — one clamp, one convention. Only the anniversary is
    needed, so an anchor stored for another year still yields this year's dates.
    """
    anniversary = anniversary_in_year(anniversary_date, year)
    return MilestoneDates(
        anniversary=anniversary,
        continuation_request=anniversary
        - timedelta(days=milestones.continuation_request_days_before),
        data_confirmation=anniversary - timedelta(days=milestones.data_confirmation_days_before),
        client_reply=anniversary - timedelta(days=milestones.client_reply_days_before),
        signature_post=anniversary - timedelta(days=milestones.signature_post_days_before),
    )
