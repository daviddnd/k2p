"""Domain rules of the draft lifecycle and the resolution engine (contract §4.2,
§5.1; scope §2.9).

The three claims that need a pure test rather than a route or a database:

- **the §4.2 table is the contract's**, row by row (ADR-0008's requirement that
  the machine encode the spec whole and be updated with it);
- **the resolution rules are the scope's** — relevance and effective dates
  (§2.9 steps 1-4), the officer and registered-office overlays, the share-capital
  sums, the §2.9 step 5 critical set and the §3.6 prior-filing cross-check;
- **the wired/refused split is stated**, because a caller must meet a named
  refusal rather than a silent acceptance.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.domain.change_events import ChangeEventStatus, ChangeEventType
from app.domain.compliance import DueDateRuleNotEstablishedError
from app.domain.drafts import (
    ACTION_PERMISSIONS,
    APPROVAL_STAMP_STATUSES,
    APPROVED_EVENT_STATUSES,
    AWAITING_REVIEW_STATUSES,
    DELIVERY_CHOICES,
    DRAFT_TRANSITIONS,
    MADE_UP_DATE_PATH,
    REVIEW_STAMP_STATUSES,
    SOURCE_BACKFILL,
    SOURCE_CHANGE_EVENT,
    SOURCE_MASTER_RECORD,
    SOURCE_NONE,
    STATEMENT_CARRIED_STATUSES,
    UNWIRED_REASONS,
    WIRED_ACTIONS,
    CompanyInput,
    DraftAction,
    DraftLeg,
    DraftStatus,
    EventInput,
    MasterFact,
    OfficerInput,
    ShareholdingInput,
    SourceRef,
    admits,
    allowed_actions,
    applies_to_draft,
    build_field_provenance,
    critical_missing_paths,
    ensure_exportable,
    filing_metadata_template,
    filing_metadata_violations,
    is_relevant,
    is_wired,
    leg_for,
    merge_filing_metadata,
    nar1_made_up_date,
    prior_filing_comparison,
    rationale_required,
    resolve_draft,
    same_officer,
    unwired_reason,
)
from app.domain.prior_filing import FormCode

TARGET_DATE = date(2026, 9, 30)
COMPANY_ID = uuid4()
BATCH_ID = uuid4()
DOCUMENT_ID = uuid4()

# The nine statuses §4.2 names, and every row of its table transcribed from the
# contract (the "any pre-exported → invalidate" row spelled out over the
# statuses its wording covers).
CONTRACT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("generated", "edit", "under_review"),
    ("generated", "mark_under_review", "under_review"),
    ("under_review", "send_for_signature", "sent_for_signature"),
    ("sent_for_signature", "upload_signed", "signature_received"),
    ("sent_for_signature", "recall", "under_review"),
    ("signature_received", "mark_reviewed", "reviewed"),
    ("signature_received", "reject", "rejected"),
    ("reviewed", "approve", "approved"),
    ("reviewed", "reject", "rejected"),
    ("approved", "export", "exported"),
    ("rejected", "regenerate", "generated"),
    ("generated", "invalidate", "invalidated"),
    ("under_review", "invalidate", "invalidated"),
    ("sent_for_signature", "invalidate", "invalidated"),
    ("signature_received", "invalidate", "invalidated"),
    ("reviewed", "invalidate", "invalidated"),
    ("approved", "invalidate", "invalidated"),
    ("rejected", "invalidate", "invalidated"),
)

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}


# --- The §4.2 table ----------------------------------------------------------


def test_the_status_vocabulary_is_the_contracts_nine() -> None:
    assert [status.value for status in DraftStatus] == [
        "generated",
        "under_review",
        "sent_for_signature",
        "signature_received",
        "reviewed",
        "approved",
        "exported",
        "rejected",
        "invalidated",
    ]


def test_the_transition_table_is_the_contracts_row_by_row() -> None:
    """ADR-0008: the machine is the spec, and this test is the copy that fails
    when the contract moves without it."""
    expected: dict[str, dict[str, str]] = {status.value: {} for status in DraftStatus}
    for from_status, action, to_status in CONTRACT_ROWS:
        expected[from_status][action] = to_status
    actual = {
        status.value: {action.value: target.value for action, target in row.items()}
        for status, row in DRAFT_TRANSITIONS.items()
    }
    assert actual == expected


def test_every_status_is_a_key_and_the_ends_are_terminal() -> None:
    assert set(DRAFT_TRANSITIONS) == set(DraftStatus)
    assert DRAFT_TRANSITIONS[DraftStatus.EXPORTED] == {}
    assert DRAFT_TRANSITIONS[DraftStatus.INVALIDATED] == {}


def test_export_is_admitted_only_from_approved() -> None:
    """§4.2's invariant, enforced at exactly one point: the guard."""
    for status in DraftStatus:
        expected = status is DraftStatus.APPROVED
        assert admits(status, DraftAction.EXPORT) is expected


def test_the_awaiting_review_set_is_generated_and_under_review() -> None:
    """The dashboard widget's set, named once: work waiting on staff. A
    decided or filed draft is not work — the invalidated end and everything
    past review are out, deliberately."""
    assert {
        DraftStatus.GENERATED,
        DraftStatus.UNDER_REVIEW,
    } == AWAITING_REVIEW_STATUSES


def test_invalidate_is_the_system_row_over_every_pre_exported_status() -> None:
    for status in DraftStatus:
        pre_exported = status not in (DraftStatus.EXPORTED, DraftStatus.INVALIDATED)
        assert admits(status, DraftAction.INVALIDATE) is pre_exported
        assert leg_for(status, DraftAction.INVALIDATE) is None


def test_the_actor_column_is_the_contracts_legs() -> None:
    assert (
        leg_for(DraftStatus.GENERATED, DraftAction.EDIT) is DraftLeg.REVIEW_DRAFTS
    )
    assert (
        leg_for(DraftStatus.GENERATED, DraftAction.MARK_UNDER_REVIEW)
        is DraftLeg.REVIEW_DRAFTS
    )
    assert (
        leg_for(DraftStatus.UNDER_REVIEW, DraftAction.SEND_FOR_SIGNATURE)
        is DraftLeg.REVIEW_DRAFTS
    )
    assert (
        leg_for(DraftStatus.SENT_FOR_SIGNATURE, DraftAction.UPLOAD_SIGNED)
        is DraftLeg.COMPANY_GRANT
    )
    assert (
        leg_for(DraftStatus.SENT_FOR_SIGNATURE, DraftAction.RECALL) is DraftLeg.REVIEW_DRAFTS
    )
    assert (
        leg_for(DraftStatus.SIGNATURE_RECEIVED, DraftAction.MARK_REVIEWED)
        is DraftLeg.REVIEW_DRAFTS
    )
    assert (
        leg_for(DraftStatus.SIGNATURE_RECEIVED, DraftAction.REJECT) is DraftLeg.REVIEW_DRAFTS
    )
    assert leg_for(DraftStatus.REVIEWED, DraftAction.APPROVE) is DraftLeg.APPROVE_DRAFTS
    assert leg_for(DraftStatus.REVIEWED, DraftAction.REJECT) is DraftLeg.APPROVE_DRAFTS
    assert leg_for(DraftStatus.APPROVED, DraftAction.EXPORT) is DraftLeg.APPROVE_DRAFTS
    assert leg_for(DraftStatus.REJECTED, DraftAction.REGENERATE) is DraftLeg.REVIEW_DRAFTS


def test_every_table_row_has_a_permission_entry_and_none_else() -> None:
    """A row without a leg would be a hole the executor silently allows; an
    entry without a row would be a leg nobody can use."""
    table_rows = {
        (status, action) for status, actions in DRAFT_TRANSITIONS.items() for action in actions
    }
    assert set(ACTION_PERMISSIONS) == table_rows


def test_the_no_flag_leg_is_pinned_for_upload_signed() -> None:
    """§4.2's one "staff (any General)" row is the company grant, the decision
    every master-data route asks — not a permission flag and not the system."""
    assert (
        leg_for(DraftStatus.SENT_FOR_SIGNATURE, DraftAction.UPLOAD_SIGNED)
        is DraftLeg.COMPANY_GRANT
    )
    assert DraftLeg.COMPANY_GRANT.value == "company_grant"


def test_wired_and_unwired_partition_the_vocabulary() -> None:
    assert set(WIRED_ACTIONS) | set(UNWIRED_REASONS) == set(DraftAction)
    assert not (set(WIRED_ACTIONS) & set(UNWIRED_REASONS))


def test_the_wired_actions_and_their_named_refusals() -> None:
    assert {
        DraftAction.EDIT,
        DraftAction.MARK_UNDER_REVIEW,
        DraftAction.INVALIDATE,
        DraftAction.SEND_FOR_SIGNATURE,
        DraftAction.UPLOAD_SIGNED,
        DraftAction.RECALL,
        DraftAction.MARK_REVIEWED,
        DraftAction.REJECT,
        DraftAction.APPROVE,
        DraftAction.REGENERATE,
        DraftAction.EXPORT,
    } == WIRED_ACTIONS
    # P6-4 wired the export: the §4.2 table is fully wired and the map is
    # empty — it stays (the API tests import it) and its emptiness is the
    # partition's claim.
    assert UNWIRED_REASONS == {}
    # P6-2 wired the approval rows: ``approval_not_wired`` is deleted, and no
    # ``signature_not_wired`` remains at all — ``mark_reviewed`` joined the
    # wired set behind the signed-copy upload.
    assert "approval_not_wired" not in UNWIRED_REASONS.values()
    assert "signature_not_wired" not in UNWIRED_REASONS.values()
    # P6-3 wired regeneration: ``regenerate_not_wired`` is retired with them.
    assert "regenerate_not_wired" not in UNWIRED_REASONS.values()
    # P6-4 wired the export: ``export_not_wired`` is retired with it.
    assert "export_not_wired" not in UNWIRED_REASONS.values()
    for action in (
        DraftAction.EDIT,
        DraftAction.SEND_FOR_SIGNATURE,
        DraftAction.UPLOAD_SIGNED,
        DraftAction.RECALL,
        DraftAction.MARK_REVIEWED,
        DraftAction.REJECT,
        DraftAction.APPROVE,
        DraftAction.REGENERATE,
        DraftAction.EXPORT,
    ):
        assert is_wired(action)
        with pytest.raises(KeyError):
            unwired_reason(action)


def test_a_rationale_is_required_where_the_table_says_so() -> None:
    """§4.2's two rejections require one; the export and the approvals — where a
    rationale is *captured* — do not."""
    assert rationale_required(DraftAction.REJECT)
    assert not rationale_required(DraftAction.APPROVE)
    assert not rationale_required(DraftAction.EXPORT)
    assert not rationale_required(DraftAction.EDIT)


def test_allowed_actions_offer_the_wired_leg_admitted_rows_only() -> None:
    """A reviewer at ``generated`` sees the two wired actions and nothing else —
    not the signature rows (unwired), not ``invalidate`` (the system's)."""
    offered = allowed_actions(
        status=DraftStatus.GENERATED, may_review=True, may_approve=False, has_grant=True
    )
    assert [item.action for item in offered] == [
        DraftAction.EDIT,
        DraftAction.MARK_UNDER_REVIEW,
    ]
    assert all(item.to_status is DraftStatus.UNDER_REVIEW for item in offered)
    assert not any(item.rationale_required for item in offered)


def test_allowed_actions_respect_the_two_flags_and_the_no_flag_leg() -> None:
    assert (
        allowed_actions(
            status=DraftStatus.GENERATED, may_review=False, may_approve=True, has_grant=True
        )
        == []
    )
    assert (
        allowed_actions(
            status=DraftStatus.GENERATED, may_review=False, may_approve=False, has_grant=True
        )
        == []
    )
    # An administrator holds all three legs and still sees only the wired rows.
    assert [
        item.action
        for item in allowed_actions(
            status=DraftStatus.GENERATED, may_review=True, may_approve=True, has_grant=True
        )
    ] == [DraftAction.EDIT, DraftAction.MARK_UNDER_REVIEW]


def test_allowed_actions_never_offer_a_system_or_unwired_row() -> None:
    for status in DraftStatus:
        actions = {
            item.action
            for item in allowed_actions(
                status=status, may_review=True, may_approve=True, has_grant=True
            )
        }
        assert DraftAction.INVALIDATE not in actions
        assert actions <= {
            DraftAction.EDIT,
            DraftAction.MARK_UNDER_REVIEW,
            DraftAction.SEND_FOR_SIGNATURE,
            DraftAction.UPLOAD_SIGNED,
            DraftAction.RECALL,
            DraftAction.MARK_REVIEWED,
            DraftAction.REJECT,
            DraftAction.APPROVE,
            DraftAction.REGENERATE,
            DraftAction.EXPORT,
        }
        # P6-3: ``regenerate`` is wired and offered from ``rejected`` alone.
        assert (DraftAction.REGENERATE in actions) is (status is DraftStatus.REJECTED)
        # P6-4: ``export`` is wired and offered from ``approved`` alone.
        assert (DraftAction.EXPORT in actions) is (status is DraftStatus.APPROVED)


def test_allowed_actions_offer_export_from_approved_to_the_approver() -> None:
    """P6-4 wired the ``approved`` row: an approver is offered ``export``
    (target ``exported``, no rationale), a reviewer-only or grant-only actor
    is offered nothing — and at ``exported`` the table is empty, so the
    second export is unreachable from the read that drives the buttons."""
    approver = allowed_actions(
        status=DraftStatus.APPROVED, may_review=False, may_approve=True, has_grant=True
    )
    assert [item.action for item in approver] == [DraftAction.EXPORT]
    assert approver[0].to_status is DraftStatus.EXPORTED
    assert not approver[0].rationale_required
    assert (
        allowed_actions(
            status=DraftStatus.APPROVED, may_review=True, may_approve=False, has_grant=True
        )
        == []
    )
    assert (
        allowed_actions(
            status=DraftStatus.APPROVED, may_review=False, may_approve=False, has_grant=True
        )
        == []
    )
    assert (
        allowed_actions(
            status=DraftStatus.EXPORTED, may_review=True, may_approve=True, has_grant=True
        )
        == []
    )


def test_allowed_actions_offer_regenerate_from_rejected_to_the_reviewer() -> None:
    """P6-3 wired the ``rejected`` row: a reviewer is offered ``regenerate``
    (no rationale — the rejection's own row carried it), an approver-only or
    grant-only actor is offered nothing, and the target is §4.2's ``generated``."""
    reviewer = allowed_actions(
        status=DraftStatus.REJECTED, may_review=True, may_approve=False, has_grant=True
    )
    assert [item.action for item in reviewer] == [DraftAction.REGENERATE]
    assert reviewer[0].to_status is DraftStatus.GENERATED
    assert not reviewer[0].rationale_required
    assert (
        allowed_actions(
            status=DraftStatus.REJECTED, may_review=False, may_approve=True, has_grant=True
        )
        == []
    )
    assert (
        allowed_actions(
            status=DraftStatus.REJECTED, may_review=False, may_approve=False, has_grant=True
        )
        == []
    )


def test_allowed_actions_offer_the_signature_rows_from_sent_for_signature() -> None:
    """P6-1 wired the two ``sent_for_signature`` rows: ``upload_signed`` rides
    the no-flag company grant alone, ``recall`` needs ``review_drafts``, and
    the order is §4.2's own row order."""
    grant_only = allowed_actions(
        status=DraftStatus.SENT_FOR_SIGNATURE,
        may_review=False,
        may_approve=False,
        has_grant=True,
    )
    assert [item.action for item in grant_only] == [DraftAction.UPLOAD_SIGNED]
    reviewer = allowed_actions(
        status=DraftStatus.SENT_FOR_SIGNATURE,
        may_review=True,
        may_approve=False,
        has_grant=True,
    )
    assert [item.action for item in reviewer] == [
        DraftAction.UPLOAD_SIGNED,
        DraftAction.RECALL,
    ]
    assert not any(item.rationale_required for item in reviewer)
    # ``recall`` rides the review flag, not the grant: only ``upload_signed``
    # drops when the grant is missing (the reads refuse a grant-less actor
    # before any action is computed, so this row reaches nobody in practice).
    assert [
        item.action
        for item in allowed_actions(
            status=DraftStatus.SENT_FOR_SIGNATURE,
            may_review=True,
            may_approve=False,
            has_grant=False,
        )
    ] == [DraftAction.RECALL]


def test_reject_is_a_rationale_required_row_at_both_statuses() -> None:
    """P6-2 wired reject's two legs (§4.2: a reviewer's from
    ``signature_received``, an approver's from ``reviewed``) and both are
    "rationale required" rows — the refusal is §2.11's record of the
    decision."""
    for status in (DraftStatus.SIGNATURE_RECEIVED, DraftStatus.REVIEWED):
        assert admits(status, DraftAction.REJECT)
        assert rationale_required(DraftAction.REJECT)


def test_allowed_actions_offer_the_approval_rows_with_the_right_legs() -> None:
    """P6-2's rows per status: at ``signature_received`` the reviewer is offered
    ``mark_reviewed`` and ``reject`` (the latter rationale-required); at
    ``reviewed`` the approver is offered ``approve`` and ``reject``. The other
    flag sees nothing, and §4.2's row order is what the read returns."""
    from_signature_received = allowed_actions(
        status=DraftStatus.SIGNATURE_RECEIVED,
        may_review=True,
        may_approve=False,
        has_grant=True,
    )
    assert [item.action for item in from_signature_received] == [
        DraftAction.MARK_REVIEWED,
        DraftAction.REJECT,
    ]
    assert from_signature_received[1].rationale_required
    assert from_signature_received[0].to_status is DraftStatus.REVIEWED
    assert from_signature_received[1].to_status is DraftStatus.REJECTED
    assert (
        allowed_actions(
            status=DraftStatus.SIGNATURE_RECEIVED,
            may_review=False,
            may_approve=True,
            has_grant=True,
        )
        == []
    )
    from_reviewed = allowed_actions(
        status=DraftStatus.REVIEWED,
        may_review=False,
        may_approve=True,
        has_grant=True,
    )
    assert [item.action for item in from_reviewed] == [
        DraftAction.APPROVE,
        DraftAction.REJECT,
    ]
    assert from_reviewed[1].rationale_required
    assert (
        allowed_actions(
            status=DraftStatus.REVIEWED,
            may_review=True,
            may_approve=False,
            has_grant=True,
        )
        == []
    )


# --- The review/approval stamp sets (P6-2) -----------------------------------


def test_the_stamp_sets_are_exactly_the_contracts() -> None:
    assert frozenset(
        {DraftStatus.REVIEWED, DraftStatus.APPROVED, DraftStatus.EXPORTED}
    ) == REVIEW_STAMP_STATUSES
    assert frozenset(
        {DraftStatus.APPROVED, DraftStatus.EXPORTED}
    ) == APPROVAL_STAMP_STATUSES
    assert frozenset(
        {DraftStatus.APPROVED, DraftStatus.EXPORTED}
    ) == STATEMENT_CARRIED_STATUSES


@pytest.mark.parametrize(
    ("landing", "review", "approval", "statement"),
    [
        # Landing on `reviewed` sets the review stamp and clears the approval
        # and statement pairs.
        (DraftStatus.REVIEWED, True, False, False),
        # `approved` (and beyond) carries all three.
        (DraftStatus.APPROVED, True, True, True),
        (DraftStatus.EXPORTED, True, True, True),
        # Every other landing clears everything.
        (DraftStatus.GENERATED, False, False, False),
        (DraftStatus.UNDER_REVIEW, False, False, False),
        (DraftStatus.SENT_FOR_SIGNATURE, False, False, False),
        (DraftStatus.SIGNATURE_RECEIVED, False, False, False),
        (DraftStatus.REJECTED, False, False, False),
        (DraftStatus.INVALIDATED, False, False, False),
    ],
)
def test_stamps_cleared_semantics_table(
    landing: DraftStatus, review: bool, approval: bool, statement: bool
) -> None:
    """The change-events convention, transcribed (plan ruling: "reaching a
    status clears the stamps it does not carry"): the application layer's
    write tail asks these sets which pairs a landing carries."""
    assert (landing in REVIEW_STAMP_STATUSES) is review
    assert (landing in APPROVAL_STAMP_STATUSES) is approval
    assert (landing in STATEMENT_CARRIED_STATUSES) is statement


# --- The export gate's one rule (P6-2, called by P6-4's export) ---------------


@dataclass(frozen=True)
class _GateRow:
    """The row face ``ensure_exportable`` reads — the persistence Draft
    satisfies it, the test builds it directly."""

    form_type: str
    private_company_statement: bool | None
    statement_confirmed_by: UUID | None


@pytest.mark.parametrize(
    ("form_type", "statement", "confirmed_by", "expected"),
    [
        # An NAR1 draft making the statement, unconfirmed, refuses export.
        ("nar1", True, None, "statement_not_confirmed"),
        # The confirmation pair set is what clears the refusal.
        ("nar1", True, uuid4(), None),
        # A draft whose flag is false or absent makes no statement to confirm.
        ("nar1", False, None, None),
        ("nar1", None, None, None),
        # ND2A has no item-16 statement at all.
        ("nd2a", None, None, None),
        ("nd2a", True, None, None),
    ],
)
def test_ensure_exportable_is_the_statement_truth_table(
    form_type: str,
    statement: bool | None,
    confirmed_by: UUID | None,
    expected: str | None,
) -> None:
    """``ensure_exportable`` is domain-owned so the export route and the
    executor share one rule (brief P6-2); the approved hard gate itself is the
    §4.2 guard, a separate mechanism."""
    row = _GateRow(
        form_type=form_type,
        private_company_statement=statement,
        statement_confirmed_by=confirmed_by,
    )
    assert ensure_exportable(row) == expected


# --- §3.1 relevance and effective dates -------------------------------------


def test_form_relevance_is_the_contracts_split() -> None:
    """NAR1 takes the officer events and the registered-office change; ND2A
    takes the officer events alone."""
    officer_types = (
        ChangeEventType.DIRECTOR_APPOINTMENT,
        ChangeEventType.SECRETARY_APPOINTMENT,
        ChangeEventType.DIRECTOR_CESSATION,
        ChangeEventType.SECRETARY_CESSATION,
    )
    for event_type in officer_types:
        assert is_relevant(FormCode.NAR1, event_type)
        assert is_relevant(FormCode.ND2A, event_type)
    assert is_relevant(FormCode.NAR1, ChangeEventType.REGISTERED_OFFICE_CHANGE)
    assert not is_relevant(FormCode.ND2A, ChangeEventType.REGISTERED_OFFICE_CHANGE)
    for form in (FormCode.NAR1, FormCode.ND2A):
        assert not is_relevant(form, ChangeEventType.OTHER)
        assert not is_relevant(form, ChangeEventType.COMPANY_NAME_CHANGE)


def test_the_approved_set_is_confirmed_and_applied() -> None:
    assert {
        ChangeEventStatus.CONFIRMED,
        ChangeEventStatus.APPLIED_TO_DRAFT,
    } == APPROVED_EVENT_STATUSES
    for status in ChangeEventStatus:
        assert applies_to_draft(
            form_type=FormCode.NAR1,
            event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
            status=status,
            effective_date=TARGET_DATE,
            target_date=TARGET_DATE,
        ) is (status in APPROVED_EVENT_STATUSES)


def test_the_effective_date_boundary_is_inclusive() -> None:
    """A change effective on the target date has happened as of it; one
    effective the day after has not."""
    def applies(effective_date: date) -> bool:
        return applies_to_draft(
            form_type=FormCode.NAR1,
            event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
            status=ChangeEventStatus.CONFIRMED,
            effective_date=effective_date,
            target_date=TARGET_DATE,
        )

    assert applies(TARGET_DATE)
    assert applies(date(2026, 9, 29))
    assert not applies(date(2026, 10, 1))


# --- The NAR1 made-up date ---------------------------------------------------


def test_the_made_up_date_is_the_anniversary_on_or_before_the_target() -> None:
    anchor = date(2010, 3, 1)
    assert nar1_made_up_date(
        company_type="private", anniversary_date=anchor, target_date=date(2026, 9, 30)
    ) == date(2026, 3, 1)
    # The anniversary day itself, and a target before it in the same year.
    assert nar1_made_up_date(
        company_type="private", anniversary_date=anchor, target_date=date(2026, 3, 1)
    ) == date(2026, 3, 1)
    assert nar1_made_up_date(
        company_type="private", anniversary_date=anchor, target_date=date(2026, 2, 28)
    ) == date(2025, 3, 1)


def test_a_february_29_anchor_clamps_in_a_non_leap_year() -> None:
    assert nar1_made_up_date(
        company_type="private",
        anniversary_date=date(2012, 2, 29),
        target_date=date(2027, 3, 1),
    ) == date(2027, 2, 28)


def test_a_non_private_company_has_no_established_rule() -> None:
    for company_type in ("public", "company_limited_by_guarantee"):
        with pytest.raises(DueDateRuleNotEstablishedError):
            nar1_made_up_date(
                company_type=company_type,
                anniversary_date=date(2010, 3, 1),
                target_date=TARGET_DATE,
            )


# --- §3.2's identity match ---------------------------------------------------


def test_the_partial_hkid_wins_when_the_payload_carries_one() -> None:
    record = {"name_zh": "陳大文", "name_en": "CHAN Tai Man", "hkid_partial": "A1234"}
    assert same_officer(record=record, officer={"hkid_partial": "A1234", "name_en": "X"})
    assert not same_officer(record=record, officer={"hkid_partial": "B9999", "name_en": "X"})
    # An hkid the record does not carry is not a match, whatever the names say.
    assert not same_officer(
        record={"name_en": "CHAN Tai Man"},
        officer={"hkid_partial": "A1234", "name_en": "CHAN Tai Man"},
    )


def test_the_name_pair_matches_across_the_two_english_shapes() -> None:
    record = {"name_zh": "陳大文", "surname_en": "CHAN", "other_names_en": "Tai Man"}
    assert same_officer(
        record=record, officer={"name_zh": "陳大文", "name_en": "CHAN Tai Man"}
    )
    assert same_officer(
        record={"name_en": "CHAN Tai Man"},
        officer={"surname_en": "CHAN", "other_names_en": "Tai Man"},
    )
    assert not same_officer(record=record, officer={"name_en": "CHAN Tai Man"})
    assert not same_officer(record=record, officer={"name_zh": "陳大文", "name_en": "CHAN T M"})
    assert not same_officer(record=record, officer={})


# --- The resolution ----------------------------------------------------------

def company_input() -> CompanyInput:
    """A private company with a name, an office, contacts and share capital."""
    return CompanyInput(
        company_id=COMPANY_ID,
        statics={
            "br_number": "12345678",
            "cr_number": "1234567",
            "type": "private",
            "date_of_incorporation": date(2010, 3, 1),
            "anniversary_date": date(2010, 3, 1),
        },
        status="active",
        facts={
            "name": MasterFact(
                values={
                    "name_en": "BRIGHT LANTERN LIMITED",
                    "name_zh": "明燈有限公司",
                    "business_name": None,
                },
                effective_date=date(2020, 1, 1),
                source=SourceRef(SOURCE_MASTER_RECORD, uuid4()),
            ),
            "registered_office": MasterFact(
                values={**ADDRESS, "country": None},
                effective_date=date(2020, 1, 1),
                source=SourceRef(SOURCE_MASTER_RECORD, uuid4()),
            ),
            "contact": MasterFact(
                values={"email": "info@brightlantern.example.hk", "contact_phone_hk": "21234567"},
                effective_date=date(2020, 1, 1),
                source=SourceRef(SOURCE_BACKFILL, DOCUMENT_ID),
            ),
            "share_capital": MasterFact(
                values={
                    "classes": [
                        {
                            "class_name": "Ordinary",
                            "currency": "HKD",
                            "total_shares": 1000,
                            "total_amount": "1000.00",
                            "total_paid_up": "1000.00",
                        },
                        {
                            "class_name": "Preference",
                            "currency": "HKD",
                            "total_shares": 100,
                            "total_amount": "200.00",
                            "total_paid_up": "0.00",
                        },
                    ]
                },
                effective_date=date(2020, 1, 1),
                source=SourceRef(SOURCE_MASTER_RECORD, uuid4()),
            ),
        },
    )


def officer_input() -> OfficerInput:
    return OfficerInput(
        appointment_id=uuid4(),
        person={
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "hkid_partial": "A1234",
            "email": "chan@x.hk",
        },
        role="director",
        capacity=["director"],
        alternate_to=None,
        correspondence_address=ADDRESS,
        registered_office_address=None,
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )


def appointment_event(
    *, effective_date: date, officer: dict[str, object] | None = None
) -> EventInput:
    return EventInput(
        event_id=uuid4(),
        type=ChangeEventType.DIRECTOR_APPOINTMENT,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=effective_date,
        payload={
            "appointment": {
                "officer": officer
                or {
                    "role": "director",
                    "name_en": "WONG Siu Fung",
                    "surname_en": "WONG",
                    "other_names_en": "Siu Fung",
                    "capacity": ["director"],
                }
            }
        },
    )


def resolve(
    *,
    form_type: FormCode = FormCode.NAR1,
    events: tuple[EventInput, ...] = (),
    officers: tuple[OfficerInput, ...] = (officer_input(),),
    shareholdings: tuple[ShareholdingInput, ...] = (),
    made_up_date: date | None = date(2026, 3, 1),
    company: CompanyInput | None = None,
):
    return resolve_draft(
        form_type=form_type,
        target_date=TARGET_DATE,
        company=company or company_input(),
        officers=list(officers),
        shareholdings=list(shareholdings),
        events=list(events),
        made_up_date=made_up_date,
    )


def test_the_company_block_is_resolved_with_derived_totals() -> None:
    resolution = resolve()
    company = resolution.resolved_state["company"]
    assert company["br_number"] == "12345678"
    assert company["cr_number"] == "1234567"
    assert company["type"] == "private"
    assert company["date_of_incorporation"] == "2010-03-01"
    assert company["name"] == {
        "name_en": "BRIGHT LANTERN LIMITED",
        "name_zh": "明燈有限公司",
        "business_name": None,
    }
    assert company["registered_office"] == ADDRESS
    assert company["contact"] == {
        "email": "info@brightlantern.example.hk",
        "contact_phone_hk": "21234567",
    }
    assert company["filing"] is None
    assert company["records_kept"] == []
    assert company["status"] == "active"
    total = company["share_capital"]["total"]
    assert total == {
        "derived": True,
        "currency": "HKD",
        "total_shares": 1100,
        "total_amount": "1200.00",
        "total_paid_up": "1000.00",
    }


def test_a_missing_fact_group_is_absent_and_its_items_unresolved() -> None:
    company = company_input()
    without_office = CompanyInput(
        company_id=company.company_id,
        statics=company.statics,
        status=company.status,
        facts={
            key: value
            for key, value in company.facts.items()
            if key != "registered_office"
        },
    )
    resolution = resolve(company=without_office)
    assert resolution.resolved_state["company"]["registered_office"] is None
    items = [("6", "company.registered_office.flat + company.registered_office.building")]
    provenance = build_field_provenance(
        items, path_sources=resolution.path_sources, changed_paths={}
    )
    assert provenance["6"]["source_type"] == SOURCE_NONE
    assert provenance["6"]["source_id"] is None


def test_an_absent_fact_group_is_a_critical_missing_flag() -> None:
    company = company_input()
    without_contact_and_office = CompanyInput(
        company_id=company.company_id,
        statics=company.statics,
        status=company.status,
        facts={"name": company.facts["name"]},
    )
    resolution = resolve(company=without_contact_and_office)
    paths = [flag["path"] for flag in resolution.qc_flags if flag["kind"] == "missing_source"]
    assert "company.registered_office.flat" in paths
    assert "company.registered_office.building" in paths
    assert "company.registered_office.street" in paths
    assert "company.registered_office.district" in paths
    # The name is stated, so the either-or group raises nothing; the made-up
    # date is supplied, so NAR1 has no date flag either.
    assert "company.name_en" not in paths
    assert MADE_UP_DATE_PATH not in paths


def test_the_critical_set_is_per_form() -> None:
    company = company_input()
    stripped = CompanyInput(
        company_id=company.company_id,
        statics={**company.statics, "br_number": None},
        status=company.status,
        facts=company.facts,
    )
    nar1 = critical_missing_paths(
        form_type=FormCode.NAR1, values={"company.name_en": "X"}, made_up_date=None
    )
    assert nar1 == [
        "company.br_number",
        "company.type",
        "company.registered_office.flat",
        "company.registered_office.building",
        "company.registered_office.street",
        "company.registered_office.district",
        MADE_UP_DATE_PATH,
    ]
    nd2a = critical_missing_paths(
        form_type=FormCode.ND2A, values={"company.name_en": "X"}, made_up_date=None
    )
    assert MADE_UP_DATE_PATH not in nd2a
    assert critical_missing_paths(
        form_type=FormCode.NAR1, values={}, made_up_date=date(2026, 3, 1)
    )[0] == "company.name_en"
    resolution = resolve(company=stripped)
    assert any(
        flag == {"kind": "missing_source", "path": "company.br_number"}
        for flag in resolution.qc_flags
    )


def test_the_officer_entry_carries_its_source_and_the_registry_paths_resolve() -> None:
    row = officer_input()
    resolution = resolve(officers=(row,))
    entry = resolution.resolved_state["officers"][0]
    assert entry["role"] == "director"
    assert entry["source"] == {"type": SOURCE_MASTER_RECORD, "id": str(row.appointment_id)}
    provenance = build_field_provenance(
        [
            ("12A.name", "officers[].name_zh + officers[].surname_en + officers[].other_names_en"),
            ("12A.address", "officers[].correspondence_address.flat"),
            ("12B.address", "officers[].registered_office_address.flat"),
            ("12A.email", "officers[].email"),
        ],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["12A.name"]["source_type"] == SOURCE_MASTER_RECORD
    assert provenance["12A.name"]["source_id"] == str(row.appointment_id)
    assert provenance["12A.address"]["source_id"] == str(row.appointment_id)
    # A body-corporate address the appointment does not carry resolves to
    # nothing: the item says so rather than borrowing a source.
    assert provenance["12B.address"]["source_type"] == SOURCE_NONE
    assert provenance["12A.email"]["source_type"] == SOURCE_MASTER_RECORD


def test_an_appointment_event_adds_an_officer_and_is_applied() -> None:
    event = appointment_event(effective_date=date(2026, 6, 1))
    resolution = resolve(events=(event,))
    entries = resolution.resolved_state["officers"]
    assert len(entries) == 2
    added = entries[-1]
    assert added["name_en"] == "WONG Siu Fung"
    assert added["valid_from"] == "2026-06-01"
    assert added["valid_to"] is None
    assert added["source"] == {"type": SOURCE_CHANGE_EVENT, "id": str(event.event_id)}
    assert resolution.applied_event_ids == [str(event.event_id)]
    assert resolution.resolved_state["changes"] == [
        {
            "event_id": str(event.event_id),
            "type": "director_appointment",
            "effective_date": "2026-06-01",
            "payload": event.payload,
        }
    ]
    provenance = build_field_provenance(
        [("12A.name", "officers[].name_zh + officers[].surname_en + officers[].other_names_en")],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["12A.name"]["source_type"] == SOURCE_CHANGE_EVENT
    assert provenance["12A.name"]["source_id"] == str(event.event_id)


def test_an_effective_date_after_the_target_does_not_apply() -> None:
    event = appointment_event(effective_date=date(2026, 10, 1))
    resolution = resolve(events=(event,))
    assert len(resolution.resolved_state["officers"]) == 1
    assert resolution.applied_event_ids == []
    assert resolution.resolved_state["changes"] == []


def test_an_unapproved_event_does_not_apply() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.DIRECTOR_APPOINTMENT,
        status=ChangeEventStatus.REVIEWED,
        effective_date=date(2026, 6, 1),
        payload=appointment_event(effective_date=date(2026, 6, 1)).payload,
    )
    resolution = resolve(events=(event,))
    assert len(resolution.resolved_state["officers"]) == 1
    assert resolution.applied_event_ids == []


def test_a_duplicate_appointment_is_unapplied_with_a_flag() -> None:
    event = appointment_event(
        effective_date=date(2026, 6, 1),
        officer={
            "role": "director",
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "capacity": ["director"],
        },
    )
    resolution = resolve(events=(event,))
    assert len(resolution.resolved_state["officers"]) == 1
    assert resolution.applied_event_ids == []
    flags = [flag for flag in resolution.qc_flags if flag["kind"] == "unapplied_event"]
    assert flags == [
        {
            "kind": "unapplied_event",
            "event_id": str(event.event_id),
            "detail": "an in-office record of the same person in the same role already exists",
        }
    ]


def test_a_payload_naming_one_name_does_not_match_a_record_naming_two() -> None:
    """§3.2's match is the *name pair*: a payload that names only the English
    name does not match a record that also carries the Chinese one, so the
    appointment is added rather than treated as a duplicate. The strict rule is
    the fail-safe direction — a wrong merge would silently rewrite an office."""
    event = appointment_event(
        effective_date=date(2026, 6, 1),
        officer={"role": "director", "name_en": "CHAN Tai Man", "capacity": ["director"]},
    )
    resolution = resolve(events=(event,))
    assert len(resolution.resolved_state["officers"]) == 2
    assert resolution.applied_event_ids == [str(event.event_id)]
    assert not any(flag["kind"] == "unapplied_event" for flag in resolution.qc_flags)


def test_a_cessation_closes_the_record_it_names() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.DIRECTOR_CESSATION,
        status=ChangeEventStatus.APPLIED_TO_DRAFT,
        effective_date=date(2026, 8, 1),
        payload={
            "cessation": {
                "officer": {
                    "role": "director",
                    "hkid_partial": "A1234",
                    "cessation_date": "2026-08-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    resolution = resolve(events=(event,))
    entry = resolution.resolved_state["officers"][0]
    assert entry["valid_to"] == "2026-08-01"
    assert resolution.applied_event_ids == [str(event.event_id)]


def secretary_record() -> OfficerInput:
    """The same person's other office: the common sole-director-and-secretary shape."""
    return OfficerInput(
        appointment_id=uuid4(),
        person={"name_zh": "陳大文", "name_en": "CHAN Tai Man", "hkid_partial": "A1234"},
        role="company_secretary",
        capacity=["company_secretary"],
        alternate_to=None,
        correspondence_address=ADDRESS,
        registered_office_address=None,
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )


def cessation_event(
    *, event_type: ChangeEventType, role: str, effective_date: date = date(2026, 8, 1)
) -> EventInput:
    return EventInput(
        event_id=uuid4(),
        type=event_type,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=effective_date,
        payload={
            "cessation": {
                "officer": {
                    "role": role,
                    "hkid_partial": "A1234",
                    "cessation_date": effective_date.isoformat(),
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )


def test_a_director_cessation_closes_only_the_director_record() -> None:
    """The identity alone would match both of one person's offices — the common
    sole-director-and-secretary shape — so the match also requires the record's
    role to be one a ``director_*`` event can carry. The secretary stays in
    office."""
    director = officer_input()
    secretary = secretary_record()
    event = cessation_event(event_type=ChangeEventType.DIRECTOR_CESSATION, role="director")
    resolution = resolve(events=(event,), officers=(director, secretary))
    entries = {entry["role"]: entry for entry in resolution.resolved_state["officers"]}
    assert entries["director"]["valid_to"] == "2026-08-01"
    assert entries["company_secretary"]["valid_to"] is None
    assert resolution.applied_event_ids == [str(event.event_id)]
    assert [flag for flag in resolution.qc_flags if flag["kind"] == "unapplied_event"] == []


def test_a_secretary_cessation_closes_only_the_secretary_record() -> None:
    director = officer_input()
    secretary = secretary_record()
    event = cessation_event(
        event_type=ChangeEventType.SECRETARY_CESSATION, role="company_secretary"
    )
    resolution = resolve(events=(event,), officers=(director, secretary))
    entries = {entry["role"]: entry for entry in resolution.resolved_state["officers"]}
    assert entries["company_secretary"]["valid_to"] == "2026-08-01"
    assert entries["director"]["valid_to"] is None
    assert resolution.applied_event_ids == [str(event.event_id)]
    assert [flag for flag in resolution.qc_flags if flag["kind"] == "unapplied_event"] == []


def test_a_cessation_matching_two_offices_of_its_own_kind_is_unapplied() -> None:
    """A person may hold a directorship and a reserve directorship — two offices
    a ``director_*`` event can carry — so a director cessation over both is
    genuinely ambiguous: the fail-safe answer is the named flag rather than a
    chosen office."""
    reserve = OfficerInput(
        appointment_id=uuid4(),
        person={"name_zh": "陳大文", "name_en": "CHAN Tai Man", "hkid_partial": "A1234"},
        role="reserve_director",
        capacity=[],
        alternate_to=None,
        correspondence_address=ADDRESS,
        registered_office_address=None,
        valid_from=date(2024, 1, 1),
        valid_to=None,
    )
    event = cessation_event(event_type=ChangeEventType.DIRECTOR_CESSATION, role="director")
    resolution = resolve(events=(event,), officers=(officer_input(), reserve))
    assert all(entry["valid_to"] is None for entry in resolution.resolved_state["officers"])
    assert resolution.applied_event_ids == []
    assert resolution.qc_flags[0]["detail"] == (
        "several in-office officer records match the event's identity and role"
    )


def test_a_cessation_of_the_other_office_is_unapplied_for_this_event_type() -> None:
    """A ``secretary_cessation`` cannot close a directorship: the record's role
    is not one the event type carries, so there is no match rather than a
    chosen office."""
    event = cessation_event(
        event_type=ChangeEventType.SECRETARY_CESSATION, role="company_secretary"
    )
    resolution = resolve(events=(event,), officers=(officer_input(),))
    assert resolution.resolved_state["officers"][0]["valid_to"] is None
    assert resolution.applied_event_ids == []
    assert resolution.qc_flags[0]["detail"] == (
        "no in-office officer record matches the event's identity and role"
    )


def test_an_appointment_for_a_second_office_is_not_a_duplicate() -> None:
    """The duplicate check stays office-level: the same person may hold a
    directorship and a company secretaryship at once (§2.4), so appointing the
    director office beside the secretary office is not a double entry."""
    event = appointment_event(
        effective_date=date(2026, 6, 1),
        officer={
            "role": "director",
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "capacity": ["director"],
            "appointment_date": "2026-06-01",
        },
    )
    resolution = resolve(events=(event,), officers=(secretary_record(),))
    roles = sorted(entry["role"] for entry in resolution.resolved_state["officers"])
    assert roles == ["company_secretary", "director"]
    assert resolution.applied_event_ids == [str(event.event_id)]




def test_a_cessation_with_nobody_to_close_is_unapplied() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.SECRETARY_CESSATION,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=date(2026, 8, 1),
        payload={
            "cessation": {
                "officer": {
                    "role": "company_secretary",
                    "name_en": "NOBODY AT ALL",
                    "cessation_date": "2026-08-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    resolution = resolve(events=(event,))
    assert resolution.qc_flags[0]["detail"] == (
        "no in-office officer record matches the event's identity and role"
    )


def test_a_cessation_not_newer_than_the_record_is_unapplied() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.DIRECTOR_CESSATION,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=date(2024, 1, 1),
        payload={
            "cessation": {
                "officer": {
                    "role": "director",
                    "hkid_partial": "A1234",
                    "cessation_date": "2024-01-01",
                    "cessation_reason": "resignation_others",
                }
            }
        },
    )
    resolution = resolve(events=(event,))
    assert resolution.resolved_state["officers"][0]["valid_to"] is None
    assert resolution.qc_flags[0]["detail"] == (
        "the cessation's effective date is not newer than the record's valid_from"
    )


def test_a_registered_office_change_replaces_the_office_when_it_is_newer() -> None:
    moved = {
        "flat": "5B",
        "building": "Harbour House",
        "street": "2 Connaught Road",
        "district": "Sheung Wan",
    }
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=date(2026, 5, 1),
        payload={"registered_office": {"address": moved}},
    )
    resolution = resolve(events=(event,))
    assert resolution.resolved_state["company"]["registered_office"] == moved
    assert resolution.values["company.registered_office.street"] == moved["street"]
    assert resolution.applied_event_ids == [str(event.event_id)]
    provenance = build_field_provenance(
        [
            (
                "6",
                "company.registered_office.flat + company.registered_office.building + "
                "company.registered_office.street + company.registered_office.district",
            )
        ],
        path_sources=resolution.path_sources,
        changed_paths={"company.registered_office.flat": True},
    )
    assert provenance["6"]["source_type"] == SOURCE_CHANGE_EVENT
    assert provenance["6"]["source_id"] == str(event.event_id)
    assert provenance["6"]["changed"] is True


def test_a_registered_office_change_older_than_the_fact_is_unapplied() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=date(2019, 1, 1),
        payload={"registered_office": {"address": {**ADDRESS, "flat": "5B"}}},
    )
    resolution = resolve(events=(event,))
    assert resolution.resolved_state["company"]["registered_office"] == ADDRESS
    assert resolution.applied_event_ids == []
    assert resolution.qc_flags[0] == {
        "kind": "unapplied_event",
        "event_id": str(event.event_id),
        "detail": "not newer than the registered-office fact it would replace",
    }


def test_nd2a_takes_the_officer_events_and_not_the_office_move() -> None:
    office_event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
        status=ChangeEventStatus.CONFIRMED,
        effective_date=date(2026, 5, 1),
        payload={"registered_office": {"address": {**ADDRESS, "flat": "5B"}}},
    )
    appointment = appointment_event(effective_date=date(2026, 6, 1))
    resolution = resolve(form_type=FormCode.ND2A, events=(office_event, appointment))
    assert resolution.applied_event_ids == [str(appointment.event_id)]
    assert resolution.resolved_state["company"]["registered_office"] == ADDRESS
    assert any(
        flag["kind"] == "missing_source" and flag["path"] == MADE_UP_DATE_PATH
        for flag in resolution.qc_flags
    ) is False


def test_nd2a_resolves_the_change_event_payload_paths() -> None:
    event = appointment_event(
        effective_date=date(2026, 6, 1),
        officer={
            "role": "director",
            "name_zh": "黃少峰",
            "surname_en": "WONG",
            "other_names_en": "Siu Fung",
            "capacity": ["director"],
            "appointment_date": "2026-06-01",
        },
    )
    resolution = resolve(form_type=FormCode.ND2A, events=(event,))
    assert resolution.values["change_event.company.br_number"] == "12345678"
    assert resolution.values["change_event.company.name_en"] == "BRIGHT LANTERN LIMITED"
    provenance = build_field_provenance(
        [
            ("3", "change_event.payload.appointment.officer.role"),
            (
                "3",
                "change_event.payload.appointment.officer.surname_en + "
                "change_event.payload.appointment.officer.other_names_en",
            ),
            ("PI", "change_event.payload.appointment.officer.hkid_full"),
        ],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["3"]["source_type"] == SOURCE_CHANGE_EVENT
    assert provenance["3"]["source_id"] == str(event.event_id)
    # The §1 protected class has no column and no payload field: the PI sheet
    # item is unresolved, which is what F4's boundary expects.
    assert provenance["PI"]["source_type"] == SOURCE_NONE


def test_shareholdings_resolve_with_their_source_and_the_registry_paths() -> None:
    holding = ShareholdingInput(
        shareholding_id=uuid4(),
        person={"name_en": "CHAN Tai Man"},
        class_name="Ordinary",
        shares_held=1000,
        jointly_held=None,
        remarks=None,
        percentage_held="100.00",
        allotment_date=date(2020, 1, 1),
        amount_paid="1000.00",
        amount_unpaid="0.00",
        address=ADDRESS,
        valid_from=date(2020, 1, 1),
        valid_to=None,
    )
    resolution = resolve(shareholdings=(holding,))
    entry = resolution.resolved_state["shareholders"][0]
    assert entry["class_name"] == "Ordinary"
    assert entry["shares_held"] == 1000
    assert entry["amount_paid"] == "1000.00"
    assert entry["source"] == {"type": SOURCE_MASTER_RECORD, "id": str(holding.shareholding_id)}
    provenance = build_field_provenance(
        [
            ("S1.member.1.name", "shareholders[].name_zh + shareholders[].name_en"),
            ("S1.member.1.shares", "shareholders[].shares[]"),
            (
                "S1.class",
                "share_capital.classes[].class_name + share_capital.classes[].total_shares",
            ),
        ],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["S1.member.1.name"]["source_type"] == SOURCE_MASTER_RECORD
    assert provenance["S1.member.1.shares"]["source_id"] == str(holding.shareholding_id)
    assert provenance["S1.class"]["source_type"] == SOURCE_MASTER_RECORD


def test_events_apply_in_effective_date_order() -> None:
    first = appointment_event(
        effective_date=date(2026, 5, 1),
        officer={"role": "director", "name_en": "AAA One", "capacity": ["director"]},
    )
    second = appointment_event(
        effective_date=date(2026, 7, 1),
        officer={"role": "director", "name_en": "BBB Two", "capacity": ["director"]},
    )
    resolution = resolve(events=(second, first))
    assert resolution.applied_event_ids == [str(first.event_id), str(second.event_id)]
    assert [change["effective_date"] for change in resolution.resolved_state["changes"]] == [
        "2026-05-01",
        "2026-07-01",
    ]


def test_a_backfilled_fact_is_a_document_source() -> None:
    resolution = resolve()
    provenance = build_field_provenance(
        [("7", "company.email")], path_sources=resolution.path_sources, changed_paths={}
    )
    assert provenance["7"]["source_type"] == SOURCE_BACKFILL
    assert provenance["7"]["source_id"] == str(DOCUMENT_ID)


def test_a_company_static_is_sourced_to_the_company() -> None:
    resolution = resolve()
    provenance = build_field_provenance(
        [("HDR-BRN", "company.br_number")],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["HDR-BRN"]["source_type"] == SOURCE_MASTER_RECORD
    assert provenance["HDR-BRN"]["source_id"] == str(COMPANY_ID)


# --- §3.6's prior-filing cross-check ----------------------------------------


def test_a_difference_no_event_explains_is_flagged() -> None:
    resolution = resolve()
    comparison = prior_filing_comparison(
        values=resolution.values,
        path_sources=resolution.path_sources,
        prior={
            "company.name_en": "OLD NAME LIMITED",
            "company.br_number": "12345678",
        },
    )
    assert comparison.changed_paths == {
        "company.name_en": True,
        "company.br_number": False,
    }
    assert comparison.flags == [
        {
            "kind": "unexpected_change",
            "target": "company.name_en",
            "detail": "OLD NAME LIMITED -> BRIGHT LANTERN LIMITED",
        }
    ]


def test_an_event_explained_change_is_changed_without_a_flag() -> None:
    event = EventInput(
        event_id=uuid4(),
        type=ChangeEventType.REGISTERED_OFFICE_CHANGE,
        status=ChangeEventStatus.APPLIED_TO_DRAFT,
        effective_date=date(2026, 5, 1),
        payload={"registered_office": {"address": {**ADDRESS, "flat": "5B"}}},
    )
    resolution = resolve(events=(event,))
    comparison = prior_filing_comparison(
        values=resolution.values,
        path_sources=resolution.path_sources,
        prior={"company.registered_office.flat": "12A"},
    )
    assert comparison.changed_paths == {"company.registered_office.flat": True}
    assert comparison.flags == []
    provenance = build_field_provenance(
        [("6", "company.registered_office.flat")],
        path_sources=resolution.path_sources,
        changed_paths=comparison.changed_paths,
    )
    assert provenance["6"]["changed"] is True
    assert provenance["6"]["source_type"] == SOURCE_CHANGE_EVENT


def test_no_prior_filing_leaves_every_flag_false() -> None:
    resolution = resolve()
    comparison = prior_filing_comparison(
        values=resolution.values, path_sources=resolution.path_sources, prior={}
    )
    assert comparison.changed_paths == {}
    assert comparison.flags == []
    provenance = build_field_provenance(
        [("HDR-BRN", "company.br_number")],
        path_sources=resolution.path_sources,
        changed_paths=comparison.changed_paths,
    )
    assert all(entry["changed"] is False for entry in provenance.values())


def test_canonicalisation_uses_the_extraction_normalisers() -> None:
    """A BRN written with a space and a money value written without a trailing
    zero are not changes."""
    resolution = resolve()
    comparison = prior_filing_comparison(
        values=resolution.values,
        path_sources=resolution.path_sources,
        prior={"company.br_number": "1234 5678"},
    )
    assert comparison.changed_paths == {"company.br_number": False}
    assert comparison.flags == []


def test_a_value_the_draft_no_longer_names_compares_as_no_value() -> None:
    resolution = resolve()
    comparison = prior_filing_comparison(
        values=resolution.values,
        path_sources=resolution.path_sources,
        prior={"company.business_name": "OLD BUSINESS NAME"},
    )
    assert comparison.changed_paths == {"company.business_name": True}
    assert comparison.flags == [
        {
            "kind": "unexpected_change",
            "target": "company.business_name",
            "detail": "OLD BUSINESS NAME -> (none)",
        }
    ]


# --- The filing-metadata document -------------------------------------------


def test_the_filing_metadata_template_fills_the_made_up_date_only() -> None:
    assert filing_metadata_template(made_up_date=date(2026, 3, 1)) == {
        "return": {
            "made_up_date": "2026-03-01",
            "fs_period_start": None,
            "fs_period_end": None,
        },
        "member_particulars_delivery": None,
        "presentor": None,
        "signature": None,
        "consent": None,
    }
    assert filing_metadata_template(made_up_date=None)["return"]["made_up_date"] is None


def test_the_merge_is_partial_and_nests_one_level() -> None:
    current = filing_metadata_template(made_up_date=date(2026, 3, 1))
    merged = merge_filing_metadata(
        current,
        {
            "return": {"fs_period_start": "2025-04-01", "fs_period_end": "2026-03-31"},
            "member_particulars_delivery": "schedule_1",
            "presentor": {"name": "K2P SERVICES LIMITED"},
        },
    )
    assert merged["return"] == {
        "made_up_date": "2026-03-01",
        "fs_period_start": "2025-04-01",
        "fs_period_end": "2026-03-31",
    }
    assert merged["member_particulars_delivery"] == "schedule_1"
    assert merged["presentor"] == {"name": "K2P SERVICES LIMITED"}
    # The stored document is not mutated in place.
    assert current["return"]["fs_period_start"] is None
    # An explicit null clears one key without touching its siblings.
    cleared = merge_filing_metadata(merged, {"return": {"fs_period_start": None}})
    assert cleared["return"]["fs_period_start"] is None
    assert cleared["return"]["fs_period_end"] == "2026-03-31"


def test_a_valid_document_has_no_violations() -> None:
    assert (
        filing_metadata_violations(
            {
                "return": {
                    "made_up_date": "2026-03-01",
                    "fs_period_start": "2025-04-01",
                    "fs_period_end": "2026-03-31",
                },
                "member_particulars_delivery": DELIVERY_CHOICES[0],
                "presentor": {"name": "K2P", "address": "1 Queen's Road", "tel": "21234567"},
                "signature": {"name": "CHAN Tai Man", "date": "2026-09-30", "capacity": "director"},
                "consent": {"signatory_name": "CHAN Tai Man", "signatory_capacity": "director"},
            }
        )
        == []
    )


def test_the_metadata_violations_are_named_fields() -> None:
    violations = filing_metadata_violations(
        {
            "return": {
                "made_up_date": "not a date",
                "fs_period_start": "2026-04-01",
                "fs_period_end": "2026-03-31",
            },
            "member_particulars_delivery": "carrier_pigeon",
            "presentor": {"name": "K2P", "vat": "nope"},
            "signature": "CHAN",
            "consent": {"signatory_name": "   "},
            "surprise": True,
        }
    )
    assert "filing_metadata_invalid: surprise" in violations
    assert "filing_metadata_invalid: return.made_up_date: not an ISO date" in violations
    assert "filing_metadata_invalid: return.fs_period_end: before fs_period_start" in violations
    assert "filing_metadata_invalid: member_particulars_delivery" in violations
    assert "filing_metadata_invalid: presentor.vat" in violations
    assert "filing_metadata_invalid: signature" in violations
    assert "filing_metadata_invalid: consent.signatory_name: must not be blank" in violations


def test_a_null_document_is_legal_and_an_unknown_key_is_not() -> None:
    assert filing_metadata_violations(filing_metadata_template(made_up_date=None)) == []
    assert filing_metadata_violations({"returns": {}}) == ["filing_metadata_invalid: returns"]


def test_the_sources_of_a_collection_path_are_the_first_record_in_order() -> None:
    first = ShareholdingInput(
        shareholding_id=UUID("00000000-0000-0000-0000-000000000001"),
        person={"name_en": "FIRST"},
        class_name="Ordinary",
        shares_held=1,
        jointly_held=None,
        remarks=None,
        percentage_held=None,
        allotment_date=None,
        amount_paid=None,
        amount_unpaid=None,
        address=None,
        valid_from=date(2020, 1, 1),
        valid_to=None,
    )
    second = ShareholdingInput(
        shareholding_id=uuid4(),
        person={"name_en": "SECOND"},
        class_name="Ordinary",
        shares_held=2,
        jointly_held=None,
        remarks=None,
        percentage_held=None,
        allotment_date=None,
        amount_paid=None,
        amount_unpaid=None,
        address=None,
        valid_from=date(2021, 1, 1),
        valid_to=None,
    )
    resolution = resolve(shareholdings=(second, first))
    provenance = build_field_provenance(
        [("S1.member.1.name", "shareholders[].name_zh + shareholders[].name_en")],
        path_sources=resolution.path_sources,
        changed_paths={},
    )
    assert provenance["S1.member.1.name"]["source_id"] == str(first.shareholding_id)


def test_a_capital_total_of_no_classes_is_null_rather_than_zero() -> None:
    company = company_input()
    without_capital = CompanyInput(
        company_id=company.company_id,
        statics=company.statics,
        status=company.status,
        facts={key: value for key, value in company.facts.items() if key != "share_capital"},
    )
    resolution = resolve(company=without_capital)
    assert resolution.resolved_state["company"]["share_capital"] == {
        "classes": [],
        "total": {
            "derived": True,
            "currency": None,
            "total_shares": None,
            "total_amount": None,
            "total_paid_up": None,
        },
    }


def test_a_capital_total_of_stated_zeroes_is_zero_rather_than_null() -> None:
    """``total_shares: 0`` is a stated figure (the master write accepts it), so
    a list whose stated totals are all zero sums to 0 — null is only for a list
    that states nothing at all."""
    company = company_input()
    capital = company.facts["share_capital"]
    zeroed = MasterFact(
        values={
            "classes": [
                {
                    "class_name": "Ordinary",
                    "currency": "HKD",
                    "total_shares": 0,
                    "total_amount": "0.00",
                    "total_paid_up": "0.00",
                }
            ]
        },
        effective_date=capital.effective_date,
        source=capital.source,
    )
    with_zeroes = CompanyInput(
        company_id=company.company_id,
        statics=company.statics,
        status=company.status,
        facts={**company.facts, "share_capital": zeroed},
    )
    resolution = resolve(company=with_zeroes)
    assert resolution.resolved_state["company"]["share_capital"]["total"]["total_shares"] == 0


def test_a_mixed_currency_total_names_no_currency() -> None:
    company = company_input()
    capital = company.facts["share_capital"]
    mixed = MasterFact(
        values={
            "classes": [
                {
                    "class_name": "Ordinary",
                    "currency": "HKD",
                    "total_shares": 10,
                    "total_amount": "10.00",
                    "total_paid_up": "10.00",
                },
                {
                    "class_name": "Preference",
                    "currency": "USD",
                    "total_shares": 5,
                    "total_amount": "5.00",
                    "total_paid_up": "0.00",
                },
            ]
        },
        effective_date=capital.effective_date,
        source=capital.source,
    )
    with_mixed = CompanyInput(
        company_id=company.company_id,
        statics=company.statics,
        status=company.status,
        facts={**company.facts, "share_capital": mixed},
    )
    resolution = resolve(company=with_mixed)
    assert resolution.resolved_state["company"]["share_capital"]["total"]["currency"] is None
    assert resolution.resolved_state["company"]["share_capital"]["total"]["total_shares"] == 15
    assert Decimal(
        resolution.resolved_state["company"]["share_capital"]["total"]["total_amount"]
    ) == Decimal("15.00")
