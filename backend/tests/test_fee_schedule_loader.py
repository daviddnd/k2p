"""The NAR1 fee schedule: dated policy data and its licensed-pack seam
(ADR-0010; contract §5.1.4's slice-L note).

``load_fee_schedule`` is the one place a fee schedule is read, so it is the
one place a licensed ``fee_schedule`` pack (ADR-0012, §2.2's pack list) can
come to matter. This suite pins:

- the in-image schedule parses and carries the figures verified against
  CR PAM 3C (12/2023) section (六) — on-time HK$105; late bands HK$870 /
  1,740 / 2,610 / 3,480 at >42 days-3 months / >3-6 / >6-9 / >9 from the
  anniversary — with the effective-dating ADR-0010 requires;
- a pack payload carrying the image's own document reproduces it exactly,
  so a pack carrying today's content changes nothing;
- a pack entry the caller supplies wins over the image; a pack that does
  not carry the form, and no payload at all, fall back to the image — the
  path every caller takes today;
- a malformed pack entry or image file is a hard error, the form
  registry's rule: a schedule that cannot be trusted is never half-read.

**No caller passes a payload yet** — the seam exists so the control plane's
``fee_schedule`` pack can replace the in-image data without rewriting this
loader once CP2 pack signing lands (§5.1.6's posture, one slice later in
the ADR-0010 sequence).
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.adapters.rules.loader import (
    FeeScheduleError,
    image_fee_schedule,
    load_fee_schedule,
    rules_root,
    schedule_from_pack,
)
from app.domain.compliance import late_fee_exposure
from app.domain.prior_filing import FormCode

NAR1 = FormCode.NAR1


def nar1_document() -> dict[str, object]:
    """The image's own schedule document, as a pack would carry it."""
    path = Path(rules_root()) / "nar1_fee_schedule.json"
    return json.loads(path.read_text(encoding="utf-8"))


def pack_payload(**overrides: object) -> dict[str, object]:
    schedules: dict[str, object] = {NAR1.value: nar1_document()}
    schedules.update(overrides)  # type: ignore[arg-type]
    return {"fee_schedules": schedules}


# --- The in-image data: figures verified against CR PAM 3C (12/2023) ---


def test_image_schedule_carries_the_verified_pam3c_figures() -> None:
    schedule = image_fee_schedule(NAR1)

    assert schedule.form == "NAR1"
    assert schedule.company_type == "private"
    assert schedule.currency == "HKD"
    assert len(schedule.entries) == 1
    entry = schedule.entries[0]
    assert entry.effective_from.isoformat() == "2023-12-01"
    assert entry.effective_to is None
    assert entry.on_time_fee == 105
    assert [band.fee for band in entry.late_bands] == [870, 1740, 2610, 3480]
    assert [band.within_months for band in entry.late_bands] == [3, 6, 9, None]
    assert entry.late_bands[0].over_days == 42
    assert entry.source != ""


# --- The pack seam ---


def test_a_pack_carrying_the_schedule_reproduces_it() -> None:
    packed = schedule_from_pack(NAR1, pack_payload())

    assert packed is not None
    assert packed == image_fee_schedule(NAR1)


def test_a_pack_entry_is_preferred_over_the_image() -> None:
    """A cached pack wins for the forms it carries — that is the seam's point."""
    document = nar1_document()
    entries = document["schedules"]  # type: ignore[index]
    document = {
        **document,
        "schedules": [
            {**entries[0], "on_time_fee": 140}  # type: ignore[index]
        ],
    }

    chosen = load_fee_schedule(NAR1, pack_payload=pack_payload(**{NAR1.value: document}))

    assert chosen.entries[0].on_time_fee == 140
    # The image is still there and still what an unqualified call reads.
    assert load_fee_schedule(NAR1) == image_fee_schedule(NAR1)


def test_a_pack_that_does_not_carry_the_form_falls_back_to_the_image() -> None:
    payload = {"fee_schedules": {}}

    assert load_fee_schedule(NAR1, pack_payload=payload) == image_fee_schedule(NAR1)
    assert schedule_from_pack(NAR1, payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"nope": {}}, id="no-fee-schedules-object"),
        pytest.param({"fee_schedules": {"nar1": "not an object"}}, id="entry-not-an-object"),
        pytest.param({"fee_schedules": {"nar1": {}}}, id="empty-document"),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        "form": "NAR1",
                        "company_type": "private",
                        "currency": "HKD",
                        "schedules": [],
                    }
                }
            },
            id="no-schedule-entries",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "on_time_fee": -5,
                            }
                        ],
                    }
                }
            },
            id="negative-fee",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "late_bands": [
                                    {"over_days": 42, "over_months": 3,
                                     "within_months": 3, "fee": 870}
                                ],
                            }
                        ],
                    }
                }
            },
            id="band-with-two-lower-edges",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "late_bands": [
                                    {"over_days": 42, "within_months": 3, "fee": 870},
                                    {"over_months": 6, "within_months": 9, "fee": 1740},
                                ],
                            }
                        ],
                    }
                }
            },
            id="non-contiguous-bands",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "effective_to": "2026-05-31",
                            },
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "effective_from": "2026-07-01",
                                "on_time_fee": 140,
                            },
                        ],
                    }
                }
            },
            id="gap-between-dated-entries",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "late_bands": [
                                    {"over_days": 42, "within_months": 0, "fee": 870},
                                ],
                            }
                        ],
                    }
                }
            },
            id="non-positive-within-months",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "late_bands": [
                                    {"over_days": 42, "within_months": True, "fee": 870},
                                ],
                            }
                        ],
                    }
                }
            },
            id="bool-within-months",
        ),
        pytest.param(
            {
                "fee_schedules": {
                    "nar1": {
                        **nar1_document(),
                        "schedules": [
                            {
                                **nar1_document()["schedules"][0],  # type: ignore[index]
                                "late_bands": [
                                    {"over_days": 42, "within_months": 3, "fee": 870},
                                    {"over_months": 3, "within_months": 6, "fee": 700},
                                ],
                            }
                        ],
                    }
                }
            },
            id="non-ascending-fees",
        ),
    ],
)
def test_a_malformed_pack_entry_is_a_hard_error(payload: dict[str, object]) -> None:
    """Never half-read: a pack that cannot be trusted is refused like a bad file."""
    with pytest.raises(FeeScheduleError):
        schedule_from_pack(NAR1, payload)


def test_the_image_path_is_untouched_by_the_seam() -> None:
    """The default call — the one every caller makes — reads the image."""
    cached = load_fee_schedule(NAR1)

    assert cached == image_fee_schedule(NAR1)
    # And the cache still returns one shared object, not a fresh parse.
    assert load_fee_schedule(NAR1) is cached


def test_the_image_schedule_evaluates_to_the_verified_pam3c_bands() -> None:
    """The data file end to end: the figures the loader pins are the figures
    the domain rule prices — on time, each band edge, and the open top band."""
    schedule = image_fee_schedule(NAR1)
    anniversary = date(2026, 3, 1)

    assert late_fee_exposure(anniversary, date(2026, 4, 12), schedule).fee_hkd == 105
    assert late_fee_exposure(anniversary, date(2026, 4, 13), schedule).fee_hkd == 870
    assert late_fee_exposure(anniversary, date(2026, 6, 1), schedule).fee_hkd == 870
    assert late_fee_exposure(anniversary, date(2026, 6, 2), schedule).fee_hkd == 1740
    assert late_fee_exposure(anniversary, date(2026, 9, 2), schedule).fee_hkd == 2610
    assert late_fee_exposure(anniversary, date(2026, 12, 2), schedule).fee_hkd == 3480
    assert late_fee_exposure(anniversary, date(2028, 1, 1), schedule).fee_hkd == 3480
    assert not late_fee_exposure(anniversary, date(2026, 4, 12), schedule).late
    assert late_fee_exposure(anniversary, date(2026, 4, 13), schedule).late
