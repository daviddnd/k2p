"""DB-backed scenarios for the AI gateway (M6.4; needs a migrated PostgreSQL).

The orchestration end to end against a real database: the real company-row
read, the real entitlement read (a cached Standard lease), the real hash-
chained audit append on the same session, and the custody search — the raw
officer name planted in the caller's content appears in **no** audit entry,
log line, or returned value, while the composed-snippet marker appears in
exactly the one ``llm_call`` (inside the Q1.5 digest summary, which describes
the scrubbed and tokenized outbound bytes). Also: the pre-flight
short-circuit (``ai_disabled``) writes nothing and calls
nothing, and an attempted-but-refused transport still books the audit entry
with null token counts and the bounded error code.

The transport is the module seam (``ai_gateway.llm_client``), replaced with
an in-memory fake — no suite talks to a provider. Entitlement facts come
from a planted activation credential and a cached Standard lease, so the
pre-flight legs that read them are exercised for real.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the
review database stays clean. Test data is synthetic throughout.
"""

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.llm.control_plane_channel import InferenceChannelRefused
from app.adapters.llm.port import TransportResult
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Company, Tenant, User
from app.application import ai_gateway, risk_acknowledgment
from app.core.config import settings
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.ai_gateway import TaskType

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

GATE = "enterprise_dpa"
BRN = "22334455"
# The raw-PII marker IS the officer's real name: natural-person data the
# caller hands over raw and the gateway must tokenize before the wire — the
# thing the custody search hunts for outside the restored result.
OFFICER = "RAWPIINAME8C4E Chan"
COMPOSED_MARKER = "MARKERCOMPOSED7F3D"
REPLY_TEXT = f"{OFFICER} confirmed the {COMPOSED_MARKER} relocation."

ENVELOPE = {
    "protocol_version": 1,
    "provider": "stub-provider",
    "task_type": "reply_parsing",
    "model_id": "stub-model",
    "prompt_name": "reply_parser",
    "prompt_version": 1,
    "result": {"events": [{"officer": "[PERSON_01]", "note": "resigned"}]},
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "completed_at": "2026-09-30T10:00:00+00:00",
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


class FakeTransport:
    """The replaced port: records the outbound snippet, then answers/raises."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.snippets: list[str] = []
        self.error = error

    async def complete(self, *, task_type: str, language: str, snippet: str) -> TransportResult:
        self.snippets.append(snippet)
        if self.error is not None:
            raise self.error
        return TransportResult(envelope=dict(ENVELOPE))


async def seed(
    session: AsyncSession,
    *,
    ai_disabled: bool = False,
    activation_credential_file: object,
) -> tuple[Tenant, Company]:
    """A tenant and company, plus the deployment facts the pre-flight reads:
    a Standard lease cached, an activation credential in the per-test
    credential file (so the entitlement source is the lease, for real), and
    the operator's acknowledgment of the risk statement recorded through the
    real acceptance seam (the 2026-10-04 rulings: the scenarios below pin the
    layers downstream of it)."""
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    operator = User(
        tenant_id=tenant.id,
        email=f"operator-{uuid4()}@example.com",
        display_name="Operator",
        role="administrator",
    )
    session.add(operator)
    await session.flush()
    await risk_acknowledgment.accept(
        session, tenant_id=tenant.id, actor_user_id=operator.id
    )
    company = Company(
        tenant_id=tenant.id,
        br_number=BRN,
        cr_number="1234567",
        date_of_incorporation="2010-03-01",
        anniversary_date="2027-03-01",
        type="private",
        ai_disabled=ai_disabled,
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
    return tenant, company


async def audit_rows(session: AsyncSession, tenant: Tenant) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return list(rows.all())


async def call_infer(
    session: AsyncSession, *, tenant: Tenant, company: Company
) -> object:
    return await ai_gateway.infer(
        session,
        tenant_id=tenant.id,
        company_id=company.id,
        task_type=TaskType.REPLY_PARSING,
        language="en",
        content={
            "officer_name": OFFICER,
            "reply_text": REPLY_TEXT,
            "role_reference": "director",
        },
    )


async def test_allowed_call_end_to_end_with_the_custody_search(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    activation_credential_file: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", GATE)
    transport = FakeTransport()
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)
    tenant, company = await seed(
        session, activation_credential_file=activation_credential_file
    )

    with caplog.at_level(logging.INFO):
        result = await call_infer(session, tenant=tenant, company=company)

    # The caller's content carried raw client data; the returned result has
    # the locally restored name and nothing else leaked back.
    assert result.result == {"events": [{"officer": OFFICER, "note": "resigned"}]}
    assert result.provider == "stub-provider"
    assert result.model_id == "stub-model"
    assert result.prompt_name == "reply_parser"
    assert result.prompt_version == 1
    (outbound,) = transport.snippets
    assert OFFICER not in outbound
    assert "[PERSON_01]" in outbound

    # The seed's operator acknowledgment, then exactly one audit entry for
    # the call, hash-chained on the same session.
    rows = await audit_rows(session, tenant)
    assert [row.action for row in rows] == ["risk_acknowledgment_accepted", "llm_call"]
    (entry,) = [row for row in rows if row.action == "llm_call"]
    assert entry.company_id == company.id
    assert set(entry.llm_call) == {
        "task_type",
        "provider",
        "model_id",
        "prompt_tokens",
        "completion_tokens",
        "snippet_summary",
        "snippet_sha256",
    }
    assert entry.llm_call["task_type"] == "reply_parsing"
    assert entry.llm_call["provider"] == "stub-provider"
    assert entry.llm_call["prompt_tokens"] == 12
    # The digest binds the exact bytes that left.
    assert entry.llm_call["snippet_sha256"] == hashlib.sha256(
        outbound.encode("utf-8")
    ).hexdigest()
    assert COMPOSED_MARKER in entry.llm_call["snippet_summary"]

    # The custody search: the raw-PII marker and the officer's real name
    # appear in no stored field, in no log line, and in no part of the
    # response. The composed marker appears exactly once, in the one
    # llm_call's summary.
    stored = " ".join(
        str(piece)
        for row in rows
        for piece in (row.details, row.llm_call, row.action, row.entity_type)
    )
    assert OFFICER not in stored
    assert OFFICER not in stored
    assert REPLY_TEXT not in stored
    assert OFFICER not in caplog.text
    assert OFFICER not in caplog.text
    assert stored.count(COMPOSED_MARKER) == 1


async def test_ai_disabled_short_circuit_writes_nothing_and_calls_nothing(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    activation_credential_file: object,
) -> None:
    monkeypatch.setattr(settings, "ai_q13_gate", GATE)
    transport = FakeTransport()
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)
    tenant, company = await seed(
        session, ai_disabled=True, activation_credential_file=activation_credential_file
    )

    with pytest.raises(ai_gateway.InferenceRefused) as refused:
        await call_infer(session, tenant=tenant, company=company)
    assert refused.value.reason == "ai_disabled"
    assert transport.snippets == []
    # The seeded acknowledgment stands; the refused call wrote nothing.
    assert [row.action for row in await audit_rows(session, tenant)] == [
        "risk_acknowledgment_accepted"
    ]


async def test_refused_transport_still_audits_with_null_counts(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    activation_credential_file: object,
) -> None:
    """The snippet left: the attempt is a compliance fact even when the
    provider refuses it — null token counts, the bounded code, the digest
    of the exact bytes that left, and none of the raw content."""
    monkeypatch.setattr(settings, "ai_q13_gate", GATE)
    transport = FakeTransport(error=InferenceChannelRefused("entitlement_denied"))
    monkeypatch.setattr(ai_gateway, "llm_client", lambda decision: transport)
    tenant, company = await seed(
        session, activation_credential_file=activation_credential_file
    )

    with pytest.raises(ai_gateway.InferenceUnavailable) as unavailable:
        await call_infer(session, tenant=tenant, company=company)
    assert unavailable.value.code == "entitlement_denied"

    rows = await audit_rows(session, tenant)
    assert [row.action for row in rows] == ["risk_acknowledgment_accepted", "llm_call"]
    (entry,) = [row for row in rows if row.action == "llm_call"]
    assert entry.details == {"error": "entitlement_denied"}
    assert entry.llm_call["prompt_tokens"] is None
    assert entry.llm_call["completion_tokens"] is None
    assert entry.llm_call["provider"] is None
    (outbound,) = transport.snippets
    assert entry.llm_call["snippet_sha256"] == hashlib.sha256(
        outbound.encode("utf-8")
    ).hexdigest()
    stored = str(entry.llm_call) + str(entry.details)
    assert OFFICER not in stored
    assert OFFICER not in stored
