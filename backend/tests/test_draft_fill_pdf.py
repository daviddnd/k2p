"""The CR-draft filler's tests: one plan → the official filled PDF.

Database-free and network-free: the inputs are hand-built resolved snapshots
(``app.domain.drafts``' own shape, the F4a fixtures' values) and the pinned
templates; the outputs are read back with pypdf and rendered with pypdfium2.

What these tests pin, and why each layer is checked separately — the Phase 0
spike's own rule ("``/AP`` streams and pdfium renders verify different layers,
both are needed"):

- **round trip**: every widget the plan wrote holds exactly its value, with the
  /V that survives a save and reload;
- **appearance**: a Latin text widget's regenerated ``/AP`` stream draws its
  value, character by character (the comb fields place one glyph per mask box);
- **ticks**: the checkbox's ``/AS`` is the state its own ``/AP /N`` names;
- **strikes**: the widget is hidden with its value preserved and a vector line
  is drawn across the term — checked as pixels, because that layer is the
  overlay page rather than any widget's appearance;
- **CJK**: the embedded font, the rewritten appearance and the *rendered* page
  text, which Latin substitutions cannot fake;
- **page selection**: the unused sheets stay in the package blank, with their
  BRN and made-up-date boxes empty (the official specimens' convention);
- **continuation sheets**: a used sheet's first copy is the template's own page
  (live fields), each later copy is filled in its own pass, drawn flat and
  inserted after it — its widget-free page carries the data the field-by-field
  read cannot reach, checked through the renderer;
- **the derived counts**: item 8A's sheet counts against the pages that are
  actually used, and each Schedule 1 copy's own footer page-number pair (the
  spike's stage-5 consistency proof);
- **the refusals**: a missing font and a character the font cannot render both
  fail loudly, by name, before anything is written.

The last test measures DoD §9 item 22's deterministic half over this file's
six scenarios (the slice's representative set, not the Phase 0 corpus).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject

from app.adapters.forms.registry import (
    load_form_registry,
    page_header_fields,
    sheet_pages,
    template_path,
)
from app.adapters.pdf.filler import (
    DraftFontUnavailableError,
    GlyphUnavailableError,
    draft_font_path,
    fill_template,
    packaged_font_path,
)
from app.application.drafts import fill_plan_for
from app.domain.documents import MAX_UPLOAD_BYTES
from app.domain.draft_fill import STRIKE, FillPlan
from app.domain.prior_filing import FormCode

# --- The NAR1 scenarios' resolved state -------------------------------------

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

SECRETARY: dict[str, Any] = {
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

DIRECTOR: dict[str, Any] = {
    "person_id": "dir-1",
    "person_type": "natural_person",
    "name_zh": "陳大文",
    "name_en": "CHAN Tai Man",
    "surname_en": "CHAN",
    "other_names_en": "TAI MAN",
    "email": "tm.chan@example.com",
    "hkid_partial": "A123",
    "previous_names": [],
    "aliases": [],
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

MEMBERS: list[dict[str, Any]] = [
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

DERIVED_TOTAL = COMPANY["share_capital"]["total"]

COMPANY_NO_ZH: dict[str, Any] = {
    **COMPANY,
    "name": {"name_en": "SYNTHETIC HARBOUR TRADING LIMITED", "business_name": None},
}

PRESENTOR = {
    "name": "SYNTHETIC SECRETARIAL SERVICES LIMITED",
    "address": "SUITE 800, 8/F, SECRETARIAL TOWER, 88 QUEENSWAY, ADMIRALTY, HK",
    "tel": "2987 6543",
    "fax": "2987 6500",
    "email": "filings@synthetic-secretarial.example.com.hk",
    "reference": "SH-2026-AR-001",
}

NAR1_METADATA: dict[str, Any] = {
    "return": {"made_up_date": MADE_UP_DATE, "fs_period_start": None, "fs_period_end": None},
    "member_particulars_delivery": "schedule_1",
    "presentor": PRESENTOR,
    "signature": {"name": "CHAN TAI MAN", "date": "2026-07-30", "capacity": "director"},
    "consent": None,
}

# The CJK scenario: the same return with Traditional-Chinese values in the
# bilingual boxes — the company name, both officers' Chinese names and the
# first member's Chinese address.
NAR1_CJK_STATE: dict[str, Any] = {
    "company": COMPANY,
    "officers": [SECRETARY, DIRECTOR],
    "shareholders": MEMBERS,
    "changes": [],
}

NAR1_EN_STATE: dict[str, Any] = {
    "company": COMPANY_NO_ZH,
    "officers": [SECRETARY, {**DIRECTOR, "name_zh": None}],
    "shareholders": [{**MEMBER, "name_zh": None} for MEMBER in MEMBERS],
    "changes": [],
}

# The Simplified variant: one character (乐 for 樂) outside Noto Sans HK's cmap.
NAR1_SIMPLIFIED_STATE: dict[str, Any] = {
    "company": COMPANY_NO_ZH,
    "officers": [{**DIRECTOR, "name_zh": "李文乐"}],
    "shareholders": [],
    "changes": [],
}

# --- The ND2A scenarios' resolved state -------------------------------------

ND2A_COMPANY: dict[str, Any] = {
    "br_number": "76543210",
    "type": "private",
    "name": {"name_en": "SYNTHETIC HARBOUR TRADING LIMITED"},
    "registered_office": {},
    "share_capital": {"classes": [], "total": {}},
}

ND2A_METADATA: dict[str, Any] = {
    "return": {},
    "member_particulars_delivery": None,
    "presentor": PRESENTOR,
    "signature": {"name": "CHAN TAI MAN", "date": "2026-08-01", "capacity": "company_secretary"},
    "consent": None,
}

APPOINTMENT = {
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

CESSATION = {
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


def _change(event_id: str, kind: str, officer: Mapping[str, Any], effective: str) -> dict[str, Any]:
    body = "appointment" if kind.endswith("appointment") else "cessation"
    return {
        "event_id": event_id,
        "type": kind,
        "effective_date": effective,
        "payload": {body: {"officer": dict(officer)}},
    }


ND2A_APPOINTMENT_STATE: dict[str, Any] = {
    "company": ND2A_COMPANY,
    "officers": [],
    "shareholders": [],
    "changes": [_change("e1", "director_appointment", APPOINTMENT, "2026-08-01")],
}

ND2A_CESSATION_STATE: dict[str, Any] = {
    "company": ND2A_COMPANY,
    "officers": [],
    "shareholders": [],
    "changes": [_change("e2", "director_cessation", CESSATION, "2026-07-31")],
}

# --- The overflow scenarios -------------------------------------------------

# The spike's stage-5 shape: a second natural director (Continuation Sheet C)
# and a third member (a second Schedule 1 sheet).
DIRECTOR_2: dict[str, Any] = {
    **DIRECTOR,
    "person_id": "dir-2",
    "name_zh": "黃小明",
    "name_en": "WONG Siu Ming",
    "surname_en": "WONG",
    "other_names_en": "SIU MING",
    "hkid_partial": "B456",
    "email": "sm.wong@example.com",
    "valid_from": "2021-01-01",
}

MEMBER_3: dict[str, Any] = {
    **MEMBERS[0],
    "person_id": "mem-3",
    "name_zh": "黃小明",
    "name_en": "WONG SIU MING",
    "surname_en": "WONG",
    "other_names_en": "SIU MING",
    "shares_held": 2000,
    "address": {
        "flat": "FLAT B, 8/F",
        "building": "PEAK VIEW MANSION",
        "street": "10 PEAK ROAD",
        "district": "THE PEAK",
        "country": "HONG KONG",
    },
}

NAR1_OVERFLOW_STATE: dict[str, Any] = {
    "company": COMPANY,
    "officers": [SECRETARY, DIRECTOR, DIRECTOR_2],
    "shareholders": [*MEMBERS, MEMBER_3],
    "changes": [],
}

BODY_APPOINTMENT: dict[str, Any] = {
    "role": "director",
    "capacity": ["director"],
    "appointment_date": "2026-08-03",
    "existing_director_at_appointment": False,
    "name_en": "SYNTHETIC HOLDINGS LIMITED",
    "br_number": "99887766",
    "registered_office_address": {
        "flat": "ROOM 1",
        "building": "HARBOUR BUILDING",
        "street": "2 HARBOUR ROAD",
        "district": "WAN CHAI",
        "country": "HONG KONG",
    },
}

# Two cessations and two natural appointees make Continuation Sheets A and B
# (and a second PI-ND2A sheet); the second body-corporate appointee makes
# Sheet C.
ND2A_CONTINUATION_STATE: dict[str, Any] = {
    "company": ND2A_COMPANY,
    "officers": [],
    "shareholders": [],
    "changes": [
        _change("e1", "director_cessation", CESSATION, "2026-07-30"),
        _change(
            "e2",
            "director_cessation",
            {**CESSATION, "name_zh": "丁", "surname_en": "TING", "hkid_partial": "C789"},
            "2026-07-31",
        ),
        _change("e3", "director_appointment", APPOINTMENT, "2026-08-01"),
        _change(
            "e4",
            "director_appointment",
            {**APPOINTMENT, "name_zh": "戊", "surname_en": "NG", "hkid_partial": "E567"},
            "2026-08-02",
        ),
        _change("e5", "director_appointment", BODY_APPOINTMENT, "2026-08-03"),
        _change(
            "e6",
            "director_appointment",
            {**BODY_APPOINTMENT, "name_en": "SECOND HOLDINGS LIMITED", "br_number": "11223344"},
            "2026-08-04",
        ),
    ],
}


# --- The scenario table ------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One filled package: its plan, bytes and the widgets they are about."""

    name: str
    form: FormCode
    plan: FillPlan
    pdf: bytes

    @property
    def reader(self) -> PdfReader:
        import io

        return PdfReader(io.BytesIO(self.pdf))


def _plan(form: FormCode, state: Mapping[str, Any], metadata: Mapping[str, Any]) -> FillPlan:
    return fill_plan_for(
        form_type=form.value,
        resolved_state=state,
        filing_metadata=metadata,
        private_company_statement=True if form is FormCode.NAR1 else None,
    )


def _build(form: FormCode, state: Mapping[str, Any], metadata: Mapping[str, Any]) -> Scenario:
    plan = _plan(form, state, metadata)
    registry = load_form_registry(form)
    pdf = fill_template(
        template_path(form),
        plan,
        header_fields=page_header_fields(registry),
        sheet_pages=sheet_pages(registry),
    )
    return Scenario(name="", form=form, plan=plan, pdf=pdf)


SCENARIO_STATES: dict[str, tuple[FormCode, Mapping[str, Any], Mapping[str, Any]]] = {
    "nar1_no_change": (FormCode.NAR1, NAR1_EN_STATE, NAR1_METADATA),
    "nar1_cjk": (FormCode.NAR1, NAR1_CJK_STATE, NAR1_METADATA),
    "nar1_overflow": (FormCode.NAR1, NAR1_OVERFLOW_STATE, NAR1_METADATA),
    "nd2a_appointment": (FormCode.ND2A, ND2A_APPOINTMENT_STATE, ND2A_METADATA),
    "nd2a_cessation": (FormCode.ND2A, ND2A_CESSATION_STATE, ND2A_METADATA),
    "nd2a_continuation": (FormCode.ND2A, ND2A_CONTINUATION_STATE, ND2A_METADATA),
}


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    """Every scenario filled once (the font is parsed once per process)."""
    filled: dict[str, Scenario] = {}
    for name, (form, state, metadata) in SCENARIO_STATES.items():
        scenario = _build(form, state, metadata)
        filled[name] = Scenario(name=name, form=form, plan=scenario.plan, pdf=scenario.pdf)
    return filled


@pytest.fixture(scope="module")
def nar1(scenarios: dict[str, Scenario]) -> Scenario:
    return scenarios["nar1_cjk"]


# --- Reading a filled PDF back ----------------------------------------------


def widgets_of(reader: PdfReader) -> dict[str, list[tuple[int, DictionaryObject]]]:
    """Every widget annotation, keyed by its fully-qualified field name."""
    index: dict[str, list[tuple[int, DictionaryObject]]] = {}
    for page_index, page in enumerate(reader.pages):
        annots = page.get("/Annots", [])
        for annot in annots.get_object() if annots else []:
            obj = annot.get_object()
            if obj.get("/Subtype") != "/Widget":
                continue
            index.setdefault(_qualified_name(obj), []).append((page_index, obj))
    return index


def _qualified_name(obj: DictionaryObject) -> str:
    parts: list[str] = []
    node = obj
    while True:
        name = node.get("/T")
        if name:
            parts.append(str(name))
        parent = node.get("/Parent")
        if parent is None:
            break
        node = parent.get_object()
    return ".".join(reversed(parts))


def stored_values(reader: PdfReader) -> dict[str, str]:
    """``{field name: /V}`` for every field, as pypdf reads the form."""
    return {
        name: None if field.get("/V") is None else str(field.get("/V"))
        for name, field in (reader.get_fields() or {}).items()
    }


def strike_option(obj: DictionaryObject) -> str:
    """The field's non-blank ``/Opt`` entry (the form's own dash option)."""
    node: DictionaryObject | None = obj
    while node is not None:
        options = node.get("/Opt")
        if options:
            return next(str(entry) for entry in options if str(entry).strip())
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    raise AssertionError("no /Opt on the field or its parents")


def has_options(obj: DictionaryObject) -> bool:
    """Whether the widget belongs to a choice (dropdown) field."""
    node: DictionaryObject | None = obj
    while node is not None:
        if node.get("/Opt"):
            return True
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return False


def on_state(obj: DictionaryObject) -> str:
    """The ticked state the widget's ``/AP /N`` names."""
    states = obj["/AP"].get_object()["/N"]
    return next(str(key) for key in states if str(key) != "/Off")


def expected_value(
    widgets: Mapping[str, list[tuple[int, DictionaryObject]]], field: str, value: object
) -> str:
    """What the filled form must hold for one plan value."""
    if value is STRIKE:
        return strike_option(widgets[field][0][1])
    if isinstance(value, bool):
        return on_state(widgets[field][0][1]) if value else "/Off"
    return str(value)


def printed_fields(scenario: Scenario) -> dict[str, object]:
    """The plan's writes as the package prints them, sheets' copies included.

    The assembly's convention restated here, independently of the writer: the
    main form's writes plus each used sheet's **first copy** print in place (the
    template's own pages), a sheet page whose only planned writes are its header
    boxes prints none of them (an unused sheet stays blank), and a sheet's later
    copies are duplicates the writer flattens — their values are read back from
    the rendered page, not from any field, so they are not part of this set.
    """
    registry = load_form_registry(scenario.form)
    headers = page_header_fields(registry)
    sheets = sheet_pages(registry)
    written = dict(scenario.plan.fields)
    seen: set[str] = set()
    for copy in scenario.plan.sheets:
        if copy.sheet in seen:
            continue
        seen.add(copy.sheet)
        written.update(copy.fields)
    by_page: dict[int, list[str]] = {}
    for field in written:
        by_page.setdefault(registry.geometry[field].page, []).append(field)
    printed = dict(written)
    for page, fields in by_page.items():
        if page in sheets and all(field in headers for field in fields):
            for field in fields:
                del printed[field]
    return printed


def appearance_text(obj: DictionaryObject) -> str:
    """The characters a widget's appearance stream draws, in order."""
    appearance = obj.get("/AP")
    if appearance is None or "/N" not in appearance:
        return ""
    return str(appearance["/N"].get_object().get_data())


def _render(scenario: Scenario, page_index: int) -> Any:
    document = pdfium.PdfDocument(scenario.pdf)
    document.init_forms()
    return document[page_index].render(scale=2.0).to_pil().convert("L")


def _line_coverage(
    image: Any, page_height: float, rect: Sequence[float], scale: float = 2.0
) -> float:
    """The fraction of columns in the rect's mid-band carrying a dark pixel.

    A continuous strike line covers ~100% of its term's columns; a surviving
    term covers only its glyphs — the spike's own pixel measure.
    """
    left, bottom, right, top = rect
    y_mid = (bottom + top) / 2
    px_left, px_right = int(left * scale) + 3, int(right * scale) - 3
    px_y = int((page_height - y_mid) * scale)
    band = image.crop((px_left, px_y - 3, px_right, px_y + 4))
    width, height = band.size
    pixels = band.load()
    covered = sum(
        1 for x in range(width) if any(pixels[x, y] < 160 for y in range(height))
    )
    return covered / width


def _ink(image: Any, page_height: float, rect: Sequence[float], scale: float = 2.0) -> float:
    """The fraction of a widget rect's pixels carrying ink.

    A drawn tick covers a good part of its box; the box's own pre-printed
    outline lies on the rect's edge, which the inset keeps out — the measure
    ``_line_coverage`` cannot make for a tick.
    """
    left, bottom, right, top = rect
    box = image.crop(
        (
            int(left * scale) + 4,
            int((page_height - top) * scale) + 4,
            int(right * scale) - 4,
            int((page_height - bottom) * scale) - 4,
        )
    )
    width, height = box.size
    pixels = box.load()
    dark = sum(1 for x in range(width) for y in range(height) if pixels[x, y] < 160)
    return dark / (width * height)


# --- Round trip, ticks, appearance ------------------------------------------


@pytest.mark.parametrize("name", list(SCENARIO_STATES))
def test_every_written_widget_holds_its_plan_value(
    scenarios: dict[str, Scenario], name: str
) -> None:
    """The plan's value, field by field, after a save and reload."""
    scenario = scenarios[name]
    stored = stored_values(scenario.reader)
    widgets = widgets_of(scenario.reader)
    for field, value in printed_fields(scenario).items():
        assert field in stored, f"{field} is not in the filled form"
        expected = expected_value(widgets, field, value)
        assert stored[field] == expected, f"{field}: {stored[field]!r} != {expected!r}"


@pytest.mark.parametrize("name", list(SCENARIO_STATES))
def test_ticked_checkboxes_show_the_widgets_own_on_state(
    scenarios: dict[str, Scenario], name: str
) -> None:
    scenario = scenarios[name]
    widgets = widgets_of(scenario.reader)
    ticks = [field for field, value in scenario.plan.fields.items() if value is True]
    assert ticks, "every scenario ticks at least one box"
    for field in ticks:
        for _page, obj in widgets[field]:
            assert str(obj.get("/AS")) == on_state(obj)


def test_a_tick_on_a_stateless_widget_leaves_it_as_it_stands(tmp_path: Path) -> None:
    """A widget with no appearance states has nothing to tick.

    The pinned templates all carry states, so this pins the latent branch: the
    plan asks for a tick and the widget cannot show one — it is left exactly as
    it stands rather than written ``/Off`` behind the plan's back.
    """
    template = tmp_path / "no-state.pdf"
    writer = PdfWriter(clone_from=str(template_path(FormCode.ND2A)))
    stripped = 0
    for page in writer.pages:
        annots = page.get("/Annots", [])
        for annot in annots.get_object() if annots else []:
            obj = annot.get_object()
            if obj.get("/Subtype") != "/Widget":
                continue
            if _qualified_name(obj) == "cb_2_P.2":
                obj.pop("/AP", None)
                stripped += 1
    assert stripped == 1, "the checkbox the appointment scenario ticks"
    with open(template, "wb") as handle:
        writer.write(handle)

    plan = _plan(FormCode.ND2A, ND2A_APPOINTMENT_STATE, ND2A_METADATA)
    assert plan.fields["cb_2_P.2"] is True
    registry = load_form_registry(FormCode.ND2A)
    pdf = fill_template(
        template,
        plan,
        header_fields=page_header_fields(registry),
        sheet_pages=sheet_pages(registry),
    )

    _page, obj = widgets_of(PdfReader(BytesIO(pdf)))["cb_2_P.2"][0]
    assert obj.get("/AP") is None  # the state was never put back
    assert str(obj.get("/AS")) == "/Off"  # and the box was not written on
    assert str(obj.get("/V")) == "None"  # the field's value stays unset


@pytest.mark.parametrize("name", list(SCENARIO_STATES))
def test_latin_appearances_draw_their_values(
    scenarios: dict[str, Scenario], name: str
) -> None:
    """Each written Latin text widget's ``/AP`` stream draws its own value."""
    scenario = scenarios[name]
    widgets = widgets_of(scenario.reader)
    checked = 0
    for field, value in printed_fields(scenario).items():
        if not isinstance(value, str) or value is STRIKE or any(ord(c) > 0xFF for c in value):
            continue
        _page, obj = widgets[field][0]
        if str(obj.get("/FT") or "") != "/Tx":
            continue  # choice widgets carry no appearance of ours
        data = appearance_text(obj)
        drawn = "".join(character for character in data if character.isprintable())
        for character in value:
            assert character in drawn, f"{field}: appearance does not draw {character!r}"
        checked += 1
    assert checked > 0


def test_unwritten_widgets_are_untouched(scenarios: dict[str, Scenario]) -> None:
    """The blank template's values survive where the plan wrote nothing."""
    scenario = scenarios["nar1_no_change"]
    template = stored_values(PdfReader(str(template_path(FormCode.NAR1))))
    filled = stored_values(scenario.reader)
    written = set(printed_fields(scenario))
    # The CJK/Nil boxes the plan writes are exempt; everything else is equal.
    differing = {
        name for name in template if name not in written and template[name] != filled[name]
    }
    assert differing == set()


# --- Strikes -----------------------------------------------------------------


def test_strikes_hide_the_widget_and_draw_the_line(nar1: Scenario) -> None:
    """The NAR1 signature capacity: one line struck, the other kept."""
    widgets = widgets_of(nar1.reader)
    struck = "Dropdown_2_P.8"  # capacity = director
    survivor = "Dropdown_1_P.8"
    assert nar1.plan.fields[struck] is STRIKE
    assert survivor not in nar1.plan.fields

    image = _render(nar1, 7)
    page_height = float(PdfReader(__import__("io").BytesIO(nar1.pdf)).pages[7].mediabox.height)
    struck_rect = [float(value) for value in widgets[struck][0][1]["/Rect"]]
    survivor_rect = [float(value) for value in widgets[survivor][0][1]["/Rect"]]
    assert _line_coverage(image, page_height, struck_rect) > 0.9
    assert _line_coverage(image, page_height, survivor_rect) < 0.8

    _page, obj = widgets[struck][0]
    assert int(obj["/F"]) == 2  # hidden: no combo chrome over the term
    assert "/AP" not in obj  # its rendering is the drawn line, not an appearance


def test_a_strike_keeps_the_semantic_value(scenarios: dict[str, Scenario]) -> None:
    """``/V`` keeps the field's own strike option, so the value stays readable."""
    scenario = scenarios["nd2a_appointment"]
    widgets = widgets_of(scenario.reader)
    stored = stored_values(scenario.reader)
    strikes = [field for field, value in scenario.plan.fields.items() if value is STRIKE]
    assert strikes
    for field in strikes:
        expected = strike_option(widgets[field][0][1])
        assert stored[field] == expected
        for _page, obj in widgets[field]:
            assert "/AP" not in obj


@pytest.mark.parametrize("name", list(SCENARIO_STATES))
def test_choice_widgets_the_plan_did_not_write_end_hidden(
    scenarios: dict[str, Scenario], name: str
) -> None:
    """No combo chrome anywhere: every delete-whichever widget the plan leaves alone.

    Form renderers draw a ▼ over *any* dropdown once the file asks for
    appearances to be regenerated — including a group the plan writes nothing
    at all into (an ND2A consent-signatory line with no body-corporate
    appointment, whose ▼ would print on the artifact the client signs). The
    spike hides every choice widget it names; the package must carry none of
    them, so the ones the plan does not strike are hidden too, with their
    value in ``/V`` and **no** strike line drawn (the pixels are the strike
    test's).
    """
    scenario = scenarios[name]
    printed = printed_fields(scenario)
    widgets = widgets_of(scenario.reader)
    checked = 0
    for field, entries in widgets.items():
        if field in printed:
            continue  # the plan wrote it (a struck line gets its own treatment)
        for _page, obj in entries:
            if not has_options(obj):
                continue  # not a choice widget
            checked += 1
            assert int(obj.get("/F") or 0) & 2 == 2, f"{field} still shows chrome"
            assert "/AP" not in obj, f"{field} still carries an appearance"
    assert checked > 0, "this scenario has choice widgets the plan leaves alone"


# --- CJK ---------------------------------------------------------------------


def test_cjk_values_ride_the_embedded_overlay(nar1: Scenario) -> None:
    """The font is embedded, the value is drawn on the page, the data stays.

    The widget keeps its ``/V`` and its ``/DA`` points at the embedded font,
    but its appearance is gone: the overlay page merged into the content
    carries the rendering (pypdf's own appearance for a CJK value is literal
    ``???``, and the template's declared face is not embedded).
    """
    reader = nar1.reader
    widgets = widgets_of(reader)
    stored = stored_values(reader)
    drawn = [
        (field, str(value))
        for field, value in printed_fields(nar1).items()
        if isinstance(value, str) and any(ord(c) > 0xFF for c in value)
    ]
    assert drawn, "the CJK scenario writes Chinese values"
    # The overlay's text lands in the page's own text layer (the merge writes
    # the drawn string into the page content).
    text = [page.extract_text() or "" for page in reader.pages]
    for field, value in drawn:
        page_index, obj = widgets[field][0]
        assert "/FCJK" in str(obj.get("/DA"))
        assert int(obj["/F"]) == 2  # hidden: the overlay draws it, not the widget
        assert "/AP" not in obj
        assert stored[field] == value  # the data stays in the form
        assert value in text[page_index]

    # The font is embedded as a Type0/Identity-H descendant with its file.
    resources = reader.trailer["/Root"]["/AcroForm"]["/DR"]["/Font"]
    font = resources["/FCJK"].get_object()
    assert str(font["/Subtype"]) == "/Type0"
    assert str(font["/Encoding"]) == "/Identity-H"
    descendant = font["/DescendantFonts"][0].get_object()
    assert str(descendant["/Subtype"]) == "/CIDFontType2"
    descriptor = descendant["/FontDescriptor"].get_object()
    assert "/FontFile2" in descriptor


def test_cjk_renders_on_the_page(nar1: Scenario) -> None:
    """pdfium's own renderer — the layer an appearance-stream check cannot see."""
    document = pdfium.PdfDocument(nar1.pdf)
    document.init_forms()
    pages = [document[index].get_textpage().get_text_range() for index in range(len(document))]
    assert "合成海港貿易有限公司" in pages[0]  # item 1, the bilingual name box
    assert "陳大文" in pages[4]  # the director's Chinese name (13A)
    assert "海港集團有限公司" in pages[8]  # Schedule 1's member 2
    assert "花園道1號" not in pages[8]  # the English scenario's street, not this one


# --- Page selection and the package -----------------------------------------


def test_the_package_is_the_whole_template(scenarios: dict[str, Scenario]) -> None:
    assert len(scenarios["nar1_no_change"].reader.pages) == 27
    assert len(scenarios["nd2a_appointment"].reader.pages) == 17


def test_every_package_fits_the_upload_cap(scenarios: dict[str, Scenario]) -> None:
    """A released package is stored through the records registry: the 25 MiB cap.

    The overflow packages are the ones that failed it: each flattened duplicate
    embedded the whole ~12 MB face, so a NAR1 overflow package was 26,367,955
    bytes and an ND2A continuation one 26,361,048 — over ``MAX_UPLOAD_BYTES``
    (26,214,400), which ``document_upload``'s validation refuses, so
    ``send_for_signature`` could not release such a draft. The writer now
    embeds a **subset** of the face (fontTools) covering exactly the package's
    characters, and every package lands far inside the cap — the second bound
    is the subsetting's own proof, not a fitting margin.
    """
    sizes = {name: len(scenario.pdf) for name, scenario in scenarios.items()}
    for name, size in sizes.items():
        assert size < MAX_UPLOAD_BYTES, f"{name}: {size} bytes"
    for name in ("nar1_overflow", "nd2a_continuation"):
        assert sizes[name] < 4 * 1024 * 1024, f"{name}: {sizes[name]} bytes"


def test_unused_sheets_print_no_header(scenarios: dict[str, Scenario]) -> None:
    """The specimen convention: present but blank, date and BRN left empty."""
    nar1_values = stored_values(scenarios["nar1_no_change"].reader)
    for page in (10, 11, 12, 13, 14, 15):  # Schedule 2 and Continuation Sheets A-E
        brn = f"fill_4_P.{page}"
        triple = [f"fill_{index}_P.{page}" for index in (1, 2, 3)]
        assert nar1_values.get(brn) is None, f"page {page} prints a BRN header"
        assert all(nar1_values.get(field) is None for field in triple)
    # The used sheet — Schedule 1 — carries both, as the specimens show.
    assert nar1_values["fill_4_P.9"] == "76543210"
    assert [nar1_values[f"fill_{index}_P.9"] for index in (1, 2, 3)] == ["30", "06", "2026"]

    nd2a_values = stored_values(scenarios["nd2a_cessation"].reader)
    for page in (4, 5, 6):  # Continuation Sheets A, B, C
        assert nd2a_values.get(f"fill_1_P.{page}") is None
    # No appointee: the PI-ND2A sheet is unused too, so its header stays empty.
    assert nd2a_values.get("fill_1_P.7") is None
    assert nd2a_values["fill_1_P.3"] == "76543210"  # the main pages always carry it


def test_the_pi_sheet_used_by_an_appointment_carries_its_header() -> None:
    scenario = _build(FormCode.ND2A, ND2A_APPOINTMENT_STATE, ND2A_METADATA)
    values = stored_values(scenario.reader)
    assert values["fill_1_P.7"] == "76543210"
    assert values["fill_2_P.7"] == "李文樂"


# --- The continuation sheets and the assembled package -----------------------


def rendered_texts(scenario: Scenario) -> list[str]:
    """Every page's text, the way a reader's renderer sees it (pdfium)."""
    document = pdfium.PdfDocument(scenario.pdf)
    document.init_forms()
    return [document[index].get_textpage().get_text_range() for index in range(len(document))]


def widgets_on_page(reader: PdfReader, page_index: int) -> list[DictionaryObject]:
    """The widget annotations one page of the package carries."""
    annots = reader.pages[page_index].get("/Annots")
    resolved = annots.get_object() if hasattr(annots, "get_object") else (annots or [])
    return [a.get_object() for a in resolved if a.get_object().get("/Subtype") == "/Widget"]


def page_used(reader: PdfReader, texts: list[str], page_index: int) -> bool:
    """The spike's measure: a sheet page is used if its BRN renders or a widget holds a value."""
    if "76543210" in texts[page_index]:
        return True
    for obj in widgets_on_page(reader, page_index):
        value = obj.get("/V")
        if value is None and obj.get("/Parent"):
            value = obj["/Parent"].get_object().get("/V")
        if value is not None and str(value).strip() not in {"", "/Off"}:
            return True
    return False


def test_the_overflow_package_follows_the_spikes_structure(
    scenarios: dict[str, Scenario],
) -> None:
    """The spike's stage-5 arithmetic: main 1-8, two Schedule 1 sheets, the
    unused sheets kept blank, CS-C's own page, then the guidance notes."""
    scenario = scenarios["nar1_overflow"]
    assert len(scenario.reader.pages) == 28
    texts = rendered_texts(scenario)
    assert "附表一 Schedule 1" in texts[8][:120]
    assert "附表一 Schedule 1" in texts[9][:120]  # the duplicate, after the page it copies
    assert "附表二 Schedule 2" in texts[10][:120]
    assert "續頁 A Continuation Sheet A" in texts[11][:120]
    assert "續頁 B Continuation Sheet B" in texts[12][:120]
    assert "續頁 C Continuation Sheet C" in texts[13][:120]
    assert "續頁 D Continuation Sheet D" in texts[14][:120]
    assert "續頁 E Continuation Sheet E" in texts[15][:120]
    assert "填表須知" in texts[16]  # the guidance notes close the package


def test_the_overflow_packages_used_sheets_are_the_roster(
    scenarios: dict[str, Scenario],
) -> None:
    """8A states the used sheets, and the package prints them (the consistency proof)."""
    scenario = scenarios["nar1_overflow"]
    reader = scenario.reader
    texts = rendered_texts(scenario)
    values = stored_values(reader)
    # The plan's roster: one Schedule 1 sheet and one Continuation Sheet C.
    assert [copy.sheet for copy in scenario.plan.sheets] == ["s1", "s1", "cs-c"]
    # 8A's counts are the used sheets per type.
    assert [values[f"fill_{index}_P.8"] for index in range(4, 11)] == [
        "0",  # 續頁 A
        "0",  # 續頁 B
        "1",  # 續頁 C: the second natural director
        "0",  # 續頁 D
        "0",  # 續頁 E
        "2",  # Schedule 1: three members on two sheets
        "0",  # Schedule 2: not this build
    ]
    used = {"s1": [8, 9], "s2": [10], "cs-a": [11], "cs-b": [12], "cs-c": [13],
            "cs-d": [14], "cs-e": [15]}
    counts = {"fill_4_P.8": "cs-a", "fill_5_P.8": "cs-b", "fill_6_P.8": "cs-c",
              "fill_7_P.8": "cs-d", "fill_8_P.8": "cs-e", "fill_9_P.8": "s1",
              "fill_10_P.8": "s2"}
    for field, sheet in counts.items():
        filed = sum(1 for index in used[sheet] if page_used(reader, texts, index))
        assert int(values[field]) == filed, field


def test_a_used_sheet_carries_both_headers_and_an_unused_one_neither(
    scenarios: dict[str, Scenario],
) -> None:
    """The specimens' convention, read off the assembled package."""
    scenario = scenarios["nar1_overflow"]
    reader = scenario.reader
    texts = rendered_texts(scenario)
    values = stored_values(reader)
    # Schedule 1's template page: live fields; its duplicate: drawn into the text.
    assert values["fill_4_P.9"] == "76543210"
    assert [values[f"fill_{index}_P.9"] for index in (1, 2, 3)] == ["30", "06", "2026"]
    assert "76543210" in texts[9]
    assert "2026" in texts[9]
    # Continuation Sheet C's own page keeps its fields.
    assert values["fill_4_P.13"] == "76543210"
    assert values["fill_6_P.13"] == "黃小明"
    # The unused sheets (S2, CS-A, CS-B, CS-D, CS-E) print none of either.
    for page_index in (10, 11, 12, 14, 15):
        assert not page_used(reader, texts, page_index), f"page {page_index + 1} is used"
        assert "76543210" not in texts[page_index]


def test_the_flattened_copy_prints_its_data(scenarios: dict[str, Scenario]) -> None:
    """The duplicate has no widgets, so its values ride the drawn page (pdfium)."""
    scenario = scenarios["nar1_overflow"]
    texts = rendered_texts(scenario)
    for needle in ("黃小明", "WONG", "SIU MING", "2,000", "PEAK VIEW MANSION", "10 PEAK ROAD"):
        assert needle in texts[9], needle
    # The template's own page still holds its member as a field.
    assert stored_values(scenario.reader)["fill_7_P.9"] == "陳大文"


def test_the_schedule_footers_number_each_sheet(scenarios: dict[str, Scenario]) -> None:
    """Each Schedule 1 copy carries its own page-number pair, both boxes the same."""
    scenario = scenarios["nar1_overflow"]
    values = stored_values(scenario.reader)
    texts = rendered_texts(scenario)
    assert [values["fill_29_P.9"], values["fill_30_P.9"]] == ["1", "1"]  # the template's page
    assert texts[9].rstrip().endswith("2 2")  # the flattened copy, drawn into the text


def test_main_fields_round_trip_through_the_assembly(scenarios: dict[str, Scenario]) -> None:
    """The spike's own round trip: the template's pages stay live form fields."""
    scenario = scenarios["nar1_overflow"]
    values = stored_values(scenario.reader)
    assert values["fill_1_P.1"] == "76543210"
    assert "合成海港貿易有限公司" in str(values["fill_2_P.1"])
    assert values["fill_7_P.13"] == "WONG"  # CS-C: the second natural director
    assert values["fill_19_P.13"] == "B456"


def test_a_duplicate_sheet_carries_no_widgets(scenarios: dict[str, Scenario]) -> None:
    """The copy's widgets are stripped before assembly, so nothing collides."""
    scenario = scenarios["nar1_overflow"]
    reader = scenario.reader
    assert widgets_on_page(reader, 8), "the template's own Schedule 1 page stays live"
    assert widgets_on_page(reader, 9) == [], "the duplicate must be widget-free"
    # The form's field set is the template's: no field of a copy arrives twice.
    template = stored_values(PdfReader(str(template_path(FormCode.NAR1))))
    assert set(stored_values(reader)) == set(template)


def test_the_nd2a_continuation_package(scenarios: dict[str, Scenario]) -> None:
    """ND2A's own sheets: A, B, C in place, and a second PI sheet flattened."""
    scenario = scenarios["nd2a_continuation"]
    reader = scenario.reader
    values = stored_values(reader)
    texts = rendered_texts(scenario)
    assert len(reader.pages) == 18  # the template plus one duplicate PI-ND2A sheet
    assert [copy.sheet for copy in scenario.plan.sheets] == ["a-2a", "b-3", "c-4", "pi", "pi"]
    # Continuation Sheets A/B/C are the template's own pages, filled in place.
    assert values["fill_3_P.4"] == "丁"  # the second cessation's name
    assert values["fill_3_P.5"] == "戊"  # the second natural appointee's name
    assert values["fill_4_P.6"] == "SECOND HOLDINGS LIMITED"  # the second body corporate
    # The unnumbered grid counts each sheet: A1 B1 C1, two PI-ND2A sheets.
    grid = ("fill_18_P.3", "fill_19_P.3", "fill_20_P.3", "fill_21_P.3")
    assert [values[name] for name in grid] == ["1", "1", "1", "2"]
    # The first PI sheet is the template's page 7; the second is the flattened
    # copy the assembly put after it.
    assert widgets_on_page(reader, 6)
    assert widgets_on_page(reader, 7) == []
    assert "李文樂" in texts[6]
    assert "戊" in texts[7]


def test_a_flattened_copy_draws_its_ticks(scenarios: dict[str, Scenario]) -> None:
    """A tick on a duplicate is drawn: the widget's own on-state appearance.

    The copy has no widget left to render the tick, so the writer draws the
    appearance stream the widget would have shown — checked as pixels, because
    that layer is the drawn page rather than any field.
    """
    scenario = scenarios["nd2a_continuation"]
    geometry = load_form_registry(FormCode.ND2A).geometry["cb_2_P.7"]
    rect = geometry.rect
    page_height = float(scenario.reader.pages[6].mediabox.height)
    copy_image = _render(scenario, 7)  # the flattened second PI sheet
    template_page = _render(scenario, 6)  # the first, live PI sheet
    assert _ink(copy_image, page_height, rect) > 0.05
    assert _ink(copy_image, page_height, rect) > 0.5 * _ink(template_page, page_height, rect)


# --- The refusals ------------------------------------------------------------


def test_a_missing_font_refuses_loudly(tmp_path: Path) -> None:
    plan = _plan(FormCode.ND2A, ND2A_APPOINTMENT_STATE, ND2A_METADATA)
    with pytest.raises(DraftFontUnavailableError):
        fill_template(
            template_path(FormCode.ND2A),
            plan,
            header_fields=page_header_fields(load_form_registry(FormCode.ND2A)),
            sheet_pages=sheet_pages(load_form_registry(FormCode.ND2A)),
            font_path=tmp_path / "absent.ttf",
        )


def test_the_configured_font_path_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``AICS_DRAFT_FONT_PATH`` overrides the packaged asset, and only it."""
    from app.core.config import settings

    assert draft_font_path() == packaged_font_path()
    monkeypatch.setattr(settings, "draft_font_path", str(tmp_path / "elsewhere.ttf"))
    assert draft_font_path() == tmp_path / "elsewhere.ttf"


def test_a_character_the_font_cannot_render_refuses() -> None:
    """Simplified Chinese has no glyph in Noto Sans HK: refused, not tofu."""
    plan = _plan(FormCode.NAR1, NAR1_SIMPLIFIED_STATE, NAR1_METADATA)
    with pytest.raises(GlyphUnavailableError) as caught:
        fill_template(
            template_path(FormCode.NAR1),
            plan,
            header_fields=page_header_fields(load_form_registry(FormCode.NAR1)),
            sheet_pages=sheet_pages(load_form_registry(FormCode.NAR1)),
        )
    assert caught.value.characters == "乐"


# --- DoD §9 item 22's deterministic half -------------------------------------


def test_item22_deterministic_prefill_agreement(scenarios: dict[str, Scenario]) -> None:
    """Every planned value arrives, over this slice's six scenarios.

    The measurement the exit gate records: fields written, widget instances
    they reach (a name can carry several widgets — the repeated headers, the
    dual-rect strike pairs), and the agreement between the plan and the filled
    form's own values. The set is the representative one the slice pinned: the
    four everyday NAR1/ND2A shapes plus the two overflow packages (the
    continuation sheets and a flattened duplicate each), not the Phase 0
    corpus; a flattened copy's own values are drawn, not stored, so they are
    outside this field-by-field read and the overflow tests probe them.

    The counts are pinned **exactly**, not as floors: the plan line and this
    test must move together, so a registry or writer change that drops a box
    from the package fails here rather than passing silently under a ``>``.
    """
    scenarios_checked = 0
    fields_checked = 0
    widgets_checked = 0
    matched = 0
    for scenario in scenarios.values():
        stored = stored_values(scenario.reader)
        widgets = widgets_of(scenario.reader)
        scenarios_checked += 1
        for field, value in printed_fields(scenario).items():
            expected = expected_value(widgets, field, value)
            fields_checked += 1
            widgets_checked += len(widgets[field])
            if stored.get(field) == expected:
                matched += 1

    assert (scenarios_checked, fields_checked, widgets_checked, matched) == (
        6,
        476,
        482,
        476,
    ), "the item-22 measurement moved: update the plan's exit-gate line with it"
    assert matched == fields_checked, "every printed value arrives in the filled form"
