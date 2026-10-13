"""DB-backed tenant-config scenarios (opt-in; needs a migrated PostgreSQL).

These verify migrations ``0006_tenant_config`` and ``0019_tenant_milestones``
and the tenant-config services / endpoints against a real database:
defaults-before-first-PUT, the PUT → GET round trip, upsert semantics (one row
per tenant), the two blocks' validation at the write path, the backfilled
milestone block, the audit entries each block's write leaves, cross-tenant
isolation, and fail-closed re-validation of a corrupted stored row. Skipped
unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to head (see
``test_rbac_scenarios_db.py`` for the setup recipe).

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef, get_current_actor
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
)
from app.domain.send_modes import default_send_modes
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

URL = "/api/v1/tenant-config"
CUSTOM_DOCUMENT = "{CR}/originals/{Form}_{DocType}_{YYYYMMDD}.pdf"
CUSTOM_GENERATED = "{CR}/generated/{Form}_{DocType}_{YYYYMMDD}_v{Version}.pdf"
CUSTOM_MILESTONES = {
    "continuation_request_days_before": 90,
    "data_confirmation_days_before": 45,
    "client_reply_days_before": 30,
    "signature_post_days_before": 20,
    "minimum_reply_window_days": 12,
    "reminder_cadence_days": [1, 5],
    "anchor": "anniversary",
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def make_admin(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


def admin_client(session: AsyncSession, tenant: Tenant, admin: User) -> TestClient:
    return make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))


def put_config(
    client: TestClient,
    *,
    document: str = CUSTOM_DOCUMENT,
    generated: str = CUSTOM_GENERATED,
    milestones: dict[str, object] | None = None,
) -> dict:
    response = client.put(
        URL,
        json={
            "naming_templates": {"document": document, "generated": generated},
            "milestones": dict(CUSTOM_MILESTONES if milestones is None else milestones),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Defaults and round trip through the real stack ---


async def test_get_returns_shipped_defaults_before_first_put(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["naming_templates"]["document"] == DEFAULT_DOCUMENT_TEMPLATE
    assert body["naming_templates"]["generated"] == DEFAULT_GENERATED_TEMPLATE
    assert body["milestones"] == default_milestones().as_dict()
    assert body["updated_at"] is None
    # No row is created by reading.
    assert (await session.scalar(select(TenantConfig))) is None


async def test_put_get_round_trip(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    stored = put_config(client)
    assert stored["naming_templates"]["document"] == CUSTOM_DOCUMENT
    assert stored["naming_templates"]["generated"] == CUSTOM_GENERATED
    assert stored["milestones"] == CUSTOM_MILESTONES
    assert stored["updated_at"] is not None

    fetched = client.get(URL)
    assert fetched.status_code == 200
    assert fetched.json()["naming_templates"] == stored["naming_templates"]
    assert fetched.json()["milestones"] == stored["milestones"]


async def test_put_is_an_upsert_with_one_row_per_tenant(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    put_config(client)
    updated = put_config(
        client,
        document="{CompanyCode}/{Form}_{CR}_{YYYYMMDD}.pdf",
        generated="{CompanyCode}/{Form}_{CR}_{YYYYMMDD}_v{Version}.pdf",
        milestones={**CUSTOM_MILESTONES, "client_reply_days_before": 25},
    )
    assert updated["naming_templates"]["document"] == "{CompanyCode}/{Form}_{CR}_{YYYYMMDD}.pdf"
    assert updated["milestones"]["client_reply_days_before"] == 25

    rows = list((await session.scalars(select(TenantConfig))).all())
    assert len(rows) == 1
    assert rows[0].naming_templates["document"] == "{CompanyCode}/{Form}_{CR}_{YYYYMMDD}.pdf"
    # The block is replaced whole, not merged: the second PUT's values are the
    # tenant's configuration, and nothing of the first survives in it.
    assert rows[0].milestones == validate_milestones(
        {**CUSTOM_MILESTONES, "client_reply_days_before": 25}
    ).as_dict()


async def test_invalid_put_stores_nothing(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.put(
        URL,
        json={
            "naming_templates": {
                # No {CompanyCode}/{CR}; generated lacks {Version}.
                "document": "{Year}/{Form}_{Bogus}.pdf",
                "generated": "{Year}/{Form}.pdf",
            },
            "milestones": dict(CUSTOM_MILESTONES),
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("Bogus" in error for error in detail)
    assert any("Version" in error for error in detail)

    # Nothing was written; the tenant still reads the shipped defaults.
    assert (await session.scalar(select(TenantConfig))) is None
    assert client.get(URL).json()["updated_at"] is None


async def test_an_invalid_milestone_block_writes_nothing(session: AsyncSession) -> None:
    """The template block is valid and the milestone block is not: the request
    writes neither, so a tenant never ends up with half a configuration."""
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.put(
        URL,
        json={
            "naming_templates": {
                "document": CUSTOM_DOCUMENT,
                "generated": CUSTOM_GENERATED,
            },
            "milestones": {
                **CUSTOM_MILESTONES,
                "continuation_request_days_before": 10,  # below data_confirmation
                "anchor": "planned_filing_date",
            },
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("continuation_request_days_before" in error for error in detail)
    assert any("anchor" in error for error in detail)

    assert (await session.scalar(select(TenantConfig))) is None
    assert client.get(URL).json()["updated_at"] is None


async def test_a_reply_offset_past_the_statutory_window_is_refused_at_put_time(
    session: AsyncSession,
) -> None:
    """The bound is the point: a configuration whose reply-by would leave the
    client less than their minimum window before CR's deadline is refused when
    it is written, not left for the generator to refuse at runtime."""
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    response = client.put(
        URL,
        json={
            "naming_templates": {"document": CUSTOM_DOCUMENT, "generated": CUSTOM_GENERATED},
            "milestones": {
                **CUSTOM_MILESTONES,
                "client_reply_days_before": 31,  # the bound is 42 - 12 = 30
            },
        },
    )

    assert response.status_code == 422
    assert any(
        "client_reply_days_before" in error and "30" in error
        for error in response.json()["detail"]
    )
    assert (await session.scalar(select(TenantConfig))) is None


async def test_an_existing_row_is_untouched_by_a_refused_put(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    stored = put_config(client)

    response = client.put(
        URL,
        json={
            "naming_templates": {"document": CUSTOM_DOCUMENT, "generated": CUSTOM_GENERATED},
            "milestones": {**CUSTOM_MILESTONES, "reminder_cadence_days": [5, 3]},
        },
    )
    assert response.status_code == 422
    # The stored configuration is exactly what it was.
    assert client.get(URL).json()["milestones"] == stored["milestones"]


async def test_cross_tenant_isolation(session: AsyncSession) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    put_config(admin_client(session, tenant_a, admin_a))

    # Tenant B is unaffected and still reads the shipped defaults.
    body_b = admin_client(session, tenant_b, admin_b).get(URL).json()
    assert body_b["naming_templates"]["document"] == DEFAULT_DOCUMENT_TEMPLATE
    assert body_b["milestones"] == default_milestones().as_dict()
    assert body_b["updated_at"] is None


# --- The audit entries ---


async def test_each_block_leaves_its_own_audit_entry(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)

    put_config(client)

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
            )
        ).all()
    )
    assert [entry.action for entry in entries] == [
        "naming_templates_updated",
        "tenant_milestones_updated",
    ]
    milestones_entry = entries[1]
    assert milestones_entry.actor_user_id == admin.id
    # Before is None: no stored row was in effect, so the shipped defaults were
    # what the write replaced.
    assert milestones_entry.details["before"] is None
    assert milestones_entry.details["after"] == CUSTOM_MILESTONES


async def test_a_second_put_records_what_it_replaced(session: AsyncSession) -> None:
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    put_config(client)

    updated = {**CUSTOM_MILESTONES, "client_reply_days_before": 25}
    put_config(client, milestones=updated)

    entries = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == "tenant_milestones_updated",
                )
                .order_by(AuditLog.seq)
            )
        ).all()
    )
    assert len(entries) == 2
    assert entries[1].details["before"] == CUSTOM_MILESTONES
    assert entries[1].details["after"] == updated


# --- The migration's backfill ---


async def test_a_row_written_before_the_milestone_block_reads_the_defaults(
    session: AsyncSession,
) -> None:
    """The migration's guarantee, exercised: a ``tenant_config`` row written the
    pre-``0019`` way (naming templates only) reads the shipped milestone block.

    That is what the column's NOT NULL server default buys — the alter
    backfilled the rows that existed and keeps filling any row that omits the
    block — and why the read path has no NULL branch to fall back on.
    """
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    await session.execute(
        text(
            "INSERT INTO tenant_config (tenant_id, naming_templates) "
            "VALUES (:tenant_id, CAST(:templates AS JSON))"
        ).bindparams(
            tenant_id=tenant.id,
            templates=json.dumps(
                {"document": DEFAULT_DOCUMENT_TEMPLATE, "generated": DEFAULT_GENERATED_TEMPLATE}
            ),
        )
    )

    body = client.get(URL).json()

    assert body["milestones"] == default_milestones().as_dict()
    assert body["updated_at"] is not None


# --- Service-level guarantees ---


async def test_service_rejects_invalid_templates_without_writing(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    with pytest.raises(TemplateValidationError):
        await tenant_config.put_tenant_config(
            session,
            tenant_id=tenant.id,
            actor_user_id=None,
            document="{Bogus}",
            generated="{AlsoBogus}",
            milestones=default_milestones().as_dict(),
        )
    assert (await session.scalar(select(TenantConfig))) is None


async def test_service_rejects_invalid_milestones_without_writing(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    with pytest.raises(MilestoneValidationError):
        await tenant_config.put_tenant_config(
            session,
            tenant_id=tenant.id,
            actor_user_id=None,
            document=DEFAULT_DOCUMENT_TEMPLATE,
            generated=DEFAULT_GENERATED_TEMPLATE,
            milestones={**CUSTOM_MILESTONES, "reminder_cadence_days": "3, 7, 10"},
        )
    assert (await session.scalar(select(TenantConfig))) is None


async def test_corrupted_stored_row_fails_closed_on_read(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    # A hand-edited row bypassing the validated write path is rejected on
    # read rather than handed to the document store.
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={"document": "{Bogus}", "generated": "{Nope}"},
            milestones=default_milestones().as_dict(),
        )
    )
    await session.flush()
    with pytest.raises(TemplateValidationError):
        await tenant_config.get_naming_templates(session, tenant_id=tenant.id)


async def test_structurally_corrupted_row_fails_closed_on_read(session: AsyncSession) -> None:
    tenant, _ = await make_admin(session)
    # Missing keys fail as a clean validation error, not a bare KeyError.
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={"doc": "x"},
            milestones=default_milestones().as_dict(),
        )
    )
    await session.flush()
    with pytest.raises(TemplateValidationError):
        await tenant_config.get_naming_templates(session, tenant_id=tenant.id)


async def test_a_corrupted_milestone_block_fails_closed_on_the_narrow_read(
    session: AsyncSession,
) -> None:
    """Same discipline as the templates: the read the panel and the confirmation
    trigger derive dates from refuses a hand-edited block with every field it
    failed to read, never handing it on as arithmetic on a string."""
    tenant, _ = await make_admin(session)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones={"continuation_request_days_before": "sixty"},
        )
    )
    await session.flush()

    with pytest.raises(MilestoneValidationError) as caught:
        await tenant_config.get_milestones(session, tenant_id=tenant.id)
    assert len(caught.value.errors) == 7
    # The narrow read the document store uses is unaffected: a bad milestone
    # block must not stop a document from being keyed.
    assert (
        await tenant_config.get_naming_templates(session, tenant_id=tenant.id)
    ).naming_templates.document == DEFAULT_DOCUMENT_TEMPLATE


async def test_the_configuration_read_names_an_unreadable_milestone_block(
    session: AsyncSession,
) -> None:
    """The configuration screen's read answers, and says what it could not read.

    It is the read a full-replace `PUT` is assembled from, so it must not fail
    for the row the `PUT` repairs — but it must not paper the problem over
    either: the block's value in the response is the **shipped default**, the
    response names the block, and the violations are the specification the
    replacement has to satisfy. `updated_at` stays the stored row's, because
    the row exists.
    """
    tenant, _ = await make_admin(session)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones={"continuation_request_days_before": "sixty"},
        )
    )
    await session.flush()

    view = await tenant_config.get_tenant_config(session, tenant_id=tenant.id)

    assert view.milestones == default_milestones()
    assert view.naming_templates.document == DEFAULT_DOCUMENT_TEMPLATE
    assert view.updated_at is not None
    assert [problem.block.value for problem in view.unreadable_blocks] == ["milestones"]
    violations = view.unreadable_blocks[0].violations
    assert len(violations) == 7
    assert all(violation.startswith("milestones.") for violation in violations)


async def test_a_good_block_is_reported_readable(session: AsyncSession) -> None:
    """The other half of the field's contract: a stored row that validates
    carries nothing unreadable, so a client can read the field as the answer
    rather than as a warning it learns to ignore."""
    tenant, admin = await make_admin(session)
    put_config(admin_client(session, tenant, admin))

    view = await tenant_config.get_tenant_config(session, tenant_id=tenant.id)

    assert view.unreadable_blocks == ()
    assert view.milestones == validate_milestones(CUSTOM_MILESTONES)


async def test_a_corrupted_template_block_is_named_without_stopping_the_milestone_read(
    session: AsyncSession,
) -> None:
    """The two blocks fail independently, which is what keeps one bad block from
    taking the other's readers down: the document store's narrow read still
    refuses the templates (an invalid template must not key an object), while
    the milestone block the panel derives from is untouched — and the
    configuration read names the templates block alone."""
    tenant, _ = await make_admin(session)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={"document": "{Bogus}", "generated": "{Nope}"},
            milestones=validate_milestones(CUSTOM_MILESTONES).as_dict(),
        )
    )
    await session.flush()

    with pytest.raises(TemplateValidationError):
        await tenant_config.get_naming_templates(session, tenant_id=tenant.id)

    assert await tenant_config.get_milestones(session, tenant_id=tenant.id) == (
        validate_milestones(CUSTOM_MILESTONES)
    )

    view = await tenant_config.get_tenant_config(session, tenant_id=tenant.id)
    assert [problem.block.value for problem in view.unreadable_blocks] == ["naming_templates"]
    assert view.naming_templates.document == DEFAULT_DOCUMENT_TEMPLATE
    assert view.milestones == validate_milestones(CUSTOM_MILESTONES)


async def test_a_put_omitting_send_modes_tolerates_an_unreadable_stored_block(
    session: AsyncSession,
) -> None:
    """The keep-branch tolerance (follow-up (2), closed 2026-10-05): a PUT that
    omits ``send_modes`` and meets an unreadable stored block answers the way
    ``GET`` does — the shipped defaults in the response plus an
    ``unreadable_blocks`` entry naming the block — instead of raising the
    block's validation error (the route's 422-as-request-error), so a client
    built before the block existed can repair the row around it by omission.
    The stored block itself stays untouched: only a request that **carries**
    a valid block replaces it.
    """
    tenant, admin = await make_admin(session)
    client = admin_client(session, tenant, admin)
    corrupted = {"client": "bogus_mode", "government": "review_required"}
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones=default_milestones().as_dict(),
            send_modes=dict(corrupted),
        )
    )
    await session.flush()

    response = client.put(
        URL,
        json={
            "naming_templates": {"document": CUSTOM_DOCUMENT, "generated": CUSTOM_GENERATED},
            "milestones": dict(CUSTOM_MILESTONES),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["send_modes"] == default_send_modes().as_dict()
    assert [entry["block"] for entry in body["unreadable_blocks"]] == ["send_modes"]
    assert body["unreadable_blocks"][0]["violations"]
    # The omission repaired around the block, not by resetting it.
    row = await session.scalar(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))
    assert row is not None
    assert row.send_modes == corrupted
