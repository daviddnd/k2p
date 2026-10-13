"""Unit tests for the audit-log domain rules (ADR-0009; scope §8).

Covers the hash-chain computation (deterministic, order-independent,
tamper-evident) and the chain verifier used by the integrity checks. No
database or framework imports — this layer is pure per ADR-0006.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.domain.audit import (
    AuditAction,
    ChainEntry,
    compute_entry_hash,
    jsonable,
    verify_chain,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ACTOR = UUID("22222222-2222-2222-2222-222222222222")
COMPANY = UUID("33333333-3333-3333-3333-333333333333")
AT = datetime(2026, 8, 24, 9, 30, 0, tzinfo=UTC)


def _hash(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "occurred_at": AT,
        "actor_user_id": ACTOR,
        "company_id": COMPANY,
        "action": AuditAction.COMPANY_CREATED,
        "entity_type": "company",
        "entity_id": COMPANY,
        "details": {"br_number": "12345678"},
        "llm_call": None,
        "prev_hash": None,
    }
    kwargs.update(overrides)
    return compute_entry_hash(**kwargs)


# --- compute_entry_hash ---


def test_hash_is_deterministic() -> None:
    assert _hash() == _hash()


def test_hash_is_order_independent_for_detail_keys() -> None:
    first = _hash(details={"a": 1, "b": {"x": 1, "y": 2}})
    second = _hash(details={"b": {"y": 2, "x": 1}, "a": 1})
    assert first == second


def test_every_hashed_field_is_tamper_evident() -> None:
    baseline = _hash()
    variants = [
        _hash(tenant_id=uuid4()),
        _hash(occurred_at=datetime(2026, 8, 24, 9, 30, 1, tzinfo=UTC)),
        _hash(actor_user_id=uuid4()),
        _hash(company_id=uuid4()),
        _hash(action=AuditAction.COMPANY_STATICS_CORRECTED),
        _hash(entity_type="user"),
        _hash(entity_id=uuid4()),
        _hash(details={"br_number": "87654321"}),
        _hash(llm_call={"provider": "siliconflow"}),
        _hash(prev_hash="0" * 64),
    ]
    assert baseline not in variants


def test_hash_handles_domain_value_types() -> None:
    """UUIDs, dates, datetimes, and Decimals canonicalize without error."""
    result = _hash(
        details={
            "effective_date": date(2026, 8, 24),
            "at": AT,
            "amount": Decimal("1234.50"),
            "ids": [COMPANY],
        }
    )
    assert len(result) == 64
    int(result, 16)  # hex


# --- jsonable ---


def test_jsonable_converts_non_json_types() -> None:
    converted = jsonable(
        {
            "id": COMPANY,
            "when": AT,
            "day": date(2026, 8, 24),
            "amount": Decimal("10.00"),
            "action": AuditAction.STAFF_LOGIN,
            "nested": [{"day": date(2026, 1, 1)}],
        }
    )
    assert converted == {
        "id": str(COMPANY),
        "when": AT.isoformat(),
        "day": "2026-08-24",
        "amount": "10.00",
        "action": "staff_login",
        "nested": [{"day": "2026-01-01"}],
    }


# --- verify_chain ---


@dataclass(frozen=True)
class _Entry:
    """A minimal stored-entry double satisfying the ChainEntry protocol."""

    tenant_id: UUID
    occurred_at: datetime
    actor_user_id: UUID | None
    company_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None
    details: Any
    llm_call: Any
    prev_hash: str | None
    entry_hash: str


def _make_entry(index: int, prev_hash: str | None, *, details: Any = None) -> _Entry:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "occurred_at": datetime(2026, 8, 24, 10, index, 0, tzinfo=UTC),
        "actor_user_id": ACTOR,
        "company_id": COMPANY,
        "action": AuditAction.COMPANY_FACT_RECORDED,
        "entity_type": "company_name_records",
        "entity_id": uuid4(),
        "details": details if details is not None else {"seq": index},
        "llm_call": None,
        "prev_hash": prev_hash,
    }
    return _Entry(**fields, entry_hash=compute_entry_hash(**fields))


def _chain(length: int) -> list[_Entry]:
    entries: list[_Entry] = []
    for index in range(length):
        entries.append(_make_entry(index, entries[-1].entry_hash if entries else None))
    return entries


def test_verify_chain_accepts_a_valid_chain() -> None:
    assert verify_chain(_chain(5)) == []


def test_verify_chain_accepts_an_empty_chain() -> None:
    assert verify_chain([]) == []


def test_verify_chain_flags_a_broken_link() -> None:
    entries = _chain(3)
    tampered = _make_entry(2, "f" * 64)  # links to nothing
    problems = verify_chain([entries[0], entries[1], tampered])
    assert any("prev_hash" in problem for problem in problems)


def test_verify_chain_flags_tampered_contents() -> None:
    entries = _chain(3)
    # Same link, different contents: the stored hash no longer matches.
    forged = _Entry(**{**vars(entries[1]), "details": {"seq": "forged"}})
    problems = verify_chain([entries[0], forged, entries[2]])
    assert any("entry_hash" in problem for problem in problems)


def test_verify_chain_flags_a_non_genesis_first_entry() -> None:
    entries = _chain(3)
    problems = verify_chain(entries[1:])  # first entry has a non-null prev_hash
    assert any("prev_hash" in problem for problem in problems)


def test_chain_entry_protocol_is_satisfied_by_the_double() -> None:
    entry: ChainEntry = _chain(1)[0]
    assert entry.entry_hash
