"""Domain tests for prior-filing extraction rules (data-model §2.7, §2.9
``ImportBatch``).

Pure and database-free: the §2.7 confidence bands at their boundaries, the
master-data target vocabulary the review queue may propose (and write), the
confidence ladder behind a per-field score, and the value normalisation the
extraction path and the confirm path share.
"""

from datetime import date

import pytest

from app.domain.prior_filing import (
    AUTO_ACCEPT_THRESHOLD,
    STAFF_REVIEW_THRESHOLD,
    TARGETS,
    AssessmentComponent,
    ConfidenceBand,
    FormCode,
    InputShape,
    InvalidFieldValueError,
    ReadOrigin,
    ReadSignal,
    RecordStatus,
    TargetWrite,
    UnknownTargetError,
    band_for,
    cap_for,
    confidence_for,
    individual_paths,
    is_static_target,
    normalize_value,
    ocr_confidence,
    overall_score,
    parts_to_date,
    routing_for,
    split_bilingual,
    target_spec,
    value_is_validated,
)

# --- §2.7 confidence bands ---


def test_band_thresholds_are_the_contract_values() -> None:
    assert (AUTO_ACCEPT_THRESHOLD, STAFF_REVIEW_THRESHOLD) == (0.85, 0.50)


def test_band_boundaries_are_inclusive_from_below() -> None:
    assert band_for(1.0) is ConfidenceBand.AUTO_ACCEPT
    assert band_for(0.85) is ConfidenceBand.AUTO_ACCEPT
    assert band_for(0.849) is ConfidenceBand.STAFF_REVIEW
    assert band_for(0.50) is ConfidenceBand.STAFF_REVIEW
    assert band_for(0.499) is ConfidenceBand.CLIENT_CLARIFICATION
    assert band_for(0.0) is ConfidenceBand.CLIENT_CLARIFICATION


def test_overall_score_is_the_minimum_of_the_field_scores() -> None:
    assert overall_score({"a": 0.99, "b": 0.7}) == 0.7
    assert overall_score({"a": 0.55}) == 0.55


def test_nothing_assessed_has_no_score_and_no_routing() -> None:
    """A run that read no field cannot be routed (needs-OCR documents)."""
    assert overall_score({}) is None
    assert routing_for({}) is None


def test_routing_follows_the_minimum_field_score() -> None:
    assert routing_for({"a": 0.99, "b": 0.7}) is ConfidenceBand.STAFF_REVIEW
    assert routing_for({"a": 0.99, "b": 0.95}) is ConfidenceBand.AUTO_ACCEPT
    assert routing_for({"a": 0.4}) is ConfidenceBand.CLIENT_CLARIFICATION


# --- Vocabularies (mirrored by the migration's CHECK constraints) ---


def test_record_status_vocabulary() -> None:
    assert {member.value for member in RecordStatus} == {"pending", "confirmed", "rejected"}


def test_form_codes_follow_the_contract_form_type_vocabulary() -> None:
    assert {member.value for member in FormCode} == {"nar1", "nd2a"}


def test_input_shapes() -> None:
    assert {member.value for member in InputShape} == {"acroform", "text_layer", "needs_ocr"}


def test_assessment_component_vocabulary_is_the_contract_2_7_set() -> None:
    assert AssessmentComponent.DOCUMENT_EXTRACTOR.value == "document_extractor"


# --- Target vocabulary ---


def test_supported_targets_are_the_company_master_data_paths() -> None:
    """The E1 target set: registry paths that map 1:1 to a master-data column.

    Officer, shareholder, share-capital, presenter and signature boxes are
    composite or collection targets with no single-column home; they are not
    proposed by this slice (see the contract §2.7 note).
    """
    expected = {
        "company.br_number",
        "company.type",
        "company.name_en",
        "company.name_zh",
        "company.business_name",
        "company.email",
        "company.contact_phone_hk",
        "company.mortgages_and_charges_total",
        "company.member_count",
        "company.registered_office.flat",
        "company.registered_office.building",
        "company.registered_office.street",
        "company.registered_office.district",
        "company.business_nature_code",
        "company.business_nature_description",
        "return.made_up_date",
    }
    assert set(TARGETS) == expected


def test_unknown_target_is_refused() -> None:
    with pytest.raises(UnknownTargetError):
        target_spec("officers[].email")


def test_static_targets_are_the_anchor_statics() -> None:
    """BRN and company type are ``Company`` statics, not effective-dated facts."""
    assert is_static_target("company.br_number")
    assert is_static_target("company.type")
    assert not is_static_target("company.email")


def test_fact_targets_carry_their_group_and_column() -> None:
    contact = target_spec("company.email")
    assert contact.write is TargetWrite.FACT
    assert (contact.group, contact.column) == ("contact", "email")
    name = target_spec("company.name_zh")
    assert (name.group, name.column) == ("company_name", "name_zh")


def test_made_up_date_is_context_not_a_record() -> None:
    """The filing's made-up date dates its records; it is not master data."""
    spec = target_spec("return.made_up_date")
    assert spec.write is TargetWrite.CONTEXT
    assert spec.group is None


def test_individual_paths_splits_the_registry_join() -> None:
    assert individual_paths("company.name_en + company.name_zh") == (
        "company.name_en",
        "company.name_zh",
    )
    assert individual_paths("company.email") == ("company.email",)


# --- normalize_value ---


def test_text_values_are_whitespace_collapsed_and_stripped() -> None:
    assert normalize_value("company.name_en", "  SYNTHETIC   HARBOUR  ") == "SYNTHETIC HARBOUR"


def test_text_values_over_the_column_width_are_refused() -> None:
    with pytest.raises(InvalidFieldValueError):
        normalize_value("company.contact_phone_hk", "1" * 21)


def test_empty_values_are_refused() -> None:
    with pytest.raises(InvalidFieldValueError):
        normalize_value("company.email", "   ")


def test_brn_is_normalized_through_the_shared_helper() -> None:
    assert normalize_value("company.br_number", "1234 5678") == "12345678"
    assert normalize_value("company.br_number", "1234567") == "01234567"
    with pytest.raises(InvalidFieldValueError):
        normalize_value("company.br_number", "not-a-brn")


def test_integer_values_accept_digit_groups_only() -> None:
    assert normalize_value("company.member_count", " 1,024 ") == "1024"
    for raw in ("abc", "12.5", "-5"):
        with pytest.raises(InvalidFieldValueError):
            normalize_value("company.member_count", raw)


def test_decimal_values_strip_currency_and_grouping() -> None:
    assert normalize_value("company.mortgages_and_charges_total", "HK$1,234.50") == "1234.50"
    assert normalize_value("company.mortgages_and_charges_total", "0") == "0"
    for raw in ("Nil", "-5.00"):
        with pytest.raises(InvalidFieldValueError):
            normalize_value("company.mortgages_and_charges_total", raw)


def test_company_type_accepts_only_the_contract_vocabulary() -> None:
    assert normalize_value("company.type", "private") == "private"
    with pytest.raises(InvalidFieldValueError):
        normalize_value("company.type", "Private company")


# --- split_bilingual ---


def test_bilingual_name_box_splits_by_script() -> None:
    assert split_bilingual("SYNTHETIC HARBOUR LIMITED 合成海港有限公司") == (
        "SYNTHETIC HARBOUR LIMITED",
        "合成海港有限公司",
    )


def test_single_script_name_box_yields_only_that_script() -> None:
    assert split_bilingual("SYNTHETIC LIMITED") == ("SYNTHETIC LIMITED", None)
    assert split_bilingual("合成有限公司") == (None, "合成有限公司")


def test_mixed_single_token_splits_as_far_as_the_rule_allows() -> None:
    """Documented heuristic limit: a Latin prefix inside one token is kept."""
    assert split_bilingual("ABC有限公司") == ("ABC", "有限公司")


# --- date_parts ---


def test_date_parts_assemble_a_dd_mm_yyyy_triplet() -> None:
    assert parts_to_date(("01", "03", "2025")) == date(2025, 3, 1)
    assert parts_to_date(("1", "3", "2025")) == date(2025, 3, 1)


def test_partial_or_impossible_dates_are_not_guessed() -> None:
    assert parts_to_date(("", "03", "2025")) is None
    assert parts_to_date(("29", "02", "2025")) is None
    assert parts_to_date(("01", "03", "2025", "extra")) is None


# --- Confidence ladder ---


def test_exact_field_reads_are_high_confidence() -> None:
    assert confidence_for(origin=ReadOrigin.EXACT_FIELD_VALUE, signals=()) == 0.99


def test_text_in_rectangle_matches_are_lower() -> None:
    assert confidence_for(origin=ReadOrigin.TEXT_IN_FIELD_RECT, signals=()) == 0.70


def test_repeated_field_disagreement_caps_the_score_into_staff_review() -> None:
    score = confidence_for(
        origin=ReadOrigin.EXACT_FIELD_VALUE,
        signals=(ReadSignal.REPEATED_FIELD_DISAGREEMENT,),
    )
    assert score == 0.55
    assert band_for(score) is ConfidenceBand.STAFF_REVIEW


def test_bilingual_split_caps_an_exact_read_into_staff_review() -> None:
    score = confidence_for(
        origin=ReadOrigin.EXACT_FIELD_VALUE, signals=(ReadSignal.BILINGUAL_BOX_SPLIT,)
    )
    assert score == 0.80
    assert band_for(score) is ConfidenceBand.STAFF_REVIEW


def test_the_lowest_cap_wins() -> None:
    assert (
        confidence_for(
            origin=ReadOrigin.TEXT_IN_FIELD_RECT,
            signals=(ReadSignal.BILINGUAL_BOX_SPLIT, ReadSignal.REPEATED_FIELD_DISAGREEMENT),
        )
        == 0.55
    )


def test_cap_for_reports_the_lowest_signal_cap() -> None:
    assert cap_for(()) is None
    assert cap_for((ReadSignal.REPEATED_FIELD_DISAGREEMENT,)) == 0.55
    assert (
        cap_for((ReadSignal.BILINGUAL_BOX_SPLIT, ReadSignal.REPEATED_FIELD_DISAGREEMENT)) == 0.55
    )
    # A signal that only explains an absence imposes no cap.
    assert cap_for((ReadSignal.VALUE_UNPARSABLE,)) is None


def test_confidence_for_owns_only_the_digital_reads() -> None:
    """OCR reads carry their own score; asking for a ladder rung is a bug."""
    with pytest.raises(ValueError):
        confidence_for(origin=ReadOrigin.OCR_TEXT_IN_FIELD_RECT, signals=())


# --- The OCR ladder (E2) ---


def test_ocr_origins_are_part_of_the_read_vocabulary() -> None:
    assert {member.value for member in ReadOrigin} == {
        "exact_field_value",
        "text_in_field_rect",
        "ocr_text_in_field_rect",
        "ocr_checkbox_mark",
    }


def test_ocr_signals_are_part_of_the_signal_vocabulary() -> None:
    assert {
        "form_frame_not_found",
        "unsupported_spec",
        "spec_region_unreadable",
        "ocr_field_unreadable",
        "ocr_engine_unavailable",
        "ocr_value_unvalidated",
    } <= {member.value for member in ReadSignal}


def test_validated_ocr_values_are_capped_below_an_exact_read() -> None:
    assert ocr_confidence(word_confidences=(0.95, 0.99), value_validated=True) == 0.90
    assert ocr_confidence(word_confidences=(0.72,), value_validated=True) == 0.72


def test_unvalidated_ocr_values_stay_in_the_staff_review_band() -> None:
    assert ocr_confidence(word_confidences=(0.99, 0.95), value_validated=False) == 0.60
    assert ocr_confidence(word_confidences=(0.41,), value_validated=False) == 0.41


def test_an_unvalidated_ocr_value_can_never_reach_auto_accept() -> None:
    """The acceptance bar: validation, not engine confidence, opens the band."""
    for confidence in (0.5, 0.8, 0.95, 0.999, 1.0):
        score = ocr_confidence(word_confidences=(confidence,), value_validated=False)
        assert score < AUTO_ACCEPT_THRESHOLD
        assert band_for(score) is not ConfidenceBand.AUTO_ACCEPT


def test_ocr_confidence_needs_at_least_one_word() -> None:
    with pytest.raises(ValueError):
        ocr_confidence(word_confidences=(), value_validated=True)


def test_validation_is_per_value_kind() -> None:
    assert value_is_validated("company.name_en", "SYNTHETIC HARBOUR LIMITED")
    assert value_is_validated("company.name_zh", "合成海港有限公司")
    assert not value_is_validated("company.name_en", "1234")
    assert value_is_validated("company.br_number", "01234567")
    assert not value_is_validated("company.br_number", "1234567")
    assert value_is_validated("company.member_count", "1024")
    assert value_is_validated("company.mortgages_and_charges_total", "1234.50")
    assert not value_is_validated("company.mortgages_and_charges_total", "1234.567")
    assert value_is_validated("company.type", "private")
    assert not value_is_validated("company.type", "Sdn Bhd")


def test_a_value_outside_the_target_normalisation_is_never_validated() -> None:
    assert not value_is_validated("company.member_count", "NIL")
    assert not value_is_validated("company.br_number", "not-a-brn")
