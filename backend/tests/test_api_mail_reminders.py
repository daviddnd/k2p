"""API-layer tests for the reminder-cycle route (contract §5.1.12).

No database and no provider: the auth seam, RBAC context, session and storage
are overridden, and the sweep is monkeypatched, so what runs is the
fail-closed authentication, the single Administrator-only check, the report
mapping, and the one typed refusal a stored configuration block nobody can
read answers. The real sweep against PostgreSQL — an enqueue under
``automatic``, a held row under ``review_required``, a Free-tier denial —
lives in ``test_reminders_application.py``.

The mapping is the point: a cycle report carries ids, counts and the sweep's
own outcome vocabulary — never a body, never a letter, never client data
beyond ids.
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
from app.application import reminders
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

REMINDERS_URL = "/api/v1/mail/reminders"

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
    assert build_client().post(REMINDERS_URL).status_code == 401
    assert (
        build_client(actor=ADMIN, context=GENERAL_CONTEXT).post(REMINDERS_URL).status_code
        == 403
    )


def test_the_route_reports_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts and the sweep's own vocabulary, one entry per request — and no
    row's outcome is ever an HTTP error."""
    company_id = uuid4()
    request_id = uuid4()
    report = reminders.SweepReport(
        now=datetime.now(UTC),
        outcomes=(
            reminders.RequestReport(
                request_id=request_id,
                company_id=company_id,
                outcome=reminders.SweepOutcome.ENQUEUED,
                cadence_index=1,
                held=False,
            ),
            reminders.RequestReport(
                request_id=uuid4(),
                company_id=company_id,
                outcome=reminders.SweepOutcome.DENIED,
                reason="tier_capability_missing",
            ),
            reminders.RequestReport(
                request_id=uuid4(),
                company_id=company_id,
                outcome=reminders.SweepOutcome.SKIPPED,
                skip=reminders.SweepSkip.NOT_DUE,
            ),
        ),
    )

    async def fake_sweep(*args: object, **kwargs: object) -> reminders.SweepReport:
        return report

    monkeypatch.setattr(reminders, "sweep", fake_sweep)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(REMINDERS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["enqueued"], body["denied"], body["skipped"]) == (3, 1, 1, 1)
    assert body["held"] == 0
    (enqueued,) = [item for item in body["outcomes"] if item["outcome"] == "enqueued"]
    assert enqueued["confirmation_request_id"] == str(request_id)
    assert enqueued["cadence_index"] == 1
    assert enqueued["held"] is False
    (denied,) = [item for item in body["outcomes"] if item["outcome"] == "denied"]
    assert denied["reason"] == "tier_capability_missing"
    (skipped,) = [item for item in body["outcomes"] if item["outcome"] == "skipped"]
    assert skipped["skip_reason"] == "not_due"
    # The bounded wire shape: ids, counts and codes — nothing a client wrote.
    for item in body["outcomes"]:
        assert set(item) == {
            "confirmation_request_id",
            "company_id",
            "outcome",
            "cadence_index",
            "held",
            "reason",
            "skip_reason",
        }


def test_an_unreadable_config_block_answers_tenant_config_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1.3's vocabulary: the sweep names the block's violations, the route
    refuses with the same code the confirmation trigger answers."""
    report = reminders.SweepReport(
        now=datetime.now(UTC),
        outcomes=(),
        config_error="milestones.reminder_cadence_days: not a list of positive integers",
    )

    async def fake_sweep(*args: object, **kwargs: object) -> reminders.SweepReport:
        return report

    monkeypatch.setattr(reminders, "sweep", fake_sweep)

    response = build_client(actor=ADMIN, context=ADMIN_CONTEXT).post(REMINDERS_URL)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("tenant_config_invalid: ")
    assert "reminder_cadence_days" in detail
