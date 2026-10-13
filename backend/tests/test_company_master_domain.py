"""Unit tests for company-master domain rules (``app.domain.company_master``).

Pure domain code, no database: BRN normalization — the canonical stored
format is 8 digits, zero-padded on the left, so the ``(tenant_id,
br_number)`` unique constraint on ``companies`` catches duplicates
regardless of input formatting (spaces, dashes, prefixes).
"""

import pytest

from app.domain.company_master import InvalidBrnError, normalize_brn


def test_already_normalized_brn_is_unchanged() -> None:
    assert normalize_brn("12345678") == "12345678"


def test_separators_are_stripped() -> None:
    assert normalize_brn("1234 5678") == "12345678"
    assert normalize_brn("1234-5678") == "12345678"


def test_prefix_and_punctuation_are_stripped() -> None:
    assert normalize_brn("BR 1234 5678") == "12345678"
    assert normalize_brn("1234 5678(001)") == "12345678001"


def test_short_brn_is_zero_padded_on_the_left() -> None:
    assert normalize_brn("1234567") == "01234567"
    assert normalize_brn("123") == "00000123"


def test_leading_zeros_are_preserved() -> None:
    assert normalize_brn("01234567") == "01234567"


def test_no_digits_raises_invalid_brn_error() -> None:
    with pytest.raises(InvalidBrnError):
        normalize_brn("BR-")
    with pytest.raises(InvalidBrnError):
        normalize_brn("")
    with pytest.raises(InvalidBrnError):
        normalize_brn("   ")


def test_invalid_brn_error_is_a_value_error() -> None:
    assert issubclass(InvalidBrnError, ValueError)
