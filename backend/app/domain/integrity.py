"""Integrity-verification domain rules (ADR-0009/0011; scope §8; Review Focus 5).

Single owner of:

- the **mismatch vocabulary** (``MismatchKind``) — ``audit_chain_break`` /
  ``document_hash_mismatch`` / ``document_unreadable``: the three ways the
  deployment's own records can disagree with themselves;
- the **result shape** (``IntegrityResult``) and the one classification rule
  (``classify``) — the chain leg and the document leg report their verdicts
  honestly on their own (a blob problem never rewrites the chain verdict),
  and ``ok`` is the conjunction a caller alerts on;
- the **audit-summary shape** (``audit_details``) — the single
  ``integrity_check`` entry's details: the counts plus the first
  ``AUDIT_MISMATCH_LIMIT`` mismatch ids, so an alert is actionable without
  embedding a finding list unbounded in size.

Pure and framework-free per ADR-0006: no I/O, no clock — a check's instant
is the caller's input, never read here; same inputs, same result.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

# The audit entry's mismatch list is capped, not the count: an operator
# alerting on a large-scale mismatch needs the true count and enough ids to
# start investigating.
AUDIT_MISMATCH_LIMIT = 10


class MismatchKind(StrEnum):
    """The three findings the verification job can make."""

    AUDIT_CHAIN_BREAK = "audit_chain_break"
    DOCUMENT_HASH_MISMATCH = "document_hash_mismatch"
    DOCUMENT_UNREADABLE = "document_unreadable"


@dataclass(frozen=True)
class Mismatch:
    """One finding; ``document_id`` is ``None`` only for chain breaks, which
    name no document."""

    kind: MismatchKind
    document_id: UUID | None
    detail: str


@dataclass(frozen=True)
class IntegrityResult:
    """What one check found; nothing here is stored, everything is reported.

    ``chain_verified`` is the chain leg's own verdict — a document mismatch
    never changes it. ``ok`` is the conjunction: the chain verified and no
    mismatch of any kind was found.
    """

    chain_verified: bool
    documents_checked: int
    mismatches: tuple[Mismatch, ...]

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)

    @property
    def ok(self) -> bool:
        return self.chain_verified and not self.mismatches


def classify(
    chain_ok: bool, checked: int, mismatches: Sequence[Mismatch]
) -> IntegrityResult:
    """The one classification rule; same inputs, same result.

    ``chain_ok`` is the chain leg's verdict from ``verify_chain`` (``True``
    when it reported no problems); ``checked`` counts the documents whose
    blobs were attempted — an unreadable blob still counted as checked;
    ``mismatches`` converts to a tuple so the result is a deterministic value.
    """
    return IntegrityResult(
        chain_verified=chain_ok,
        documents_checked=checked,
        mismatches=tuple(mismatches),
    )


def audit_details(result: IntegrityResult, *, limit: int = AUDIT_MISMATCH_LIMIT) -> dict[str, Any]:
    """The single ``integrity_check`` audit entry's details.

    Counts are honest; the mismatch list carries the first ``limit`` findings'
    kinds and document ids (``None`` for a chain break) — never the detail
    strings, which can quote storage internals.
    """
    return {
        "chain_verified": result.chain_verified,
        "documents_checked": result.documents_checked,
        "mismatch_count": result.mismatch_count,
        "mismatches": [
            {
                "kind": mismatch.kind.value,
                "document_id": (
                    None if mismatch.document_id is None else str(mismatch.document_id)
                ),
            }
            for mismatch in result.mismatches[:limit]
        ],
    }
