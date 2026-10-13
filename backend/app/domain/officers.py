"""Officer, shareholding and person master-data rules (contract §2.4-§2.5;
migration ``0025``).

The master entity database the drafts are built from: who an officer is
(``Person`` with its identity groups), when they hold office
(``OfficerAppointment``), and what a member holds (``Shareholding``). This
module owns the part of that which is pure — the service window, the overlap
predicate the write path refuses with, and the named reasons every write is
validated against.

**The service window (§2, and the one rule F2 reads).** ``valid_from`` is the
day the officer holds office from; ``valid_to`` is the **cessation's effective
date** — the first day *out* of office. In office as of ``as_of`` means
``valid_from <= as_of < valid_to``, open-ended when ``valid_to`` is NULL, and
the two windows are therefore **half-open intervals**: adjacent appointments —
a cessation on 1 December, another office taken 1 December — do not overlap.
That is the same effective-date semantics the change events use (a change
effective on date D has happened on D), and it is what the draft construction
resolves officers with.

**Reuse, never duplicate.** The officer vocabulary, the capacity group's
role↔capacity mapping, the alternate-name rule, the §2.5 address shape and the
name-history bound are all the change-event payload grammar's, promoted to
public names in ``app.domain.change_events`` and imported here — one home per
rule, so a master record and a payload cannot disagree about what an officer
is.

**The refusal vocabulary.** Validators return *named reasons* rather than
messages: one request, one answer, and the reason is what contract §5.1 has
the API print verbatim (``alternate_to_required``, ``address_required``,
``capacity_role_mismatch``, ``valid_to_not_after_valid_from``,
``shareholding_invalid: <field>``, …). Two deliberate extensions of that
vocabulary, both stated here because they are the module's own:

- a *scalar* value that breaks a reused text bound keeps the change-events
  violation string for it (``name_en: longer than 200 characters``) — the
  bound is the grammar's, and inventing a named reason for it would state the
  same rule twice;
- ``person_name_required`` is the whole of the name-presence rule, per type:
  a natural person needs one of the four name parts, a body corporate its
  registered ``name_en``.

**The §1 protected class stays unbuilt.** ``hkid_full``, ``hkid_check_digit``,
``passport_full`` and ``usual_residential_address`` have no column (contract
§1 stores that class encrypted with per-tenant keys, §8/Phase 7 owns the
encryption) and no validator here accepts them: the public-page values
(``hkid_partial``, ``passport_partial``, ``passport_issuing_country_region``)
are what an officer record carries until then.

Pure and framework-free per ADR-0006: the standard library and
``app.domain.change_events`` only.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.domain.change_events import (
    MAX_CAPACITIES,
    MAX_ID_CHARS,
    MAX_TEXT_CHARS,
    OFFICE_CAPACITIES,
    OfficerCapacity,
    OfficerRole,
    address_violations,
    alternate_to_required,
    name_history_violations,
    text_violations,
)


class PersonType(StrEnum):
    """``Person.person_type`` (contract §2.4 identity group; mirrors the DB
    CHECK). A natural person carries the four name parts and a correspondence
    address; a body corporate its registered ``name_en`` and a registered
    office address."""

    NATURAL_PERSON = "natural_person"
    BODY_CORPORATE = "body_corporate"


# The master record's own bounds: the column lengths for the values whose
# shape is the column's, and the change-event bounds for the values that are
# the same kinds of value there (a name, an identifier, an alternate's name).
MAX_COUNTRY_REGION_CHARS = 100
MAX_CLASS_NAME_CHARS = 100
MAX_JOINTLY_HELD_CHARS = 300
MAX_REMARKS_CHARS = 500


def in_office(valid_from: date, valid_to: date | None, as_of: date) -> bool:
    """Whether an appointment holds office as of ``as_of`` (§2).

    Half-open: the appointment day is in, the cessation day is out — the day
    the change is effective is the day it has happened (the change events'
    rule), so ``valid_to`` is the first day *out* of office.
    """
    return valid_from <= as_of and (valid_to is None or as_of < valid_to)


def overlaps(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    """Whether two half-open windows share a day (§2; ``None`` is open-ended).

    The predicate behind the write path's refusal of two overlapping
    appointments of one person in one role. Touching windows — one ending the
    day another starts — do **not** overlap: that is the same day the office
    changes hands, and refusing it would make a same-day succession
    unrecordable.
    """
    latest_start = max(a_from, b_from)
    ends = [end for end in (a_to, b_to) if end is not None]
    if not ends:
        return True
    return latest_start < min(ends)


def decimal_value(value: object) -> Decimal | None:
    """``value`` as a finite ``Decimal``, or ``None`` when it is not one.

    The write path's parse: money and percentages cross the API as strings
    (the house ``Numeric`` convention) and land in the database as
    ``Decimal``. A value that is not a decimal number — a word, a boolean,
    ``NaN``, an empty string — is ``None``, which the shareholding validator
    turns into its field-named reason and the caller must not store.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def person_violations(
    *,
    person_type: PersonType,
    name_zh: object = None,
    name_en: object = None,
    surname_en: object = None,
    other_names_en: object = None,
    previous_names: object = None,
    aliases: object = None,
    br_number: object = None,
    hkid_partial: object = None,
    passport_partial: object = None,
    passport_issuing_country_region: object = None,
    tcsp_licence: object = None,
) -> list[str]:
    """The named reasons a person's officer-facing fields are refused, or ``[]``.

    The name-presence rule is per type (§2.4): a natural person needs at least
    one of the four name parts — which is also the relaxed
    ``persons_some_name_check`` the database now carries — and a body
    corporate its registered English name. The name history, the identification
    values and the TCSP licence are validated when they are present; the
    object's absence is not a violation, because the columns are nullable.

    The §1 protected class is deliberately not among these fields: it has no
    column and no branch here (see the module docstring).
    """
    kind = PersonType(person_type)
    names = {
        "name_zh": name_zh,
        "name_en": name_en,
        "surname_en": surname_en,
        "other_names_en": other_names_en,
    }
    reasons: list[str] = []
    for part, value in names.items():
        reasons.extend(_bounded(value, path=part, max_chars=MAX_TEXT_CHARS))
    if kind is PersonType.BODY_CORPORATE:
        if not _named(names["name_en"]):
            reasons.append("person_name_required")
    elif not any(_named(value) for value in names.values()):
        reasons.append("person_name_required")
    for path, history in (("previous_names", previous_names), ("aliases", aliases)):
        if history is not None and name_history_violations(history, path=path):
            reasons.append("name_history_invalid")
    for path, value, bound in (
        ("br_number", br_number, MAX_ID_CHARS),
        ("hkid_partial", hkid_partial, MAX_ID_CHARS),
        ("passport_partial", passport_partial, MAX_ID_CHARS),
        (
            "passport_issuing_country_region",
            passport_issuing_country_region,
            MAX_COUNTRY_REGION_CHARS,
        ),
    ):
        reasons.extend(_bounded(value, path=path, max_chars=bound))
    if tcsp_licence is not None and not _tcsp_licence_valid(tcsp_licence):
        reasons.append("tcsp_licence_invalid")
    return _dedupe(reasons)


def appointment_violations(
    *,
    person_type: PersonType,
    role: OfficerRole,
    capacity: object,
    valid_from: date,
    valid_to: date | None = None,
    alternate_to: object = None,
    correspondence_address: object = None,
    registered_office_address: object = None,
) -> list[str]:
    """The named reasons an appointment is refused, or ``[]``.

    Four groups, one list, because the caller's remedy is one request: the
    capacity group (the boxes ticked, whether the role prints them, and the
    alternate name §2.4 ties to the alternate box), the purpose-split address
    the person's type carries, and the service window. The person's own
    identity fields are not here — they belong to the person row and its
    write path.
    """
    reasons = _capacity_reasons(role=role, capacity=capacity, alternate_to=alternate_to)
    reasons.extend(
        _address_reasons(
            person_type=PersonType(person_type),
            correspondence_address=correspondence_address,
            registered_office_address=registered_office_address,
        )
    )
    if valid_to is not None and valid_to <= valid_from:
        reasons.append("valid_to_not_after_valid_from")
    return _dedupe(reasons)


def shareholding_violations(
    *,
    class_name: object,
    shares_held: object,
    valid_from: date,
    jointly_held: object = None,
    remarks: object = None,
    percentage_held: object = None,
    amount_paid: object = None,
    amount_unpaid: object = None,
    valid_to: date | None = None,
) -> list[str]:
    """The named reasons a shareholding is refused, or ``[]``.

    One reason per offending field, and the field is in the reason
    (``shareholding_invalid: shares_held``) — the member boxes are what NAR1
    prints, so a caller must be told which one to fix. The three numeric
    fields are the column's: integers cannot be negative, a percentage lives
    inside 0-100 (Schedule 2's own range), and money is not negative either.
    The window is the appointment's rule (§2): ``valid_to`` is the day the
    holding ends, so it must be strictly after ``valid_from`` — the database
    CHECK is the backstop, not the answer.
    """
    reasons: list[str] = []
    if not _named(class_name) or len(str(class_name)) > MAX_CLASS_NAME_CHARS:
        reasons.append("shareholding_invalid: class_name")
    if isinstance(shares_held, bool) or not isinstance(shares_held, int) or shares_held < 0:
        reasons.append("shareholding_invalid: shares_held")
    for path, value, bound in (
        ("jointly_held", jointly_held, MAX_JOINTLY_HELD_CHARS),
        ("remarks", remarks, MAX_REMARKS_CHARS),
    ):
        if value is not None and (not _named(value) or len(str(value)) > bound):
            reasons.append(f"shareholding_invalid: {path}")
    for path, value, maximum in (
        ("percentage_held", percentage_held, Decimal("100")),
        ("amount_paid", amount_paid, None),
        ("amount_unpaid", amount_unpaid, None),
    ):
        reason = _amount_reason(value, field=path, maximum=maximum)
        if reason is not None:
            reasons.append(reason)
    if valid_to is not None and valid_to <= valid_from:
        reasons.append("valid_to_not_after_valid_from")
    return _dedupe(reasons)


def _named(value: object) -> bool:
    """Whether the value is a text with something in it."""
    return isinstance(value, str) and bool(value.strip())


def _bounded(value: object, *, path: str, max_chars: int) -> list[str]:
    """The reused text bound for a value that is *there*.

    Absence and blankness are the presence rules' business (a blank name is a
    missing name, not a length problem), so only a non-blank value is bounded.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    return text_violations(value, path=path, max_chars=max_chars, required=False)


def _capacity_value(value: object) -> OfficerCapacity | None:
    """The capacity as its vocabulary member, or ``None`` when it is not one."""
    if isinstance(value, OfficerCapacity):
        return value
    if isinstance(value, str) and value in {member.value for member in OfficerCapacity}:
        return OfficerCapacity(value)
    return None


def _capacity_reasons(
    *, role: OfficerRole, capacity: object, alternate_to: object
) -> list[str]:
    """The capacity group's reasons (the mapping and the alternate rule are the
    payload grammar's, imported)."""
    reasons: list[str] = []
    values: list[OfficerCapacity] = []
    if isinstance(capacity, list):
        for value in capacity:
            member = _capacity_value(value)
            if member is None:
                reasons.append("capacity_invalid")
            else:
                values.append(member)
        if len(capacity) > MAX_CAPACITIES or len(set(values)) != len(values):
            reasons.append("capacity_invalid")
    elif capacity is not None:
        reasons.append("capacity_invalid")
    allowed = OFFICE_CAPACITIES[role]
    for member in values:
        if member not in allowed:
            reasons.append("capacity_role_mismatch")
    ticked = set(values)
    if alternate_to_required(ticked):
        if not _named(alternate_to):
            reasons.append("alternate_to_required")
        else:
            reasons.extend(
                _bounded(alternate_to, path="alternate_to", max_chars=MAX_TEXT_CHARS)
            )
    elif alternate_to is not None:
        reasons.append("alternate_to_not_applicable")
    return _dedupe(reasons)


def _address_reasons(
    *,
    person_type: PersonType,
    correspondence_address: object,
    registered_office_address: object,
) -> list[str]:
    """The purpose-split address rule (§2.4/§3.4): exactly the type's own.

    A natural person files a correspondence address, a body corporate the
    registered office — the appointment carries the address **as filed**, so
    the wrong one beside the right one is a filing that prints two different
    addresses, and it is refused rather than silently dropped.
    """
    if person_type is PersonType.BODY_CORPORATE:
        path, required, other = (
            "registered_office_address",
            registered_office_address,
            correspondence_address,
        )
    else:
        path, required, other = (
            "correspondence_address",
            correspondence_address,
            registered_office_address,
        )
    reasons: list[str] = []
    if required is None:
        reasons.append("address_required")
    elif address_violations(required, path=path):
        reasons.append("address_invalid")
    if other is not None:
        reasons.append("address_not_applicable")
    return reasons


def _amount_reason(value: object, *, field: str, maximum: Decimal | None) -> str | None:
    """``shareholding_invalid: <field>`` when the amount is not in range."""
    if value is None:
        return None
    amount = decimal_value(value)
    if amount is None or amount < 0 or (maximum is not None and amount > maximum):
        return f"shareholding_invalid: {field}"
    return None


def _tcsp_licence_valid(value: object) -> bool:
    """Form note 20's either/or: a licence number, or ``not_required`` with a
    reason — exactly one of the two, both branches stated."""
    if not isinstance(value, Mapping):
        return False
    if value.get("not_required") is True:
        return _named(value.get("not_required_reason")) and (
            len(str(value["not_required_reason"])) <= MAX_TEXT_CHARS
        ) and value.get("licence_no") is None
    return _named(value.get("licence_no")) and len(str(value["licence_no"])) <= MAX_ID_CHARS


def _dedupe(reasons: list[str]) -> list[str]:
    """The reasons in the order raised, each once."""
    return list(dict.fromkeys(reasons))


__all__ = [
    "MAX_CLASS_NAME_CHARS",
    "MAX_COUNTRY_REGION_CHARS",
    "MAX_JOINTLY_HELD_CHARS",
    "MAX_REMARKS_CHARS",
    "PersonType",
    "appointment_violations",
    "decimal_value",
    "in_office",
    "overlaps",
    "person_violations",
    "shareholding_violations",
]
