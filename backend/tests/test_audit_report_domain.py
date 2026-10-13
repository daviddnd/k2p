"""Unit tests for the per-company audit compliance report's composition rules
(scope §8; DoD 19; ruling R6).

The composer is pure: same inputs, same dict. These tests pin the report
shape, chain-order preservation, the honest tenant-chain verdict (a
company-filtered subset cannot re-verify its own chain), and the
completeness net over the live ``AuditAction`` vocabulary — a future action
added without a report area fails the coverage test here.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.audit import AuditAction
from app.domain.audit_report import (
    ACTION_AREA,
    REPORT_VERSION,
    ChainVerdict,
    ReportArea,
    build_report,
)

ACTOR = UUID("22222222-2222-2222-2222-222222222222")
COMPANY = UUID("33333333-3333-3333-3333-333333333333")
GENERATED_AT = datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)

COMPANY_BLOCK: dict[str, Any] = {
    "company_id": str(COMPANY),
    "name": "Acme Holdings Limited",
    "br_number": "12345678",
    "cr_number": "CR987654",
}


@dataclass(frozen=True)
class Entry:
    """One stored audit entry as the composer reads it (already chain-ordered
    and company-filtered by the application layer)."""

    seq: int
    occurred_at: datetime
    actor_user_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None
    details: dict[str, Any]


def entry(seq: int, action: AuditAction, **overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "seq": seq,
        "occurred_at": datetime(2026, 10, 2, 9, seq, 0, tzinfo=UTC),
        "actor_user_id": ACTOR,
        "action": action.value,
        "entity_type": "company",
        "entity_id": COMPANY,
        "details": {"br_number": "12345678"},
    }
    fields.update(overrides)
    return Entry(**fields)


def report(
    entries: list[Entry], verdict: ChainVerdict | None = None
) -> dict[str, Any]:
    return build_report(
        COMPANY_BLOCK,
        entries,
        verdict if verdict is not None else ChainVerdict(checked_entries=len(entries)),
        generated_at=GENERATED_AT,
    )


# --- report composition -------------------------------------------------------


def test_report_shape_is_fixed_and_versioned() -> None:
    result = report([entry(1, AuditAction.COMPANY_CREATED)])

    assert set(result) == {"report_version", "generated_at", "company", "chain", "entries"}
    assert result["report_version"] == REPORT_VERSION == 1
    assert result["generated_at"] == "2026-10-02T09:00:00+00:00"
    assert result["company"] == COMPANY_BLOCK


def test_entries_are_carried_as_stored_in_chain_order() -> None:
    first = entry(1, AuditAction.COMPANY_CREATED, details={"br_number": "12345678"})
    second = entry(
        2,
        AuditAction.MAIL_MESSAGE_SENT,
        entity_type=None,
        entity_id=None,
        details={"confirmation_request_id": str(uuid4())},
    )
    result = report([first, second])

    assert [item["seq"] for item in result["entries"]] == [1, 2]
    (sent,) = [item for item in result["entries"] if item["seq"] == 2]
    assert sent["occurred_at"] == "2026-10-02T09:02:00+00:00"
    assert sent["actor"] == str(ACTOR)
    assert sent["action"] == "mail_message_sent"
    assert sent["area"] == "client_communications"
    assert sent["entity_type"] is None
    assert sent["entity_id"] is None
    assert sent["details"] == second.details


def test_a_system_actor_and_null_details_serialize_honestly() -> None:
    result = report(
        [entry(1, AuditAction.REMINDER_ENQUEUED, actor_user_id=None, details={})]
    )
    (item,) = result["entries"]
    assert item["actor"] is None
    assert item["details"] == {}


def test_an_empty_company_carries_no_entries_but_a_valid_chain_verdict() -> None:
    result = report([], ChainVerdict(checked_entries=7))

    assert result["entries"] == []
    assert result["chain"]["verified"] is True
    assert result["chain"]["checked_entries"] == 7
    assert result["chain"]["problems"] == []


def test_the_chain_verdict_passes_through_unmodified() -> None:
    problems = (
        "entry 1: prev_hash does not link to the previous entry",
        "entry 1: entry_hash does not match its contents",
    )
    result = report([], ChainVerdict(checked_entries=3, problems=problems))

    assert result["chain"]["verified"] is False
    assert result["chain"]["problems"] == list(problems)


def test_the_scope_note_states_the_r6_posture() -> None:
    """Review Focus 4: the company filter never claims an independently
    verifiable subset — the chain block says the tenant's full chain was
    verified and why a filtered subset cannot re-verify on its own."""
    result = report([])

    scope = result["chain"]["scope"]
    assert isinstance(scope, str) and scope
    assert "tenant" in scope
    assert "cannot be re-verified" in scope


# --- the area table (the completeness net) ------------------------------------


def test_every_audit_action_is_classified_into_exactly_one_area() -> None:
    assert set(ACTION_AREA) == set(AuditAction)
    assert all(isinstance(area, ReportArea) for area in ACTION_AREA.values())


def test_the_areas_are_the_five_dod_18_areas() -> None:
    assert {area.value for area in ReportArea} == {
        "client_communications",
        "event_lifecycle",
        "data_changes",
        "ai_suggestions",
        "approvals",
    }


@pytest.mark.parametrize(
    ("action", "area"),
    [
        (AuditAction.MAIL_MESSAGE_INGESTED, ReportArea.CLIENT_COMMUNICATIONS),
        (AuditAction.CONFIRMATION_REQUEST_RELEASED, ReportArea.CLIENT_COMMUNICATIONS),
        (AuditAction.REMINDERS_EXHAUSTED, ReportArea.CLIENT_COMMUNICATIONS),
        (AuditAction.CHANGE_EVENT_RECORDED, ReportArea.EVENT_LIFECYCLE),
        (AuditAction.CHANGE_EVENT_TRANSITION_RECORDED, ReportArea.EVENT_LIFECYCLE),
        (AuditAction.COMPANY_FACT_RECORDED, ReportArea.DATA_CHANGES),
        (AuditAction.STAFF_LOGIN, ReportArea.DATA_CHANGES),
        (AuditAction.DOCUMENT_SCANNED, ReportArea.DATA_CHANGES),
        (AuditAction.PRIOR_FILING_EXTRACTED, ReportArea.DATA_CHANGES),
        (AuditAction.LLM_CALL, ReportArea.AI_SUGGESTIONS),
        (AuditAction.REPLY_PARSED, ReportArea.AI_SUGGESTIONS),
        (AuditAction.IMPORT_RECORD_CONFIRMED, ReportArea.APPROVALS),
        (AuditAction.DRAFT_TRANSITION_RECORDED, ReportArea.APPROVALS),
        (AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED, ReportArea.APPROVALS),
        (AuditAction.DRAFT_EXPORTED, ReportArea.APPROVALS),
    ],
)
def test_actions_classify_into_their_report_area(
    action: AuditAction, area: ReportArea
) -> None:
    assert ACTION_AREA[action] is area


def test_an_unmappable_stored_action_fails_loud_not_silently() -> None:
    bogus = Entry(
        seq=1,
        occurred_at=datetime(2026, 10, 2, 9, 1, 0, tzinfo=UTC),
        actor_user_id=ACTOR,
        action="not_a_real_action",
        entity_type="company",
        entity_id=COMPANY,
        details={},
    )
    with pytest.raises(ValueError, match="not_a_real_action"):
        report([bogus])
