"""API-layer tests for the confirmation-discovery cycle route.

No database and no provider: the auth seam, RBAC context, session and storage
are overridden, and the sweep is monkeypatched, so what runs is the
fail-closed authentication, the single Administrator-only check, the mailbox
precondition (the confirmation trigger's own ``mail_not_configured``
vocabulary), the report mapping, and the one typed refusal a stored
configuration block nobody can read answers. The real sweep against
PostgreSQL — a generation under ``automatic``, a held draft under
``review_required``, the trigger's refusals, the panel-alignment skip —
lives in ``test_confirmation_discovery_application.py``.

The mapping is the point: a cycle report carries ids, the period year and the
cycle's bounded outcome vocabulary — never a body, never a letter, never
client data beyond ids.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import confirmation_discovery
from app.core.config import settings
from app.domain.confirmation_discovery import DiscoveryOutcome, DiscoverySkip
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

DISCOVERY_URL = "/api/v1/mail/confirmation-discovery"
MAILBOX = "ai-filings@tcsp.example"

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


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


class FakeSession:
    """The route's session seam: the sweep runs, the dependency commits."""

    async def commit(self) -> None:
        return None


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield FakeSession()  # type: ignore[misc]  # the sweep is monkeypatched here

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def test_the_route_is_administrator_only() -> None:
    assert build_client().post(DISCOVERY_URL).status_code == 401
    assert (
        build_client(actor=ADMIN, context=GENERAL_CONTEXT).post(DISCOVERY_URL).status_code
        == 403
    )


def test_the_route_refuses_when_no_mailbox_address_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmation trigger's own precondition and vocabulary: a deployment
    with no mailbox address to write from never starts a cycle (and the sweep
    is not called)."""
    monkeypatch.setattr(settings, "mail_address", "")
    calls: list[int] = []

    async def fake_sweep(*args: object, **kwargs: object) -> object:
        calls.append(1)
        raise AssertionError("the sweep must not run without a mailbox address")

    monkeypatch.setattr(confirmation_discovery, "sweep", fake_sweep)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(DISCOVERY_URL)

    assert response.status_code == 503
    assert response.json()["detail"] == "mail_not_configured"
    assert calls == []


def test_the_route_reports_the_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts and the cycle's own vocabulary, one entry per examined company —
    and a configured mailbox means the precondition passes and the cycle runs."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    company_id = uuid4()
    request_id = uuid4()
    report = confirmation_discovery.DiscoveryCycleReport(
        now=datetime.now(UTC),
        outcomes=(
            confirmation_discovery.CompanyReport(
                company_id=company_id,
                year=2026,
                outcome=DiscoveryOutcome.GENERATED,
                request_id=request_id,
            ),
            confirmation_discovery.CompanyReport(
                company_id=uuid4(),
                year=2025,
                outcome=DiscoveryOutcome.REFUSED,
                reason="no_client_contact",
            ),
            confirmation_discovery.CompanyReport(
                company_id=uuid4(),
                year=2026,
                outcome=DiscoveryOutcome.SKIPPED,
                skip=DiscoverySkip.ALREADY_REQUESTED,
            ),
        ),
    )

    async def fake_sweep(
        *args: object, **kwargs: object
    ) -> confirmation_discovery.DiscoveryCycleReport:
        return report

    monkeypatch.setattr(confirmation_discovery, "sweep", fake_sweep)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(DISCOVERY_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["generated"], body["refused"], body["skipped"]) == (3, 1, 1, 1)
    (generated,) = [item for item in body["outcomes"] if item["outcome"] == "generated"]
    assert generated["company_id"] == str(company_id)
    assert generated["year"] == 2026
    assert generated["request_id"] == str(request_id)
    (refused,) = [item for item in body["outcomes"] if item["outcome"] == "refused"]
    assert refused["reason"] == "no_client_contact"
    (skipped,) = [item for item in body["outcomes"] if item["outcome"] == "skipped"]
    assert skipped["skip"] == "already_requested"
    # The bounded wire shape: ids, the period and codes — nothing a client wrote.
    for item in body["outcomes"]:
        assert set(item) == {"company_id", "year", "outcome", "request_id", "reason", "skip"}


def test_an_unreadable_config_block_answers_tenant_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1.3's vocabulary: the sweep names the block's violations, the route
    refuses with the same code the reminder cycle answers."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    report = confirmation_discovery.DiscoveryCycleReport(
        now=datetime.now(UTC),
        outcomes=(),
        config_error="milestones.data_confirmation_days: not a positive integer",
    )

    async def fake_sweep(
        *args: object, **kwargs: object
    ) -> confirmation_discovery.DiscoveryCycleReport:
        return report

    monkeypatch.setattr(confirmation_discovery, "sweep", fake_sweep)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(DISCOVERY_URL)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("tenant_config_invalid: ")
    assert "data_confirmation_days" in detail
