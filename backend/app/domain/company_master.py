"""Company-master domain rules (ADR-0007; data-model §2.3).

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from enum import StrEnum

_DIGITS = 8


class CompanyType(StrEnum):
    """``Company.type`` vocabulary (data-model §2.3; mirrors the DB check)."""

    PRIVATE = "private"
    PUBLIC = "public"
    COMPANY_LIMITED_BY_GUARANTEE = "company_limited_by_guarantee"


class CompanyStatus(StrEnum):
    """``CompanyStatusRecord.status`` vocabulary (§2.3; mirrors the DB check).

    ``dormant`` excludes the company from NAR1 deadline generation (§2.4).
    """

    ACTIVE = "active"
    DORMANT = "dormant"
    STRIKING_OFF = "striking_off"
    DISSOLVED = "dissolved"


class InvalidBrnError(ValueError):
    """The BRN contains no digits and cannot be normalized."""


def normalize_brn(value: str) -> str:
    """Canonical stored BRN: 8 digits, zero-padded on the left.

    All non-digit characters (spaces, dashes, "BR" prefixes) are stripped
    first, so the ``(tenant_id, br_number)`` unique constraint on
    ``companies`` catches duplicates regardless of input formatting.
    Raises ``InvalidBrnError`` when no digits remain.
    """
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise InvalidBrnError(f"BRN contains no digits: {value!r}")
    return digits.zfill(_DIGITS)
