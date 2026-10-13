"""CSV portfolio-import parsing rules (data-model §5.1.1; ADR-0013 Level 1).

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer. This module turns one CSV row (a
string mapping) into a typed ``ParsedImportRow`` or raises ``ImportRowError``
with the rejection reason; ``validate_header`` enforces the file-level
column vocabulary before any row is read.

Design (founder-approved 2026-08-24, Option E minimal-viable row): only four
columns are required — ``br_number``, at least one of
``name_en``/``name_zh``, ``date_of_incorporation``, and ``type``. Every other
column is optional; an empty cell means "no fact record from this row",
never "clear the fact". Unknown header columns fail the whole file so a
typo cannot silently drop data.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.company_master import (
    CompanyStatus,
    CompanyType,
    InvalidBrnError,
    normalize_brn,
)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")

_STATIC_COLUMNS = frozenset(
    {"br_number", "cr_number", "date_of_incorporation", "anniversary_date", "type"}
)
_NAME_COLUMNS = frozenset({"name_en", "name_zh", "business_name"})
_FACT_COLUMNS = frozenset(
    {
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
    }
)
KNOWN_COLUMNS = _STATIC_COLUMNS | _NAME_COLUMNS | _FACT_COLUMNS

_ADDRESS_FIELDS = ("flat", "building", "street", "district")


class ImportHeaderError(ValueError):
    """The CSV header misses required columns or names unknown columns."""


class ImportRowError(ValueError):
    """One CSV row failed validation; the message is the rejection reason."""


@dataclass(frozen=True)
class ParsedImportRow:
    """One validated CSV row, ready for the match-or-create write path.

    ``br_number`` is already normalized. Fact-group fields are ``None`` when
    their columns were empty; collection payloads are decoded JSON cells in
    the same shape the ``PATCH`` path stores (decimal amounts as strings).
    """

    br_number: str
    cr_number: str | None
    date_of_incorporation: date
    anniversary_date: date
    type: CompanyType
    effective_date: date
    name_en: str | None
    name_zh: str | None
    business_name: str | None
    registered_office: dict[str, Any] | None
    status: str | None
    business_nature_code: str | None
    business_nature_description: str | None
    email: str | None
    contact_phone_hk: str | None
    mortgages_and_charges_total: Decimal | None
    member_count: int | None
    records_kept: list[dict[str, Any]] | None
    share_capital_classes: list[dict[str, Any]] | None


def validate_header(fieldnames: Iterable[str | None]) -> None:
    """File-level column-vocabulary check, run before any row is parsed.

    The header must name ``br_number``, ``date_of_incorporation``, ``type``,
    and at least one of ``name_en``/``name_zh``; any unrecognized column
    fails the whole file — typos fail fast instead of silently dropping data.
    """
    names = {name.strip() for name in fieldnames if name and name.strip()}
    missing = {"br_number", "date_of_incorporation", "type"} - names
    if missing:
        raise ImportHeaderError(f"missing required column(s): {', '.join(sorted(missing))}")
    if names.isdisjoint({"name_en", "name_zh"}):
        raise ImportHeaderError("missing required column(s): name_en or name_zh")
    unknown = names - KNOWN_COLUMNS
    if unknown:
        raise ImportHeaderError(f"unknown column(s): {', '.join(sorted(unknown))}")


def derive_anniversary_date(date_of_incorporation: date, *, as_of: date) -> date:
    """The default ``anniversary_date`` for a private company (§5.1.1 D1(i)).

    The month/day of incorporation in the ``as_of`` year — the Cap. 622 NAR1
    made-up date for private companies. A February 29 incorporation clamps
    to February 28 in a non-leap ``as_of`` year.
    """
    try:
        return date_of_incorporation.replace(year=as_of.year)
    except ValueError:  # Feb 29 in a non-leap as_of year
        return date_of_incorporation.replace(year=as_of.year, day=28)


def parse_import_row(row: Mapping[str, str | None], *, data_as_of: date) -> ParsedImportRow:
    """Validate one CSV row into a ``ParsedImportRow``.

    ``data_as_of`` supplies the default ``effective_date`` for the row's fact
    records and the reference year for a derived ``anniversary_date``; a
    per-row ``effective_date`` cell overrides it. Raises ``ImportRowError``
    with the rejection reason on any invalid value.
    """
    br_number = _required(row, "br_number")
    try:
        normalized_brn = normalize_brn(br_number)
    except InvalidBrnError:
        raise ImportRowError(f"br_number contains no digits: {br_number!r}") from None

    date_of_incorporation = _parse_date(_required(row, "date_of_incorporation"),
                                        "date_of_incorporation")
    company_type = _parse_type(_required(row, "type"))

    name_en = _cell(row, "name_en")
    name_zh = _cell(row, "name_zh")
    if name_en is None and name_zh is None:
        raise ImportRowError("name_en or name_zh is required")

    explicit_anniversary = _cell(row, "anniversary_date")
    if explicit_anniversary is not None:
        anniversary_date = _parse_date(explicit_anniversary, "anniversary_date")
    elif company_type is CompanyType.PRIVATE:
        anniversary_date = derive_anniversary_date(date_of_incorporation, as_of=data_as_of)
    else:
        raise ImportRowError(f"anniversary_date is required for type {company_type.value!r}")

    explicit_effective = _cell(row, "effective_date")
    effective_date = (
        _parse_date(explicit_effective, "effective_date")
        if explicit_effective is not None
        else data_as_of
    )

    return ParsedImportRow(
        br_number=normalized_brn,
        cr_number=_cell(row, "cr_number"),
        date_of_incorporation=date_of_incorporation,
        anniversary_date=anniversary_date,
        type=company_type,
        effective_date=effective_date,
        name_en=name_en,
        name_zh=name_zh,
        business_name=_cell(row, "business_name"),
        registered_office=_parse_address(row),
        status=_parse_status(row),
        business_nature_code=(business_nature := _parse_business_nature(row))[0],
        business_nature_description=business_nature[1],
        email=_parse_email(row),
        contact_phone_hk=_cell(row, "contact_phone_hk"),
        mortgages_and_charges_total=_parse_decimal_cell(row, "mortgages_and_charges_total"),
        member_count=_parse_int_cell(row, "member_count"),
        records_kept=_parse_records_kept(row),
        share_capital_classes=_parse_share_capital_classes(row),
    )


def _cell(row: Mapping[str, str | None], name: str) -> str | None:
    """One stripped cell value; blank and missing cells are both ``None``."""
    value = row.get(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _required(row: Mapping[str, str | None], name: str) -> str:
    value = _cell(row, name)
    if value is None:
        raise ImportRowError(f"{name} is required")
    return value


def _parse_date(value: str, column: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ImportRowError(f"{column} must be an ISO date (YYYY-MM-DD): {value!r}") from None


def _parse_type(value: str) -> CompanyType:
    try:
        return CompanyType(value)
    except ValueError:
        raise ImportRowError(f"type must be one of {[t.value for t in CompanyType]}") from None


def _parse_status(row: Mapping[str, str | None]) -> str | None:
    value = _cell(row, "status")
    if value is None:
        return None
    try:
        return CompanyStatus(value).value
    except ValueError:
        raise ImportRowError(
            f"status must be one of {[s.value for s in CompanyStatus]}"
        ) from None


def _parse_address(row: Mapping[str, str | None]) -> dict[str, str | None] | None:
    """All four ``ro_*`` fields or none; ``ro_country`` is optional (§2.5)."""
    values = {field: _cell(row, f"ro_{field}") for field in _ADDRESS_FIELDS}
    present = [field for field, value in values.items() if value is not None]
    if not present:
        return None
    if len(present) != len(_ADDRESS_FIELDS):
        missing = [f"ro_{field}" for field in _ADDRESS_FIELDS if values[field] is None]
        raise ImportRowError(
            f"registered office requires all of ro_flat/ro_building/ro_street/ro_district; "
            f"missing: {', '.join(missing)}"
        )
    return {**values, "country": _cell(row, "ro_country")}


def _parse_business_nature(row: Mapping[str, str | None]) -> tuple[str | None, str | None]:
    code = _cell(row, "business_nature_code")
    description = _cell(row, "business_nature_description")
    if (code is None) != (description is None):
        raise ImportRowError(
            "business_nature_code and business_nature_description must both be present"
        )
    return code, description


def _parse_email(row: Mapping[str, str | None]) -> str | None:
    value = _cell(row, "email")
    if value is not None and not _EMAIL_PATTERN.match(value):
        raise ImportRowError(f"email is not a valid address: {value!r}")
    return value


def _parse_decimal_cell(row: Mapping[str, str | None], column: str) -> Decimal | None:
    value = _cell(row, column)
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ImportRowError(f"{column} must be a number: {value!r}") from None
    if number < 0:
        raise ImportRowError(f"{column} must not be negative: {value!r}")
    return number


def _parse_int_cell(row: Mapping[str, str | None], column: str) -> int | None:
    value = _cell(row, column)
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError:
        raise ImportRowError(f"{column} must be a whole number: {value!r}") from None
    if number < 0:
        raise ImportRowError(f"{column} must not be negative: {value!r}")
    return number


def _json_cell(row: Mapping[str, str | None], column: str) -> Any:
    value = _cell(row, column)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise ImportRowError(f"{column} must be a JSON-encoded cell") from None


def _parse_records_kept(row: Mapping[str, str | None]) -> list[dict[str, Any]] | None:
    payload = _json_cell(row, "records_kept")
    if payload is None:
        return None
    if not isinstance(payload, list) or not payload:
        raise ImportRowError("records_kept must be a non-empty JSON array")
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("description"), str):
            raise ImportRowError("records_kept items need a description string")
        address = item.get("address")
        if not isinstance(address, dict) or any(
            not isinstance(address.get(field), str) for field in _ADDRESS_FIELDS
        ):
            raise ImportRowError(
                "records_kept addresses need flat/building/street/district strings"
            )
        items.append(
            {
                "description": item["description"],
                "address": {field: address[field] for field in _ADDRESS_FIELDS}
                | {"country": address.get("country")},
            }
        )
    return items


def _parse_share_capital_classes(row: Mapping[str, str | None]) -> list[dict[str, Any]] | None:
    payload = _json_cell(row, "share_capital_classes")
    if payload is None:
        return None
    if not isinstance(payload, list) or not payload:
        raise ImportRowError("share_capital_classes must be a non-empty JSON array")
    classes: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ImportRowError("share_capital_classes items must be JSON objects")
        class_name = item.get("class_name")
        currency = item.get("currency")
        total_shares = item.get("total_shares")
        if not isinstance(class_name, str) or not class_name:
            raise ImportRowError("share_capital_classes items need a class_name string")
        if not isinstance(currency, str) or len(currency) != 3:
            raise ImportRowError("share_capital_classes currency must be a 3-letter code")
        if not isinstance(total_shares, int) or total_shares < 0:
            raise ImportRowError("share_capital_classes total_shares must be a non-negative int")
        amounts = {
            key: _class_amount(item.get(key), key) for key in ("total_amount", "total_paid_up")
        }
        classes.append(
            {
                "class_name": class_name,
                "currency": currency,
                "total_shares": total_shares,
                **amounts,
            }
        )
    return classes


def _class_amount(value: Any, key: str) -> str:
    """One share-capital amount as a string, matching the PATCH path's
    ``model_dump(mode="json")`` decimal serialization."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ImportRowError(f"share_capital_classes {key} must be a number") from None
    if amount < 0:
        raise ImportRowError(f"share_capital_classes {key} must not be negative")
    return str(amount)
