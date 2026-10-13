"""API-layer tests for the split-items review surface (workstream ③, ruling D8).

Hermetic, the ``test_api_attribution`` pattern: the auth seam, the
Administrator-only check, and the split service are monkeypatched, so what
these tests exercise is fail-closed authentication, the custody bar
(coordinates only — no subject, no body, anywhere in the response shapes),
the list's status filter, and the one typed 404. What is behind them — the
parents and items the cycle writes — is proven against real PostgreSQL in
``test_split_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import SplitParent, SplitWorkItem
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import split
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

SPLIT_ITEMS_URL = "/api/v1/mail/split-items"
NOW = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),  # even holding every grantable permission
    company_ids=frozenset(),
)


def build_client(
    actor: ActorRef | None = None, context: ActorContext | None = None
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def parent_row(
    *,
    parent_id: UUID | None = None,
    status: str = "split_done",
    mailbox_message_id: str = "msg-parent-1",
    sender: str = "director@client.example",
) -> SplitParent:
    return SplitParent(
        id=parent_id or uuid4(),
        tenant_id=ADMIN.tenant_id,
        mailbox_message_id=mailbox_message_id,
        sender_address=sender,
        subject="The client's own words — never serialized",
        received_at=NOW,
        raw_document_id=uuid4(),
        status=status,
        created_at=NOW,
    )


def item_row(parent: SplitParent, *, company_id: UUID | None = None) -> SplitWorkItem:
    return SplitWorkItem(
        id=uuid4(),
        tenant_id=ADMIN.tenant_id,
        parent_id=parent.id,
        company_id=company_id or uuid4(),
        slice_document_id=uuid4(),
        status="awaiting_parse",
        created_at=NOW,
    )


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()

    assert client.get(SPLIT_ITEMS_URL).status_code == 401
    assert client.get(f"{SPLIT_ITEMS_URL}/{uuid4()}").status_code == 401


def test_the_surface_is_administrator_only() -> None:
    client = build_client(actor=ADMIN, context=GENERAL_CONTEXT)

    assert client.get(SPLIT_ITEMS_URL).status_code == 403
    assert client.get(f"{SPLIT_ITEMS_URL}/{uuid4()}").status_code == 403


def test_the_list_carries_coordinates_only_and_the_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The custody bar, shape-pinned: parent coordinates + the items' company
    and slice coordinates — and no field anywhere that could carry what the
    client wrote (the parent's ``subject`` is deliberately never serialized)."""
    done = parent_row(status="split_done")
    parked = parent_row(status="staff_only", mailbox_message_id="msg-parent-2")
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, status: split.SplitParentStatus | None
    ) -> list[SplitParent]:
        seen["tenant_id"] = tenant_id
        seen["status"] = status
        return [done, parked]

    async def fake_items(
        session: object, *, tenant_id: UUID, parent_id: UUID
    ) -> list[SplitWorkItem]:
        return [item_row(done), item_row(done)]

    monkeypatch.setattr(split, "list_split_parents", fake_list)
    monkeypatch.setattr(split, "items_for_parent", fake_items)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(SPLIT_ITEMS_URL)

    assert response.status_code == 200, response.text
    assert seen["tenant_id"] == ADMIN.tenant_id  # the tenant is the actor's, never a parameter
    assert seen["status"] is None  # the default is every parent
    (first, second) = response.json()
    assert set(first) == {
        "id",
        "mailbox_message_id",
        "sender_address",
        "received_at",
        "status",
        "items",
        "created_at",
    }
    assert "subject" not in first
    assert first["status"] == "split_done"
    assert first["mailbox_message_id"] == "msg-parent-1"
    assert second["status"] == "staff_only"
    (item, _) = first["items"]
    assert set(item) == {"id", "company_id", "status", "slice_document_id", "created_at"}
    assert item["status"] == "awaiting_parse"


def test_the_list_filters_to_one_parent_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_list(
        session: object, *, tenant_id: UUID, status: split.SplitParentStatus | None
    ) -> list[SplitParent]:
        seen["status"] = status
        return []

    monkeypatch.setattr(split, "list_split_parents", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{SPLIT_ITEMS_URL}?status=staff_only")

    assert response.status_code == 200, response.text
    assert seen["status"] is split.SplitParentStatus.STAFF_ONLY
    assert response.json() == []


def test_an_unknown_status_filter_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    async def fake_list(
        session: object, *, tenant_id: UUID, status: split.SplitParentStatus | None
    ) -> list[SplitParent]:
        calls.append(status)
        return []

    monkeypatch.setattr(split, "list_split_parents", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{SPLIT_ITEMS_URL}?status=triage_later")

    assert response.status_code == 422
    assert calls == []


def test_one_parent_reads_by_id_and_refuses_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 for an unknown id **and** for another tenant's — one answer,
    deliberately; the read is tenant-scoped like every read here."""
    parent = parent_row()
    seen: dict[str, object] = {}

    async def fake_get(session: object, *, tenant_id: UUID, parent_id: UUID) -> object:
        seen["tenant_id"] = tenant_id
        seen["parent_id"] = parent_id
        return parent

    async def fake_items(
        session: object, *, tenant_id: UUID, parent_id: UUID
    ) -> list[SplitWorkItem]:
        return [item_row(parent)]

    async def fake_get_none(session: object, *, tenant_id: UUID, parent_id: UUID) -> object:
        return None

    monkeypatch.setattr(split, "get_split_parent", fake_get)
    monkeypatch.setattr(split, "items_for_parent", fake_items)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{SPLIT_ITEMS_URL}/{parent.id}")

    assert response.status_code == 200, response.text
    assert seen["tenant_id"] == ADMIN.tenant_id
    assert seen["parent_id"] == parent.id
    assert response.json()["id"] == str(parent.id)
    assert response.json()["status"] == "split_done"
    assert len(response.json()["items"]) == 1

    monkeypatch.setattr(split, "get_split_parent", fake_get_none)
    missing = client.get(f"{SPLIT_ITEMS_URL}/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "split parent not found"
