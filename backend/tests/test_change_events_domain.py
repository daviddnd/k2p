"""Change-event domain tests (data-model §2.6; scope §2.7-§2.8; contract §4.1).

Pure rules, no I/O: the vocabularies, the §4.1 transition table as data plus
its permission legs (ADR-0008's requirement — the table is tested against
scope §2.8's action matrix row by row, divergences included and named), the
single generic guard over it, the wired/unwired split, and the typed-payload
grammar. The wire behaviour lives in ``test_api_change_events.py`` and the
record itself in ``test_change_events_scenarios_db.py``.
"""

from datetime import date

import pytest

from app.domain.change_events import (
    ACTION_PERMISSIONS,
    AWAITING_REVIEW_STATUSES,
    CHANGE_EVENT_TRANSITIONS,
    EDIT_FIELDS,
    PROTECTED_CLASS_UNAVAILABLE,
    RESERVED_TYPES,
    UNWIRED_REASONS,
    WIRED_ACTIONS,
    Address,
    CessationReason,
    ChangeEventAction,
    ChangeEventRelation,
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
    OfficerCapacity,
    OfficerRole,
    admits,
    allowed_actions,
    event_violations,
    is_awaiting_review,
    is_reserved,
    is_wired,
    permission_for,
    protected_class_fields,
    rationale_required,
    unwired_reason,
)
from app.domain.rbac import Permission
from app.domain.service_renewals import can_transition

STATUSES = tuple(ChangeEventStatus)
ACTIONS = tuple(ChangeEventAction)


# --- The vocabularies are the contract's ---


def test_the_status_vocabulary_is_the_nine_the_contract_names() -> None:
    """§2.6/§4.1 fix the nine statuses; migration ``0024``'s CHECK is the
    database's copy. Pinned as strings so a rename cannot quietly leave the
    column accepting a status the code no longer produces."""
    assert [status.value for status in ChangeEventStatus] == [
        "pending",
        "client_reconfirming",
        "under_review",
        "reviewed",
        "confirmed",
        "applied_to_draft",
        "submitted",
        "withdrawn",
        "deleted",
    ]


def test_the_type_vocabulary_is_the_one_the_registry_maps() -> None:
    """The superset §2.6 names — the reply-parse enum plus
    ``company_name_change`` (reserved for Wave 2/NNC2) and ``other``."""
    assert [event_type.value for event_type in ChangeEventType] == [
        "director_appointment",
        "director_cessation",
        "secretary_appointment",
        "secretary_cessation",
        "registered_office_change",
        "company_name_change",
        "other",
    ]


def test_the_source_relation_and_officer_vocabularies() -> None:
    """§2.6's ``source_type`` and ``relation``, and the two value vocabularies
    the two typed payload shapes carry."""
    assert [source.value for source in ChangeEventSourceType] == [
        "communication",
        "document",
        "manual",
        # Workstream ③ (migration 0036): a work-item parse's proposals name
        # their source; the value is in the vocabulary because the column's
        # CHECK admits it.
        "split_work_item",
    ]
    assert [relation.value for relation in ChangeEventRelation] == [
        "corrects",
        "withdraws",
        "supersedes",
    ]
    assert [role.value for role in OfficerRole] == [
        "company_secretary",
        "director",
        "reserve_director",
    ]
    assert [reason.value for reason in CessationReason] == [
        "resignation_others",
        "deceased",
    ]


def test_the_capacity_vocabulary_is_the_registrys_own() -> None:
    """The three boxes ``backend/forms/nd2a/mapping.json`` prints in every
    officer block — pinned as strings, because the registry is canonical: a
    rename here would be a grammar that no longer matches the form, and the
    values are not the role vocabulary's. ND2A's 候補董事 is the
    ``alternate_director`` capacity here; NAR1 records that same office through
    its own 候補董事 checkbox (``cb_2_P.5``) as ``officers[].alternate_to``, while
    ``reserve_director`` is NAR1's separate Reserve Director block (備任董事,
    Cap. 622 s.455) — a different office, and a role rather than a capacity."""
    assert [capacity.value for capacity in OfficerCapacity] == [
        "company_secretary",
        "director",
        "alternate_director",
    ]


# --- The review-queue vocabulary ---------------------------------------------


def test_the_awaiting_review_statuses_are_the_two_staff_work_waits_at() -> None:
    """The review queue and the dashboard's pending-event-review widget list
    one set: ``pending`` (arrived, not started) and ``under_review`` (started,
    not finished). ``client_reconfirming`` is out with the client and returns
    to ``pending`` on a reply; ``reviewed`` is the *approval* gate's (scope
    §2.3's separate widget); the terminal statuses are work nobody has left to
    do. Pinned as strings so the queue's query and the widget cannot drift
    apart, and the predicate is checked across the whole vocabulary."""
    assert {status.value for status in AWAITING_REVIEW_STATUSES} == {
        "pending",
        "under_review",
    }
    assert [status for status in ChangeEventStatus if is_awaiting_review(status)] == [
        ChangeEventStatus.PENDING,
        ChangeEventStatus.UNDER_REVIEW,
    ]


# --- The §4.1 table, as data -------------------------------------------------

# The contract's §4.1 table transcribed: every From/Action/To row, the
# unwired ones included (ADR-0008: the table is the spec's, whole). Written
# out here so a change to the code's table is a change to this test.
EXPECTED_TABLE: dict[ChangeEventStatus, dict[ChangeEventAction, ChangeEventStatus]] = {
    ChangeEventStatus.PENDING: {
        ChangeEventAction.MARK_UNDER_REVIEW: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.SEND_RECONFIRMATION: ChangeEventStatus.CLIENT_RECONFIRMING,
        ChangeEventAction.EDIT: ChangeEventStatus.PENDING,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.CLIENT_RECONFIRMING: {
        ChangeEventAction.CLIENT_REPLY: ChangeEventStatus.PENDING,
        ChangeEventAction.OVERRIDE: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.MARK_UNDER_REVIEW: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.EDIT: ChangeEventStatus.CLIENT_RECONFIRMING,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.UNDER_REVIEW: {
        ChangeEventAction.MARK_REVIEWED: ChangeEventStatus.REVIEWED,
        ChangeEventAction.REVERT: ChangeEventStatus.PENDING,
        ChangeEventAction.REJECT: ChangeEventStatus.DELETED,
        ChangeEventAction.EDIT: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.REVIEWED: {
        ChangeEventAction.CONFIRM: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.REVERT: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.REJECT: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.CONFIRMED: {
        ChangeEventAction.APPLY: ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventAction.REVERT: ChangeEventStatus.REVIEWED,
        ChangeEventAction.EDIT: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.APPLIED_TO_DRAFT: {
        ChangeEventAction.REVERT: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.SUBMIT: ChangeEventStatus.SUBMITTED,
        ChangeEventAction.EDIT: ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.SUBMITTED: {
        # §4.1's target is "(new event)": a supersession creates the new event
        # and leaves the submitted one where it is — which is why the row's
        # target here is its own status, and the side effect is Phase 5's.
        ChangeEventAction.SUPERSEDE: ChangeEventStatus.SUBMITTED,
    },
    ChangeEventStatus.WITHDRAWN: {},
    ChangeEventStatus.DELETED: {},
}


def test_the_transition_table_is_the_contracts() -> None:
    assert {status: dict(actions) for status, actions in CHANGE_EVENT_TRANSITIONS.items()} == (
        EXPECTED_TABLE
    )


def test_every_status_is_a_row_and_every_action_has_a_row() -> None:
    """All nine statuses are in the table (the two terminal ones with no
    entries), and no action in the vocabulary is unreachable: an action the
    table never admits anywhere is a name the API would accept and refuse for
    the wrong reason."""
    assert set(CHANGE_EVENT_TRANSITIONS) == set(STATUSES)
    admitted = {
        action for actions in CHANGE_EVENT_TRANSITIONS.values() for action in actions
    }
    assert admitted == set(ACTIONS)


# --- The §2.8 matrix, row by row (ADR-0008) ---------------------------------

# Scope §2.8's "Allowed actions by status" table, transcribed: (reviewer's
# column, approver's column). The client's column is its own rule below.
#
# Two places where §4.1 is *narrower* than the matrix, named rather than
# smoothed over: §2.8 gives the client a "Reply" at every open status, while
# §4.1 admits ``client_reply`` only from ``client_reconfirming`` (the
# reconfirmation exchange is the parser's, and a client's unsolicited reply is
# a new communication, not an action on an event); and §2.8 gives the approver
# an "Edit" at ``reviewed``, which §4.1 does not admit (a reviewed event is
# confirmed or rejected, not silently rewritten).
MATRIX: dict[ChangeEventStatus, tuple[set[ChangeEventAction], set[ChangeEventAction]]] = {
    ChangeEventStatus.PENDING: (
        {
            ChangeEventAction.MARK_UNDER_REVIEW,
            ChangeEventAction.SEND_RECONFIRMATION,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
        },
        set(),
    ),
    ChangeEventStatus.CLIENT_RECONFIRMING: (
        {
            ChangeEventAction.OVERRIDE,
            ChangeEventAction.MARK_UNDER_REVIEW,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
        },
        set(),
    ),
    ChangeEventStatus.UNDER_REVIEW: (
        {
            ChangeEventAction.MARK_REVIEWED,
            ChangeEventAction.REVERT,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
        },
        {ChangeEventAction.REJECT},
    ),
    ChangeEventStatus.REVIEWED: (
        {ChangeEventAction.REVERT},
        {ChangeEventAction.CONFIRM, ChangeEventAction.REJECT},
    ),
    ChangeEventStatus.CONFIRMED: (
        set(),
        {
            ChangeEventAction.REVERT,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
        },
    ),
    ChangeEventStatus.APPLIED_TO_DRAFT: (
        set(),
        {
            ChangeEventAction.REVERT,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
        },
    ),
    ChangeEventStatus.SUBMITTED: (set(), {ChangeEventAction.SUPERSEDE}),
}


@pytest.mark.parametrize("status", list(MATRIX))
def test_the_reviewer_and_approver_columns_of_the_matrix(status: ChangeEventStatus) -> None:
    """Each status's reviewer actions carry ``review_events`` and its approver
    actions ``approve_events``; the union is what the matrix grants staff. The
    client's own withdraw is not in either column — §2.8 lists it under "Client
    Can", and its staff-recorded half is the test below."""
    reviewer, approver = MATRIX[status]
    legs = {
        action: permission_for(status, action)
        for action in CHANGE_EVENT_TRANSITIONS[status]
        if action is not ChangeEventAction.CLIENT_WITHDRAW
    }
    assert {action for action, leg in legs.items() if leg is Permission.REVIEW_EVENTS} == reviewer
    assert {action for action, leg in legs.items() if leg is Permission.APPROVE_EVENTS} == approver


@pytest.mark.parametrize(
    "status",
    [status for status in MATRIX if status is not ChangeEventStatus.SUBMITTED],
)
def test_a_client_can_withdraw_everywhere_the_matrix_gives_a_client_an_action(
    status: ChangeEventStatus,
) -> None:
    """§4.1's "any non-submitted" row, spelled out over the statuses §2.8's
    matrix gives a client a withdraw at. The two terminal statuses
    (``withdrawn``, ``deleted``) are deliberately not in it: the matrix ends at
    ``submitted``, and withdrawing a withdrawn event is not a product state.
    The staff-recorded half (scope §2.7's phone/in-person case) rides the
    reviewer leg."""
    assert admits(status, ChangeEventAction.CLIENT_WITHDRAW)
    assert permission_for(status, ChangeEventAction.CLIENT_WITHDRAW) is Permission.REVIEW_EVENTS


def test_the_terminal_statuses_admit_nothing() -> None:
    """§2.8: nothing is hard-deleted, and both terminal statuses are ends —
    a withdrawn or deleted event has no action left, including a second
    withdrawal."""
    assert CHANGE_EVENT_TRANSITIONS[ChangeEventStatus.WITHDRAWN] == {}
    assert CHANGE_EVENT_TRANSITIONS[ChangeEventStatus.DELETED] == {}
    assert not admits(ChangeEventStatus.WITHDRAWN, ChangeEventAction.CLIENT_WITHDRAW)
    assert not admits(ChangeEventStatus.DELETED, ChangeEventAction.DELETE)


def test_the_system_rows_have_no_staff_leg() -> None:
    """The three rows §4.1 gives to the system — the reconfirmation reply, the
    draft application and the submission — admit no staff actor at all: no
    permission can perform them (they are the parser's and Phase 5's)."""
    for status, action in (
        (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.CLIENT_REPLY),
        (ChangeEventStatus.CONFIRMED, ChangeEventAction.APPLY),
        (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.SUBMIT),
    ):
        assert admits(status, action)
        assert permission_for(status, action) is None


def test_the_guard_is_the_generic_one_over_this_table() -> None:
    """ADR-0008's single guard, asked of this table — the same function the
    renewal lifecycle uses, not a copy of it."""
    assert can_transition(
        CHANGE_EVENT_TRANSITIONS,
        status=ChangeEventStatus.PENDING,
        action=ChangeEventAction.MARK_UNDER_REVIEW,
    )
    assert not can_transition(
        CHANGE_EVENT_TRANSITIONS,
        status=ChangeEventStatus.PENDING,
        action=ChangeEventAction.CONFIRM,
    )
    assert not can_transition(
        CHANGE_EVENT_TRANSITIONS,
        status=ChangeEventStatus.SUBMITTED,
        action=ChangeEventAction.CONFIRM,
    )
    assert admits(ChangeEventStatus.REVIEWED, ChangeEventAction.CONFIRM)
    assert not admits(ChangeEventStatus.REVIEWED, ChangeEventAction.EDIT)


def test_the_permission_legs_cover_exactly_the_tables_rows() -> None:
    """Every row has a leg — reviewer, approver or system — and no leg exists
    for a row the table does not admit."""
    rows = {
        (status, action)
        for status, actions in CHANGE_EVENT_TRANSITIONS.items()
        for action in actions
    }
    assert set(ACTION_PERMISSIONS) == rows
    assert {leg for leg in ACTION_PERMISSIONS.values()} == {
        Permission.REVIEW_EVENTS,
        Permission.APPROVE_EVENTS,
        None,
    }


# --- Wired and unwired -------------------------------------------------------


def test_wired_and_unwired_partition_the_vocabulary() -> None:
    """Every action is either implemented or refused with a named reason. An
    action in neither set is a hole: the API would accept the name and then
    have nothing to say about it."""
    assert set(WIRED_ACTIONS) | set(UNWIRED_REASONS) == set(ACTIONS)
    assert not (set(WIRED_ACTIONS) & set(UNWIRED_REASONS))


def test_the_wired_actions_are_the_ones_the_slice_builds() -> None:
    """CE1's list plus F2's system leg plus M5b's reconfirmation exchange,
    pinned: the lifecycle walks, the revisions, the soft ends — ``apply``,
    which Phase 5 wires for the draft-generation path alone (a system row: no
    staff leg, never offered by the transitions read) — and M5b's two rows:
    ``send_reconfirmation`` (the review queue's re-ask; the compose-store-
    release is ``app.application.reconfirmation``'s) and ``client_reply`` (the
    system row only the reply parser's landing performs — no staff leg, never
    offered). P6-4 wires the third system row: ``submit`` is the move the
    draft's export performs for the events the filing carries. ``override``
    stays unwired (``mark_under_review`` covers its need); ``supersede`` on
    both."""
    expected_wired = frozenset(
        {
            ChangeEventAction.MARK_UNDER_REVIEW,
            ChangeEventAction.SEND_RECONFIRMATION,
            ChangeEventAction.CLIENT_REPLY,
            ChangeEventAction.REVERT,
            ChangeEventAction.MARK_REVIEWED,
            ChangeEventAction.REJECT,
            ChangeEventAction.CONFIRM,
            ChangeEventAction.APPLY,
            ChangeEventAction.SUBMIT,
            ChangeEventAction.EDIT,
            ChangeEventAction.DELETE,
            ChangeEventAction.CLIENT_WITHDRAW,
        }
    )
    assert expected_wired == WIRED_ACTIONS
    assert UNWIRED_REASONS == {
        ChangeEventAction.OVERRIDE: "reconfirmation_not_wired",
        ChangeEventAction.SUPERSEDE: "supersede_not_wired",
    }


def test_the_unwired_reason_is_named_for_every_unwired_action() -> None:
    assert is_wired(ChangeEventAction.CONFIRM)
    assert unwired_reason(ChangeEventAction.CONFIRM) is None
    assert is_wired(ChangeEventAction.APPLY)
    assert unwired_reason(ChangeEventAction.APPLY) is None
    # P6-4: ``submit`` is wired as the system row the export performs —
    # ``draft_not_wired`` left the vocabulary with it.
    assert is_wired(ChangeEventAction.SUBMIT)
    assert unwired_reason(ChangeEventAction.SUBMIT) is None
    assert "draft_not_wired" not in UNWIRED_REASONS.values()


def test_a_rationale_is_required_where_the_table_says_so() -> None:
    """§4.1's two "rationale required" rows — ``reject`` and the
    staff-recorded client withdrawal — and nothing else; a confirm or a
    revert needs no reason (nothing is being refused)."""
    assert rationale_required(ChangeEventAction.REJECT)
    assert rationale_required(ChangeEventAction.CLIENT_WITHDRAW)
    assert not rationale_required(ChangeEventAction.CONFIRM)
    assert not rationale_required(ChangeEventAction.MARK_UNDER_REVIEW)
    assert not rationale_required(ChangeEventAction.EDIT)


# --- The typed payloads ------------------------------------------------------

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}

# The §2.4/registry officer vocabulary minus the protected class — every field
# the registry's ND2A targets name that the grammar accepts. The four §1
# protected-class fields are refused by name (the ruling of 2026-09-25): they
# exist for the ND2A PI sheet, and no field-level encryption exists to store
# them under. ``PROTECTED_CLASS`` below holds them for the refusal tests.
FULL_OFFICER: dict[str, object] = {
    "role": "director",
    "name_zh": "陳大文",
    "name_en": "CHAN Tai Man",
    "surname_en": "CHAN",
    "other_names_en": "Tai Man",
    "previous_names": [{"name_zh": "陳小明", "name_en": "CHAN Siu Ming"}],
    "aliases": [{"name_zh": "大文", "name_en": "Tai Man"}],
    "email": "chan@brightlantern.example.hk",
    "br_number": "12345678",
    "correspondence_address": ADDRESS,
    "registered_office_address": ADDRESS,
    "hkid_partial": "A1234",
    "passport_partial": "K123456",
    "passport_issuing_country_region": "HKSAR",
    "tcsp_licence": {"licence_no": "TC000123", "not_required": False, "not_required_reason": None},
    "appointment_date": "2026-09-01",
    "existing_director_at_appointment": False,
}

# §1's protected class: full HKID/passport and the usual residential address.
# Contract §1 stores this class encrypted, and no field-level encryption (or
# per-tenant key) exists yet (§8 scope, Phase 7) — so the grammar refuses all
# four rather than making a change-event payload the first place the plaintext
# lives.
PROTECTED_CLASS: dict[str, object] = {
    "hkid_full": "A123456",
    "hkid_check_digit": "7",
    "passport_full": "K12345678",
    "usual_residential_address": ADDRESS,
}

FULL_CESSATION_OFFICER: dict[str, object] = {
    "role": "company_secretary",
    "name_zh": "陳大文",
    "name_en": "CHAN Tai Man",
    "surname_en": "CHAN",
    "other_names_en": "Tai Man",
    "hkid_partial": "A1234",
    "passport_partial": "K123456",
    "cessation_reason": "resignation_others",
    "cessation_date": "2026-09-10",
    "continues_office_after_cessation": True,
}


def appointment(**overrides: object) -> dict[str, object]:
    """A full appointment payload with the officer's fields overridden."""
    officer = {**FULL_OFFICER, **overrides}
    return {"appointment": {"officer": officer}}


def cessation(**overrides: object) -> dict[str, object]:
    officer = {**FULL_CESSATION_OFFICER, **overrides}
    return {"cessation": {"officer": officer}}


def violations(
    payload: object,
    *,
    event_type: ChangeEventType = ChangeEventType.DIRECTOR_APPOINTMENT,
    effective_date: date = date(2026, 9, 1),
    informed_date: date | None = None,
) -> list[str]:
    return event_violations(
        event_type=event_type,
        effective_date=effective_date,
        informed_date=informed_date,
        payload=payload,
    )


def test_the_full_officer_vocabulary_is_accepted() -> None:
    assert violations(appointment()) == []
    assert violations(
        cessation(), event_type=ChangeEventType.SECRETARY_CESSATION
    ) == []


def test_the_capacity_group_enters_an_alternate_director() -> None:
    """ND2A's capacity checkboxes, in the registry's own values: the
    ``alternate_director`` 候補董事 box and the ``alternate_to`` 代替 box are how
    an alternate director is recorded, and both shapes carry them (the
    appointment block and the cessation block print the same group)."""
    alternate = {
        "capacity": ["director", "alternate_director"],
        "alternate_to": "CHAN Tai Man",
    }

    assert violations(appointment(**alternate)) == []
    assert violations(
        cessation(**alternate, role="director"),
        event_type=ChangeEventType.DIRECTOR_CESSATION,
    ) == []
    # A company secretary's block ticks the secretary box; the list is what the
    # form prints, not the §2.4 master role.
    assert violations(
        cessation(capacity=["company_secretary"]),
        event_type=ChangeEventType.SECRETARY_CESSATION,
    ) == []


def test_the_alternate_director_capacity_requires_the_name_it_substitutes_for() -> None:
    """§2.4's own rule: ``alternate_to`` exists *only* when a capacity is
    "Alternate to 代替" — required with the alternate capacity, refused without
    it, so the form's text box and its checkbox cannot disagree."""
    missing = violations(appointment(capacity=["alternate_director"]))
    assert any(
        "alternate_to" in violation and "required" in violation for violation in missing
    ), missing

    unbacked = violations(appointment(alternate_to="CHAN Tai Man"))
    assert any(
        "alternate_to" in violation and "alternate_director" in violation for violation in unbacked
    ), unbacked

    blank = violations(
        appointment(capacity=["alternate_director"], alternate_to="   ")
    )
    assert blank


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A capacity the registry's group does not print: NAR1's separate
        # Reserve Director block (`reserve_director`, 備任董事, Cap. 622 s.455)
        # is another office entirely, not one of this form's capacity boxes.
        ({"capacity": ["reserve_director"]}, "capacity"),
        # A capacity the officer's role cannot carry: a company secretary's
        # block has no alternate box.
        ({"role": "company_secretary", "capacity": ["director"]}, "capacity"),
        # Two ticks of the same box.
        (
            {
                "capacity": ["director", "director"],
                "alternate_to": "CHAN Tai Man",
            },
            "capacity",
        ),
        # Not a list at all.
        ({"capacity": "director"}, "capacity"),
        ({"capacity": []}, "capacity"),
        ({"alternate_to": 7}, "alternate_to"),
        # A name longer than the bound.
        (
            {"capacity": ["alternate_director"], "alternate_to": "x" * 201},
            "alternate_to",
        ),
    ],
)
def test_the_capacity_group_refuses(overrides: dict[str, object], expected: str) -> None:
    found = violations(appointment(**overrides))
    assert any(f"officer.{expected}" in violation for violation in found), found


def test_the_role_vocabulary_and_the_capacity_vocabulary_are_distinct() -> None:
    """``alternate_director`` is a capacity, never a role, and
    ``reserve_director`` is a role — NAR1's separate Reserve Director block,
    another office — that no capacity list may claim: the two vocabularies are
    the form's two different things, and swapping them is a refusal rather than
    a silent reinterpretation."""
    assert violations(appointment(role="alternate_director")) != []
    assert violations(appointment(capacity=["reserve_director"])) != []


def test_the_partial_identification_fields_are_accepted() -> None:
    """§2.4's public-page identification values — what the ND2A's public pages
    carry — stay enterable, so the protected-class refusal does not make a real
    event unenterable: the partials and the issuing country are ordinary
    payload fields."""
    assert violations(appointment(hkid_partial="A1234", passport_partial="K123456")) == []
    assert violations(appointment(passport_issuing_country_region="HKSAR")) == []
    assert violations(
        cessation(hkid_partial="A1234", passport_partial="K123456"),
        event_type=ChangeEventType.SECRETARY_CESSATION,
    ) == []


@pytest.mark.parametrize("field", sorted(PROTECTED_CLASS))
def test_each_protected_class_field_is_refused_by_name(field: str) -> None:
    """§1's protected class is **refused**, not stored (ruling 2026-09-25):
    the class is stored encrypted and no field-level encryption exists yet
    (§8 scope, Phase 7), so a change-event payload must not become the first
    place the plaintext lives. The refusal names the reason and the field, and
    the field's path is what the API answers with."""
    payload = appointment(**{field: PROTECTED_CLASS[field]})

    found = violations(payload)

    assert any(
        f"officer.{field}" in violation and PROTECTED_CLASS_UNAVAILABLE in violation
        for violation in found
    ), found
    assert protected_class_fields(payload) == [f"payload.appointment.officer.{field}"]
    assert protected_class_fields(appointment()) == []


@pytest.mark.parametrize("field", sorted(PROTECTED_CLASS))
def test_the_protected_class_is_refused_wherever_it_appears(field: str) -> None:
    """Not only the appointment shape the registry names it in: the refusal
    holds for the cessation shape too, and for a type whose grammar has no
    officer block at all — the class is refused wherever the payload carries
    it, whatever the event claims to be."""
    payload = cessation(**{field: PROTECTED_CLASS[field]})
    found = violations(payload, event_type=ChangeEventType.SECRETARY_CESSATION)
    assert any(PROTECTED_CLASS_UNAVAILABLE in violation for violation in found), found

    elsewhere = {"registered_office": {"address": ADDRESS | {field: PROTECTED_CLASS[field]}}}
    found = violations(elsewhere, event_type=ChangeEventType.REGISTERED_OFFICE_CHANGE)
    assert any(PROTECTED_CLASS_UNAVAILABLE in violation for violation in found), found


def test_the_registered_office_payload_is_the_address_value_object() -> None:
    assert (
        violations(
            {"registered_office": {"address": ADDRESS}},
            event_type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
        )
        == []
    )
    # The four core boxes are the §5.1.1 all-or-nothing rule, and the country
    # is the one optional part (§2.5 — omitted for HK-only addresses).
    assert violations(
        {"registered_office": {"address": ADDRESS | {"country": "Hong Kong"}}},
        event_type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
    ) == []
    assert violations(
        {"registered_office": {"address": {"flat": "12A", "building": "Bright Tower"}}},
        event_type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
    )


def test_an_other_event_carries_one_bounded_summary() -> None:
    """``other`` is the one shape the vocabulary cannot enumerate (scope §2.7's
    unclassifiable case), so it carries one bounded line of staff's own words —
    and a change that cannot say what it is is refused rather than stored."""
    accepted = violations(
        {"summary": "Auditor changed to KPMG"}, event_type=ChangeEventType.OTHER
    )
    assert accepted == []
    assert violations({}, event_type=ChangeEventType.OTHER)
    assert violations({"summary": "   "}, event_type=ChangeEventType.OTHER)
    assert violations({"summary": "x" * 201}, event_type=ChangeEventType.OTHER)
    assert violations({"summary": "two\nlines"}, event_type=ChangeEventType.OTHER)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        # An unknown key anywhere in the shape: the grammar is a closed set,
        # not a filter.
        (ChangeEventType.DIRECTOR_APPOINTMENT, {"appointment": {"officer": {"mood": "good"}}}),
        (ChangeEventType.DIRECTOR_APPOINTMENT, {"appointment": {"officer": {}, "extra": 1}}),
        (ChangeEventType.DIRECTOR_APPOINTMENT, {"cessation": {"officer": {}}}),
        (ChangeEventType.DIRECTOR_APPOINTMENT, []),
        # The derived count is refused by name, not merely as an unknown key.
        (
            ChangeEventType.DIRECTOR_APPOINTMENT,
            {"appointment": {"officer": {"continuation_sheet_count": 2}}},
        ),
        # A wrong value type where the grammar wants a text or a date.
        (ChangeEventType.DIRECTOR_APPOINTMENT, {"appointment": {"officer": {"role": 7}}}),
        (ChangeEventType.DIRECTOR_APPOINTMENT, appointment(appointment_date="1 September")),
        (
            ChangeEventType.DIRECTOR_APPOINTMENT,
            appointment(existing_director_at_appointment="maybe"),
        ),
        (ChangeEventType.DIRECTOR_APPOINTMENT, appointment(name_en="x" * 201)),
        # A missing substance field.
        (
            ChangeEventType.SECRETARY_APPOINTMENT,
            {"appointment": {"officer": {"appointment_date": "2026-09-01", "role": "director"}}},
        ),
        (
            ChangeEventType.SECRETARY_APPOINTMENT,
            {"appointment": {"officer": {"role": "company_secretary"}}},
        ),
        (
            ChangeEventType.DIRECTOR_CESSATION,
            {"cessation": {"officer": {"role": "director", "name_en": "CHAN Tai Man"}}},
        ),
        (
            ChangeEventType.DIRECTOR_CESSATION,
            {"cessation": {"officer": {"role": "director", "cessation_date": "2026-09-10"}}},
        ),
        (
            ChangeEventType.DIRECTOR_CESSATION,
            {"cessation": {"officer": {}}},
        ),
        # An address the four-box rule refuses.
        (ChangeEventType.DIRECTOR_APPOINTMENT, appointment(correspondence_address={"flat": "12A"})),
        # A tcsp_licence that says neither a licence number nor why none is
        # needed (form note 20's either/or).
        (ChangeEventType.DIRECTOR_APPOINTMENT, appointment(tcsp_licence={"licence_no": None})),
        (
            ChangeEventType.DIRECTOR_APPOINTMENT,
            appointment(tcsp_licence={"not_required": True, "not_required_reason": None}),
        ),
        # A name history entry that names nothing at all.
        (ChangeEventType.DIRECTOR_APPOINTMENT, appointment(previous_names=[{}])),
        # An unclassifiable ``other`` payload on a typed event.
        (ChangeEventType.DIRECTOR_APPOINTMENT, {"summary": "wrong shape"}),
    ],
)
def test_the_grammar_refuses(event_type: ChangeEventType, payload: object) -> None:
    assert violations(payload, event_type=event_type) != []


def test_the_role_must_be_coherent_with_the_type() -> None:
    """A ``secretary_appointment`` naming a ``director`` is a category error,
    not a nuance: the role is what the ND2A block and the master data will
    be written from."""
    assert violations(
        appointment(role="director"), event_type=ChangeEventType.SECRETARY_APPOINTMENT
    )
    assert violations(
        appointment(role="company_secretary"), event_type=ChangeEventType.DIRECTOR_APPOINTMENT
    )
    # A reserve director is a directorship — NAR1's separate Reserve Director
    # block (備任董事, Cap. 622 s.455), not an alternate capacity — so the
    # director types admit the role (§2.4's vocabulary).
    assert violations(
        appointment(role="reserve_director"), event_type=ChangeEventType.DIRECTOR_APPOINTMENT
    ) == []
    assert violations(
        cessation(role="director"), event_type=ChangeEventType.SECRETARY_CESSATION
    )


def test_a_continuation_sheet_count_is_named_as_derived() -> None:
    """Contract §3: the count is derived at package assembly and was removed
    from the payload vocabulary. The refusal says so rather than calling it an
    unknown key, because the caller's mistake is a real one."""
    found = violations(appointment(continuation_sheet_count=2))
    assert any("derived" in violation for violation in found), found


def test_the_informed_date_rule_is_the_contracts() -> None:
    """§2.6: ``informed_date ≥ effective_date``. Equal is legal — the TCSP
    learned of it the day it took effect."""
    assert violations(
        appointment(), effective_date=date(2026, 9, 1), informed_date=date(2026, 9, 1)
    ) == []
    assert violations(
        appointment(), effective_date=date(2026, 9, 1), informed_date=date(2026, 10, 1)
    ) == []
    found = violations(
        appointment(), effective_date=date(2026, 9, 1), informed_date=date(2026, 8, 31)
    )
    assert found and any("informed_date" in violation for violation in found)


def test_the_reserved_type_is_refused_by_name() -> None:
    """§2.6: ``company_name_change`` is reserved for Wave 2/NNC2 — the type is
    in the vocabulary (the registry's enum is the superset) and entry with it
    is refused, never stored."""
    assert is_reserved(ChangeEventType.COMPANY_NAME_CHANGE)
    assert not is_reserved(ChangeEventType.DIRECTOR_APPOINTMENT)
    assert {ChangeEventType.COMPANY_NAME_CHANGE} == RESERVED_TYPES
    found = violations({"anything": 1}, event_type=ChangeEventType.COMPANY_NAME_CHANGE)
    assert any("reserved" in violation for violation in found), found


def test_the_edit_fields_are_the_revisioning_ones() -> None:
    """§4.1: an edit is a new version of type/dates/payload. The linkage and
    the provenance are not editable — they are how the event got here."""
    assert {
        "type",
        "effective_date",
        "effective_date_text",
        "informed_date",
        "payload",
    } == EDIT_FIELDS


def test_the_address_value_object_is_the_contracts() -> None:
    assert Address.__annotations__.keys() == {"flat", "building", "street", "district", "country"}


# --- The allowed-actions read (the transitions route's model) ----------------


def test_the_allowed_actions_read_is_the_table_and_the_actors_legs() -> None:
    """§4.1's read, purely: what a reviewer may do at ``pending`` (M5b's
    ``send_reconfirmation`` offered between the two lifecycle walks, then the
    edit and the soft ends — and the staff-recorded client withdrawal), what an
    approver may do there (nothing), and the two legs together at ``reviewed``,
    where ``reject`` carries the rationale requirement."""
    reviewer = allowed_actions(
        status=ChangeEventStatus.PENDING, may_review=True, may_approve=False
    )
    assert [
        (item.action, item.to_status, item.rationale_required) for item in reviewer
    ] == [
        (ChangeEventAction.MARK_UNDER_REVIEW, ChangeEventStatus.UNDER_REVIEW, False),
        (ChangeEventAction.SEND_RECONFIRMATION, ChangeEventStatus.CLIENT_RECONFIRMING, False),
        (ChangeEventAction.EDIT, ChangeEventStatus.PENDING, False),
        (ChangeEventAction.DELETE, ChangeEventStatus.DELETED, False),
        (ChangeEventAction.CLIENT_WITHDRAW, ChangeEventStatus.WITHDRAWN, True),
    ]
    assert (
        allowed_actions(status=ChangeEventStatus.PENDING, may_review=False, may_approve=True) == []
    )
    both = allowed_actions(
        status=ChangeEventStatus.REVIEWED, may_review=True, may_approve=True
    )
    assert [(item.action, item.to_status, item.rationale_required) for item in both] == [
        (ChangeEventAction.CONFIRM, ChangeEventStatus.CONFIRMED, False),
        (ChangeEventAction.REVERT, ChangeEventStatus.UNDER_REVIEW, False),
        (ChangeEventAction.REJECT, ChangeEventStatus.DELETED, True),
        (ChangeEventAction.CLIENT_WITHDRAW, ChangeEventStatus.WITHDRAWN, True),
    ]


def test_no_unwired_or_system_row_is_ever_offered_as_an_allowed_action() -> None:
    """Whatever the status and however armed the actor is, the read offers
    only rows the executor would actually perform: an unwired action would be
    a button that always 409s, and a system row is nobody's to press —
    ``apply``, ``client_reply`` and, since P6-4, ``submit`` included."""
    for status in STATUSES:
        for may_review, may_approve in ((True, False), (False, True), (True, True), (False, False)):
            offered = {
                item.action
                for item in allowed_actions(
                    status=status, may_review=may_review, may_approve=may_approve
                )
            }
            assert offered <= WIRED_ACTIONS
            assert ChangeEventAction.SUBMIT not in offered
            assert all(admits(status, action) for action in offered)


def test_the_reconfirmation_exchange_rows_in_the_read() -> None:
    """M5b's wiring, from the read's side: ``send_reconfirmation`` is a
    reviewer's offer at ``pending`` alone (the only status the table admits
    it from), and ``client_reply`` is wired yet never offered — the system row
    the parser's landing performs, nobody's button — at ``client_reconfirming``
    or anywhere else. ``override`` stays unwired, so it is not offered either."""
    pending = allowed_actions(
        status=ChangeEventStatus.PENDING, may_review=True, may_approve=True
    )
    assert ChangeEventAction.SEND_RECONFIRMATION in {item.action for item in pending}
    for status in STATUSES:
        offered = {
            item.action
            for item in allowed_actions(
                status=status, may_review=True, may_approve=True
            )
        }
        assert ChangeEventAction.CLIENT_REPLY not in offered
        assert ChangeEventAction.OVERRIDE not in offered
        if status is not ChangeEventStatus.PENDING:
            assert ChangeEventAction.SEND_RECONFIRMATION not in offered

