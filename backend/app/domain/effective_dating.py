"""Effective-dated record resolution (ADR-0007; data-model §1).

Time-bound master data is append-only: facts are records carrying
``effective_date`` plus provenance, never updated in place. "Current state"
is a derived view — per fact group, the record with the latest
``effective_date`` not after the as-of date. This module is the single,
deterministic implementation of that rule; draft construction and
prior-filing cross-checks resolve through it rather than re-implementing it.

Pure and framework-free per ADR-0006: records are accepted structurally (a
``Protocol``), so ORM rows from ``adapters/persistence`` satisfy the rule
without the domain layer importing any framework.
"""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Protocol


class EffectiveDated(Protocol):
    """Structural minimum for a resolvable record (data-model §1)."""

    effective_date: date
    created_at: datetime


def resolve_as_of[T: EffectiveDated](records: Iterable[T], as_of: date) -> T | None:
    """The record current as of ``as_of``, or ``None`` if nothing is in effect.

    The record with the latest ``effective_date`` not after ``as_of`` wins;
    a record takes effect on its ``effective_date`` itself. Two records
    sharing an ``effective_date`` (a same-day correction) break ties by
    ``created_at`` — the one the system learned about later supersedes.
    A full tie on both cannot be committed by the normal write path: fact
    tables default ``created_at`` to ``clock_timestamp()`` (per-statement
    wall clock), so even two appends in one transaction get distinct
    timestamps (migration 0004).
    """
    candidates = [record for record in records if record.effective_date <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda record: (record.effective_date, record.created_at))
