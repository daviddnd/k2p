"""API-layer tests for the tenant-config endpoints.

These run without a database: the auth seam, RBAC context, and session are
overridden, and the application services are monkeypatched, so only
fail-closed authentication, the single Administrator-only authorization
check (``require_admin`` — scope §2.15 assigns configuration to the
Administrator), request validation, and the typed-error → HTTP mappings are
exercised. Real flows against PostgreSQL live in
``test_tenant_config_scenarios_db.py``.
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
from app.application import tenant_config
from app.domain.milestones import (
    MilestoneValidationError,
    default_milestones,
    validate_milestones,
)
from app.domain.naming_templates import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DEFAULT_GENERATED_TEMPLATE,
    TemplateValidationError,
    default_naming_templates,
)
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.send_modes import default_send_modes
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

URL = "/api/v1/tenant-config"
VALID_PAYLOAD = {
    "naming_templates": {
        "document": "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.pdf",
        "generated": "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.pdf",
    },
    "milestones": default_milestones().as_dict(),
}


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


# --- Fail closed; both routes Administrator-only ---


def test_unauthenticated_fails_closed_on_both_routes() -> None:
    client = build_client()
    assert client.get(URL).status_code == 401
    assert client.put(URL, json=VALID_PAYLOAD).status_code == 401


def test_general_is_forbidden_on_both_routes() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_CONTEXT
    )
    assert client.get(URL).status_code == 403
    assert client.put(URL, json=VALID_PAYLOAD).status_code == 403


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    assert client.get(URL).status_code == 403
    assert client.put(URL, json=VALID_PAYLOAD).status_code == 403


# --- Request validation (422 before any service call) ---


def test_put_requires_both_templates() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.put(URL, json={}).status_code == 422
    assert client.put(URL, json={"naming_templates": {}}).status_code == 422
    assert (
        client.put(
            URL,
            json={
                "naming_templates": {"document": "{CR}.pdf", "generated": ""},
                "milestones": default_milestones().as_dict(),
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "field",
    [
        "continuation_request_days_before",
        "data_confirmation_days_before",
        "client_reply_days_before",
        "signature_post_days_before",
        "minimum_reply_window_days",
        "reminder_cadence_days",
        "anchor",
    ],
)
def test_put_requires_every_milestone_field(field: str) -> None:
    """The block is replaced whole, so a request that omits a field is not a
    request to keep the stored one — it is an incomplete configuration."""
    milestones = default_milestones().as_dict()
    del milestones[field]

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.put(
        URL,
        json={
            "naming_templates": VALID_PAYLOAD["naming_templates"],
            "milestones": milestones,
        },
    )

    assert response.status_code == 422


def test_a_milestone_field_pydantic_cannot_read_is_refused_before_any_service_call() -> None:
    """The edge refuses what it cannot read into the block's declared types
    (a numeric string is coerced to its number by pydantic's own rule; the
    *rules* about the numbers are the domain's, and run on the service)."""
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.put(
        URL,
        json={
            "naming_templates": VALID_PAYLOAD["naming_templates"],
            "milestones": {**default_milestones().as_dict(), "client_reply_days_before": "twenty"},
        },
    )

    assert response.status_code == 422


# --- Service mappings ---


def test_get_returns_effective_config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(session: object, *, tenant_id: object) -> tenant_config.TenantConfigView:
        assert tenant_id == ADMIN.tenant_id
        return tenant_config.TenantConfigView(
            naming_templates=default_naming_templates(),
            milestones=default_milestones(),
            send_modes=default_send_modes(),
            updated_at=None,
        )

    monkeypatch.setattr(tenant_config, "get_tenant_config", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    # The endpoint serializes whatever the service hands it; the shipped
    # defaults themselves are pinned once, in the domain test suites.
    assert body["naming_templates"] == {
        "document": DEFAULT_DOCUMENT_TEMPLATE,
        "generated": DEFAULT_GENERATED_TEMPLATE,
    }
    assert body["milestones"] == default_milestones().as_dict()
    # The third block: the founder's send-mode defaults, read from the one
    # place they are defined.
    assert body["send_modes"] == default_send_modes().as_dict()
    assert body["updated_at"] is None
    # Empty is the answer "both stored blocks validated" (or that there is no
    # row): a client can read the field rather than learn to ignore it.
    assert body["unreadable_blocks"] == []


def test_put_stores_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    stored_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    custom = {**default_milestones().as_dict(), "client_reply_days_before": 25}

    async def fake_put(
        session: object,
        *,
        tenant_id: object,
        actor_user_id: object,
        document: str,
        generated: str,
        milestones: dict[str, object],
        send_modes: dict[str, object] | None = None,
    ) -> tenant_config.TenantConfigView:
        assert tenant_id == ADMIN.tenant_id
        assert actor_user_id == ADMIN.user_id
        # The payload reaches the service as the block it validated, untyped by
        # this layer: the domain is what turns it into a ``Milestones``.
        assert milestones == custom
        # An absent send-mode block arrives as ``None`` — which is exactly what
        # "keep what is stored" means to the service — and an explicit one is
        # forwarded whole.
        assert send_modes in (None, {"client": "review_required", "government": "review_required"})
        return tenant_config.TenantConfigView(
            naming_templates=default_naming_templates(),
            milestones=validate_milestones(milestones),
            send_modes=default_send_modes(),
            updated_at=stored_at,
        )

    monkeypatch.setattr(tenant_config, "put_tenant_config", fake_put)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.put(
        URL,
        json={
            "naming_templates": VALID_PAYLOAD["naming_templates"],
            "milestones": custom,
        },
    )
    assert response.status_code == 200
    assert response.json()["updated_at"] == "2026-08-24T12:00:00Z"
    assert response.json()["milestones"] == custom


def test_put_maps_template_validation_errors_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_put(
        session: object,
        *,
        tenant_id: object,
        actor_user_id: object,
        document: str,
        generated: str,
        milestones: dict[str, object],
        send_modes: dict[str, object] | None = None,
    ) -> tenant_config.TenantConfigView:
        raise TemplateValidationError(["document: unknown tokens: Bogus"])

    monkeypatch.setattr(tenant_config, "put_tenant_config", fake_put)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.put(URL, json=VALID_PAYLOAD)
    assert response.status_code == 422
    assert response.json()["detail"] == ["document: unknown tokens: Bogus"]


def test_put_maps_milestone_validation_errors_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every violation travels, exactly as the template block's do: the caller
    fixes the configuration in one round trip."""

    async def fake_put(
        session: object,
        *,
        tenant_id: object,
        actor_user_id: object,
        document: str,
        generated: str,
        milestones: dict[str, object],
        send_modes: dict[str, object] | None = None,
    ) -> tenant_config.TenantConfigView:
        raise MilestoneValidationError(
            [
                "milestones.client_reply_days_before: must not exceed 35 "
                "(NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days)",
                "milestones.anchor: must be 'anniversary'",
            ]
        )

    monkeypatch.setattr(tenant_config, "put_tenant_config", fake_put)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.put(URL, json=VALID_PAYLOAD)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 2
    assert any("client_reply_days_before" in error for error in detail)
    assert any("anchor" in error for error in detail)


def test_get_names_a_stored_block_it_cannot_read_and_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted stored block must not take this read down — it is the repair
    path — and must not be hidden either.

    The service substitutes the unreadable block's shipped default and reports
    the block and every violation; the endpoint serializes both, so a client
    can tell "the tenant stored this" from "this is the default" and assemble
    the full-replace `PUT` that fixes the row. The list is never dropped: a
    response that carried the defaults alone would hide a real integrity
    problem behind a working screen.
    """

    async def fake_get(session: object, *, tenant_id: object) -> tenant_config.TenantConfigView:
        return tenant_config.TenantConfigView(
            naming_templates=default_naming_templates(),
            milestones=default_milestones(),
            send_modes=default_send_modes(),
            updated_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            unreadable_blocks=(
                tenant_config.UnreadableBlock(
                    block=tenant_config.ConfigBlock.MILESTONES,
                    violations=("milestones.anchor: must be 'anniversary'",),
                ),
            ),
        )

    monkeypatch.setattr(tenant_config, "get_tenant_config", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["milestones"] == default_milestones().as_dict()
    assert body["updated_at"] == "2026-09-24T12:00:00Z"
    assert body["unreadable_blocks"] == [
        {
            "block": "milestones",
            "violations": ["milestones.anchor: must be 'anniversary'"],
        }
    ]
