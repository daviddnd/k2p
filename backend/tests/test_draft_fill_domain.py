"""Fill-plan tests: one draft's resolved state → the pinned registry's fields
(scope §2.9; contract §2.8; ADR-0004 as amended).

Database-free and PDF-free. The input snapshots are hand-built in
``app.domain.drafts``' own shape (``company``, ``officers``, ``shareholders``,
``changes``) and the expectations are the fill spike's fixtures' values — where
a fixture shows a value, that value is the rule
(``research/counter-parties/cr/field-mapping/fill-spike/``).

What these tests pin: every registry item of both pinned forms gets an outcome
and every written widget belongs to a registry item; the option table is the
registry's prose and the extraction reader reads the same table; the
compositions the spike fixed (the bilingual name, the date triplets, "Nil", the
licence rendering, the grid's thousands separators, the strike-out decisions);
and that overflow and the §1 protected class are escalated by name rather than
guessed at.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.adapters.forms.registry import (
    forms_root,
    load_form_registry,
    page_header_fields,
    sheet_page_fields,
)
from app.adapters.pdf.extractor import extract
from app.adapters.pdf.options import CHECKBOX_OPTIONS, vocabulary_options
from app.application.drafts import fill_plan_for
from app.domain.change_events import PROTECTED_CLASS_FIELDS
from app.domain.draft_fill import (
    BLANK,
    ESCALATED,
    ESCALATION_KINDS,
    FILLED,
    NOT_APPLICABLE,
    OUTCOMES,
    STRIKE,
    FillPlan,
    FillPlanError,
    RegistryItem,
    SheetPage,
    SheetPlan,
    plan_fill,
)
from app.domain.prior_filing import FormCode
from tests.pdf_fixtures import acroform_pdf

# --- The pinned registry ----------------------------------------------------


def items_for(form: FormCode) -> tuple[RegistryItem, ...]:
    return tuple(
        RegistryItem(
            item=entry.item,
            item_type=entry.item_type,
            target=entry.target,
            pdf_fields=entry.pdf_fields,
        )
        for entry in load_form_registry(form).items
    )


def sheet_pages_for(form: FormCode) -> tuple[SheetPage, ...]:
    """The pinned template's sheet pages, as the application seam hands them over."""
    return tuple(
        SheetPage(sheet=sheet, fields=fields)
        for sheet, fields in sheet_page_fields(load_form_registry(form)).items()
    )


def sheets_of(plan: FillPlan, sheet: str) -> tuple[SheetPlan, ...]:
    """The plan's copies of one sheet, in package order."""
    return tuple(copy for copy in plan.sheets if copy.sheet == sheet)


def sheet_writes(plan: FillPlan, sheet: str, copy: int = 1) -> Mapping[str, object]:
    """One sheet copy's complete value set (the first copy unless told otherwise)."""
    return sheets_of(plan, sheet)[copy - 1].fields


def printed_writes(plan: FillPlan) -> dict[str, object]:
    """Every value the plan prints in place: its fields plus each sheet's first copy.

    The writer fills the main form from ``plan.fields`` and each used sheet's
    first copy from its sheet plan, so this is the value set one assembled
    package shows on the template's own pages.
    """
    writes = dict(plan.fields)
    seen: set[str] = set()
    for copy in plan.sheets:
        if copy.sheet in seen:
            continue
        seen.add(copy.sheet)
        writes.update(copy.fields)
    return writes


def statuses_of(plan: FillPlan, item: str) -> set[str]:
    """The statuses of the entries with this item id (ND2A's ids repeat)."""
    return {outcome.status for outcome in plan.outcomes if outcome.item == item}


# --- The NAR1 snapshot ------------------------------------------------------

BILINGUAL_NAME = "合成海港貿易有限公司 SYNTHETIC HARBOUR TRADING LIMITED"
MADE_UP_DATE = "2026-06-30"

COMPANY: dict[str, Any] = {
    "br_number": "76543210",
    "cr_number": "1234567",
    "type": "private",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "status": "active",
    "name": {
        "name_en": "SYNTHETIC HARBOUR TRADING LIMITED",
        "name_zh": "合成海港貿易有限公司",
        "business_name": "SYNTHETIC HARBOUR TRADING",
    },
    "registered_office": {
        "flat": "ROOM 1201, 12/F",
        "building": "HARBOUR CENTRE",
        "street": "25 HARBOUR ROAD",
        "district": "WAN CHAI",
    },
    "business_nature": {
        "business_nature_code": "045",
        "business_nature_description": "IMPORT AND EXPORT TRADE",
    },
    "contact": {
        "email": "compliance@synthetic-harbour.example.com.hk",
        "contact_phone_hk": "2345 6789",
    },
    "filing": {"mortgages_and_charges_total": "0.00", "member_count": None},
    "records_kept": [],
    "share_capital": {
        "classes": [
            {
                "class_name": "ORDINARY",
                "currency": "HKD",
                "total_shares": 10000,
                "total_amount": "10000.00",
                "total_paid_up": "10000.00",
            }
        ],
        "total": {
            "derived": True,
            "currency": "HKD",
            "total_shares": 10000,
            "total_amount": "10000.00",
            "total_paid_up": "10000.00",
        },
    },
}

SECRETARY = {
    "person_id": "sec-1",
    "person_type": "body_corporate",
    "name_zh": "合成秘書服務有限公司",
    "name_en": "SYNTHETIC SECRETARIAL SERVICES LIMITED",
    "email": "cs@synthetic-secretarial.example.com.hk",
    "br_number": "87654321",
    "tcsp_licence": {"licence_no": "TC009999"},
    "previous_names": [],
    "aliases": [],
    "registered_office_address": {
        "flat": "SUITE 800, 8/F",
        "building": "SECRETARIAL TOWER",
        "street": "88 QUEENSWAY",
        "district": "ADMIRALTY",
    },
    "role": "company_secretary",
    "capacity": ["company_secretary"],
    "alternate_to": None,
    "valid_from": "2020-01-01",
    "valid_to": None,
    "source": {"type": "master_record_id", "id": "appt-1"},
}

DIRECTOR = {
    "person_id": "dir-1",
    "person_type": "natural_person",
    "name_zh": "陳大文",
    "name_en": "CHAN Tai Man",
    "surname_en": "CHAN",
    "other_names_en": "TAI MAN",
    "email": "tm.chan@example.com",
    "hkid_partial": "A123",
    "passport_partial": None,
    "passport_issuing_country_region": None,
    "previous_names": [],
    "aliases": [],
    "tcsp_licence": None,
    "correspondence_address": {
        "flat": "FLAT A, 20/F",
        "building": "GARDEN VIEW COURT",
        "street": "1 GARDEN ROAD",
        "district": "CENTRAL",
        "country": "HONG KONG",
    },
    "role": "director",
    "capacity": ["director"],
    "alternate_to": None,
    "valid_from": "2020-01-01",
    "valid_to": None,
    "source": {"type": "master_record_id", "id": "appt-2"},
}

MEMBER_ADDRESS = {
    "flat": "FLAT A, 20/F",
    "building": "GARDEN VIEW COURT",
    "street": "1 GARDEN ROAD",
    "district": "CENTRAL",
    "country": "HONG KONG",
}

MEMBERS = [
    {
        "person_id": "dir-1",
        "person_type": "natural_person",
        "name_zh": "陳大文",
        "name_en": "CHAN Tai Man",
        "surname_en": "CHAN",
        "other_names_en": "TAI MAN",
        "class_name": "ORDINARY",
        "shares_held": 5000,
        "jointly_held": None,
        "remarks": None,
        "percentage_held": None,
        "address": MEMBER_ADDRESS,
        "valid_from": "2020-01-01",
        "valid_to": None,
        "source": {"type": "master_record_id", "id": "hold-1"},
    },
    {
        "person_id": "mem-2",
        "person_type": "body_corporate",
        "name_zh": "海港集團有限公司",
        "name_en": "HARBOUR HOLDINGS LIMITED",
        "class_name": "ORDINARY",
        "shares_held": 5000,
        "jointly_held": None,
        "remarks": None,
        "percentage_held": None,
        "address": {
            "flat": "UNIT 3, 15/F",
            "building": "PACIFIC PLAZA",
            "street": "410 DES VOEUX ROAD WEST",
            "district": "SAI WAN",
            "country": "HONG KONG",
        },
        "valid_from": "2020-01-01",
        "valid_to": None,
        "source": {"type": "master_record_id", "id": "hold-2"},
    },
]

PRESENTOR = {
    "name": "SYNTHETIC SECRETARIAL SERVICES LIMITED",
    "address": "SUITE 800, 8/F, SECRETARIAL TOWER, 88 QUEENSWAY, ADMIRALTY, HK",
    "tel": "2987 6543",
    "fax": "2987 6500",
    "email": "filings@synthetic-secretarial.example.com.hk",
    "reference": "SH-2026-AR-001",
}

METADATA: dict[str, Any] = {
    "return": {"made_up_date": MADE_UP_DATE, "fs_period_start": None, "fs_period_end": None},
    "member_particulars_delivery": "schedule_1",
    "presentor": PRESENTOR,
    "signature": {"name": "CHAN TAI MAN", "date": "2026-07-30", "capacity": "director"},
    "consent": None,
}


def nar1_state(
    *, officers: Sequence[Mapping[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "company": COMPANY,
        "officers": list(officers if officers is not None else [SECRETARY, DIRECTOR]),
        "shareholders": list(MEMBERS),
        "changes": [],
    }
    state.update(overrides)
    return state


def nar1_plan(
    *,
    state: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    statement: bool | None = True,
) -> FillPlan:
    return plan_fill(
        form="nar1",
        resolved_state=nar1_state() if state is None else state,
        filing_metadata=METADATA if metadata is None else metadata,
        private_company_statement=statement,
        items=items_for(FormCode.NAR1),
        options=CHECKBOX_OPTIONS,
        sheet_pages=sheet_pages_for(FormCode.NAR1),
    )


# --- Coverage accounting ----------------------------------------------------


@pytest.mark.parametrize("form", [FormCode.NAR1, FormCode.ND2A])
def test_every_registry_item_gets_an_outcome(form: FormCode) -> None:
    """One outcome per registry *entry*, in order — ND2A repeats item ids."""
    plan = nar1_plan() if form is FormCode.NAR1 else nd2a_plan()
    items = items_for(form)
    assert [outcome.item for outcome in plan.outcomes] == [entry.item for entry in items]
    assert all(outcome.status in OUTCOMES for outcome in plan.outcomes)


@pytest.mark.parametrize("form", [FormCode.NAR1, FormCode.ND2A])
def test_every_written_field_belongs_to_a_registry_item(form: FormCode) -> None:
    """Nothing is written outside the mapping, and no box is written twice."""
    plan = nar1_plan() if form is FormCode.NAR1 else nd2a_plan()
    mapped = {name for entry in items_for(form) for name in entry.pdf_fields}
    assert set(plan.fields) <= mapped
    # A widget belongs to exactly one item, so the plan's keys cannot collide;
    # the planner raises ``FillPlanError`` if a registry refresh ever makes two
    # items share a box.
    assert len(plan.fields) == len(set(plan.fields))


@pytest.mark.parametrize("form", [FormCode.NAR1, FormCode.ND2A])
def test_every_escalation_kind_is_in_the_closed_vocabulary(form: FormCode) -> None:
    plan = nar1_plan() if form is FormCode.NAR1 else nd2a_plan()
    assert all(escalation.kind in ESCALATION_KINDS for escalation in plan.escalations)


def test_a_field_written_by_two_items_fails_loudly() -> None:
    """A registry refresh that duplicated a box is a refusal, not a guess."""
    items = (
        RegistryItem("first", "text", "company.email", ("fill_2_P.2",)),
        RegistryItem("second", "text", "company.email", ("fill_2_P.2",)),
    )
    with pytest.raises(FillPlanError, match=r"fill_2_P\.2"):
        plan_fill(
            form="nar1",
            resolved_state=nar1_state(),
            filing_metadata=METADATA,
            private_company_statement=True,
            items=items,
            options=CHECKBOX_OPTIONS,
            sheet_pages=sheet_pages_for(FormCode.NAR1),
        )


# --- The option table -------------------------------------------------------


def test_every_option_widget_exists_in_the_pinned_geometry() -> None:
    known = {
        name
        for form in (FormCode.NAR1, FormCode.ND2A)
        for name in load_form_registry(form).geometry
    }
    for target, boxes in CHECKBOX_OPTIONS.items():
        for box in boxes:
            assert box in known, f"{target}: {box} is not a pinned widget"


@pytest.mark.parametrize("form", [FormCode.NAR1, FormCode.ND2A])
def test_every_checkbox_group_of_the_mapping_is_in_the_option_table(form: FormCode) -> None:
    registry = load_form_registry(form)
    for entry in registry.items:
        if entry.item_type != "checkbox_group":
            continue
        boxes = CHECKBOX_OPTIONS.get(entry.target)
        assert boxes is not None, f"{form}: no option group for {entry.target!r}"
        for name in entry.pdf_fields:
            if registry.geometry[name].widget_type not in {"/Btn", "/Ch"}:
                continue  # the 代替 Alternate to box beside a capacity group
            assert name in boxes, f"{entry.item}: {name} has no option"


_BOX_VALUE = re.compile(r"\b(cb_\d+_P\.\d+)\s*=\s*([a-z_]+)\b")
_NOTE_YES_NO = {"yes": True, "no": False}


def test_the_option_table_transcribes_the_registry_notes() -> None:
    """The groups the notes state as ``box=value``, box by box.

    A note edit and a table edit cannot drift apart: the table is the notes'
    transcription, and this reads the notes back.
    """
    checked = 0
    for form in (FormCode.NAR1, FormCode.ND2A):
        mapping = json.loads(
            (forms_root() / form.value / "mapping.json").read_text(encoding="utf-8")
        )
        for entry in mapping["items"]:
            if entry["type"] != "checkbox_group":
                continue
            boxes = CHECKBOX_OPTIONS.get(entry["target"], {})
            for box, stated in _BOX_VALUE.findall(entry.get("notes") or ""):
                if box not in entry["pdf_fields"]:
                    continue
                assert boxes.get(box) == _NOTE_YES_NO.get(stated, stated), (
                    f"{form.value} {entry['item']}: {box} is {stated!r} in the note"
                )
                checked += 1
    assert checked >= 20  # the ND2A capacity, reason and yes/no groups


@pytest.mark.parametrize("box", sorted(CHECKBOX_OPTIONS["company.type"]))
def test_the_reader_reads_the_shared_option_table(box: str) -> None:
    """The extraction reader and the planner read one table (the drift guard).

    A filing's ticked box must extract as exactly the value the planner writes
    for that box.
    """
    result = extract(acroform_pdf({box: "On"}), form=FormCode.NAR1)
    field = next(field for field in result.fields if field.target == "company.type")
    assert field.value == CHECKBOX_OPTIONS["company.type"][box]


def test_the_readers_group_view_is_the_shared_tables() -> None:
    assert dict(vocabulary_options("company.type")) == CHECKBOX_OPTIONS["company.type"]
    assert vocabulary_options("not.a.group") == {}


# --- NAR1: the no-change annual return --------------------------------------


def test_nar1_no_change_fills_the_form() -> None:
    plan = nar1_plan()
    registry_items = items_for(FormCode.NAR1)
    header_brn = next(entry for entry in registry_items if entry.item == "HDR-BRN")

    printed = printed_writes(plan)
    for name in header_brn.pdf_fields:
        assert printed[name] == "76543210"
    assert plan.fields["fill_2_P.1"] == BILINGUAL_NAME
    assert plan.fields["fill_3_P.1"] == "SYNTHETIC HARBOUR TRADING"
    assert plan.fields["cb_1_P.1"] is True  # private company
    assert plan.fields["fill_4_P.1"] == "045"
    assert plan.fields["fill_5_P.1"] == "IMPORT AND EXPORT TRADE"
    assert [plan.fields[name] for name in ("fill_6_P.1", "fill_7_P.1", "fill_8_P.1")] == [
        "30",
        "06",
        "2026",
    ]
    assert plan.fields["fill_15_P.1"] == "ROOM 1201, 12/F"
    assert plan.fields["fill_18_P.1"] == "WAN CHAI"
    assert plan.fields["fill_2_P.2"] == "compliance@synthetic-harbour.example.com.hk"
    assert plan.fields["fill_3_P.2"] == "2345 6789"
    assert plan.fields["fill_4_P.2"] == "Nil"  # no charges, the specimens' "Nil"
    assert plan.fields["cb_1_P.8"] is True  # item 14: schedule_1
    assert plan.fields["cb_4_P.8"] is True  # item 16: the statement
    assert plan.fields["fill_21_P.1"] == "2987 6543"
    assert plan.fields["fill_24_P.1"] == "SH-2026-AR-001"
    assert plan.fields["fill_11_P.8"] == "CHAN TAI MAN"
    assert plan.fields["fill_12_P.8"] == "30/07/2026"
    assert plan.escalations == ()


def test_nar1_officer_blocks_carry_the_officers() -> None:
    plan = nar1_plan()

    # 12B: the body-corporate secretary — names, address, email, BRN, licence.
    assert plan.fields["fill_2_P.4"] == "合成秘書服務有限公司"
    assert plan.fields["fill_3_P.4"] == "SYNTHETIC SECRETARIAL SERVICES LIMITED"
    assert plan.fields["fill_4_P.4"] == "SUITE 800, 8/F"
    assert plan.fields["fill_7_P.4"] == "ADMIRALTY"
    assert plan.fields["fill_8_P.4"] == "cs@synthetic-secretarial.example.com.hk"
    assert plan.fields["fill_9_P.4"] == "87654321"
    assert plan.fields["fill_10_P.4"] == "TC009999"
    assert "cb_1_P.4" not in plan.fields  # licence given, so no "not required" tick

    # 13A: the natural-person director — capacity, name parts, address, ID.
    assert plan.fields["cb_1_P.5"] is True
    assert "cb_2_P.5" not in plan.fields
    assert plan.fields["fill_3_P.5"] == "陳大文"
    assert plan.fields["fill_4_P.5"] == "CHAN"
    assert plan.fields["fill_5_P.5"] == "TAI MAN"
    assert plan.fields["fill_10_P.5"] == "FLAT A, 20/F"
    assert plan.fields["fill_14_P.5"] == "HONG KONG"
    assert plan.fields["fill_16_P.5"] == "A123"
    # The unused secretary blocks carry nothing.
    assert "fill_3_P.3" not in plan.fields
    assert "fill_2_P.7" not in plan.fields


def test_nar1_body_corporate_director_blocks() -> None:
    body_director = {
        **SECRETARY,
        "person_id": "bdir-1",
        "role": "director",
        "capacity": ["director"],
        "valid_from": "2020-02-02",
    }
    second = {**body_director, "person_id": "bdir-2", "valid_from": "2020-03-03"}
    third = {**body_director, "person_id": "bdir-3", "valid_from": "2020-04-04"}
    plan = nar1_plan(
        state=nar1_state(officers=[SECRETARY, DIRECTOR, body_director, second, third])
    )

    assert plan.fields["cb_1_P.6"] is True
    assert "cb_2_P.6" not in plan.fields
    assert plan.fields["fill_3_P.6"] == "合成秘書服務有限公司"
    assert plan.fields["fill_5_P.6"] == "SUITE 800, 8/F"
    assert plan.fields["fill_11_P.6"] == "87654321"
    # The second body-corporate director takes the pinned second block, and the
    # third is the one that escalates to Continuation Sheet D.
    assert plan.fields["cb_3_P.6"] is True
    # The second body-corporate director takes the pinned second block, and the
    # third is the one Continuation Sheet D carries (its first block, on a copy
    # of the sheet's own page).
    assert statuses_of(plan, "CS-D.1.capacity") == {FILLED}
    sheet = sheet_writes(plan, "cs-d")
    assert sheet["fill_6_P.14"] == "合成秘書服務有限公司"
    assert sheet["fill_8_P.14"] == "SUITE 800, 8/F"
    assert sheet["fill_14_P.14"] == "87654321"
    assert "fill_16_P.14" not in sheet  # the copy's second block carries nobody
    assert plan.escalations == ()


def test_nar1_officer_without_an_address_escalates() -> None:
    bare = {**DIRECTOR, "correspondence_address": None}
    plan = nar1_plan(state=nar1_state(officers=[SECRETARY, bare]))
    assert statuses_of(plan, "13A.address") == {ESCALATED}
    assert not any(name.startswith("fill_10_P.5") for name in plan.fields)
    assert any(
        escalation.kind == "missing_value" and "address" in escalation.detail
        for escalation in plan.escalations
    )


def test_nar1_share_capital_grid_and_total() -> None:
    plan = nar1_plan()
    item = next(entry for entry in items_for(FormCode.NAR1) if entry.item == "11")
    grid = item.pdf_fields
    assert [plan.fields[name] for name in grid[:5]] == [
        "ORDINARY",
        "HKD",
        "10,000",
        "10,000",
        "10,000",
    ]
    assert [name for name in grid[5:20] if name in plan.fields] == []
    assert [plan.fields[name] for name in grid[20:]] == ["HKD", "10,000", "10,000", "10,000"]


def test_nar1_private_company_without_share_classes_escalates() -> None:
    """A private company has share capital by construction (only private
    companies reach this build's NAR1), so no class resolving is a fact that
    has not landed: items 11, 14 and 10 all escalate rather than blank out."""
    state = nar1_state()
    state["company"] = {**COMPANY, "share_capital": {"classes": [], "total": {}}}
    plan = nar1_plan(state=state)

    assert statuses_of(plan, "11") == {ESCALATED}
    assert statuses_of(plan, "14") == {ESCALATED}
    assert statuses_of(plan, "10") == {ESCALATED}
    assert {escalation.kind for escalation in plan.escalations} == {"missing_value"}
    assert all("share_capital.classes[]" in escalation.detail for escalation in plan.escalations)
    # Nothing is written for the grid, the member count or the delivery ticks.
    assert not {"fill_5_P.2", "fill_6_P.2"} & set(plan.fields)
    assert not {"cb_1_P.8", "cb_2_P.8", "cb_3_P.8"} & set(plan.fields)


def test_nar1_no_share_capital_company_still_completes_item_ten() -> None:
    """The guarantee company's own type statement stands: item 10 applies and
    items 11 and 14 do not, with no escalation."""
    state = nar1_state()
    state["company"] = {
        **COMPANY,
        "type": "company_limited_by_guarantee",
        "filing": {"mortgages_and_charges_total": "0.00", "member_count": 3},
        "share_capital": {"classes": [], "total": {}},
    }
    plan = nar1_plan(state=state)

    assert plan.fields["fill_5_P.2"] == "3"
    assert statuses_of(plan, "10") == {FILLED}
    assert statuses_of(plan, "11") == {BLANK}
    assert statuses_of(plan, "14") == {BLANK}
    assert plan.escalations == ()


def test_nar1_schedule_one_members() -> None:
    plan = nar1_plan()
    sheet = sheet_writes(plan, "s1")
    assert sheet["fill_5_P.9"] == "ORDINARY"
    assert sheet["fill_6_P.9"] == "10,000"
    # Member 1: a natural person — the Chinese name and the English split.
    assert sheet["fill_7_P.9"] == "陳大文"
    assert sheet["fill_8_P.9"] == "CHAN"
    assert sheet["fill_9_P.9"] == "TAI MAN"
    assert "fill_10_P.9" not in sheet
    assert sheet["fill_16_P.9"] == "5,000"
    assert sheet["fill_11_P.9"] == "FLAT A, 20/F"
    assert sheet["fill_14_P.9"] == "CENTRAL"
    # Member 2: a body corporate — the Chinese name and the OR English box.
    assert sheet["fill_18_P.9"] == "海港集團有限公司"
    assert sheet["fill_21_P.9"] == "HARBOUR HOLDINGS LIMITED"
    assert "fill_19_P.9" not in sheet
    assert sheet["fill_27_P.9"] == "5,000"
    # The page's own headers ride the copy — a used sheet prints both.
    assert sheet["fill_4_P.9"] == "76543210"
    assert [sheet[f"fill_{index}_P.9"] for index in (1, 2, 3)] == ["30", "06", "2026"]
    # And its footer page numbers, both boxes the same (the spike's pairs).
    assert [sheet["fill_29_P.9"], sheet["fill_30_P.9"]] == ["1", "1"]


def test_nar1_dates_are_zero_padded_and_every_header_triplet_takes_the_date() -> None:
    metadata = {
        **METADATA,
        "return": {
            "made_up_date": "2026-01-05",
            "fs_period_start": "2025-02-03",
            "fs_period_end": "2025-12-31",
        },
    }
    plan = nar1_plan(metadata=metadata)
    assert [plan.fields[name] for name in ("fill_6_P.1", "fill_7_P.1", "fill_8_P.1")] == [
        "05",
        "01",
        "2026",
    ]
    assert [plan.fields[name] for name in ("fill_9_P.1", "fill_10_P.1", "fill_11_P.1")] == [
        "03",
        "02",
        "2025",
    ]
    assert [plan.fields[name] for name in ("fill_12_P.1", "fill_13_P.1", "fill_14_P.1")] == [
        "31",
        "12",
        "2025",
    ]
    header = next(
        entry for entry in items_for(FormCode.NAR1) if entry.item == "HDR-DATE"
    ).pdf_fields
    printed = printed_writes(plan)
    for start in range(0, len(header), 3):
        assert [printed[name] for name in header[start : start + 3]] == ["05", "01", "2026"]


def test_nar1_english_only_name_uses_the_one_name() -> None:
    state = nar1_state()
    state["company"] = {**COMPANY, "name": {"name_en": "SYNTHETIC HARBOUR TRADING LIMITED"}}
    plan = nar1_plan(state=state)
    assert plan.fields["fill_2_P.1"] == "SYNTHETIC HARBOUR TRADING LIMITED"


def test_nar1_schedule_two_is_not_applicable() -> None:
    plan = nar1_plan()
    assert statuses_of(plan, "S2.class") == {NOT_APPLICABLE}
    assert statuses_of(plan, "S2.page_no") == {NOT_APPLICABLE}
    # The package arithmetic is derived from the plan's own sheets.
    assert statuses_of(plan, "8A") == {FILLED}
    assert statuses_of(plan, "S1.page_no") == {FILLED}
    assert sheets_of(plan, "s2") == ()  # Schedule 2 prints no copy


def test_nar1_sheet_counts_are_the_used_sheets() -> None:
    """Item 8A: the used sheets per type, in the registry's own box order."""
    plan = nar1_plan()
    item = next(entry for entry in items_for(FormCode.NAR1) if entry.item == "8A")
    counts = {name: plan.fields[name] for name in item.pdf_fields}
    assert counts == {
        "fill_4_P.8": "0",  # 續頁 A
        "fill_5_P.8": "0",  # 續頁 B
        "fill_6_P.8": "0",  # 續頁 C
        "fill_7_P.8": "0",  # 續頁 D
        "fill_8_P.8": "0",  # 續頁 E
        "fill_9_P.8": "1",  # Schedule 1: the members' one sheet
        "fill_10_P.8": "0",  # Schedule 2: not this build
    }
    # The count is the roster's, sheet by sheet: two natural directors and one
    # records row beyond the pinned block make C and E one each.
    second = {**DIRECTOR, "person_id": "dir-2", "valid_from": "2021-05-05"}
    rows = [{"description": "D", "address": "A"}] * 2
    state = nar1_state(officers=[SECRETARY, DIRECTOR, second])
    state["company"] = {**COMPANY, "records_kept": rows}
    moved = nar1_plan(state=state)
    assert moved.fields["fill_6_P.8"] == "1"
    assert moved.fields["fill_8_P.8"] == "1"
    assert moved.fields["fill_9_P.8"] == "1"


def test_the_plans_sheet_vocabulary_is_the_registrys() -> None:
    """The domain's sheet slugs and the registry's read cannot drift apart."""
    assert set(sheet_page_fields(load_form_registry(FormCode.NAR1))) == {
        "s1",
        "s2",
        "cs-a",
        "cs-b",
        "cs-c",
        "cs-d",
        "cs-e",
    }
    assert set(sheet_page_fields(load_form_registry(FormCode.ND2A))) == {
        "a-2a",
        "b-3",
        "c-4",
        "pi",
    }


@pytest.mark.parametrize("form", [FormCode.NAR1, FormCode.ND2A])
def test_every_sheet_copy_prints_only_its_pages_boxes(form: FormCode) -> None:
    """A copy's value set is a subset of its own page's widgets — headers included.

    The header boxes are the other half of "a used sheet prints them": the copy
    carries every header box of its page, which is what makes the set complete
    for the writer's single pass.
    """
    registry = load_form_registry(form)
    plan = nar1_plan() if form is FormCode.NAR1 else nd2a_plan(
        changes=[change("e1", "director_appointment", appointment_officer(), "2026-08-01")]
    )
    pages = sheet_page_fields(registry)
    headers = page_header_fields(registry)
    assert plan.sheets, "this scenario uses a sheet"
    for copy in plan.sheets:
        assert set(copy.fields) <= set(pages[copy.sheet])
        assert set(f for f in pages[copy.sheet] if f in headers) <= set(copy.fields)


def test_the_sheet_copies_carry_their_headers() -> None:
    """A used sheet prints its BRN and made-up date; the plan's copies agree."""
    plan = nar1_plan()
    sheet = sheet_writes(plan, "s1")
    assert sheet["fill_4_P.9"] == COMPANY["br_number"]
    assert [sheet[f"fill_{index}_P.9"] for index in (1, 2, 3)] == ["30", "06", "2026"]


def test_nar1_financial_period_and_item_ten_follow_the_form_notes() -> None:
    plan = nar1_plan()
    # Item 5 is blank when the metadata states no period (form note 11), and
    # item 10 only applies to a company without share capital.
    assert statuses_of(plan, "5.from") == {BLANK}
    assert statuses_of(plan, "5.to") == {BLANK}
    assert statuses_of(plan, "10") == {BLANK}
    assert "fill_5_P.2" not in plan.fields


def test_nar1_officer_overflow_fills_the_continuation_sheets() -> None:
    """An officer past a pinned block lands on the sheet that carries the block."""
    second = {**DIRECTOR, "person_id": "dir-2", "name_zh": "黃小明", "valid_from": "2021-05-05"}
    extra_secretary = {
        **DIRECTOR,
        "person_id": "sec-2",
        "name_zh": "辛小秘",
        "role": "company_secretary",
        "capacity": ["company_secretary"],
        "tcsp_licence": {"licence_no": "TC001111"},
        "valid_from": "2021-06-06",
    }
    plan = nar1_plan(
        state=nar1_state(officers=[SECRETARY, DIRECTOR, second, extra_secretary])
    )

    assert statuses_of(plan, "13A.name") == {FILLED}  # the first natural director
    assert statuses_of(plan, "CS-C.name") == {FILLED}
    assert statuses_of(plan, "CS-A.name") == {FILLED}
    assert plan.escalations == ()
    # The second natural director's block is the copy's, never the main page's.
    assert "fill_6_P.13" not in plan.fields
    cs_c = sheet_writes(plan, "cs-c")
    assert cs_c["fill_6_P.13"] == "黃小明"
    assert cs_c["fill_7_P.13"] == "CHAN"
    assert cs_c["fill_8_P.13"] == "TAI MAN"
    assert cs_c["fill_19_P.13"] == "A123"
    assert cs_c["cb_1_P.13"] is True  # director
    assert cs_c["fill_4_P.13"] == "76543210"
    cs_a = sheet_writes(plan, "cs-a")
    assert cs_a["fill_5_P.11"] == "辛小秘"
    assert cs_a["fill_20_P.11"] == "TC001111"
    # Item 8A states the sheets the package carries.
    assert (plan.fields["fill_4_P.8"], plan.fields["fill_6_P.8"]) == ("1", "1")
    # And the copies carry the footer number they print: none — CS sheets are
    # unnumbered — where Schedule 1's copies carry 1..N among themselves.
    assert [copy.page_no for copy in sheets_of(plan, "cs-c")] == [None]
    assert [copy.page_no for copy in sheets_of(plan, "cs-a")] == [None]


def test_sheet_plans_carry_the_schedule_one_footer_numbers() -> None:
    """The roster's printed number rides the plan: Schedule 1 copies carry
    1..N numbered among themselves (the ``S1.page_no`` item prints exactly
    these), and the export envelope's ``page_pairs`` read them — never a
    number the flat PDF does not print."""
    extra = {
        **MEMBERS[0],
        "person_id": "mem-3",
        "name_zh": "丙",
        "name_en": "TING SIU MING",
        "shares_held": 10,
    }
    plan = nar1_plan(state={**nar1_state(), "shareholders": [*MEMBERS, extra]})
    copies = sheets_of(plan, "s1")
    assert [copy.page_no for copy in copies] == [1, 2]
    assert [copies[0].fields["fill_29_P.9"], copies[0].fields["fill_30_P.9"]] == ["1", "1"]
    assert [copies[1].fields["fill_29_P.9"], copies[1].fields["fill_30_P.9"]] == ["2", "2"]


def test_nd2a_sheet_plans_carry_no_footer_numbers() -> None:
    """ND2A's grid prints no footer page numbers at all, so every sheet copy
    — continuation sheets included — carries ``None`` and an ND2A export's
    envelope records no page pairs."""
    plan = nd2a_plan(
        changes=[
            change("e1", "director_cessation", cessation_officer(), "2026-07-30"),
            change("e2", "director_cessation", cessation_officer(name_zh="丁"), "2026-07-31"),
            change("e3", "director_appointment", appointment_officer(), "2026-08-01"),
            change("e4", "director_appointment", appointment_officer(name_zh="戊"), "2026-08-02"),
        ]
    )
    assert [copy.page_no for copy in plan.sheets] == [None] * len(plan.sheets)
    assert len(plan.sheets) > 0


def test_nar1_beyond_a_sheets_capacity_is_another_copy() -> None:
    """CS-C prints one block, so the third natural director is a second copy."""
    second = {**DIRECTOR, "person_id": "dir-2", "name_zh": "黃小明", "valid_from": "2021-05-05"}
    third = {**DIRECTOR, "person_id": "dir-3", "name_zh": "丁", "valid_from": "2022-06-06"}
    plan = nar1_plan(state=nar1_state(officers=[SECRETARY, DIRECTOR, second, third]))

    copies = sheets_of(plan, "cs-c")
    assert [copy.sheet for copy in copies] == ["cs-c", "cs-c"]
    assert copies[0].fields["fill_6_P.13"] == "黃小明"
    assert copies[1].fields["fill_6_P.13"] == "丁"
    assert plan.fields["fill_6_P.8"] == "2"  # 8A counts the used sheets
    assert plan.escalations == ()


def test_nar1_body_corporate_directors_share_a_sheet_two_at_a_time() -> None:
    """CS-D prints two blocks, so the fifth body director starts a second copy."""
    body = {
        **SECRETARY,
        "person_id": "bdir-1",
        "role": "director",
        "capacity": ["director"],
        "valid_from": "2020-02-02",
    }
    officers = [SECRETARY, DIRECTOR] + [
        {**body, "person_id": f"bdir-{index}", "valid_from": f"202{index}-03-03"}
        for index in range(1, 6)
    ]
    plan = nar1_plan(state=nar1_state(officers=officers))

    copies = sheets_of(plan, "cs-d")
    assert len(copies) == 2
    first_blocks = [name for name in ("fill_6_P.14", "fill_16_P.14") if name in copies[0].fields]
    second_blocks = [name for name in ("fill_6_P.14", "fill_16_P.14") if name in copies[1].fields]
    assert first_blocks == ["fill_6_P.14", "fill_16_P.14"]
    assert second_blocks == ["fill_6_P.14"]
    assert plan.fields["fill_7_P.8"] == "2"


def test_nar1_more_than_four_share_classes_escalates() -> None:
    classes = [
        {
            "class_name": f"CLASS {index}",
            "currency": "HKD",
            "total_shares": index * 100,
            "total_amount": f"{index * 100}.00",
            "total_paid_up": f"{index * 100}.00",
        }
        for index in range(1, 6)
    ]
    state = nar1_state()
    state["company"] = {**COMPANY, "share_capital": {"classes": classes, "total": {}}}
    plan = nar1_plan(state=state)

    item = next(entry for entry in items_for(FormCode.NAR1) if entry.item == "11")
    grid = item.pdf_fields
    assert plan.fields[grid[0]] == "CLASS 1"
    assert plan.fields[grid[15]] == "CLASS 4"
    assert "CLASS 5" not in plan.fields.values()  # row 5 has no box
    assert statuses_of(plan, "11") == {ESCALATED}
    assert [escalation.kind for escalation in plan.escalations] == ["share_classes_over_4"]
    assert "CLASS 5" in plan.escalations[0].detail


def test_nar1_records_rows_overflow_to_continuation_sheet_e() -> None:
    rows = [{"description": "REGISTER OF MEMBERS", "address": "8/F, HARBOUR CENTRE"}]
    state = nar1_state()
    state["company"] = {**COMPANY, "records_kept": rows}
    plan = nar1_plan(state=state)
    assert plan.fields["fill_2_P.8"] == "REGISTER OF MEMBERS"
    assert plan.fields["fill_3_P.8"] == "8/F, HARBOUR CENTRE"
    assert statuses_of(plan, "CS-E.records") == {BLANK}
    assert sheets_of(plan, "cs-e") == ()

    extra = {"description": "REGISTER OF DIRECTORS", "address": "1 QUEENSWAY"}
    two = nar1_plan(state={**state, "company": {**COMPANY, "records_kept": [*rows, extra]}})
    assert statuses_of(two, "15") == {FILLED}
    assert statuses_of(two, "CS-E.records") == {FILLED}
    assert two.escalations == ()
    # Sheet E's row is the extra rows' — never the main page's first row.
    sheet_e = sheet_writes(two, "cs-e")
    assert sheet_e["fill_5_P.15"] == "REGISTER OF DIRECTORS"
    assert sheet_e["fill_6_P.15"] == "1 QUEENSWAY"
    assert sheet_e["fill_4_P.15"] == "76543210"
    assert two.fields["fill_8_P.8"] == "1"


def test_nar1_member_overflow_fills_the_schedule_one_sheets() -> None:
    extra = {
        **MEMBERS[0],
        "person_id": "mem-3",
        "name_zh": "丙",
        "name_en": "TING SIU MING",
        "shares_held": 10,
    }
    plan = nar1_plan(state={**nar1_state(), "shareholders": [*MEMBERS, extra]})
    # The third member is the second Schedule 1 sheet's, never the pinned page's.
    assert "fill_29_P.9" not in plan.fields
    assert plan.escalations == ()
    copies = sheets_of(plan, "s1")
    assert len(copies) == 2
    assert copies[0].fields["fill_7_P.9"] == "陳大文"
    assert copies[1].fields["fill_7_P.9"] == "丙"
    assert copies[1].fields["fill_16_P.9"] == "10"
    # Each copy its own footer pair, and 8A counts both sheets.
    assert [copies[0].fields["fill_29_P.9"], copies[0].fields["fill_30_P.9"]] == ["1", "1"]
    assert [copies[1].fields["fill_29_P.9"], copies[1].fields["fill_30_P.9"]] == ["2", "2"]
    assert plan.fields["fill_9_P.8"] == "2"
    assert statuses_of(plan, "S1.member.2.name") == {FILLED}


def test_nar1_a_second_class_gets_its_own_schedule_one_sheets() -> None:
    """Schedule 1 prints one class per sheet; a second class is another sheet."""
    extra = {
        **MEMBERS[0],
        "person_id": "mem-3",
        "name_zh": "丙",
        "class_name": "PREFERENCE",
        "shares_held": 1,
    }
    state = nar1_state()
    state["company"] = {
        **COMPANY,
        "share_capital": {
            "classes": [
                *COMPANY["share_capital"]["classes"],
                {
                    "class_name": "PREFERENCE",
                    "currency": "HKD",
                    "total_shares": 1,
                    "total_amount": "1.00",
                    "total_paid_up": "0.00",
                },
            ],
            "total": {},
        },
    }
    plan = nar1_plan(state={**state, "shareholders": [*MEMBERS, extra]})

    copies = sheets_of(plan, "s1")
    assert len(copies) == 2
    assert copies[0].fields["fill_5_P.9"] == "ORDINARY"
    assert copies[1].fields["fill_5_P.9"] == "PREFERENCE"
    assert copies[1].fields["fill_7_P.9"] == "丙"
    assert plan.escalations == ()


# --- NAR1: missing sources --------------------------------------------------


def test_nar1_missing_metadata_escalates_by_name() -> None:
    empty = {
        "return": {},
        "member_particulars_delivery": None,
        "presentor": None,
        "signature": None,
        "consent": None,
    }
    plan = nar1_plan(metadata=empty, statement=None)

    assert statuses_of(plan, "4") == {ESCALATED}
    assert statuses_of(plan, "PR") == {ESCALATED}
    assert statuses_of(plan, "16") == {ESCALATED}
    assert statuses_of(plan, "SIG.name") == {ESCALATED}
    assert statuses_of(plan, "SIG.date") == {ESCALATED}
    assert statuses_of(plan, "SIG.capacity") == {ESCALATED}
    assert statuses_of(plan, "14") == {ESCALATED}  # a share-capital company must complete it
    assert all(escalation.kind == "missing_value" for escalation in plan.escalations)
    details = " | ".join(escalation.detail for escalation in plan.escalations)
    assert "made_up_date" in details
    assert "filing_metadata.presentor" in details
    assert "signature.capacity" in details
    # The date items the form leaves optional stay blank, not escalated.
    assert statuses_of(plan, "5.from") == {BLANK}
    assert statuses_of(plan, "HDR-DATE") == {BLANK}


def test_nar1_schedule_two_delivery_is_refused_not_ticked() -> None:
    """Item 14's Schedule 2 box is the listed company's channel, and listed
    companies are outside Wave 1 (every S2.* item prints no item): a stated
    ``schedule_2`` escalates rather than ticking a box this build cannot
    honour. ``cd_rom`` is the TCSP's own action — the form only ticks the
    channel — and stays selectable."""
    stated = {**METADATA, "member_particulars_delivery": "schedule_2"}
    plan = nar1_plan(metadata=stated)

    assert statuses_of(plan, "14") == {ESCALATED}
    assert [escalation.kind for escalation in plan.escalations] == ["missing_value"]
    assert "member_particulars_delivery" in plan.escalations[0].detail
    assert "schedule_2" in plan.escalations[0].detail
    assert "Wave 1" in plan.escalations[0].detail
    assert not {"cb_1_P.8", "cb_2_P.8", "cb_3_P.8"} & set(plan.fields)

    cd_rom = nar1_plan(metadata={**METADATA, "member_particulars_delivery": "cd_rom"})
    assert cd_rom.fields["cb_3_P.8"] is True
    assert cd_rom.escalations == ()


def test_nar1_statement_not_made_is_a_blank_tick() -> None:
    plan = nar1_plan(statement=False)
    assert "cb_4_P.8" not in plan.fields
    assert statuses_of(plan, "16") == {BLANK}


def test_nar1_officer_without_identification_escalates() -> None:
    bare = {
        **DIRECTOR,
        "hkid_partial": None,
        "passport_partial": None,
        "passport_issuing_country_region": None,
    }
    plan = nar1_plan(state=nar1_state(officers=[SECRETARY, bare]))
    assert statuses_of(plan, "13A.identification") == {ESCALATED}
    assert "hkid_partial" in plan.escalations[0].detail


def test_nar1_signature_capacity_strikes_the_other_line() -> None:
    plan = nar1_plan()
    assert "Dropdown_1_P.8" not in plan.fields  # director survives
    assert plan.fields["Dropdown_2_P.8"] == STRIKE  # company secretary struck

    secretary_signed = nar1_plan(
        metadata={
            **METADATA,
            "signature": {**METADATA["signature"], "capacity": "company_secretary"},
        }
    )
    assert secretary_signed.fields["Dropdown_1_P.8"] == STRIKE
    assert "Dropdown_2_P.8" not in secretary_signed.fields


# --- ND2A -------------------------------------------------------------------

ND2A_METADATA: dict[str, Any] = {
    "return": {},
    "member_particulars_delivery": None,
    "presentor": PRESENTOR,
    "signature": {
        "name": "CHAN TAI MAN",
        "date": "2026-08-01",
        "capacity": "company_secretary",
    },
    "consent": None,
}


def change(event_id: str, kind: str, officer: Mapping[str, Any], effective: str) -> dict[str, Any]:
    body = "appointment" if kind.endswith("appointment") else "cessation"
    return {
        "event_id": event_id,
        "type": kind,
        "effective_date": effective,
        "payload": {body: {"officer": dict(officer)}},
    }


def appointment_officer(**overrides: Any) -> dict[str, Any]:
    officer: dict[str, Any] = {
        "role": "director",
        "capacity": ["director"],
        "appointment_date": "2026-08-01",
        "existing_director_at_appointment": False,
        "name_zh": "李文樂",
        "surname_en": "LI",
        "other_names_en": "MAN LOK",
        "email": "ml.li@example.com",
        "hkid_partial": "D234",
        "correspondence_address": {
            "flat": "ROOM 8801, 88/F",
            "building": "HAPPY COMMERCIAL BUILDING",
            "street": "1 QUEENSWAY",
            "district": "ADMIRALTY",
            "country": "HONG KONG",
        },
    }
    officer.update(overrides)
    return officer


def cessation_officer(**overrides: Any) -> dict[str, Any]:
    officer: dict[str, Any] = {
        "role": "director",
        "capacity": ["director"],
        "cessation_reason": "resignation_others",
        "cessation_date": "2026-07-31",
        "continues_office_after_cessation": False,
        "name_zh": "黃小明",
        "surname_en": "WONG",
        "other_names_en": "SIU MING",
        "hkid_partial": "B456",
    }
    officer.update(overrides)
    return officer


def nd2a_plan(
    *,
    changes: Sequence[Mapping[str, Any]] = (),
    metadata: Mapping[str, Any] | None = None,
) -> FillPlan:
    return plan_fill(
        form="nd2a",
        resolved_state={
            "company": {
                "br_number": "76543210",
                "type": "private",
                "name": {"name_en": "SYNTHETIC HARBOUR TRADING LIMITED"},
                "registered_office": {},
                "share_capital": {"classes": [], "total": {}},
            },
            "officers": [],
            "shareholders": [],
            "changes": list(changes),
        },
        filing_metadata=ND2A_METADATA if metadata is None else metadata,
        private_company_statement=None,
        items=items_for(FormCode.ND2A),
        options=CHECKBOX_OPTIONS,
        sheet_pages=sheet_pages_for(FormCode.ND2A),
    )


def test_nd2a_appointment_of_a_natural_person() -> None:
    plan = nd2a_plan(
        changes=[change("e1", "director_appointment", appointment_officer(), "2026-08-01")]
    )

    assert plan.fields["cb_2_P.2"] is True  # director
    assert plan.fields["fill_3_P.2"] == "李文樂"
    assert plan.fields["fill_4_P.2"] == "LI"
    assert plan.fields["fill_5_P.2"] == "MAN LOK"
    # The official specimen's "Nil" for the empty previous-name and alias boxes.
    assert plan.fields["fill_6_P.2"] == "Nil"
    assert plan.fields["fill_7_P.2"] == "Nil"
    assert plan.fields["fill_8_P.2"] == "Nil"
    assert plan.fields["fill_9_P.2"] == "Nil"
    assert plan.fields["fill_10_P.2"] == "ROOM 8801, 88/F"
    assert plan.fields["fill_14_P.2"] == "HONG KONG"
    assert plan.fields["fill_15_P.2"] == "ml.li@example.com"
    assert plan.fields["fill_16_P.2"] == "D234"
    assert [plan.fields[name] for name in ("fill_21_P.2", "fill_22_P.2", "fill_23_P.2")] == [
        "01",
        "08",
        "2026",
    ]
    assert plan.fields["cb_6_P.2"] is True  # not already a director
    # The consent sentence keeps the director line and strikes the alternate's.
    assert "Dropdown_1_P.2" not in plan.fields
    assert plan.fields["Dropdown_2_P.2"] == STRIKE
    # No cessation, so the cessation block carries nothing.
    assert statuses_of(plan, "2A") == {BLANK}
    assert "fill_4_P.1" not in plan.fields


def test_nd2a_pi_sheet_fills_the_public_values_and_escalates_the_protected() -> None:
    officer = appointment_officer(passport_issuing_country_region="HONG KONG")
    plan = nd2a_plan(
        changes=[change("e1", "director_appointment", officer, "2026-08-01")]
    )

    pi = sheet_writes(plan, "pi")
    assert pi["cb_2_P.7"] is True
    assert pi["fill_2_P.7"] == "李文樂"
    assert pi["fill_3_P.7"] == "LI"
    assert pi["fill_4_P.7"] == "MAN LOK"
    assert pi["fill_7_P.7"] == "HONG KONG"  # a public value
    assert pi["fill_1_P.7"] == "76543210"  # a used sheet prints its header
    for protected_box in ("fill_5_P.7", "fill_6_P.7", "fill_8_P.7"):
        assert protected_box not in pi
    # The usual residential address's five boxes are left untouched too.
    for address_box in ("fill_9_P.7", "fill_10_P.7", "fill_11_P.7", "fill_12_P.7", "fill_13_P.7"):
        assert address_box not in pi

    assert ESCALATED in statuses_of(plan, "PI")
    protected = [
        escalation
        for escalation in plan.escalations
        if escalation.kind == "protected_class_manual"
    ]
    assert len(PROTECTED_CLASS_FIELDS) == 4
    assert len(protected) == len(PROTECTED_CLASS_FIELDS)
    for field in PROTECTED_CLASS_FIELDS:
        assert any(field in escalation.detail for escalation in protected)
    # The unnumbered sheet-count grid counts the PI sheet.
    assert plan.fields["fill_21_P.3"] == "1"


def test_nd2a_each_pi_sheets_protected_boxes_get_their_own_entry() -> None:
    """Two appointees make two PI-ND2A sheets; the ledger names each one's boxes.

    The second sheet is a flattened duplicate whose §1 boxes are completed by
    hand like the first's, so each protected escalation has to name the
    appointee it is about — identical details would dedupe into one entry and a
    person working down the checklist would never learn the second sheet needs
    the same hand-completion.
    """
    plan = nd2a_plan(
        changes=[
            change("e1", "director_appointment", appointment_officer(), "2026-08-01"),
            change("e2", "director_appointment", appointment_officer(name_zh="戊"), "2026-08-02"),
        ]
    )
    assert [copy.sheet for copy in plan.sheets] == ["b-3", "pi", "pi"]
    protected = [
        escalation
        for escalation in plan.escalations
        if escalation.kind == "protected_class_manual"
    ]
    assert len(protected) == 2 * len(PROTECTED_CLASS_FIELDS)
    for field in PROTECTED_CLASS_FIELDS:
        named = [escalation for escalation in protected if field in escalation.detail]
        assert len(named) == 2, field
        assert any("e1" in escalation.detail for escalation in named)
        assert any("e2" in escalation.detail for escalation in named)


def test_nd2a_cessation_of_a_natural_person() -> None:
    plan = nd2a_plan(
        changes=[change("e1", "director_cessation", cessation_officer(), "2026-07-31")]
    )

    assert plan.fields["cb_2_P.1"] is True
    assert plan.fields["fill_4_P.1"] == "黃小明"
    assert plan.fields["fill_5_P.1"] == "WONG"
    assert plan.fields["fill_6_P.1"] == "SIU MING"
    assert plan.fields["fill_7_P.1"] == "B456"
    assert plan.fields["cb_4_P.1"] is True  # resignation/others
    assert [plan.fields[name] for name in ("fill_11_P.1", "fill_12_P.1", "fill_13_P.1")] == [
        "31",
        "07",
        "2026",
    ]
    assert plan.fields["cb_7_P.1"] is True  # does not continue in office
    # The body-corporate half of the block stays untouched.
    for name in ("fill_9_P.1", "fill_10_P.1"):
        assert name not in plan.fields
    # The signature block is signed by the company secretary, so the director
    # line is struck.
    assert plan.fields["Dropdown_6_P.3"] == STRIKE
    assert "Dropdown_7_P.3" not in plan.fields
    # No appointment, so the appointment blocks are blank.
    assert statuses_of(plan, "3") == {BLANK}
    assert statuses_of(plan, "4") == {BLANK}
    assert "fill_3_P.2" not in plan.fields


def test_nd2a_body_corporate_appointment_and_consent_block() -> None:
    body = appointment_officer(
        name_zh=None,
        surname_en=None,
        other_names_en=None,
        hkid_partial=None,
        correspondence_address=None,
        email=None,
        name_en="SYNTHETIC HOLDINGS LIMITED",
        br_number="99887766",
        registered_office_address={
            "flat": "ROOM 1",
            "building": "B",
            "street": "S",
            "district": "D",
            "country": "HONG KONG",
        },
        previous_names=None,
        aliases=None,
    )
    metadata = {
        **ND2A_METADATA,
        "consent": {
            "signatory_name": "TSANG SIU FUNG",
            "signatory_capacity": "authorized_person",
        },
    }
    plan = nd2a_plan(
        changes=[change("e1", "director_appointment", body, "2026-08-01")], metadata=metadata
    )

    assert plan.fields["cb_2_P.3"] is True
    assert plan.fields["fill_4_P.3"] == "SYNTHETIC HOLDINGS LIMITED"
    assert plan.fields["fill_11_P.3"] == "99887766"
    assert plan.fields["cb_6_P.3"] is True
    assert plan.fields["fill_17_P.3"] == "TSANG SIU FUNG"
    # The consent's three-way strike keeps the authorized person alone.
    assert plan.fields["Dropdown_3_P.3"] == STRIKE
    assert plan.fields["Dropdown_4_P.3"] == STRIKE
    assert "Dropdown_5_P.3" not in plan.fields
    # The body-corporate appointment has no PI sheet.
    assert statuses_of(plan, "PI") == {BLANK}
    assert not any(
        escalation.kind == "protected_class_manual" for escalation in plan.escalations
    )


def test_nd2a_alternate_director_keeps_its_own_line() -> None:
    officer = appointment_officer(capacity=["alternate_director"], alternate_to="CHAN TAI MAN")
    plan = nd2a_plan(
        changes=[change("e1", "director_appointment", officer, "2026-08-01")]
    )

    assert plan.fields["cb_3_P.2"] is True
    assert "cb_2_P.2" not in plan.fields
    assert plan.fields["fill_2_P.2"] == "CHAN TAI MAN"
    # The consent sentence keeps 候補董事 and strikes the plain directorship.
    assert plan.fields["Dropdown_1_P.2"] == STRIKE
    assert "Dropdown_2_P.2" not in plan.fields


def test_nd2a_company_secretary_appointment_strikes_both_consent_lines() -> None:
    officer = appointment_officer(
        role="company_secretary",
        capacity=["company_secretary"],
        tcsp_licence={"licence_no": "TC001234"},
    )
    plan = nd2a_plan(
        changes=[change("e1", "secretary_appointment", officer, "2026-08-01")]
    )

    assert plan.fields["cb_1_P.2"] is True
    assert plan.fields["fill_19_P.2"] == "TC001234"
    assert plan.fields["Dropdown_1_P.2"] == STRIKE
    assert plan.fields["Dropdown_2_P.2"] == STRIKE
    # A natural-person secretary still files the PI sheet.
    assert ESCALATED in statuses_of(plan, "PI")
    assert sheet_writes(plan, "pi")["cb_1_P.7"] is True


def test_nd2a_licence_or_reason_rendering() -> None:
    not_required = appointment_officer(
        role="company_secretary",
        capacity=["company_secretary"],
        tcsp_licence={"not_required": True, "not_required_reason": "AN EXEMPTED PERSON"},
    )
    plan = nd2a_plan(
        changes=[change("e1", "secretary_appointment", not_required, "2026-08-01")]
    )
    assert "fill_19_P.2" not in plan.fields
    assert plan.fields["cb_4_P.2"] is True
    assert plan.fields["fill_20_P.2"] == "AN EXEMPTED PERSON"

    missing = nd2a_plan(
        changes=[
            change(
                "e1",
                "secretary_appointment",
                appointment_officer(role="company_secretary", capacity=["company_secretary"]),
                "2026-08-01",
            )
        ]
    )
    assert any(
        escalation.kind == "missing_value" and "tcsp_licence" in escalation.detail
        for escalation in missing.escalations
    )


def test_nd2a_more_than_one_change_of_a_kind_fills_the_continuation_sheets() -> None:
    """A second cessation and a second appointment land on their sheets' copies."""
    plan = nd2a_plan(
        changes=[
            change("e1", "director_cessation", cessation_officer(), "2026-07-30"),
            change("e2", "director_cessation", cessation_officer(name_zh="丁"), "2026-07-31"),
            change("e3", "director_appointment", appointment_officer(), "2026-08-01"),
            change("e4", "director_appointment", appointment_officer(name_zh="戊"), "2026-08-02"),
        ]
    )
    assert not any(
        escalation.kind in {"multiple_changes", "officer_overflow"}
        for escalation in plan.escalations
    )
    assert FILLED in statuses_of(plan, "2A")
    assert FILLED in statuses_of(plan, "A-2A")
    assert FILLED in statuses_of(plan, "3")
    assert FILLED in statuses_of(plan, "B-3")
    # The second cessation's boxes are never written into the pinned block.
    assert "fill_3_P.4" not in plan.fields
    sheet_a = sheet_writes(plan, "a-2a")
    assert sheet_a["fill_3_P.4"] == "丁"
    assert sheet_a["fill_10_P.4"] == "31"
    sheet_b = sheet_writes(plan, "b-3")
    assert sheet_b["fill_3_P.5"] == "戊"
    # The unnumbered sheet-count grid counts each sheet, copies included.
    assert plan.fields["fill_18_P.3"] == "1"
    assert plan.fields["fill_19_P.3"] == "1"
    assert plan.fields["fill_20_P.3"] == "0"
    assert plan.fields["fill_21_P.3"] == "2"  # one PI-ND2A sheet per appointee


def test_nd2a_a_third_change_is_another_copy_of_the_sheet() -> None:
    """Sheet B prints one appointment; the third natural appointee is a copy."""
    plan = nd2a_plan(
        changes=[
            change("e1", "director_appointment", appointment_officer(), "2026-08-01"),
            change("e2", "director_appointment", appointment_officer(name_zh="丁"), "2026-08-02"),
            change("e3", "director_appointment", appointment_officer(name_zh="戊"), "2026-08-03"),
        ]
    )

    copies = sheets_of(plan, "b-3")
    assert [copy.sheet for copy in plan.sheets] == ["b-3", "b-3", "pi", "pi", "pi"]
    assert copies[0].fields["fill_3_P.5"] == "丁"
    assert copies[1].fields["fill_3_P.5"] == "戊"
    assert plan.fields["fill_19_P.3"] == "2"
    assert plan.fields["fill_21_P.3"] == "3"
    assert not any(
        escalation.kind in {"multiple_changes", "officer_overflow"}
        for escalation in plan.escalations
    )


def test_nd2a_undecided_person_type_escalates_instead_of_guessing() -> None:
    bare = {"role": "director", "capacity": ["director"], "appointment_date": "2026-08-01"}
    plan = nd2a_plan(changes=[change("e1", "director_appointment", bare, "2026-08-01")])

    assert statuses_of(plan, "3") == {ESCALATED}
    assert statuses_of(plan, "4") == {ESCALATED}
    assert "fill_3_P.2" not in plan.fields
    assert "fill_3_P.3" not in plan.fields
    assert any(
        escalation.kind == "missing_value" and "person type" in escalation.detail
        for escalation in plan.escalations
    )


def test_nd2a_unanswered_existing_director_question_escalates() -> None:
    """Items 3/4/B-3/C-4 ask it, and the payload grammar leaves it optional.

    The registry's note exempts a company-secretary appointment alone: for a
    director or alternate-director appointment the box is required, so an
    omitted answer is a value the draft's edit has to supply — not a box the
    plan may leave silently unticked.
    """
    officer = appointment_officer()
    officer.pop("existing_director_at_appointment")
    plan = nd2a_plan(
        changes=[change("e1", "director_appointment", officer, "2026-08-01")]
    )

    assert "cb_5_P.2" not in plan.fields
    assert "cb_6_P.2" not in plan.fields
    assert ESCALATED in statuses_of(plan, "3")
    assert any(
        escalation.kind == "missing_value"
        and "existing_director_at_appointment" in escalation.detail
        for escalation in plan.escalations
    )


def test_nd2a_secretary_appointment_leaves_the_existing_director_box_blank() -> None:
    """The same group is genuinely not applicable when only a secretary is appointed."""
    officer = appointment_officer(
        role="company_secretary",
        capacity=["company_secretary"],
        tcsp_licence={"licence_no": "TC001234"},
    )
    officer.pop("existing_director_at_appointment")
    plan = nd2a_plan(
        changes=[change("e1", "secretary_appointment", officer, "2026-08-01")]
    )

    assert "cb_5_P.2" not in plan.fields
    assert "cb_6_P.2" not in plan.fields
    assert not any(
        "existing_director_at_appointment" in escalation.detail
        for escalation in plan.escalations
    )


def test_nd2a_cessation_without_the_continues_office_answer_escalates() -> None:
    """Item 2B/A-2B's question is required of every cessation."""
    officer = cessation_officer()
    officer.pop("continues_office_after_cessation")
    plan = nd2a_plan(
        changes=[change("e1", "director_cessation", officer, "2026-07-31")]
    )

    assert "cb_6_P.1" not in plan.fields
    assert "cb_7_P.1" not in plan.fields
    assert any(
        escalation.kind == "missing_value"
        and "continues_office_after_cessation" in escalation.detail
        for escalation in plan.escalations
    )


def test_nd2a_cessation_without_a_reason_escalates_too() -> None:
    officer = cessation_officer()
    officer.pop("cessation_reason")
    plan = nd2a_plan(
        changes=[change("e1", "director_cessation", officer, "2026-07-31")]
    )

    assert "cb_4_P.1" not in plan.fields
    assert "cb_5_P.1" not in plan.fields
    assert any(
        escalation.kind == "missing_value" and "cessation_reason" in escalation.detail
        for escalation in plan.escalations
    )


def test_nd2a_without_changes_plans_the_notice_shell() -> None:
    plan = nd2a_plan()

    assert statuses_of(plan, "2A") == {BLANK}
    assert statuses_of(plan, "3") == {BLANK}
    assert statuses_of(plan, "4") == {BLANK}
    assert statuses_of(plan, "PI") == {BLANK}
    assert statuses_of(plan, "CONSENT.name") == {BLANK}
    assert plan.sheets == ()  # no continuation sheet and no PI sheet is used
    # The unnumbered sheet-count grid is still stated, zeros and all.
    assert statuses_of(plan, "continuation_sheets") == {FILLED}
    grid = ("fill_18_P.3", "fill_19_P.3", "fill_20_P.3", "fill_21_P.3")
    assert [plan.fields[name] for name in grid] == ["0", "0", "0", "0"]
    # The notice is still signed, and the BRN headers are written.
    assert plan.fields["fill_22_P.3"] == "CHAN TAI MAN"
    assert plan.fields["fill_23_P.3"] == "01/08/2026"
    header = next(entry for entry in items_for(FormCode.ND2A) if entry.item == "0")
    for name in header.pdf_fields:
        assert plan.fields[name] == "76543210"
    assert plan.escalations == ()


# --- The application seam ---------------------------------------------------


def test_application_helper_plans_over_the_pinned_registry() -> None:
    """The seam the writer will call: registry + options + the snapshot."""
    plan = fill_plan_for(
        form_type="nar1",
        resolved_state=nar1_state(),
        filing_metadata=METADATA,
        private_company_statement=True,
    )
    assert plan == nar1_plan()
