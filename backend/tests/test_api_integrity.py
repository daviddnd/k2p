"""API-layer tests for the integrity-verification route (ADR-0009/0011;
scope §8; Review Focus 5).

These run without a database: the auth seam, the administrator gate, the
session, the storage client and the application seam are overridden, so what
is exercised is fail-closed authentication, the Administrator-only 403, the
200 result shape, exactly one ``integrity_check`` audit entry with the
administrator as actor, the ``{limit}`` pass-through, the honest-200 storage
rule, and the named 503 when the check cannot run at all.

The property this file guards: the route records the single audit entry the
check itself never writes, and a mismatch — even a total storage outage —
still answers 200 with the honest result.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import audit, integrity
from app.domain.audit import AuditAction
from app.domain.integrity import (
    IntegrityResult,
    Mismatch,
    MismatchKind,
    audit_details,
    classify,
)
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

URL = "/api/v1/admin/integrity-check"

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)


def clean_result() -> IntegrityResult:
    return classify(True, 7, [])


def mismatch_result() -> IntegrityResult:
    return classify(
        True,
        3,
        [Mismatch(MismatchKind.DOCUMENT_HASH_MISMATCH, uuid4(), "recomputed digest differs")],
    )


async def no_audit(*args: object, **kwargs: object) -> None:
    return None


class _FakeStore:
    """The storage client is injected but never touched — the seam is stubbed."""


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the seam is stubbed in these tests

    app.dependency_overrides[get_session] = no_session
    app.dependency_overrides[get_storage] = lambda: _FakeStore()
    return TestClient(app)


def test_the_route_fails_closed_without_an_actor_and_refuses_a_general_account() -> None:
    assert build_client().post(URL).status_code == 401
    assert (
        build_client(
            actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL
        ).post(URL).status_code
        == 403
    )


def test_an_administrator_runs_the_check_and_the_one_audit_entry_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    written: dict[str, Any] = {}
    check_result = mismatch_result()

    async def fake_check(*args: object, **kwargs: object) -> IntegrityResult:
        seen.update(kwargs)
        return check_result

    async def fake_record(*args: object, **kwargs: object) -> None:
        written.update(kwargs)

    monkeypatch.setattr(integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(audit, "record_event", fake_record)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(URL, json={"limit": 5})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "checked_at",
        "chain_verified",
        "documents_checked",
        "mismatch_count",
        "mismatches",
    }
    assert body["chain_verified"] is True
    assert body["documents_checked"] == 3
    assert body["mismatch_count"] == 1
    assert set(body["mismatches"][0]) == {"kind", "document_id", "detail"}
    assert body["mismatches"][0]["kind"] == "document_hash_mismatch"
    # The seam got the actor's tenant and the body's limit.
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["limit"] == 5
    assert isinstance(seen["now"], datetime)
    # Exactly one audit entry, recorded by the route: the administrator is the
    # actor, the details carry the counts and the first mismatch id.
    assert written["action"] is AuditAction.INTEGRITY_CHECK
    assert written["actor_user_id"] == ADMIN.user_id
    assert written["tenant_id"] == ADMIN.tenant_id
    assert written["details"] == audit_details(check_result)


def test_the_limit_is_optional_and_defaults_to_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_check(*args: object, **kwargs: object) -> IntegrityResult:
        seen.update(kwargs)
        return clean_result()

    monkeypatch.setattr(integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(audit, "record_event", no_audit)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(URL)

    assert response.status_code == 200, response.text
    assert seen["limit"] is None
    assert response.json()["documents_checked"] == 7


def test_a_storage_outage_mid_check_still_answers_200_with_the_honest_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam degrades an unreadable store to ``document_unreadable``
    findings; the route's job is only to report them — never to fail the
    check on the store's behalf."""

    async def fake_check(*args: object, **kwargs: object) -> IntegrityResult:
        return classify(
            True,
            2,
            [Mismatch(MismatchKind.DOCUMENT_UNREADABLE, uuid4(), "store error")],
        )

    monkeypatch.setattr(integrity, "run_integrity_check", fake_check)
    monkeypatch.setattr(audit, "record_event", no_audit)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chain_verified"] is True
    assert body["documents_checked"] == 2
    assert body["mismatches"][0]["kind"] == "document_unreadable"


def test_a_check_that_cannot_run_at_all_is_the_named_503_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def dead_database(*args: object, **kwargs: object) -> IntegrityResult:
        raise SQLAlchemyError("connection refused")

    async def fake_record(*args: object, **kwargs: object) -> None:
        calls.append(1)

    monkeypatch.setattr(integrity, "run_integrity_check", dead_database)
    monkeypatch.setattr(audit, "record_event", fake_record)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(URL)

    assert response.status_code == 503
    assert response.json()["detail"] == "integrity_check_unavailable"
    assert calls == []
