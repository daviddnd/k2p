"""The submission package's pure rules (contract §2.8's FilingPackage, §2.12,
§5.1; scope §2.11's export gate; ADR-0004's "flatten only at the final export
step"; ruling R6's dated, sourced fee copy).

Export is the release's sibling: ``app.application.drafts`` performs it, and
this module owns the part of it that is pure — the **mode vocabulary**, the
FilingPackage **envelope** (the derived counts the assembly computed: sheet
counts per slug, the footer page-number pairs the numbered sheets actually
printed, and the companion documents' ids), the **api-ready JSON** (the
deterministic serialization the api_ready mode stores), and the two
**companion documents' texts** (the e-Registry step-by-step helper and the
paper covering checklist with its fee block).

**The envelope is derived, never stored twice.** The sheet counts and the
page-number pairs are the same arithmetic F4c's continuation-sheet assembly
computes from ``FillPlan.sheets`` (item 8A's boxes and each Schedule 1
copy's footer pair); recording them on the FilingPackage is what lets a
reader audit the package against the draft without re-running the planner.
One pair per **numbered** copy, in package order — NAR1's Schedule 1 copies
print ``1 1``, ``2 2``, … numbered among themselves (the ``S1.page_no``
registry item's own convention, which the roster carries on each
``SheetPlan.page_no``); the continuation sheets and ND2A's unnumbered grid
print no footer numbers and contribute no pairs, because the envelope must
record what the flat PDF actually carries and nothing else.

**The fee block is sourced, dated and flagged (ruling R6).** ``FEE_SCHEDULE``
transcribes the NAR1 figures from the archived official table —
``research/counter-parties/cr/10-fees-and-charges/pam03c_main-service-fee-table_c.pdf``
(CR PAM 3C, December 2023), section (六) "登記周年申報表", row "有股本的本地私人
公司" — with the "as of 2026-10" label, and every rendered checklist carries
the line "verify against the CR's current fee table before filing". The same
archived table carries **no fee item for ND2A** (its sections cover
incorporation, name changes, charges, deregistration, prospectuses, annual
returns and e-search services — notification forms are not listed): ND2A's
entry is ``None`` and the ND2A checklist says the absence rather than
printing an invented figure. The figures are client-facing filing copy and
are flagged for founder line-by-line review in the phase's report.

Pure and framework-free per ADR-0006: the standard library, ``json`` for
the api-ready serialization, and ``app.domain.draft_fill`` for the plan's
shape. Nothing here touches a database, a store, or a PDF.
"""

import json
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.domain.draft_fill import FillPlan
from app.domain.drafts import ensure_exportable  # re-exported for readers of this module

__all__ = [
    "FEE_SCHEDULE",
    "ExportMode",
    "ExportModeInvalidError",
    "api_ready_bytes",
    "api_ready_payload",
    "build_envelope",
    "ensure_exportable",
    "ered_registry_helper_text",
    "paper_checklist_text",
    "validate_mode",
]


class ExportMode(StrEnum):
    """How one approved draft becomes a filing package (§2.12, §5.1).

    All three are **preparation** — no mode submits anything. ``e_registry_prep``
    flattens the filled PDF and walks a human through uploading it to the CR's
    e-Registry; ``paper`` flattens the PDF and covers it with a checklist
    (fees, payment instructions); ``api_ready`` serializes the resolved state
    and filing metadata to deterministic JSON for a future API submission
    channel (v1.1's integration; v1.0 stores the JSON as the package).
    """

    E_REGISTRY_PREP = "e_registry_prep"
    PAPER = "paper"
    API_READY = "api_ready"


class ExportModeInvalidError(ValueError):
    """The mode string is outside §2.12's vocabulary.

    ``code`` is the §5.1.3 name the API answers with
    (``export_mode_invalid``) — a body the caller misunderstood, refused by
    name before anything is written, never silently coerced.
    """

    code = "export_mode_invalid"

    def __init__(self, value: str) -> None:
        super().__init__(
            f"export mode {value!r} is not one of e_registry_prep | paper | api_ready"
        )


def validate_mode(value: str) -> ExportMode:
    """The one acceptance rule for an export request's ``mode`` field.

    The full vocabulary is accepted (a StrEnum, so the accepted spellings are
    the values themselves); anything else raises ``ExportModeInvalidError``
    carrying the §5.1.3 code. Never a silent default: the mode decides what
    the filing package is.
    """
    try:
        return ExportMode(value)
    except ValueError:
        raise ExportModeInvalidError(value) from None


def build_envelope(
    *,
    plan: FillPlan,
    helper_document_id: UUID | None,
    checklist_document_id: UUID | None,
) -> dict[str, Any]:
    """The FilingPackage's envelope: the derived counts and the companion ids.

    ``sheet_counts`` maps each sheet slug to its copy count in the order the
    package first uses it (F4c's item-8A arithmetic). ``page_pairs`` records
    the footer page-number pairs **assembly actually printed** — one
    ``[n, n]`` per numbered copy, in package order. Only NAR1's Schedule 1
    prints footer numbers (its copies numbered ``1 1``, ``2 2``, … among
    themselves, the ``S1.page_no`` registry item's own convention, which the
    roster carries on each ``SheetPlan.page_no``); the continuation sheets
    and ND2A's unnumbered grid print none, so they contribute no pairs —
    the envelope must not record numbers that appear nowhere on the flat
    PDF. The companion ids are ``None`` for modes that carry no companion
    (``api_ready`` carries none; ``paper`` has no e-Registry helper).
    """
    counts: dict[str, int] = {}
    for sheet in plan.sheets:
        counts[sheet.sheet] = counts.get(sheet.sheet, 0) + 1
    page_pairs = [
        [copy.page_no, copy.page_no] for copy in plan.sheets if copy.page_no is not None
    ]
    return {
        "sheet_counts": counts,
        "page_pairs": page_pairs,
        "helper_document_id": None if helper_document_id is None else str(helper_document_id),
        "checklist_document_id": (
            None if checklist_document_id is None else str(checklist_document_id)
        ),
    }


class _DraftFace(Protocol):
    """The draft version row's face as the api-ready payload reads it.

    The container members are concrete ``dict``/``list`` (not ``Mapping``):
    the persistence row's JSONB columns are exactly those types, and a
    Protocol's data attributes are invariant, so the face names the real
    shapes rather than a wider abstraction the row cannot satisfy.
    """

    id: UUID
    version_no: int
    form_type: str
    form_version: dict[str, Any]
    target_date: date
    filing_metadata: dict[str, Any]
    resolved_state: dict[str, Any]
    field_provenance: dict[str, Any]
    qc_flags: list[dict[str, Any]]


def api_ready_payload(*, row: _DraftFace) -> dict[str, Any]:
    """The api_ready package's structured content (§2.12): the snapshot, the
    filing-metadata document, the provenance, the QC flags and the registry
    pin — with the draft's identity and version so a filing authority can
    quote the exact revision it received. Plain dicts throughout: the row's
    JSONB columns arrive as plain data, and nothing here mutates them."""
    return {
        "form_type": row.form_type,
        "form_version": dict(row.form_version),
        "target_date": row.target_date.isoformat(),
        "filing_metadata": dict(row.filing_metadata),
        "resolved_state": dict(row.resolved_state),
        "field_provenance": dict(row.field_provenance),
        "qc_flags": [dict(flag) for flag in row.qc_flags],
        "draft": {"id": str(row.id), "version_no": row.version_no},
    }


def api_ready_bytes(*, row: _DraftFace) -> bytes:
    """The api_ready package as stored: one deterministic UTF-8 JSON document.

    Sorted keys (a reader can diff two packages line by line) and
    ``ensure_ascii=False`` (CJK values cross as UTF-8, not escapes) — the two
    choices that make two serializations of the same row byte-identical.
    """
    return json.dumps(api_ready_payload(row=row), sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )


# --- The companion documents' texts -------------------------------------------

# The fee block, transcribed from the archived official table
# (research/counter-parties/cr/10-fees-and-charges/pam03c_main-service-fee-table_c.pdf
# — CR PAM 3C, December 2023), section (六) "登記周年申報表" (registration of annual
# returns), row "有股本的本地私人公司" (local private company with share capital) —
# the company shape this product's NAR1 rule dates by anniversary. Figures as
# strings: they are filing copy, never arithmetic. As of 2026-10; the rendered
# checklist carries the verify line, and the ND2A entry is None because the
# archived table lists no fee item for that form (ruling R6; founder review).
FEE_SCHEDULE: Mapping[str, Mapping[str, Any] | None] = {
    "NAR1": {
        "source": (
            "CR PAM 3C main service fee table (Dec 2023), section (vi), "
            "local private company with share capital"
        ),
        "on_time_window": "within 42 days after the return date",
        "currency": "HKD",
        "on_time": "105",
        "late_tiers": [
            {"window": "more than 42 days but not more than 3 months late", "fee": "870"},
            {"window": "more than 3 but not more than 6 months late", "fee": "1740"},
            {"window": "more than 6 but not more than 9 months late", "fee": "2610"},
            {"window": "more than 9 months late", "fee": "3480"},
        ],
    },
    "ND2A": None,
}

# The line every rendered checklist carries (ruling R6).
VERIFY_FEE_LINE = "verify against the CR's current fee table before filing"


class _CompanyFace(Protocol):
    """The company row's face the companion texts read: the identification
    block the checklist and the helper print."""

    br_number: str
    cr_number: str | None


def ered_registry_helper_text(
    *,
    company: _CompanyFace,
    form_code: str,
    filing_metadata: Mapping[str, Any],
) -> str:
    """The e-Registry step-by-step helper (the ``e_registry_prep`` companion).

    Markdown, human-performed end to end: it names the company and the form,
    then walks the upload steps a person performs at e-Services — the flattened
    package is attached, the fee answered, and a human presses every button
    (no unattended submission anywhere; the helper is a document, not an
    integration).
    """
    made_up = _metadata_value(filing_metadata, ("return", "made_up_date"))
    return "\n".join(
        [
            f"# e-Registry filing helper — {form_code}",
            "",
            f"- Business Registration No.: {company.br_number}",
            f"- CR No.: {company.cr_number or '(not recorded)'}",
            f"- Form: {form_code}",
            f"- Made-up date: {made_up or '(not recorded)'}",
            "",
            "## Steps (performed by a human at e-Services, www.e-services.cr.gov.hk)",
            "",
            "1. Sign in to the CR e-Registry with the TCSP's e-Services account.",
            f"2. Open the filing for form {form_code} of the company above.",
            "3. Attach the flattened package PDF stored with this helper.",
            "4. Answer the form's remaining questions (the package's fields are read-only).",
            "5. Pay the registration fee shown by e-Services.",
            "6. Review every page, then submit — a human reviews and submits; nothing "
            "in this product files on its own.",
            "7. Note the e-Registry transaction reference on this filing's record.",
            "",
            "This helper is guidance only; the e-Registry screens govern.",
        ]
    )


def paper_checklist_text(
    *,
    company: _CompanyFace,
    form_code: str,
    filing_metadata: Mapping[str, Any],
    fee_schedule: Mapping[str, Mapping[str, Any] | None] = FEE_SCHEDULE,
) -> str:
    """The paper covering checklist (the ``paper`` companion): what is filed,
    for whom, the fee block, and the payment instructions — with ruling R6's
    dated, sourced figures for NAR1 and the honest "no fee item in the
    archived table" answer for a form the archived table does not list."""
    made_up = _metadata_value(filing_metadata, ("return", "made_up_date"))
    lines = [
        f"# Paper filing checklist — {form_code}",
        "",
        f"- Business Registration No.: {company.br_number}",
        f"- CR No.: {company.cr_number or '(not recorded)'}",
        f"- Form: {form_code}",
        f"- Made-up date: {made_up or '(not recorded)'}",
        "",
        "## Package contents",
        "",
        "- [ ] Flattened form PDF (no live form fields; prints as filed).",
        "- [ ] Cheque for the registration fee, crossed and made payable to "
        "「公司註冊處」 (Companies Registry).",
        "- [ ] Any supporting documents the form's notes require.",
        "",
        "## Registration fee (HKD)",
        "",
    ]
    fee_row = fee_schedule.get(form_code)
    if fee_row is None:
        lines.append(
            f"- The archived CR PAM 3C main service fee table (Dec 2023) lists no fee "
            f"item for {form_code}; no figure is stated here."
        )
    else:
        lines.append(f"- On time ({fee_row['on_time_window']}): HK$ {fee_row['on_time']}.")
        for tier in fee_row["late_tiers"]:
            lines.append(f"- {tier['window']}: HK$ {tier['fee']}.")
    lines.extend(
        [
            f"- Figures transcribed from the archived CR fee table; as of 2026-10 — "
            f"{VERIFY_FEE_LINE}.",
            "",
            "## Lodgement",
            "",
            "- [ ] Deliver to the Companies Registry, 14/F, Queensway Government Offices, "
            "66 Queensway, Hong Kong — or by post (post early; postage dates govern).",
            "- [ ] Record the filing date and any CR reference on this filing's record.",
            "",
        ]
    )
    return "\n".join(lines)


def _metadata_value(metadata: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """One filing-metadata value walked by key path, or ``None`` when absent."""
    value: Any = metadata
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value
