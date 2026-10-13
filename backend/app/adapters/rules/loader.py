"""Fee-schedule loader: ``backend/rules/`` dated policy data (ADR-0010).

The rules directory is the single source of truth for regulatory policy
expressed as dated data — the NAR1 fee schedule today, statutory windows
and the rest of ADR-0010's rule packs as that sequence continues. This
module reads it; nothing here evaluates a fee (that is
``app.domain.compliance.late_fee_exposure``).

Data-only, no writes: the files are human-curated (a human-in-the-loop
zone per AGENTS.md — these figures feed filing-affecting computations), so
the loader never repairs, defaults, or reorders what it reads — a malformed
file is a hard error, not something to work around.

**The licensed-pack seam (ADR-0012).** A cached ``fee_schedule`` pack
(§2.2's pack list) carries the same JSON document per form, and
``load_fee_schedule`` prefers one when a caller supplies it — the pack's
entry for that form wins, and a form the pack does not carry falls back to
the image. The seam is a parameter, not a switch: **no caller passes a
payload yet**, because the consumers are the synchronous compliance reads
and the pack cache is a database read, and because the pack signature is
refused until CP2 pack signing lands (§5.1.6) — the in-image data is what
every caller reads until then. The seam is implemented and tested so the
switch can be thrown without rewriting this loader.

One dependency beyond the standard library (``app.domain.compliance``) and
no framework imports: this is an adapter, per ADR-0006's layer rule.
"""

import json
from collections.abc import Mapping
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from app.domain.compliance import FeeBand, FeeSchedule, FeeScheduleEntry
from app.domain.prior_filing import FormCode


class FeeScheduleError(RuntimeError):
    """The schedule is missing or malformed (never silently defaulted)."""


def rules_root() -> Path:
    """The rules root: ``backend/rules`` in the repo, ``/app/rules`` in
    the deployment image (both are the package's grandparent, per
    ``deploy/backend.Dockerfile``)."""
    return Path(__file__).resolve().parents[3] / "rules"


@cache
def image_fee_schedule(form: FormCode) -> FeeSchedule:
    """The in-image schedule, parsed once per process.

    Raises ``FeeScheduleError`` when the file is missing, unparsable, or
    inconsistent (undated entries, non-contiguous bands, negative fees) — a
    schedule that cannot be trusted is never half-read.
    """
    path = rules_root() / f"{form.value}_fee_schedule.json"
    return _schedule_from(form, _load_json(path))


def load_fee_schedule(
    form: FormCode, *, pack_payload: Mapping[str, Any] | None = None
) -> FeeSchedule:
    """The form's fee schedule: a supplied ``fee_schedule`` pack, else the image.

    The one consumption point for licensed fee-schedule data (ADR-0012). A
    caller holding a cached pack payload passes it and gets the pack's entry
    for this form when it carries one; a form the pack does not carry, or no
    payload at all, reads the in-image schedule — so the fallback is the same
    path that runs today, unchanged. See the module note for why no caller
    passes a payload yet.
    """
    if pack_payload is not None:
        packed = schedule_from_pack(form, pack_payload)
        if packed is not None:
            return packed
    return image_fee_schedule(form)


def schedule_from_pack(
    form: FormCode, payload: Mapping[str, Any]
) -> FeeSchedule | None:
    """One ``fee_schedule`` pack's entry for this form; ``None`` if it has none.

    A pack carries the same JSON document the image holds per form, so the
    entry it produces is validated exactly like an in-image one — a pack
    that is malformed is a hard error rather than a partial read.
    """
    schedules = payload.get("fee_schedules")
    if not isinstance(schedules, Mapping):
        raise FeeScheduleError("fee_schedule pack: no 'fee_schedules' object")
    entry = schedules.get(form.value)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise FeeScheduleError(f"{form.value}: pack entry is not an object")
    return _schedule_from(form, entry)


def _schedule_from(form: FormCode, document: Mapping[str, Any]) -> FeeSchedule:
    """One form's schedule document, checked: dated, contiguous, ascending."""
    try:
        form_code = str(document["form"])
        company_type = str(document["company_type"])
        currency = str(document["currency"])
        raw_entries = document["schedules"]
    except KeyError as exc:
        raise FeeScheduleError(f"{form.value}: schedule document lacks {exc}") from None
    if not isinstance(raw_entries, list) or not raw_entries:
        raise FeeScheduleError(f"{form.value}: schedule document has no dated entries")
    entries = tuple(_entry_from(form, raw, position) for position, raw in enumerate(raw_entries))
    _check_dating(form, entries)
    return FeeSchedule(
        form=form_code, company_type=company_type, currency=currency, entries=entries
    )


def _entry_from(form: FormCode, raw: object, position: int) -> FeeScheduleEntry:
    if not isinstance(raw, Mapping):
        raise FeeScheduleError(f"{form.value}: schedule entry {position} is not an object")
    label = f"{form.value}: schedule entry {position}"
    bands_raw = raw.get("late_bands")
    if not isinstance(bands_raw, list) or not bands_raw:
        raise FeeScheduleError(f"{label} has no late bands")
    bands = tuple(_band_from(form, band, position, i) for i, band in enumerate(bands_raw))
    _check_bands(form, bands, position)
    return FeeScheduleEntry(
        effective_from=_required_iso_date(raw.get("effective_from"), label, "effective_from"),
        effective_to=_iso_date(raw.get("effective_to"), label, "effective_to"),
        on_time_fee=_positive_int(raw.get("on_time_fee"), label, "on_time_fee"),
        late_bands=bands,
        source=str(raw.get("source", "")),
    )


def _band_from(form: FormCode, raw: object, position: int, index: int) -> FeeBand:
    if not isinstance(raw, Mapping):
        raise FeeScheduleError(f"{form.value}: band {position}/{index} is not an object")
    label = f"{form.value}: band {position}/{index}"
    over_days = raw.get("over_days")
    over_months = raw.get("over_months")
    if (over_days is None) == (over_months is None):
        raise FeeScheduleError(f"{label}: set exactly one of over_days / over_months")
    within_months = raw.get("within_months")
    return FeeBand(
        over_days=_positive_int(over_days, label, "over_days") if over_days is not None else None,
        over_months=(
            _positive_int(over_months, label, "over_months") if over_months is not None else None
        ),
        within_months=(
            None if within_months is None else _positive_int(within_months, label, "within_months")
        ),
        fee=_positive_int(raw.get("fee"), label, "fee"),
    )


def _check_bands(form: FormCode, bands: tuple[FeeBand, ...], position: int) -> None:
    """Late bands must be contiguous, ascending in time and fee, days-first.

    Evaluation walks only the upper edges, so a gap or an overlap between
    consecutive bands would make some delivery dates unpriceable or
    doubly-priced — the loader refuses rather than guess which edge wins.
    """
    label = f"{form.value}: schedule entry {position} late bands"
    if bands[0].over_days is None:
        raise FeeScheduleError(f"{label}: the first band's lower edge is over_days")
    previous_within: int | None = None
    for index, band in enumerate(bands):
        if index > 0 and band.over_months != previous_within:
            raise FeeScheduleError(
                f"{label}: band {index} does not start where band {index - 1} ends"
            )
        if (
            previous_within is not None
            and band.within_months is not None
            and band.within_months <= previous_within
        ):
            raise FeeScheduleError(f"{label}: within_months not ascending")
        if index > 0 and band.fee <= bands[index - 1].fee:
            raise FeeScheduleError(f"{label}: fees not ascending")
        previous_within = band.within_months


def _check_dating(form: FormCode, entries: tuple[FeeScheduleEntry, ...]) -> None:
    """Dated entries must tile the timeline: ascending, adjacent, one open.

    Adjacency matters because evaluation picks the entry covering the
    delivery date and refuses a date no entry covers — a gap between two
    entries would load cleanly here and 500 every compliance read for dates
    inside it, so the gap is a load-time error like any other malformation.
    """
    for index, entry in enumerate(entries):
        if entry.effective_to is not None and entry.effective_to < entry.effective_from:
            raise FeeScheduleError(f"{form.value}: schedule entry {index} ends before it starts")
        if index > 0:
            if entry.effective_from <= entries[index - 1].effective_from:
                raise FeeScheduleError(f"{form.value}: effective_from not ascending")
            closes = entries[index - 1].effective_to
            if closes is None:
                raise FeeScheduleError(
                    f"{form.value}: schedule entry {index - 1} is open but not the last"
                )
            if closes >= entry.effective_from:
                raise FeeScheduleError(f"{form.value}: effective dates overlap")
            if closes + timedelta(days=1) != entry.effective_from:
                raise FeeScheduleError(
                    f"{form.value}: schedule entries must be contiguous "
                    f"(gap before entry {index})"
                )


def _required_iso_date(value: object, label: str, field: str) -> date:
    parsed = _iso_date(value, label, field)
    if parsed is None:
        raise FeeScheduleError(f"{label}: {field} must be an ISO date string")
    return parsed


def _iso_date(value: object, label: str, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FeeScheduleError(f"{label}: {field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise FeeScheduleError(f"{label}: {field} is not an ISO date: {value!r}") from None


def _positive_int(value: object, label: str, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FeeScheduleError(f"{label}: {field} must be a positive integer")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FeeScheduleError(f"schedule file missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise FeeScheduleError(f"schedule file is not valid JSON: {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise FeeScheduleError(f"schedule file is not a JSON object: {path}")
    return payload
