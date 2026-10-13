"""Checkbox and choice option semantics for the pinned CR templates — one home
for both PDF paths (scope §2.9; ADR-0004 as amended 2026-08-19).

A registry item's checkbox group and its ``Dropdown_*`` choice group are
positional widgets, and which box means what is prose in
``backend/forms/<form>/mapping.json`` ("cb_1_P.1 = 私人公司 Private company; …")
— not machine-readable data. This module is that prose transcribed into code,
in one place, so the two paths that read it cannot drift apart:

- the extraction reader (``app.adapters.pdf.extractor``) reads a ticked box of
  a group as the option value the box represents;
- the fill planner (``app.domain.draft_fill``) takes this table as plain data
  and ticks the box whose option equals the item's value — or strikes every
  line whose option is *not* it.

Two kinds of group are tabulated:

- **tick groups** — the value each box stands for: a vocabulary word
  (``private``, ``schedule_1``, ``resignation_others``) or one half of a yes/no
  pair as ``True``/``False``. Every box of the templates starts Off, so the
  planner only ever sets the boxes a value matches.
- **strike groups** — the "delete whichever does not apply" ``Dropdown_*``
  fields: the wording each line prints. The planner strikes the lines whose
  option is not the item's value, so exactly the applicable wording survives
  (the fill spike's stage-4/7 convention).

A group is keyed by the registry target its item carries. A group whose boxes
repeat across a form's blocks (the capacity group) lists every block's box: a
caller considers only the boxes of the item it is planning. Both forms live
here, and a box name is unique within its own template — ``cb_1_P.1`` is NAR1's
company-type *private* box and ND2A's cessation *company secretary* box — so
the group key is what tells them apart. A test holds every widget named below
to one of the two pinned templates' geometry.

The ND2A capacity groups are keyed by the capacity path
(``change_event.payload.{appointment,cessation}.officer.capacity[]``), the
founder-approved registry retarget of 2026-09-28; the ``Dropdown_*`` strike
items keep the role path their notes describe (they are role-driven
strike-outs).
"""

from collections.abc import Mapping
from typing import Any

# The checkboxes a registry item may carry, and the value each one represents.
# Keyed by the registry target the group's item names.
CHECKBOX_OPTIONS: Mapping[str, Mapping[str, Any]] = {
    # --- NAR1 ---------------------------------------------------------------
    # Item 3: the type-of-company box (mapping.json item 3).
    "company.type": {
        "cb_1_P.1": "private",
        "cb_2_P.1": "public",
        "cb_3_P.1": "company_limited_by_guarantee",
    },
    # The capacity group of items 13A, 13B.1/.2, CS-C and CS-D.1/.2: the boxes
    # carry the capacity vocabulary (候補董事 is the alternate directorship —
    # the office NAR1 prints beside its 代替 Alternate to box). The item's target
    # names role + alternate_to, and the ticks come from the officer's capacity
    # list, which is what the ND2A grammar and the master record carry too.
    "officers[].role + officers[].alternate_to": {
        "cb_1_P.5": "director",
        "cb_2_P.5": "alternate_director",
        "cb_1_P.6": "director",
        "cb_2_P.6": "alternate_director",
        "cb_3_P.6": "director",
        "cb_4_P.6": "alternate_director",
        "cb_1_P.13": "director",
        "cb_2_P.13": "alternate_director",
        "cb_1_P.14": "director",
        "cb_2_P.14": "alternate_director",
        "cb_3_P.14": "director",
        "cb_4_P.14": "alternate_director",
    },
    # Item 14: how the member particulars are delivered (§3.3's three values).
    "draft.filing_metadata.member_particulars_delivery": {
        "cb_1_P.8": "schedule_1",
        "cb_2_P.8": "schedule_2",
        "cb_3_P.8": "cd_rom",
    },
    # Item 16: the private-company statement — one box, set when the draft
    # makes the statement.
    "return.private_company_statement": {"cb_4_P.8": True},
    # --- The signature block's delete-whichever pair (both forms print it) ---
    "signature.capacity": {
        "Dropdown_1_P.8": "director",
        "Dropdown_2_P.8": "company_secretary",
        "Dropdown_6_P.3": "director",
        "Dropdown_7_P.3": "company_secretary",
    },
    # --- ND2A ---------------------------------------------------------------
    # The cessation block's capacity group (items 2A and A-2A).
    "change_event.payload.cessation.officer.capacity[]": {
        "cb_1_P.1": "company_secretary",
        "cb_2_P.1": "director",
        "cb_3_P.1": "alternate_director",
        "cb_1_P.4": "company_secretary",
        "cb_2_P.4": "director",
        "cb_3_P.4": "alternate_director",
    },
    # The appointment block's capacity group (items 3, 4, B-3, C-4 and the
    # PI-ND2A sheet — the PI sheet prints the same three boxes).
    "change_event.payload.appointment.officer.capacity[]": {
        "cb_1_P.2": "company_secretary",
        "cb_2_P.2": "director",
        "cb_3_P.2": "alternate_director",
        "cb_1_P.3": "company_secretary",
        "cb_2_P.3": "director",
        "cb_3_P.3": "alternate_director",
        "cb_1_P.5": "company_secretary",
        "cb_2_P.5": "director",
        "cb_3_P.5": "alternate_director",
        "cb_1_P.6": "company_secretary",
        "cb_2_P.6": "director",
        "cb_3_P.6": "alternate_director",
        "cb_1_P.7": "company_secretary",
        "cb_2_P.7": "director",
        "cb_3_P.7": "alternate_director",
    },
    # Item 2B/A-2B: the reason for cessation (the note's cb_4=辭職/其他,
    # cb_5=去世).
    "change_event.payload.cessation.officer.cessation_reason": {
        "cb_4_P.1": "resignation_others",
        "cb_5_P.1": "deceased",
        "cb_4_P.4": "resignation_others",
        "cb_5_P.4": "deceased",
    },
    # Item 2B/A-2B: "still in office after the cessation?" (yes/no).
    "change_event.payload.cessation.officer.continues_office_after_cessation": {
        "cb_6_P.1": True,
        "cb_7_P.1": False,
        "cb_6_P.4": True,
        "cb_7_P.4": False,
    },
    # Item 3/4/B-3/C-4: "already an existing director at the appointment?"
    # (yes/no).
    "change_event.payload.appointment.officer.existing_director_at_appointment": {
        "cb_5_P.2": True,
        "cb_6_P.2": False,
        "cb_5_P.3": True,
        "cb_6_P.3": False,
        "cb_5_P.5": True,
        "cb_6_P.5": False,
        "cb_5_P.6": True,
        "cb_6_P.6": False,
    },
    # The appointment blocks' consent sentence: 董事/director and
    # 候補董事/alternate director, struck to leave the capacity that applies.
    "change_event.payload.appointment.officer.role": {
        "Dropdown_1_P.2": "director",
        "Dropdown_2_P.2": "alternate_director",
        "Dropdown_1_P.3": "director",
        "Dropdown_2_P.3": "alternate_director",
        "Dropdown_1_P.5": "director",
        "Dropdown_2_P.5": "alternate_director",
        "Dropdown_1_P.6": "director",
        "Dropdown_2_P.6": "alternate_director",
    },
    # CONSENT.capacity: the body-corporate consent's signatory capacity — the
    # note's 董事/Director, 公司秘書/Company Secretary and 獲授權人士/
    # Authorized Person (the two that do not apply are struck).
    "consent.signatory_capacity": {
        "Dropdown_3_P.3": "director",
        "Dropdown_4_P.3": "company_secretary",
        "Dropdown_5_P.3": "authorized_person",
        "Dropdown_3_P.6": "director",
        "Dropdown_4_P.6": "company_secretary",
        "Dropdown_5_P.6": "authorized_person",
    },
}


def vocabulary_options(target: str) -> Mapping[str, str]:
    """``target``'s boxes whose option is a vocabulary word.

    The extraction reader's view: an ``ExtractedField`` proposes a string, so a
    box standing for a yes/no half (``True``/``False``) has no value it could
    propose. An unknown target yields an empty mapping — the reader asks this
    of every target it meets, and then reads a group only when something came
    back.
    """
    return {
        box: value
        for box, value in CHECKBOX_OPTIONS.get(target, {}).items()
        if isinstance(value, str)
    }


__all__ = ["CHECKBOX_OPTIONS", "vocabulary_options"]
