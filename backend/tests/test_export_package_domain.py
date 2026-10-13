"""The export package's pure rules (contract §2.8, §2.12, §5.1; ADR-0004).

Database-free and network-free: the mode vocabulary and its named refusal,
the FilingPackage envelope derived from one ``FillPlan``, the api-ready JSON
payload and its byte determinism, and the two companion documents' text
builders — including the fee block transcribed from the archived CR fee
table (ruling R6) with its "verify before filing" line.

The money paths — the export's assembly, the store writes, the event
submissions — live in ``test_api_exports.py`` (the route mappings), the fill
and flatten machinery's own render tests, and ``test_drafts_scenarios_db.py``
(the lifecycle).
"""

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.draft_fill import FillPlan, SheetPlan
from app.domain.drafts import ensure_exportable
from app.domain.export_package import (
    FEE_SCHEDULE,
    ExportMode,
    ExportModeInvalidError,
    api_ready_bytes,
    api_ready_payload,
    build_envelope,
    ered_registry_helper_text,
    paper_checklist_text,
    validate_mode,
)

# --- The mode vocabulary ------------------------------------------------------


def test_the_mode_vocabulary_is_the_contracts_three_values() -> None:
    assert ExportMode.E_REGISTRY_PREP.value == "e_registry_prep"
    assert ExportMode.PAPER.value == "paper"
    assert ExportMode.API_READY.value == "api_ready"
    assert {mode.value for mode in ExportMode} == {
        "e_registry_prep",
        "paper",
        "api_ready",
    }


def test_validate_mode_accepts_the_vocabulary_and_refuses_the_rest_by_name() -> None:
    assert validate_mode("e_registry_prep") is ExportMode.E_REGISTRY_PREP
    assert validate_mode("paper") is ExportMode.PAPER
    assert validate_mode("api_ready") is ExportMode.API_READY
    for value in ("", "E_REGISTRY_PREP", "e-registry", "post", "api-ready"):
        with pytest.raises(ExportModeInvalidError) as excinfo:
            validate_mode(value)
        assert excinfo.value.code == "export_mode_invalid"


# --- The envelope -------------------------------------------------------------


def _plan(*copies: tuple[str, int | None]) -> FillPlan:
    """One plan with one copy per (slug, printed footer number) pair."""
    return FillPlan(
        fields={"fill_1_P": "12345678"},
        sheets=tuple(
            SheetPlan(sheet=slug, fields={"fill_1_P": "12345678"}, page_no=page_no)
            for slug, page_no in copies
        ),
        outcomes=(),
        escalations=(),
    )


def test_the_envelope_carries_the_derived_sheet_counts_in_package_order() -> None:
    plan = _plan(("s1", 1), ("cs-a", None), ("s1", 2))
    envelope = build_envelope(plan=plan, helper_document_id=None, checklist_document_id=None)
    # One count per slug, in the order the package first uses it: s1's two
    # copies (its template page and one duplicate), then cs-a's one.
    assert envelope["sheet_counts"] == {"s1": 2, "cs-a": 1}
    assert envelope["helper_document_id"] is None
    assert envelope["checklist_document_id"] is None


def test_the_envelope_pairs_are_the_schedule_one_footers_nar1_only() -> None:
    """NAR1 with Schedule 1 copies only: each S1 copy prints its own number
    twice (``1 1``, ``2 2``), numbered among the S1 copies — the envelope
    records exactly those pairs."""
    envelope = build_envelope(
        plan=_plan(("s1", 1), ("s1", 2)),
        helper_document_id=None,
        checklist_document_id=None,
    )
    assert envelope["page_pairs"] == [[1, 1], [2, 2]]


def test_the_envelope_pairs_skip_the_unnumbered_continuation_sheets() -> None:
    """NAR1 with continuation sheets and S1 copies: CS-A…E print no footer
    page numbers, so they contribute no pairs — only the numbered S1 copies
    do, numbered among themselves regardless of where the CS copies sit in
    the package."""
    envelope = build_envelope(
        plan=_plan(("cs-a", None), ("s1", 1), ("cs-b", None), ("s1", 2)),
        helper_document_id=None,
        checklist_document_id=None,
    )
    assert envelope["sheet_counts"] == {"cs-a": 1, "s1": 2, "cs-b": 1}
    assert envelope["page_pairs"] == [[1, 1], [2, 2]]


def test_the_envelope_pairs_are_empty_for_nd2a() -> None:
    """ND2A's grid (Sheets A/B/C, PI-ND2A) is unnumbered — no registry item
    prints footer page numbers — so an ND2A export records no pairs at all,
    however many continuation sheets the changes needed."""
    envelope = build_envelope(
        plan=_plan(("a-2a", None), ("b-3", None), ("c-4", None), ("pi", None)),
        helper_document_id=None,
        checklist_document_id=None,
    )
    assert envelope["sheet_counts"] == {"a-2a": 1, "b-3": 1, "c-4": 1, "pi": 1}
    assert envelope["page_pairs"] == []


def test_the_envelope_names_the_companion_documents_when_a_mode_carries_one() -> None:
    helper_id = uuid4()
    checklist_id = uuid4()
    envelope = build_envelope(
        plan=_plan(("s1", 1)),
        helper_document_id=helper_id,
        checklist_document_id=checklist_id,
    )
    assert envelope["helper_document_id"] == str(helper_id)
    assert envelope["checklist_document_id"] == str(checklist_id)


def test_an_empty_plan_is_an_empty_roster() -> None:
    plan = FillPlan(fields={}, sheets=(), outcomes=(), escalations=())
    envelope = build_envelope(plan=plan, helper_document_id=None, checklist_document_id=None)
    assert envelope["sheet_counts"] == {}
    assert envelope["page_pairs"] == []


# --- The api-ready payload ----------------------------------------------------


@dataclass(frozen=True)
class _DraftRow:
    """The persistence row's face as ``api_ready_payload`` reads it."""

    id: UUID
    version_no: int
    form_type: str
    form_version: dict[str, Any]
    target_date: date
    filing_metadata: dict[str, Any]
    resolved_state: dict[str, Any]
    field_provenance: dict[str, Any]
    qc_flags: list[dict[str, Any]]


def _row() -> _DraftRow:
    return _DraftRow(
        id=uuid4(),
        version_no=3,
        form_type="nar1",
        form_version={"specification_no": "2/2025 (May 2025)", "template_sha256": "a" * 64},
        target_date=date(2026, 9, 30),
        filing_metadata={"return": {"made_up_date": "2026-03-01"}},
        resolved_state={"company": {"br_number": "12345678", "name": {"name_en": "明燈有限公司"}}},
        field_provenance={"HDR-BRN": {"source_type": "master_record_id", "changed": False}},
        qc_flags=[{"kind": "missing_source", "path": "company.email"}],
    )


def test_the_api_ready_payload_is_the_snapshot_and_metadata_with_the_versions() -> None:
    row = _row()
    payload = api_ready_payload(row=row)
    assert payload == {
        "form_type": "nar1",
        "form_version": {"specification_no": "2/2025 (May 2025)", "template_sha256": "a" * 64},
        "target_date": "2026-09-30",
        "filing_metadata": {"return": {"made_up_date": "2026-03-01"}},
        "resolved_state": {
            "company": {"br_number": "12345678", "name": {"name_en": "明燈有限公司"}}
        },
        "field_provenance": {
            "HDR-BRN": {"source_type": "master_record_id", "changed": False}
        },
        "qc_flags": [{"kind": "missing_source", "path": "company.email"}],
        "draft": {"id": str(row.id), "version_no": 3},
    }


def test_the_api_ready_json_is_byte_deterministic_with_sorted_keys() -> None:
    """Two serializations of the same row are byte-identical, the keys are
    sorted (a reader can diff two packages), and CJK crosses as UTF-8 rather
    than escapes."""
    row = _row()
    first = api_ready_bytes(row=row)
    second = api_ready_bytes(row=row)
    assert first == second
    assert first == first.decode("utf-8").encode("utf-8")
    assert '"form_type"' in first.decode("utf-8")
    # Sorted keys: "draft" sorts before "field_provenance" before "form_type".
    text = first.decode("utf-8")
    assert text.index('"draft"') < text.index('"field_provenance"') < text.index('"form_type"')
    # CJK is not escaped (ensure_ascii=False).
    assert "明燈有限公司".encode() in first


# --- The companion documents' texts -------------------------------------------


@dataclass(frozen=True)
class _CompanyRow:
    br_number: str
    cr_number: str | None


COMPANY = _CompanyRow(br_number="12345678", cr_number="1234567")
FILING_METADATA = {
    "return": {"made_up_date": "2026-03-01", "fs_period_start": None, "fs_period_end": None},
    "presentor": {"name": "K2P Secretaries Limited", "reference": "BL-2026"},
}


def test_the_ered_helper_names_the_company_the_form_and_the_steps() -> None:
    text = ered_registry_helper_text(
        company=COMPANY, form_code="NAR1", filing_metadata=FILING_METADATA
    )
    assert "12345678" in text
    assert "NAR1" in text
    # The helper is an e-Registry walk-through, not a submission: the human
    # performs every step (no unattended filing anywhere).
    assert "e-Registry" in text
    assert "manual" in text or "human" in text


def test_the_paper_checklist_names_the_company_the_form_and_the_fee_figures() -> None:
    text = paper_checklist_text(
        company=COMPANY, form_code="NAR1", filing_metadata=FILING_METADATA
    )
    assert "12345678" in text
    assert "NAR1" in text
    # The NAR1 fee figures transcribed from the archived PAM 3C table
    # (ruling R6): the on-time fee and every late tier.
    for figure in ("105", "870", "1740", "2610", "3480"):
        assert figure in text
    assert "42" in text  # the on-time window is 42 days after the return date
    # The dated-and-flagged rule: every checklist carries the verify line.
    assert "verify against the CR's current fee table before filing" in text
    assert "2026-10" in text  # the "as of" label rides with the figures


def test_the_nd2a_checklist_says_the_archived_table_carries_no_fee_item() -> None:
    """R6's stop-line, stated honestly: the archived PAM 3C main service fee
    table lists no fee item for ND2A, so the checklist says exactly that and
    leans entirely on the verify line — never an invented figure."""
    text = paper_checklist_text(
        company=COMPANY, form_code="ND2A", filing_metadata={"presentor": None}
    )
    assert "ND2A" in text
    assert "no fee item" in text
    assert "verify against the CR's current fee table before filing" in text


def test_the_fee_schedule_carries_the_nar1_figures_and_a_none_for_nd2a() -> None:
    """The constant's shape, pinned: NAR1's row transcribed from the archived
    table, ND2A's absence recorded as ``None`` (not a zero — "no fee" is a
    claim the archived source does not make)."""
    nar1 = FEE_SCHEDULE["NAR1"]
    assert nar1 is not None
    assert nar1["on_time"] == "105"
    assert [tier["fee"] for tier in nar1["late_tiers"]] == ["870", "1740", "2610", "3480"]
    assert FEE_SCHEDULE["ND2A"] is None


# --- The statement gate (P6-2's rule, read from this side) --------------------


def test_ensure_exportable_is_the_export_side_of_the_statement_gate() -> None:
    """P6-2's rule, domain-owned so the export route and the executor share
    it: an NAR1 draft whose item-16 statement stands unconfirmed names the
    §5.1.3 code; everything else passes."""
    row = _row()

    class _FlagRow:
        form_type = "nar1"
        private_company_statement = True
        statement_confirmed_by: UUID | None = None

    assert ensure_exportable(_FlagRow()) == "statement_not_confirmed"

    class _ConfirmedRow:
        form_type = "nar1"
        private_company_statement = True
        statement_confirmed_by = row.id

    assert ensure_exportable(_ConfirmedRow()) is None

    class _NotMadeRow:
        form_type = "nar1"
        private_company_statement: bool | None = None
        statement_confirmed_by: UUID | None = None

    assert ensure_exportable(_NotMadeRow()) is None

    class _Nd2aRow:
        form_type = "nd2a"
        private_company_statement: bool | None = None
        statement_confirmed_by: UUID | None = None

    assert ensure_exportable(_Nd2aRow()) is None
