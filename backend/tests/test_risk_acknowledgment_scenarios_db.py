"""The pre-use risk acknowledgment's walk (opt-in; needs a migrated PostgreSQL).

The slice's acceptance evidence for the operator-consent layer, driven through
the real route stack and the real gateway pre-flight against the real
database, one walk per step:

1. **a fresh, fully-provisioned deployment refuses `ai_acknowledgment_missing`.**
   The deployment's Q1.3 gate is set (``residual_risk_accepted``, the 2026-10-04
   election), the tenant holds a cached Standard lease, the activation
   credential is in the per-test credential file — every pre-flight leg
   downstream of the operator layer stands, so the refusal is provably the
   acknowledgment's, not the gate's. The refusal writes nothing: no audit
   entry, no transport.
2. **the Administrator accepts through the real route.** ``POST
   /api/v1/risk-acknowledgment`` answers 200 with ``acknowledged: true``; the
   hash chain verifies; exactly one ``risk_acknowledgment_accepted`` entry
   landed, its payload the served statement version, its actor the accepting
   Administrator.
3. **the same call now passes.** With the acknowledgment recorded, the
   identical call proceeds through the real ``infer``: the transport seam
   (``ai_gateway.llm_client``) is the one thing replaced — an in-memory
   fake, the family's stubbed-transport convention; everything else (compose,
   tokenize, the audit append) is the real pipeline. The snippet left, the
   result restored locally, and exactly one ``llm_call`` entry rides the
   chain after the acceptance.
4. **a statement version bump re-opens by construction.** A test override of
   the served version makes the GET read ``acknowledged: false`` and the call
   refuse ``ai_acknowledgment_missing`` again — until an Administrator
   confirms the revised statement through the real route. The chain then
   holds **both** versions' acceptance entries in ``seq`` order (append-only:
   the bump stopped the old entry from counting, it never rewrote it), and
   the call passes again.

**The harness** mirrors ``tests/test_ai_gateway_scenarios_db.py`` (the rolled-
back session, the seeded tenant/Administrator/company, the cached Standard
lease and activation credential, the transport fake) and
``tests/test_confirmation_discovery_scenarios_db.py`` (the real app behind
dependency-overridden auth and session) rather than inventing a new one — but
it is restated here, not imported, because this family's convention is that
no test module imports another's fixtures or helpers.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.llm.port import TransportResult
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Company, Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import ai_gateway, risk_acknowledgment
from app.core.config import settings
from app.domain import risk_acknowledgment as statement
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.ai_gateway import TaskType
from app.domain.audit import AuditAction, verify_chain
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

ACKNOWLEDGMENT_URL = "/api/v1/risk-acknowledgment"

# The 2026-10-04 Q1.3 election (counsel's written residual-risk acceptance) —
# set in config for the whole walk so the new refusal is provably the
# acknowledgment's, not the gate's.
GATE = "residual_risk_accepted"
BRN = "22334455"
OFFICER = "RAWPIINAME8C4E Chan"
REPLY_TEXT = f"{OFFICER} confirmed the MARKERCOMPOSED7F3D relocation."

ENVELOPE = {
    "protocol_version": 1,
    "provider": "stub-provider",
    "task_type": "reply_parsing",
    "model_id": "stub-model",
    "prompt_name": "reply_parser",
    "prompt_version": 1,
    "result": {"events": [{"officer": "[PERSON_01]", "note": "resigned"}]},
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "completed_at": "2026-10-04T10:00:00+00:00",
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


@pytest.fixture(autouse=True)
def gated_standard_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Q1.3 gate resolved in config — the walk's fixed deployment fact."""
    monkeypatch.setattr(settings, "ai_q13_gate", GATE)


@pytest.fixture
async def actor(session: AsyncSession) -> ActorRef:
    """A fresh tenant and its Administrator (the acceptance's actor)."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"admin-{uuid4()}@example.com",
        display_name="Administrator",
        role="administrator",
    )
    session.add(admin)
    await session.flush()
    return ActorRef(user_id=admin.id, tenant_id=tenant.id)


class FakeTransport:
    """The replaced port: records the outbound snippet, then answers."""

    def __init__(self) -> None:
        self.snippets: list[str] = []

    async def complete(self, *, task_type: str, language: str, snippet: str) -> TransportResult:
        self.snippets.append(snippet)
        return TransportResult(envelope=dict(ENVELOPE))


def make_client(session: AsyncSession, actor: ActorRef) -> TestClient:
    """The real app with the auth seam and session overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


async def seed_company(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    activation_credential_file: object,
) -> Company:
    """The company plus the deployment facts the pre-flight reads: the
    activation credential in the per-test credential file and a cached
    Standard lease (so the entitlement source is the lease, for real). The
    operator's acknowledgment is deliberately absent — the walk's subject."""
    company = Company(
        tenant_id=tenant_id,
        br_number=BRN,
        cr_number="1234567",
        date_of_incorporation="2010-03-01",
        anniversary_date="2027-03-01",
        type="private",
        ai_disabled=False,
    )
    session.add(company)
    now = datetime.now(UTC)
    credential = ActivationCredential(
        token="activation-token",
        token_id=uuid4(),
        control_plane_tenant_id=uuid4(),
        expires_at=now + timedelta(days=1),
    )
    ActivationStore(activation_credential_file).save(ActivationState(credential=credential))
    session.add(
        LeaseCache(
            control_plane_tenant_id=credential.control_plane_tenant_id,
            tier="standard",
            license_expires_at=now + timedelta(days=30),
            entitlement_version=1,
            fetched_at=now,
            last_attempt_at=now,
        )
    )
    await session.flush()
    return company


async def audit_rows(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
    )
    return list(rows.all())


async def acceptance_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED.value,
        )
        .order_by(AuditLog.seq)
    )
    return list(rows.all())


async def call_infer(session: AsyncSession, *, tenant_id: UUID, company_id: UUID) -> object:
    return await ai_gateway.infer(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        task_type=TaskType.REPLY_PARSING,
        language="en",
        content={
            "officer_name": OFFICER,
            "reply_text": REPLY_TEXT,
            "role_reference": "director",
        },
    )


# --- 1. a fresh, fully-provisioned deployment refuses the new reason --------


async def test_a_fresh_deployment_refuses_ai_acknowledgment_missing(
    session: AsyncSession,
    actor: ActorRef,
    activation_credential_file: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = await seed_company(
        session,
        tenant_id=actor.tenant_id,
        activation_credential_file=activation_credential_file,
    )
    transport = FakeTransport()
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)

    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await call_infer(session, tenant_id=actor.tenant_id, company_id=company.id)
    # The gate is set, the lease and credential stand — the acknowledgment is
    # the only missing leg, so the reason is provably the right one.
    assert refused.value.reason == "ai_acknowledgment_missing"
    assert transport.snippets == []
    # A pre-flight refusal writes nothing (ruling R5).
    assert await audit_rows(session, actor.tenant_id) == []


# --- 2. the Administrator accepts through the real route --------------------


async def test_the_administrator_accepts_through_the_real_route(
    session: AsyncSession, actor: ActorRef
) -> None:
    client = make_client(session, actor)
    response = client.post(ACKNOWLEDGMENT_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["statement_version"] == statement.STATEMENT_VERSION
    assert body["acknowledged"] is True

    # Exactly one acceptance entry: the payload the served version, the actor
    # the Administrator, hash-chained on the real database.
    entries = await acceptance_entries(session, actor.tenant_id)
    assert len(entries) == 1
    (entry,) = entries
    assert entry.details == {"statement_version": statement.STATEMENT_VERSION}
    assert entry.actor_user_id == actor.user_id
    assert verify_chain(await audit_rows(session, actor.tenant_id)) == []

    # The read the interstitial relies on agrees with the write (ruling R2).
    assert client.get(ACKNOWLEDGMENT_URL).json()["acknowledged"] is True


# --- 3. the same call now passes ----------------------------------------------


async def test_after_acceptance_the_call_passes_the_pre_flight(
    session: AsyncSession,
    actor: ActorRef,
    activation_credential_file: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = await seed_company(
        session,
        tenant_id=actor.tenant_id,
        activation_credential_file=activation_credential_file,
    )
    transport = FakeTransport()
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)

    # The acceptance is seeded through the real application seam (the route
    # walk is step 2's subject); the transport seam is the one thing replaced.
    await risk_acknowledgment.accept(
        session, tenant_id=actor.tenant_id, actor_user_id=actor.user_id
    )

    result = await call_infer(session, tenant_id=actor.tenant_id, company_id=company.id)
    assert result.result == {"events": [{"officer": OFFICER, "note": "resigned"}]}

    # The call proceeded: the snippet left, tokenized, and the chain carries
    # the acceptance then exactly one llm_call.
    (outbound,) = transport.snippets
    assert OFFICER not in outbound
    assert "[PERSON_01]" in outbound
    rows = await audit_rows(session, actor.tenant_id)
    assert [row.action for row in rows] == [
        AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED.value,
        AuditAction.LLM_CALL.value,
    ]
    assert verify_chain(rows) == []


# --- 4. a statement version bump re-opens until re-accepted -----------------


async def test_a_statement_version_bump_reopens_until_reaccepted(
    session: AsyncSession,
    actor: ActorRef,
    activation_credential_file: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = await seed_company(
        session,
        tenant_id=actor.tenant_id,
        activation_credential_file=activation_credential_file,
    )
    transport = FakeTransport()
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)
    client = make_client(session, actor)

    assert client.post(ACKNOWLEDGMENT_URL).status_code == 200
    await call_infer(session, tenant_id=actor.tenant_id, company_id=company.id)

    # The wording changes (a test override stands in for the copy edit): the
    # GET re-opens, the call refuses the acknowledgment again.
    monkeypatch.setattr(statement, "STATEMENT_VERSION", "v4-test")
    body = client.get(ACKNOWLEDGMENT_URL).json()
    assert body["statement_version"] == "v4-test"
    assert body["acknowledged"] is False
    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await call_infer(session, tenant_id=actor.tenant_id, company_id=company.id)
    assert refused.value.reason == "ai_acknowledgment_missing"

    # Re-acceptance through the real route restores the pass; the chain holds
    # both versions' entries in seq order — append-only, never rewritten.
    assert client.post(ACKNOWLEDGMENT_URL).status_code == 200
    entries = await acceptance_entries(session, actor.tenant_id)
    assert [entry.details["statement_version"] for entry in entries] == ["v3", "v4-test"]
    assert [entry.actor_user_id for entry in entries] == [actor.user_id, actor.user_id]
    assert verify_chain(await audit_rows(session, actor.tenant_id)) == []

    await call_infer(session, tenant_id=actor.tenant_id, company_id=company.id)
    assert len(transport.snippets) == 2
