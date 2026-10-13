"""Extractor tests: form-registry promotion and the digital-shape reads
(scope §2.1, §7; contract §2.7).

Database-free. The input PDFs are generated at test time by
``tests.pdf_fixtures`` from the pinned NAR1 template: the AcroForm shape
writes values through the template's own widgets, the text-layer shape
draws them into the named boxes as page text (the shape a flattened or
CR-printed filing has), and a composed package inserts sheets before the
template pages after them. The registry template stays read-only and no
binary is committed.

The scan shape is routed to the OCR reader (``tests/test_pdf_ocr.py``,
which needs the Tesseract binary). What this file pins is the refusal: a
page that is not the pinned form is recognised and read no further, engine
or no engine.
"""

from dataclasses import replace
from datetime import date

import pytest

from app.adapters.forms.registry import FormRegistry, load_form_registry, template_path
from app.adapters.pdf import extractor
from app.adapters.pdf.extractor import ExtractionResult, extract
from app.domain.prior_filing import (
    FormCode,
    InputShape,
    ReadOrigin,
    ReadSignal,
    individual_paths,
    is_supported_target,
)
from tests.pdf_fixtures import acroform_pdf, scan_shaped_pdf, text_layer_pdf

NAME_BOX = "fill_2_P.1"
BUSINESS_NAME_BOX = "fill_3_P.1"
EMAIL_BOX = "fill_2_P.2"
ADDRESS_BOXES = ("fill_15_P.1", "fill_16_P.1", "fill_17_P.1", "fill_18_P.1")
DATE_BOXES = ("fill_6_P.1", "fill_7_P.1", "fill_8_P.1")
TYPE_BOXES = ("cb_1_P.1", "cb_2_P.1", "cb_3_P.1")


def fields_by_target(result: ExtractionResult) -> dict[str, object]:
    return {field.target: field for field in result.fields}


# --- Registry and geometry promotion ---


def test_registry_loads_the_pinned_form() -> None:
    registry = load_form_registry(FormCode.NAR1)

    assert registry.form_code == "NAR1"
    assert registry.specification_no == "2/2025 (May 2025)"
    assert registry.template_sha256 == (
        "3a0a9028ed1e4b7110f31844c3fc36add7aeea75afde5cd34e7c382d6f633b0f"
    )
    assert template_path(FormCode.NAR1).is_file()


def test_geometry_carries_exactly_the_mapped_widgets() -> None:
    """Every mapping field has a rectangle, and nothing else is in the file."""
    registry: FormRegistry = load_form_registry(FormCode.NAR1)
    mapped = {name for item in registry.items for name in item.pdf_fields}

    assert set(registry.geometry) == mapped
    # The pinned spec 2/2025 template carries 327 widgets (manifest.json
    # field_counts); a registry refresh changes this number on purpose.
    assert len(mapped) == 327


def test_every_item_target_path_is_supported_or_skipped_whole() -> None:
    """No registry item mixes supported and unsupported paths.

    The reader maps a box to a path positionally, so a half-supported item
    would have to guess which box goes where; this guard keeps that
    impossible for the pinned registry.
    """
    registry = load_form_registry(FormCode.NAR1)
    for item in registry.items:
        paths = individual_paths(item.target)
        supported = [path for path in paths if is_supported_target(path)]
        assert supported in ([], list(paths)), f"{item.item} mixes supported and unsupported paths"


# --- Shape detection ---


def test_acroform_values_are_read_exactly() -> None:
    result = extract(
        acroform_pdf({EMAIL_BOX: "staff@tcsp.example"}),
        form=FormCode.NAR1,
    )

    assert result.shape is InputShape.ACROFORM
    field = fields_by_target(result)["company.email"]
    assert (field.value, field.confidence, field.origin) == (
        "staff@tcsp.example",
        0.99,
        ReadOrigin.EXACT_FIELD_VALUE,
    )
    assert (field.pdf_field, field.page) == (EMAIL_BOX, 2)


def test_text_layer_words_are_matched_against_the_geometry() -> None:
    result = extract(
        text_layer_pdf({BUSINESS_NAME_BOX: "SYNTHETIC BUSINESS NAME"}), form=FormCode.NAR1
    )

    assert result.shape is InputShape.TEXT_LAYER
    field = fields_by_target(result)["company.business_name"]
    assert (field.value, field.confidence, field.origin) == (
        "SYNTHETIC BUSINESS NAME",
        0.70,
        ReadOrigin.TEXT_IN_FIELD_RECT,
    )


def test_scan_shaped_document_is_reported_as_needing_ocr() -> None:
    """The scan shape carries no frame, so nothing is read off it.

    Refusing here needs no OCR engine: the page is measured first, and a
    page that is not the pinned form is never handed to the reader.
    """
    result = extract(scan_shaped_pdf(), form=FormCode.NAR1)

    assert result.shape is InputShape.NEEDS_OCR
    assert result.fields == ()
    assert result.document_signals == (ReadSignal.NEEDS_OCR, ReadSignal.FORM_FRAME_NOT_FOUND)


def test_blank_template_yields_no_master_data_records() -> None:
    """The pinned template itself: a text layer, but no filled company boxes."""
    result = extract(template_path(FormCode.NAR1).read_bytes(), form=FormCode.NAR1)

    assert result.shape is InputShape.TEXT_LAYER
    assert fields_by_target(result) == {}


# --- The form's own specification, on the digital shapes ---


def test_a_digital_filing_of_another_specification_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geometry alone is not the form: the printed specification decides.

    An older specification's PDF can carry the same positional field names
    and rectangles, and would be read silently against the pinned geometry.
    The pinned registry is what names this slice's edition, so a pinned
    registry that expects another number is the mismatch this guards: both
    digital shapes refuse, propose nothing, and say which check failed.
    """
    registry = load_form_registry(FormCode.NAR1)
    monkeypatch.setattr(
        extractor,
        "load_form_registry",
        lambda form, **kwargs: replace(registry, specification_no="1/2023 (December 2023)"),
    )

    for data, shape in (
        (acroform_pdf({EMAIL_BOX: "staff@tcsp.example"}), InputShape.ACROFORM),
        (text_layer_pdf({EMAIL_BOX: "staff@tcsp.example"}), InputShape.TEXT_LAYER),
    ):
        result = extract(data, form=FormCode.NAR1)

        assert result.shape is shape
        assert result.fields == ()
        assert result.document_signals == (ReadSignal.UNSUPPORTED_SPEC,)


def test_a_digital_page_that_is_not_the_pinned_form_is_refused() -> None:
    """Another form's PDF is not read through a coincidental geometry.

    The pinned ND2A template is a real filing-shaped PDF whose print is not
    NAR1's: no page of it carries NAR1's specification region, so the
    region yields no number and nothing is read off it.
    """
    result = extract(template_path(FormCode.ND2A).read_bytes(), form=FormCode.NAR1)

    assert result.fields == ()
    assert result.document_signals == (ReadSignal.SPEC_REGION_UNREADABLE,)


# --- Composed packages ---


def test_a_composed_acroform_package_places_the_shifted_pages() -> None:
    """An inserted sheet shifts every later template page.

    The registry's geometry addresses the template's absolute pages: NAR1
    page 10 is the Schedule 2 sheet, which this package carries on its
    eleventh page (the sample corpus assembles its packages the same way).
    The read must come from that page's own box, not from whichever box of
    the same number happens to sit there.
    """
    result = extract(
        acroform_pdf({"fill_4_P.10": "12345678"}, inserted_pages=(9,)), form=FormCode.NAR1
    )

    assert result.shape is InputShape.ACROFORM
    field = fields_by_target(result)["company.br_number"]
    assert (field.value, field.pdf_field, field.page) == ("12345678", "fill_4_P.10", 11)


def test_a_composed_text_layer_package_is_placed_too() -> None:
    """The text layer is placed by content, like the AcroForm shape by name."""
    result = extract(
        text_layer_pdf({"fill_4_P.10": "12345678"}, inserted_pages=(9,)), form=FormCode.NAR1
    )

    assert result.shape is InputShape.TEXT_LAYER
    field = fields_by_target(result)["company.br_number"]
    assert (field.value, field.pdf_field, field.page) == ("12345678", "fill_4_P.10", 11)


# --- Repeated header fields ---


def test_repeated_header_field_reads_consistently_are_high_confidence() -> None:
    values = {f"fill_1_P.{page}": "12345678" for page in range(1, 9)}
    result = extract(acroform_pdf(values), form=FormCode.NAR1)

    field = fields_by_target(result)["company.br_number"]
    assert field.value == "12345678"
    assert field.confidence == 0.99
    assert field.signals == ()


def test_repeated_header_field_disagreement_lowers_confidence() -> None:
    lots_wrong = {f"fill_1_P.{page}": "12345678" for page in range(1, 9)}
    lots_wrong["fill_1_P.2"] = "99999999"
    result = extract(acroform_pdf(lots_wrong), form=FormCode.NAR1)

    field = fields_by_target(result)["company.br_number"]
    # The majority value is proposed, flagged for staff review rather than
    # auto-accepted.
    assert field.value == "12345678"
    assert field.confidence == 0.55
    assert ReadSignal.REPEATED_FIELD_DISAGREEMENT in field.signals


def test_an_even_split_disagreement_still_proposes_the_first_page_value() -> None:
    result = extract(
        acroform_pdf({"fill_1_P.1": "12345678", "fill_1_P.2": "99999999"}),
        form=FormCode.NAR1,
    )

    field = fields_by_target(result)["company.br_number"]
    assert field.value == "12345678"
    assert field.confidence == 0.55


# --- Bilingual boxes and CJK values ---


def test_bilingual_name_box_is_split_by_script() -> None:
    result = extract(
        acroform_pdf({NAME_BOX: "SYNTHETIC HARBOUR LIMITED 合成海港有限公司"}),
        form=FormCode.NAR1,
    )

    fields = fields_by_target(result)
    english, chinese = fields["company.name_en"], fields["company.name_zh"]
    assert (english.value, chinese.value) == ("SYNTHETIC HARBOUR LIMITED", "合成海港有限公司")
    for field in (english, chinese):
        assert field.confidence == 0.80
        assert ReadSignal.BILINGUAL_BOX_SPLIT in field.signals


def test_single_script_name_box_needs_no_split() -> None:
    result = extract(acroform_pdf({NAME_BOX: "SYNTHETIC HARBOUR LIMITED"}), form=FormCode.NAR1)

    fields = fields_by_target(result)
    assert fields["company.name_en"].value == "SYNTHETIC HARBOUR LIMITED"
    assert fields["company.name_en"].confidence == 0.99
    assert "company.name_zh" not in fields


def test_chinese_value_survives_the_acroform_decode() -> None:
    result = extract(acroform_pdf({BUSINESS_NAME_BOX: "合成海港貿易有限公司"}), form=FormCode.NAR1)

    assert fields_by_target(result)["company.business_name"].value == "合成海港貿易有限公司"


# --- Positional mapping, normalisation, and refusals ---


def test_address_boxes_map_positionally_to_their_parts() -> None:
    result = extract(
        acroform_pdf(
            {
                "fill_15_P.1": "FLAT 12",
                "fill_16_P.1": "HARBOUR BUILDING",
                "fill_17_P.1": "1 QUEEN'S ROAD",
                "fill_18_P.1": "CENTRAL",
            }
        ),
        form=FormCode.NAR1,
    )

    fields = fields_by_target(result)
    assert fields["company.registered_office.flat"].value == "FLAT 12"
    assert fields["company.registered_office.building"].value == "HARBOUR BUILDING"
    assert fields["company.registered_office.street"].value == "1 QUEEN'S ROAD"
    assert fields["company.registered_office.district"].value == "CENTRAL"


def test_amounts_and_counts_are_normalised() -> None:
    result = extract(
        acroform_pdf({"fill_4_P.2": "HK$1,234.50", "fill_5_P.2": "1,024"}),
        form=FormCode.NAR1,
    )

    fields = fields_by_target(result)
    assert fields["company.mortgages_and_charges_total"].value == "1234.50"
    assert fields["company.member_count"].value == "1024"


def test_value_that_does_not_normalise_is_not_proposed() -> None:
    result = extract(acroform_pdf({"fill_5_P.2": "NIL"}), form=FormCode.NAR1)

    assert "company.member_count" not in fields_by_target(result)
    assert result.skipped["company.member_count"] == (ReadSignal.VALUE_UNPARSABLE,)


# --- Checkbox groups ---


def test_ticked_checkbox_maps_to_its_option() -> None:
    result = extract(acroform_pdf({"cb_2_P.1": "On"}), form=FormCode.NAR1)

    field = fields_by_target(result)["company.type"]
    assert (field.value, field.confidence) == ("public", 0.99)


def test_two_ticks_in_one_group_are_not_guessed() -> None:
    result = extract(acroform_pdf({"cb_1_P.1": "On", "cb_3_P.1": "On"}), form=FormCode.NAR1)

    assert "company.type" not in fields_by_target(result)
    assert result.skipped["company.type"] == (ReadSignal.CHECKBOX_GROUP_NOT_SINGLE,)


def test_text_layer_tick_marks_select_the_option() -> None:
    """A flattened filing carries the tick as a glyph inside the box."""
    result = extract(text_layer_pdf({"cb_1_P.1": "X"}), form=FormCode.NAR1)

    field = fields_by_target(result)["company.type"]
    assert (field.value, field.confidence) == ("private", 0.70)


# --- The filing's own date ---


def test_date_parts_assemble_the_made_up_date() -> None:
    result = extract(
        acroform_pdf(dict(zip(DATE_BOXES, ("31", "03", "2025"), strict=True))),
        form=FormCode.NAR1,
    )

    assert result.made_up_date == date(2025, 3, 31)


def test_a_blank_date_is_left_to_the_reviewer() -> None:
    result = extract(acroform_pdf({EMAIL_BOX: "staff@tcsp.example"}), form=FormCode.NAR1)

    assert result.made_up_date is None


def test_date_parts_from_the_text_layer_assemble_too() -> None:
    result = extract(
        text_layer_pdf(dict(zip(DATE_BOXES, ("01", "03", "2025"), strict=True))),
        form=FormCode.NAR1,
    )

    assert result.made_up_date == date(2025, 3, 1)


def test_the_header_date_repeats_the_made_up_date_across_pages() -> None:
    """Item 4 and the page-header triplet are the same date; a mismatch is
    a disagreement like any other repeated field."""
    consistent = extract(
        acroform_pdf(
            dict(zip(DATE_BOXES, ("31", "03", "2025"), strict=True))
            | {
                "fill_1_P.9": "31",
                "fill_2_P.9": "03",
                "fill_3_P.9": "2025",
            }
        ),
        form=FormCode.NAR1,
    )
    assert consistent.made_up_date == date(2025, 3, 31)

    split = extract(
        acroform_pdf(
            dict(zip(DATE_BOXES, ("31", "03", "2025"), strict=True))
            | {
                "fill_1_P.9": "01",
                "fill_2_P.9": "04",
                "fill_3_P.9": "2025",
            }
        ),
        form=FormCode.NAR1,
    )
    # Two equal-weight readings: the first page wins, and the disagreement
    # is recorded rather than hidden.
    assert split.made_up_date == date(2025, 3, 31)
    assert ReadSignal.REPEATED_FIELD_DISAGREEMENT in split.skipped["return.made_up_date"]
