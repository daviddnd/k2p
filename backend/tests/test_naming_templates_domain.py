"""Unit tests for the naming-template domain rules (contract §5.3).

Pure domain tests — no database, no framework. They pin the §5.3 token
vocabulary and validation rules: the closed token set, mandatory company
attribution, the ``{Version}`` requirement for generated drafts/exports,
segment/key safety rules, and the render-time value checks the document
store will rely on when it produces storage keys.
"""

from typing import ClassVar

import pytest

from app.domain.naming_templates import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DEFAULT_GENERATED_TEMPLATE,
    InvalidTokenValueError,
    MissingTokenValueError,
    NamingTemplates,
    TemplateValidationError,
    default_naming_templates,
    render_key,
    validate_naming_templates,
)

VALID_DOCUMENT = "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}"
VALID_GENERATED = "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}"


class TestDefaults:
    def test_shipped_defaults_are_valid(self) -> None:
        templates = validate_naming_templates(
            document=DEFAULT_DOCUMENT_TEMPLATE, generated=DEFAULT_GENERATED_TEMPLATE
        )
        assert templates == default_naming_templates()

    def test_defaults_match_contract_5_3(self) -> None:
        assert DEFAULT_GENERATED_TEMPLATE == VALID_GENERATED
        assert default_naming_templates() == NamingTemplates(
            document=VALID_DOCUMENT, generated=VALID_GENERATED
        )


class TestValidation:
    def test_valid_templates_pass(self) -> None:
        templates = validate_naming_templates(document=VALID_DOCUMENT, generated=VALID_GENERATED)
        assert templates.document == VALID_DOCUMENT
        assert templates.generated == VALID_GENERATED

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        templates = validate_naming_templates(
            document=f"  {VALID_DOCUMENT}\n", generated=f"\t{VALID_GENERATED} "
        )
        assert templates.document == VALID_DOCUMENT
        assert templates.generated == VALID_GENERATED

    def test_unknown_token_rejected(self) -> None:
        with pytest.raises(TemplateValidationError) as excinfo:
            validate_naming_templates(
                document="{CompanyCode}/{Year}/{Form}_{Bogus}.pdf",
                generated=VALID_GENERATED,
            )
        assert any("Bogus" in error for error in excinfo.value.errors)

    def test_company_attribution_mandatory(self) -> None:
        # Neither {CompanyCode} nor {CR}: the path is not attributable.
        with pytest.raises(TemplateValidationError) as excinfo:
            validate_naming_templates(
                document="{Year}/{Form}_{BRN}_{YYYYMMDD}.pdf",
                generated=VALID_GENERATED,
            )
        assert any("CompanyCode" in error and "CR" in error for error in excinfo.value.errors)

    def test_brn_alone_is_not_attribution(self) -> None:
        # §5.3: {CompanyCode} or {CR} is mandatory — {BRN} does not satisfy it.
        with pytest.raises(TemplateValidationError):
            validate_naming_templates(
                document="{BRN}/{Form}_{YYYYMMDD}.pdf", generated=VALID_GENERATED
            )

    def test_cr_alone_satisfies_attribution(self) -> None:
        templates = validate_naming_templates(
            document="{CR}/{Form}_{YYYYMMDD}.pdf", generated=VALID_GENERATED
        )
        assert templates.document == "{CR}/{Form}_{YYYYMMDD}.pdf"

    def test_version_mandatory_for_generated(self) -> None:
        with pytest.raises(TemplateValidationError) as excinfo:
            validate_naming_templates(
                document=VALID_DOCUMENT,
                generated="{CompanyCode}/{Year}/{Form}_{CR}_{YYYYMMDD}.pdf",
            )
        assert any("Version" in error for error in excinfo.value.errors)

    def test_version_optional_for_document(self) -> None:
        # Stored originals have no draft version — {Version} is not required.
        templates = validate_naming_templates(
            document="{CompanyCode}/{Form}_{CR}.pdf", generated=VALID_GENERATED
        )
        assert templates.document == "{CompanyCode}/{Form}_{CR}.pdf"

    def test_both_templates_validated_and_errors_labelled(self) -> None:
        with pytest.raises(TemplateValidationError) as excinfo:
            validate_naming_templates(document="{Bogus}", generated="{AlsoBogus}")
        errors = excinfo.value.errors
        assert any(error.startswith("document:") for error in errors)
        assert any(error.startswith("generated:") for error in errors)

    @pytest.mark.parametrize(
        "template",
        [
            "",  # empty
            "   ",  # whitespace only
            "{CompanyCode}/../{CR}.pdf",  # parent traversal
            "/{CompanyCode}/{CR}.pdf",  # leading slash
            "{CompanyCode}/{CR}<.pdf",  # illegal character
            '{CompanyCode}/{CR}".pdf',
            "{CompanyCode}/{CR}|.pdf",
            "{CompanyCode}/{CR}?.pdf",
            "{CompanyCode}/{CR}*.pdf",
            "{CompanyCode}/{CR}\t.pdf",  # control character
            "{CompanyCode}/{CR.pdf",  # unclosed brace
            "{CompanyCode}/CR}.pdf",  # stray closing brace
            "{CompanyCode}/{}.pdf",  # empty token
            "{CompanyCode}/" + "a" * 129 + "/{CR}.pdf",  # segment too long
        ],
    )
    def test_invalid_templates_rejected(self, template: str) -> None:
        with pytest.raises(TemplateValidationError):
            validate_naming_templates(document=template, generated=VALID_GENERATED)

    def test_template_over_key_length_cap_rejected(self) -> None:
        long_template = "{CompanyCode}/" + "a" * 500 + "/{CR}.pdf"
        with pytest.raises(TemplateValidationError) as excinfo:
            validate_naming_templates(document=long_template, generated=VALID_GENERATED)
        assert any("512" in error for error in excinfo.value.errors)


class TestRender:
    VALUES: ClassVar[dict[str, str]] = {
        "CompanyCode": "ACME",
        "CR": "1234567",
        "BRN": "12345678",
        "Year": "2026",
        "YYYY": "2026",
        "MM": "08",
        "DD": "24",
        "YYYYMMDD": "20260824",
        "Form": "NAR1",
        "DocType": "draft",
        "Ext": "pdf",
        "Version": "3",
    }

    def test_full_substitution(self) -> None:
        key = render_key(VALID_GENERATED, self.VALUES)
        assert key == "ACME/2026/NAR1_draft_1234567_20260824_v3.pdf"

    def test_literal_text_and_repeated_tokens_preserved(self) -> None:
        key = render_key("{CompanyCode}/{YYYY}-{MM}-{DD}/{Form}_{CR}.pdf", self.VALUES)
        assert key == "ACME/2026-08-24/NAR1_1234567.pdf"

    def test_missing_value_raises(self) -> None:
        values = {key: value for key, value in self.VALUES.items() if key != "Version"}
        with pytest.raises(MissingTokenValueError):
            render_key(VALID_GENERATED, values)

    def test_extra_values_ignored(self) -> None:
        key = render_key("{CR}.pdf", {**self.VALUES, "Unrelated": "x"})
        assert key == "1234567.pdf"

    @pytest.mark.parametrize("value", ["", "a/b", "a\\b", ".", "..", "a<b", "a\tb"])
    def test_unsafe_values_rejected(self, value: str) -> None:
        with pytest.raises(InvalidTokenValueError):
            render_key("{CR}.pdf", {"CR": value})

    def test_unknown_token_in_template_raises(self) -> None:
        # Defense in depth: render never emits an unsubstituted token even
        # though every stored template was validated at PUT time.
        with pytest.raises(TemplateValidationError):
            render_key("{CR}_{Bogus}.pdf", self.VALUES)

    def test_rendered_segment_over_cap_rejected(self) -> None:
        with pytest.raises(InvalidTokenValueError):
            render_key("{CR}/{Form}.pdf", {"CR": "x" * 129, "Form": "NAR1"})
