"""Officer/shareholding master-data domain tests (contract §2.4-§2.5).

Pure rules, no I/O: the service window (§2's ``[valid_from, valid_to)``
semantics — in office on the first day, out on the cessation day), the
overlap predicate the appointment write path refuses with, and the named
reasons every write is validated against. The wire behaviour lives in
``test_api_officers.py`` and the records themselves in
``test_officers_scenarios_db.py``.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.domain.change_events import (
    OFFICE_CAPACITIES,
    OfficerCapacity,
    OfficerRole,
    alternate_to_required,
)
from app.domain.officers import (
    PersonType,
    appointment_violations,
    in_office,
    overlaps,
    person_violations,
    shareholding_violations,
)

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}


def natural_person(**overrides: object) -> dict[str, object]:
    person: dict[str, object] = {"person_type": PersonType.NATURAL_PERSON, "name_zh": "陳大文"}
    person.update(overrides)
    return person


def appointment(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "person_type": PersonType.NATURAL_PERSON,
        "role": OfficerRole.DIRECTOR,
        "capacity": [OfficerCapacity.DIRECTOR.value],
        "valid_from": date(2026, 9, 1),
        "valid_to": None,
        "correspondence_address": ADDRESS,
    }
    values.update(overrides)
    return values


def reasons(**overrides: object) -> list[str]:
    """The appointment validator's reasons with the base fields overridden."""
    return appointment_violations(**appointment(**overrides))  # type: ignore[arg-type]


def person(**overrides: object) -> list[str]:
    """The person validator's reasons with the base fields overridden."""
    return person_violations(**natural_person(**overrides))  # type: ignore[arg-type]


# --- The service window (§2) -------------------------------------------------

VALID_FROM = date(2026, 9, 1)
VALID_TO = date(2026, 12, 1)


def test_the_valid_from_day_is_in_office() -> None:
    """The window is half-open: appointment day counts (a change effective on
    D has happened on D — the change events' own rule)."""
    assert in_office(VALID_FROM, VALID_TO, VALID_FROM)
    assert in_office(VALID_FROM, None, VALID_FROM)


def test_the_valid_to_day_is_out_of_office() -> None:
    """``valid_to`` is the cessation's effective date: the first day out."""
    assert not in_office(VALID_FROM, VALID_TO, VALID_TO)
    assert in_office(VALID_FROM, VALID_TO, date(2026, 11, 30))


def test_an_open_ended_window_stays_in_office() -> None:
    assert in_office(VALID_FROM, None, date(2099, 1, 1))
    assert not in_office(VALID_FROM, None, date(2026, 8, 31))


def test_a_day_before_the_window_is_not_in_office() -> None:
    assert not in_office(VALID_FROM, VALID_TO, date(2026, 8, 31))


def test_a_zero_length_window_holds_nothing() -> None:
    """Unrepresentable at the database (``valid_to > valid_from`` is a CHECK),
    and the predicate agrees: nothing is in office for a whole day that ends
    where it starts."""
    assert not in_office(VALID_FROM, VALID_FROM, VALID_FROM)


# --- The overlap predicate ---------------------------------------------------


def test_touching_windows_do_not_overlap() -> None:
    """Half-open intervals: a cessation on 1 December and an appointment on
    1 December are adjacent, not overlapping — the officer left and another
    (or the same person in another capacity) took office the same day."""
    assert not overlaps(VALID_FROM, VALID_TO, VALID_TO, None)
    assert not overlaps(VALID_TO, None, VALID_FROM, VALID_TO)


def test_any_shared_day_overlaps() -> None:
    assert overlaps(VALID_FROM, VALID_TO, date(2026, 11, 30), date(2027, 1, 1))
    assert overlaps(VALID_FROM, VALID_TO, date(2026, 8, 1), date(2026, 9, 2))


def test_containment_and_identical_windows_overlap() -> None:
    assert overlaps(VALID_FROM, VALID_TO, VALID_FROM, VALID_TO)
    assert overlaps(VALID_FROM, VALID_TO, date(2026, 10, 1), date(2026, 11, 1))
    assert overlaps(date(2026, 10, 1), date(2026, 11, 1), VALID_FROM, VALID_TO)


def test_open_ended_windows_overlap_anything_they_reach() -> None:
    assert overlaps(VALID_FROM, None, VALID_TO, None)
    assert overlaps(VALID_FROM, None, date(2026, 8, 1), date(2027, 1, 1))
    assert not overlaps(VALID_FROM, None, date(2026, 8, 1), VALID_FROM)


def test_two_open_ended_windows_overlap() -> None:
    assert overlaps(VALID_FROM, None, date(2026, 10, 1), None)


# --- The role ↔ capacity mapping (one home: app.domain.change_events) --------


def test_the_mapping_is_the_change_events_one() -> None:
    """The master data's capacity rule is the payload grammar's, imported —
    not a second copy that could drift."""
    assert OFFICE_CAPACITIES[OfficerRole.COMPANY_SECRETARY] == {
        OfficerCapacity.COMPANY_SECRETARY
    }
    assert OFFICE_CAPACITIES[OfficerRole.DIRECTOR] == {
        OfficerCapacity.DIRECTOR,
        OfficerCapacity.ALTERNATE_DIRECTOR,
    }
    assert OFFICE_CAPACITIES[OfficerRole.RESERVE_DIRECTOR] == {
        OfficerCapacity.DIRECTOR,
        OfficerCapacity.ALTERNATE_DIRECTOR,
    }
    assert alternate_to_required([OfficerCapacity.ALTERNATE_DIRECTOR])
    assert not alternate_to_required([OfficerCapacity.DIRECTOR])


def test_a_capacity_the_role_cannot_carry_is_a_role_mismatch() -> None:
    """A company secretary's block has no director box (the registry prints the
    capacity group per office)."""
    assert reasons(role=OfficerRole.COMPANY_SECRETARY, capacity=["director"]) == [
        "capacity_role_mismatch"
    ]
    assert reasons(role=OfficerRole.DIRECTOR, capacity=["company_secretary"]) == [
        "capacity_role_mismatch"
    ]


def test_a_value_outside_the_registrys_boxes_is_invalid() -> None:
    """``reserve_director`` is NAR1's separate Reserve Director block — an
    office, not one of the three capacity boxes the form prints."""
    assert reasons(capacity=["reserve_director"]) == ["capacity_invalid"]
    assert reasons(capacity=["mood"]) == ["capacity_invalid"]
    assert reasons(capacity="director") == ["capacity_invalid"]
    assert reasons(capacity=["director", "director"]) == ["capacity_invalid"]


def test_the_capacity_list_may_be_empty() -> None:
    """The column is NOT NULL with an empty-array default: an office recorded
    without its ticked boxes is storable (the boxes are what ND2A prints, and
    NAR1's blocks tick none)."""
    assert reasons(capacity=[]) == []


def test_the_alternate_capacity_requires_the_name_it_substitutes_for() -> None:
    """§2.4's own rule, promoted from the payload grammar: ``alternate_to``
    exists exactly when a capacity is "Alternate to 代替"."""
    assert reasons(capacity=["director", "alternate_director"]) == ["alternate_to_required"]
    assert reasons(capacity=["alternate_director"], alternate_to="   ") == [
        "alternate_to_required"
    ]
    assert reasons(
        capacity=["director", "alternate_director"], alternate_to="CHAN Tai Man"
    ) == []
    assert reasons(alternate_to="CHAN Tai Man") == ["alternate_to_not_applicable"]


# --- Addresses (purpose-split, §2.5) -----------------------------------------


def test_a_natural_person_carries_a_correspondence_address_only() -> None:
    assert reasons() == []
    assert reasons(correspondence_address=None) == ["address_required"]
    assert reasons(registered_office_address=ADDRESS) == ["address_not_applicable"]


def test_a_body_corporate_carries_a_registered_office_address_only() -> None:
    body = {
        "person_type": PersonType.BODY_CORPORATE,
        "registered_office_address": ADDRESS,
        "correspondence_address": None,
    }
    assert reasons(**body) == []
    assert reasons(**{**body, "registered_office_address": None}) == ["address_required"]
    assert reasons(**{**body, "correspondence_address": ADDRESS}) == [
        "address_not_applicable"
    ]


def test_an_address_the_four_box_rule_refuses_is_invalid() -> None:
    """§5.1.1's all-or-nothing rule, reused from the payload grammar: an
    address that names a street and no district is not one a form can print."""
    assert reasons(correspondence_address={"flat": "12A"}) == ["address_invalid"]
    assert reasons(correspondence_address="12A Bright Tower") == ["address_invalid"]
    assert reasons(correspondence_address=ADDRESS | {"country": "Hong Kong"}) == []


# --- Person names per type ---------------------------------------------------


def test_a_natural_person_needs_at_least_one_name_part() -> None:
    assert person(name_zh="陳大文") == []
    assert person(name_zh=None, name_en="CHAN Tai Man") == []
    assert person(name_zh=None, name_en=None, surname_en="CHAN") == []
    assert person(name_zh=None, name_en=None, surname_en=None, other_names_en="Tai Man") == []
    assert person(name_zh=None) == ["person_name_required"]
    assert person(name_zh="   ") == ["person_name_required"]


def test_a_body_corporate_needs_its_registered_name() -> None:
    """§2.4: the body corporate's identity is ``name_en`` (a Chinese name is
    optional beside it); a body corporate with only a Chinese name is refused."""
    assert person(person_type=PersonType.BODY_CORPORATE, name_en="BRIGHT LIMITED") == []
    assert (
        person(person_type=PersonType.BODY_CORPORATE, name_en="BRIGHT LIMITED", name_zh="明亮")
        == []
    )
    assert person(person_type=PersonType.BODY_CORPORATE, name_en=None, name_zh="明亮") == [
        "person_name_required"
    ]
    assert person(person_type=PersonType.BODY_CORPORATE, name_en=None) == [
        "person_name_required"
    ]


def test_the_name_check_is_the_relaxed_one_the_database_carries() -> None:
    """The migration relaxed ``persons_some_name_check`` to the four name
    columns: English name parts are names, and the domain agrees."""
    assert person(name_zh=None, name_en=None, surname_en="CHAN", other_names_en="Tai Man") == []


# --- Name history and aliases ------------------------------------------------


def test_the_name_history_is_the_canonical_pair_list() -> None:
    assert person(previous_names=[{"name_zh": "陳小明", "name_en": "CHAN Siu Ming"}]) == []
    assert person(aliases=[{"name_en": "Tai Man"}]) == []
    assert person(previous_names=[{}]) == ["name_history_invalid"]
    assert person(aliases=["CHAN Siu Ming"]) == ["name_history_invalid"]
    assert person(previous_names={"name_en": "CHAN"}) == ["name_history_invalid"]


def test_the_name_history_bound_is_the_change_events_one() -> None:
    entry = {"name_en": "CHAN Siu Ming"}
    assert person(previous_names=[entry] * 20) == []
    assert person(previous_names=[entry] * 21) == ["name_history_invalid"]


# --- TCSP licence (form note 20) ---------------------------------------------


def test_the_tcsp_licence_is_a_licence_number_or_a_stated_refusal() -> None:
    assert person(tcsp_licence={"licence_no": "TC000123", "not_required": False}) == []
    assert (
        person(
            tcsp_licence={
                "not_required": True,
                "not_required_reason": "Sole director, no TCSP service provided",
            }
        )
        == []
    )
    assert person(tcsp_licence={"licence_no": None, "not_required": False}) == [
        "tcsp_licence_invalid"
    ]
    assert person(tcsp_licence={"not_required": True, "not_required_reason": "  "}) == [
        "tcsp_licence_invalid"
    ]
    assert person(
        tcsp_licence={"licence_no": "TC000123", "not_required": True, "not_required_reason": "x"}
    ) == ["tcsp_licence_invalid"]
    assert person(tcsp_licence="TC000123") == ["tcsp_licence_invalid"]


def test_an_absent_tcsp_licence_is_not_a_violation() -> None:
    """The object is optional; when it is there, it must say one of the two
    things (the form's own either/or)."""
    assert person(tcsp_licence=None) == []


# --- Windows on the appointment ----------------------------------------------


def test_the_window_must_end_after_it_starts() -> None:
    """The database CHECK and the domain agree: ``valid_to`` is the first day
    *out* of office, so it must be strictly after ``valid_from``."""
    assert reasons(valid_to=date(2026, 9, 2)) == []
    assert reasons(valid_to=date(2026, 9, 1)) == ["valid_to_not_after_valid_from"]
    assert reasons(valid_to=date(2026, 8, 31)) == ["valid_to_not_after_valid_from"]
    assert reasons(valid_to=None) == []


# --- Shareholdings -----------------------------------------------------------


def shareholding(**overrides: object) -> list[str]:
    values: dict[str, object] = {
        "class_name": "Ordinary",
        "shares_held": 1_000,
        "jointly_held": None,
        "remarks": None,
        "percentage_held": Decimal("10.00"),
        "amount_paid": Decimal("1000.00"),
        "amount_unpaid": Decimal("0.00"),
        "valid_from": date(2026, 9, 1),
        "valid_to": None,
    }
    values.update(overrides)
    return shareholding_violations(**values)  # type: ignore[arg-type]


def test_a_complete_shareholding_is_accepted() -> None:
    assert shareholding() == []
    assert shareholding(jointly_held="CHAN Tai Man", remarks="Joint tenant") == []
    assert shareholding(percentage_held=None, amount_paid=None, amount_unpaid=None) == []
    assert shareholding(shares_held=0, amount_paid=Decimal("0")) == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"class_name": ""}, "shareholding_invalid: class_name"),
        ({"class_name": "   "}, "shareholding_invalid: class_name"),
        ({"shares_held": -1}, "shareholding_invalid: shares_held"),
        ({"percentage_held": Decimal("-0.01")}, "shareholding_invalid: percentage_held"),
        ({"percentage_held": Decimal("100.01")}, "shareholding_invalid: percentage_held"),
        ({"amount_paid": Decimal("-1")}, "shareholding_invalid: amount_paid"),
        ({"amount_unpaid": Decimal("-0.01")}, "shareholding_invalid: amount_unpaid"),
        ({"remarks": "x" * 501}, "shareholding_invalid: remarks"),
        ({"jointly_held": "x" * 301}, "shareholding_invalid: jointly_held"),
    ],
)
def test_the_shareholding_refusals_name_the_field(
    overrides: dict[str, object], reason: str
) -> None:
    assert reason in shareholding(**overrides)


def test_the_percentage_bounds_are_inclusive() -> None:
    assert shareholding(percentage_held=Decimal("0")) == []
    assert shareholding(percentage_held=Decimal("100")) == []


def test_the_shareholding_window_must_end_after_it_starts() -> None:
    """A member position carries the same service window an office does (§2):
    the database CHECK is the backstop, and the domain's named reason is what
    the caller is answered with."""
    assert shareholding(valid_to=date(2026, 9, 2)) == []
    assert shareholding(valid_to=None) == []
    assert shareholding(valid_to=date(2026, 9, 1)) == ["valid_to_not_after_valid_from"]
    assert shareholding(valid_to=date(2026, 8, 31)) == ["valid_to_not_after_valid_from"]


# --- Composition: the reasons are the ones the API answers -------------------


def test_the_appointment_validator_collects_every_reason_once() -> None:
    """One request, one answer: the refusal names every problem, in a stable
    order, without repeating a reason a second field raised too."""
    found = reasons(
        role=OfficerRole.COMPANY_SECRETARY,
        capacity=["director", "mood"],
        alternate_to="CHAN Tai Man",
        valid_to=VALID_FROM,
        correspondence_address=None,
        registered_office_address=ADDRESS,
    )
    assert found == [
        "capacity_invalid",
        "capacity_role_mismatch",
        "alternate_to_not_applicable",
        "address_required",
        "address_not_applicable",
        "valid_to_not_after_valid_from",
    ]
