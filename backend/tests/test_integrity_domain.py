"""Domain tests for the integrity-verification rules (ADR-0009/0011; scope §8;
Review Focus 5).

Pure rules only: the mismatch vocabulary, the classification rule, and the
audit-summary shape the single ``integrity_check`` entry carries. No I/O, no
clock — determinism and the read-only classification are the properties under
test.
"""

from uuid import uuid4

from app.domain.integrity import (
    AUDIT_MISMATCH_LIMIT,
    Mismatch,
    MismatchKind,
    audit_details,
    classify,
)


def test_a_clean_check_classifies_as_verified() -> None:
    result = classify(True, 12, [])

    assert result.chain_verified is True
    assert result.documents_checked == 12
    assert result.mismatches == ()
    assert result.mismatch_count == 0
    assert result.ok is True


def test_a_chain_problem_marks_the_result_unverified() -> None:
    result = classify(False, 0, [])

    assert result.chain_verified is False
    assert result.ok is False


def test_a_document_mismatch_marks_the_result_not_ok_but_leaves_the_chain_verdict() -> None:
    document_id = uuid4()
    result = classify(
        True,
        1,
        [Mismatch(MismatchKind.DOCUMENT_HASH_MISMATCH, document_id, "recomputed digest differs")],
    )

    # The two legs report honestly on their own: the chain verified; the
    # check as a whole is not ok.
    assert result.chain_verified is True
    assert result.ok is False
    assert result.mismatch_count == 1


def test_classify_converts_the_mismatch_list_to_a_tuple() -> None:
    found = [Mismatch(MismatchKind.DOCUMENT_UNREADABLE, uuid4(), "blob could not be read")]

    result = classify(True, 1, found)

    assert isinstance(result.mismatches, tuple)
    assert result.mismatches == tuple(found)


def test_the_mismatch_vocabulary_is_exactly_the_three_named_kinds() -> None:
    assert MismatchKind.AUDIT_CHAIN_BREAK.value == "audit_chain_break"
    assert MismatchKind.DOCUMENT_HASH_MISMATCH.value == "document_hash_mismatch"
    assert MismatchKind.DOCUMENT_UNREADABLE.value == "document_unreadable"
    assert len(MismatchKind) == 3


def test_results_are_deterministic_for_the_same_inputs() -> None:
    mismatches = [Mismatch(MismatchKind.DOCUMENT_HASH_MISMATCH, uuid4(), "differ")]
    one = classify(True, 3, mismatches)
    two = classify(True, 3, [Mismatch(m.kind, m.document_id, m.detail) for m in mismatches])

    assert one == two


def test_audit_details_carries_the_counts_and_each_mismatch_id() -> None:
    document_id = uuid4()
    result = classify(
        True,
        4,
        [Mismatch(MismatchKind.DOCUMENT_HASH_MISMATCH, document_id, "recomputed digest differs")],
    )

    assert audit_details(result) == {
        "chain_verified": True,
        "documents_checked": 4,
        "mismatch_count": 1,
        "mismatches": [
            {"kind": "document_hash_mismatch", "document_id": str(document_id)},
        ],
    }


def test_audit_details_caps_the_mismatch_list_at_the_first_ten() -> None:
    mismatches = [
        Mismatch(MismatchKind.DOCUMENT_HASH_MISMATCH, uuid4(), f"document {index}")
        for index in range(12)
    ]

    details = audit_details(classify(False, 12, mismatches))

    # The count is honest; the id list is the first N, in finding order.
    assert details["mismatch_count"] == 12
    assert len(details["mismatches"]) == AUDIT_MISMATCH_LIMIT == 10
    assert details["mismatches"][0]["document_id"] == str(mismatches[0].document_id)
    assert details["mismatches"][-1]["document_id"] == str(mismatches[9].document_id)


def test_a_chain_break_carries_no_document_id() -> None:
    result = classify(
        False,
        0,
        [Mismatch(MismatchKind.AUDIT_CHAIN_BREAK, None, "entry 3: prev_hash does not link")],
    )

    (item,) = audit_details(result)["mismatches"]
    assert item == {"kind": "audit_chain_break", "document_id": None}
