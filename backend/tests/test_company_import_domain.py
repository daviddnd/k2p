"""Unit tests for the CSV portfolio-import parsing rules (data-model §5.1.1).

Pure domain tests — no database, no framework. The parser turns one CSV row
(a string mapping) into a typed ``ParsedImportRow`` or raises
``ImportRowError`` with the reason; ``validate_header`` enforces the
file-level column vocabulary (unknown columns fail fast so typos cannot
silently drop data).
"""

from datetime import date
from decimal import Decimal

import pytest

from app.domain.company_import import (
    ImportHeaderError,
    ImportRowError,
    derive_anniversary_date,
    parse_import_row,
    validate_header,
)
from app.domain.company_master import CompanyType

DATA_AS_OF = date(2026, 6, 30)

MINIMAL_ROW = {
    "br_number": "1234 5678",
    "name_en": "EXAMPLE LIMITED",
    "date_of_incorporation": "2010-03-01",
    "type": "private",
}

FULL_HEADER = [
    "br_number",
    "cr_number",
    "name_en",
    "name_zh",
    "business_name",
    "date_of_incorporation",
    "anniversary_date",
    "type",
    "effective_date",
    "ro_flat",
    "ro_building",
    "ro_street",
    "ro_district",
    "ro_country",
    "status",
    "business_nature_code",
    "business_nature_description",
    "email",
    "contact_phone_hk",
    "mortgages_and_charges_total",
    "member_count",
    "records_kept",
    "share_capital_classes",
]


# --- Header validation ---


def test_validate_header_accepts_minimal_header() -> None:
    validate_header(["br_number", "name_en", "date_of_incorporation", "type"])


def test_validate_header_accepts_zh_name_only() -> None:
    validate_header(["br_number", "name_zh", "date_of_incorporation", "type"])


def test_validate_header_accepts_full_vocabulary() -> None:
    validate_header(FULL_HEADER)


def test_validate_header_rejects_missing_required_column() -> None:
    with pytest.raises(ImportHeaderError, match="date_of_incorporation"):
        validate_header(["br_number", "name_en", "type"])


def test_validate_header_rejects_missing_both_name_columns() -> None:
    with pytest.raises(ImportHeaderError, match="name_en"):
        validate_header(["br_number", "date_of_incorporation", "type"])


def test_validate_header_rejects_unknown_column() -> None:
    # A typo'd column must fail fast, not silently drop the data it holds.
    with pytest.raises(ImportHeaderError, match="emal"):
        validate_header(["br_number", "name_en", "date_of_incorporation", "type", "emal"])


# --- Minimal row and BRN handling ---


def test_minimal_row_parses_with_derived_anniversary_and_default_effective_date() -> None:
    parsed = parse_import_row(MINIMAL_ROW, data_as_of=DATA_AS_OF)
    assert parsed.br_number == "12345678"  # normalized: digits only, zero-padded
    assert parsed.cr_number is None
    assert parsed.date_of_incorporation == date(2010, 3, 1)
    # Private company, no explicit anniversary: month/day of incorporation
    # in the data_as_of year.
    assert parsed.anniversary_date == date(2026, 3, 1)
    assert parsed.type is CompanyType.PRIVATE
    assert parsed.effective_date == DATA_AS_OF
    assert parsed.name_en == "EXAMPLE LIMITED"
    assert parsed.name_zh is None
    # No optional fact columns populated → no optional facts.
    assert parsed.registered_office is None
    assert parsed.status is None
    assert parsed.email is None
    assert parsed.records_kept is None


def test_brn_without_digits_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="br_number"):
        parse_import_row(MINIMAL_ROW | {"br_number": "BR-"}, data_as_of=DATA_AS_OF)


def test_missing_name_is_rejected() -> None:
    row = {k: v for k, v in MINIMAL_ROW.items() if k != "name_en"}
    with pytest.raises(ImportRowError, match="name"):
        parse_import_row(row, data_as_of=DATA_AS_OF)


def test_zh_name_only_row_parses() -> None:
    row = {k: v for k, v in MINIMAL_ROW.items() if k != "name_en"} | {"name_zh": "示例有限公司"}
    parsed = parse_import_row(row, data_as_of=DATA_AS_OF)
    assert parsed.name_zh == "示例有限公司"
    assert parsed.name_en is None


def test_blank_cells_are_treated_as_absent() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW | {"cr_number": "  ", "email": ""}, data_as_of=DATA_AS_OF
    )
    assert parsed.cr_number is None
    assert parsed.email is None


# --- Dates and enums ---


def test_invalid_date_of_incorporation_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="date_of_incorporation"):
        parse_import_row(
            MINIMAL_ROW | {"date_of_incorporation": "01/03/2010"}, data_as_of=DATA_AS_OF
        )


def test_invalid_type_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="type"):
        parse_import_row(MINIMAL_ROW | {"type": "ltd"}, data_as_of=DATA_AS_OF)


def test_explicit_anniversary_wins_over_derivation() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW | {"anniversary_date": "2026-04-15"}, data_as_of=DATA_AS_OF
    )
    assert parsed.anniversary_date == date(2026, 4, 15)


def test_non_private_type_requires_explicit_anniversary() -> None:
    with pytest.raises(ImportRowError, match="anniversary_date"):
        parse_import_row(
            MINIMAL_ROW | {"type": "company_limited_by_guarantee"}, data_as_of=DATA_AS_OF
        )


def test_non_private_type_with_explicit_anniversary_parses() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW | {"type": "public", "anniversary_date": "2026-12-31"}, data_as_of=DATA_AS_OF
    )
    assert parsed.type is CompanyType.PUBLIC
    assert parsed.anniversary_date == date(2026, 12, 31)


def test_derive_anniversary_clamps_feb_29_in_non_leap_year() -> None:
    assert derive_anniversary_date(date(2020, 2, 29), as_of=date(2026, 1, 1)) == date(2026, 2, 28)
    assert derive_anniversary_date(date(2020, 2, 29), as_of=date(2024, 1, 1)) == date(2024, 2, 29)


def test_per_row_effective_date_overrides_data_as_of() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW | {"effective_date": "2025-12-31"}, data_as_of=DATA_AS_OF
    )
    assert parsed.effective_date == date(2025, 12, 31)


def test_invalid_per_row_effective_date_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="effective_date"):
        parse_import_row(MINIMAL_ROW | {"effective_date": "31-12-2025"}, data_as_of=DATA_AS_OF)


# --- Optional fact groups ---


def test_full_registered_office_parses() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW
        | {
            "ro_flat": "12/F",
            "ro_building": "EXAMPLE TOWER",
            "ro_street": "1 EXAMPLE ROAD",
            "ro_district": "Central",
        },
        data_as_of=DATA_AS_OF,
    )
    assert parsed.registered_office == {
        "flat": "12/F",
        "building": "EXAMPLE TOWER",
        "street": "1 EXAMPLE ROAD",
        "district": "Central",
        "country": None,
    }


def test_partial_registered_office_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="ro_"):
        parse_import_row(MINIMAL_ROW | {"ro_flat": "12/F"}, data_as_of=DATA_AS_OF)


def test_valid_status_parses() -> None:
    parsed = parse_import_row(MINIMAL_ROW | {"status": "dormant"}, data_as_of=DATA_AS_OF)
    assert parsed.status == "dormant"


def test_invalid_status_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="status"):
        parse_import_row(MINIMAL_ROW | {"status": "closed"}, data_as_of=DATA_AS_OF)


def test_business_nature_requires_both_columns() -> None:
    with pytest.raises(ImportRowError, match="business_nature"):
        parse_import_row(
            MINIMAL_ROW | {"business_nature_code": "620"}, data_as_of=DATA_AS_OF
        )
    parsed = parse_import_row(
        MINIMAL_ROW
        | {"business_nature_code": "620", "business_nature_description": "Programming"},
        data_as_of=DATA_AS_OF,
    )
    assert parsed.business_nature_code == "620"
    assert parsed.business_nature_description == "Programming"


def test_contact_with_one_channel_parses() -> None:
    parsed = parse_import_row(MINIMAL_ROW | {"contact_phone_hk": "23456789"}, data_as_of=DATA_AS_OF)
    assert parsed.contact_phone_hk == "23456789"
    assert parsed.email is None


def test_invalid_email_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="email"):
        parse_import_row(MINIMAL_ROW | {"email": "not-an-email"}, data_as_of=DATA_AS_OF)


def test_filing_figures_parse_as_decimal_and_int() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW | {"mortgages_and_charges_total": "1500.25", "member_count": "7"},
        data_as_of=DATA_AS_OF,
    )
    assert parsed.mortgages_and_charges_total == Decimal("1500.25")
    assert parsed.member_count == 7


def test_negative_filing_figures_are_rejected() -> None:
    with pytest.raises(ImportRowError, match="mortgages_and_charges_total"):
        parse_import_row(
            MINIMAL_ROW | {"mortgages_and_charges_total": "-1"}, data_as_of=DATA_AS_OF
        )


def test_records_kept_json_cell_parses() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW
        | {
            "records_kept": (
                '[{"description": "Registers", "address": '
                '{"flat": "12/F", "building": "EXAMPLE TOWER", '
                '"street": "1 EXAMPLE ROAD", "district": "Central"}}]'
            )
        },
        data_as_of=DATA_AS_OF,
    )
    assert parsed.records_kept == [
        {
            "description": "Registers",
            "address": {
                "flat": "12/F",
                "building": "EXAMPLE TOWER",
                "street": "1 EXAMPLE ROAD",
                "district": "Central",
                "country": None,
            },
        }
    ]


def test_records_kept_invalid_json_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="records_kept"):
        parse_import_row(MINIMAL_ROW | {"records_kept": "not json"}, data_as_of=DATA_AS_OF)


def test_records_kept_missing_address_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="records_kept"):
        parse_import_row(
            MINIMAL_ROW | {"records_kept": '[{"description": "Registers"}]'},
            data_as_of=DATA_AS_OF,
        )


def test_share_capital_classes_json_cell_parses_amounts_as_strings() -> None:
    parsed = parse_import_row(
        MINIMAL_ROW
        | {
            "share_capital_classes": (
                '[{"class_name": "Ordinary", "currency": "HKD", "total_shares": 10000, '
                '"total_amount": 10000, "total_paid_up": 10000}]'
            )
        },
        data_as_of=DATA_AS_OF,
    )
    # Decimal values are stored as strings, matching the PATCH path's
    # model_dump(mode="json") serialization.
    assert parsed.share_capital_classes == [
        {
            "class_name": "Ordinary",
            "currency": "HKD",
            "total_shares": 10000,
            "total_amount": "10000",
            "total_paid_up": "10000",
        }
    ]


def test_share_capital_classes_bad_currency_is_rejected() -> None:
    with pytest.raises(ImportRowError, match="share_capital_classes"):
        parse_import_row(
            MINIMAL_ROW
            | {
                "share_capital_classes": (
                    '[{"class_name": "Ordinary", "currency": "HKDL", "total_shares": 1, '
                    '"total_amount": 1, "total_paid_up": 1}]'
                )
            },
            data_as_of=DATA_AS_OF,
        )


# --- The committed template fixture stays valid ---


def test_template_fixture_parses_cleanly() -> None:
    """The downloadable template (``tests/fixtures/companies_import_template.csv``)
    must always satisfy its own vocabulary — it is the example users copy."""
    import csv
    import io
    from pathlib import Path

    fixture = (
        Path(__file__).parent / "fixtures" / "companies_import_template.csv"
    ).read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(fixture))
    validate_header(reader.fieldnames or [])
    rows = list(reader)
    assert len(rows) == 3
    for row in rows:
        assert row.get(None) is None  # no surplus cells from column drift
        parse_import_row(row, data_as_of=DATA_AS_OF)
