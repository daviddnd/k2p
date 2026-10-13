"""The M6 exit gate: one inference call per task type, end to end, zero PII.

The slice's last proof is the real channel, both deployables, over a real
socket — nothing in this suite is replaced with an in-process fake:

- the vendor service is the real ``control_plane`` application, served by a
  subprocess on a loopback socket (``tests/control_plane_server.py``; both
  deployables answer to the package name ``app``, so the side that is not
  under test is the one that leaves the process);
- its vendor-held LLM provider is environment-pointed at a hand-rolled
  OpenAI-compatible stub HTTP server this test spins up on loopback (stdlib
  only — the provider's loopback carve-out exists for exactly this);
- the prompt templates each task binds to are authored through the vendor's
  own registry path (``POST /prompts``, then the one publication act,
  ``POST /prompt-packs``) with the vendor-admin credential, so the served
  call resolves a published, non-empty template the way a paying tenant's
  would;
- the deployment side is the real gateway orchestration — the real
  compose → scrub → tokenize → transport → restore pipeline, the real
  hash-chained ``llm_call`` audit append on the same session, and the real
  activation credential from the per-test credential file, which is also the
  credential the vendor's activation-token authentication checks; and
- the vendor's tenant, entitlement and activation token are seeded into its
  database directly (the activation handshake itself is the command-channel
  suite's subject, and this suite's CP rows are removed again at the end).

What the round trip proves, mapped to the assertions below:

1. one call per anchored task type (``reply_parsing``, ``confirmation_request``,
   ``document_extraction``, ``ordinance_qa``) through the real ``infer`` —
   the split (``split``) is company-less by design and rides the same
   channel through ``infer_split``; its own round trip is the second test
   below;
2. the stub provider received the composed snippet exactly as scrubbed and
   tokenized — the audit entry's ``snippet_sha256`` binds the exact bytes
   the stub saw, and the reply-parsing call crossed the wire with
   ``[PERSON_01]`` where the officer's name was;
3. the response flowed back and restored locally (the reply-parsing result
   carries the officer's real name again, and nothing else does);
4. exactly one ``llm_call`` audit entry per call, carrying exactly the
   closed Q1.5 seven-key shape;
5. the custody sweep: the planted raw-PII marker (the officer's name, and
   the raw reply sentence) appears in **no** control-plane table, in **no**
   execution-plane audit field, and in **no** captured log on either side —
   and neither does the activation token's plaintext nor the provider key;
   the vendor's metering rows match the calls one for one, all succeeded;
6. skip behaviour matches the command-channel suite: without both migrated
   test databases the suite skips with a documented reason, never errors.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a migrated PostgreSQL
and the control-plane database is reachable beside it (see
``tests/control_plane_integration.py``). The deployment's rows roll back
with the test transaction; the vendor service commits for real (it is a
separate process), so its rows are removed again at the end.
"""

import hashlib
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Company, Tenant, User
from app.application import ai_gateway, risk_acknowledgment
from app.core.config import settings
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.ai_gateway import TaskType
from tests.vendor_harness import VendorService, delete_vendor_rows, vendor_headers

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

# The one credential this test hands the vendor service — through the
# environment, so no secret is printed or parsed out of a subprocess.
VENDOR_ADMIN_KEY = "inference-channel-vendor-key"

GATE = "enterprise_dpa"
BRN = "22334455"
LANGUAGE = "en"
# The raw-PII marker IS the officer's real name: natural-person data handed
# over raw, which the reply-parsing profile must tokenize before the wire —
# the thing the custody sweep hunts for outside the restored result.
OFFICER = "RAWPIINAME8C4E Chan"
REPLY_TEXT = f"{OFFICER} confirmed the MARKERCOMPOSED7F3D relocation."
ACTIVATION_TOKEN = f"m67-activation-token-{uuid4()}"

# The vendor-held provider configuration, environment-pointed at the stub.
STUB_PROVIDER_ID = "m67-stub-provider"
STUB_MODEL_ID = "m67-stub-model"
STUB_API_KEY = "sk-m67-stub-key-that-must-stay-secret"

# The task → prompt-binding table, pinned product data on both deployables
# (the control plane's ``app.domain.inference.TASK_PROMPT_NAMES`` — keyed by
# task-type value on the vendor side, by the enum member here; a twin test
# pins the two against each other, ``test_ai_gateway_domain.py``).
TASK_PROMPT_NAMES = {
    TaskType.REPLY_PARSING: "reply_parser",
    TaskType.CONFIRMATION_REQUEST: "confirmation_request_drafter",
    TaskType.DOCUMENT_EXTRACTION: "document_extractor",
    TaskType.ORDINANCE_QA: "ordinance_qa",
    TaskType.SPLIT: "content_splitter",
}

# One column per task type: the caller content, the exact bytes the stub
# must see (composed, scrubbed, tokenized — verified empirically against the
# deterministic scrubber before this suite was written), and the restored
# result the caller must get back. The confirmation-request address is
# scrubbed to ``[ADDRESS_01]`` on the wire: the suite asserts scrubbing
# end to end, not just tokenization.
TASK_CALLS: tuple[tuple[TaskType, Any, str, dict[str, Any], str], ...] = (
    (
        TaskType.REPLY_PARSING,
        {
            "officer_name": OFFICER,
            "reply_text": REPLY_TEXT,
            "role_reference": "director",
        },
        "company: 22334455\n"
        "officer_name: [PERSON_01]\n"
        "reply_text: [PERSON_01] confirmed the MARKERCOMPOSED7F3D relocation.\n"
        "role_reference: director",
        {"events": [{"officer": "[PERSON_01]", "note": "resigned"}]},
        "MARKERCOMPOSED7F3D",
    ),
    (
        TaskType.CONFIRMATION_REQUEST,
        {
            "ambiguous_field": "registered_office",
            "current_value": "22/F, Acme Tower, 1 Dummy Street, Central",
            "field_label": "Registered office",
            "named_person": "Jordan Tester",
        },
        "company: 22334455\n"
        "ambiguous_field: registered_office\n"
        "current_value: 22/F, Acme Tower, [ADDRESS_01]\n"
        "field_label: Registered office\n"
        "named_person: Jordan Tester",
        {
            "draft_question": "Please confirm the registered office remains unchanged.",
            "target_field": "registered_office",
        },
        "Acme Tower",
    ),
    (
        TaskType.DOCUMENT_EXTRACTION,
        {
            "document_text": "NAR1 part: director MARKERDOC9A1B Kwok Wai Kit resigned.",
            "field_region": "page 2 box B",
            "officer_name_as_filed": "Kwok Wai Kit",
        },
        "company: 22334455\n"
        "document_text: NAR1 part: director MARKERDOC9A1B Kwok Wai Kit resigned.\n"
        "field_region: page 2 box B\n"
        "officer_name_as_filed: Kwok Wai Kit",
        {"fields": {"officer_name_as_filed": "Kwok Wai Kit"}, "region": "page 2 box B"},
        "MARKERDOC9A1B",
    ),
    (
        TaskType.ORDINANCE_QA,
        "Does Cap. 622 s.622 require the MARKERQA5E annual return within 42 days?",
        "Does Cap. 622 s.622 require the MARKERQA5E annual return within 42 days?",
        {"answer": "Yes — within 42 days after the return date.", "citation": "Cap. 622 s.622"},
        "MARKERQA5E",
    ),
)

# The split's plant (workstream ③, ADR-0019 §3): ONE message answering two
# companies' confirmation requests. The company names and the officer name
# are this call's raw-PII needles — the wire must carry only tokens, and the
# gateway's answer must come back UNRESTORED (the caller restores each span
# locally, after the span grammar passes).
SPLIT_COMPANY_ALPHA = "SPLITALPHA Holdings Limited"
SPLIT_COMPANY_BETA = "SPLITBETA Trading Limited"
SPLIT_OFFICER = "SPLITOFFICER8C4E Chan"
SPLIT_CONTENT = (
    f"{SPLIT_COMPANY_ALPHA}: director {SPLIT_OFFICER} confirmed no change. "
    f"{SPLIT_COMPANY_BETA}: the registered office moved."
)
SPLIT_TOKENIZED = (
    "[COMPANY_01]: director [PERSON_01] confirmed no change. "
    "[COMPANY_02]: the registered office moved."
)
SPLIT_WIRE_TEXT = f"content: {SPLIT_TOKENIZED}"
SPLIT_SPANS: list[dict[str, str]] = [
    {
        "company": "[COMPANY_01]",
        "text": "[COMPANY_01]: director [PERSON_01] confirmed no change.",
    },
    {"company": "[COMPANY_02]", "text": "[COMPANY_02]: the registered office moved."},
]

# The stub's canned answers, keyed by the prompt binding the request's
# system message resolves to. Distinct token counts per task make the
# metering match meaningful. Only the reply-parser answer carries a
# placeholder — it is the only anchored profile that tokenizes names (the
# split's answer is spans over the split call's own tokens).
STUB_RESULTS: Mapping[str, tuple[Any, int, int]] = {
    "reply_parser": ({"events": [{"officer": "[PERSON_01]", "note": "resigned"}]}, 33, 9),
    "confirmation_request_drafter": (
        {
            "draft_question": "Please confirm the registered office remains unchanged.",
            "target_field": "registered_office",
        },
        41,
        12,
    ),
    "document_extractor": (
        {"fields": {"officer_name_as_filed": "Kwok Wai Kit"}, "region": "page 2 box B"},
        52,
        15,
    ),
    "ordinance_qa": (
        {"answer": "Yes — within 42 days after the return date.", "citation": "Cap. 622 s.622"},
        27,
        18,
    ),
    "content_splitter": (SPLIT_SPANS, 37, 14),
}


def prompt_body(name: str) -> str:
    """The registry body for one binding; the stub keys its answer on it."""
    return f"m6.7-suite binding {name}: answer with one JSON value."


# Every planted needle the custody sweep hunts for, and where each is
# allowed to appear: the officer's name and raw reply sentence only in the
# caller's content and the restored reply-parsing result; the activation
# token's plaintext only in the credential file; the provider key only in
# the one provider request's Authorization header.
CUSTODY_NEEDLES = (OFFICER, REPLY_TEXT, ACTIVATION_TOKEN, STUB_API_KEY)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after the test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


# --- The stub provider, on loopback ---------------------------------------


class _StubServer(ThreadingHTTPServer):
    """The recording half of the stub: what the provider seam must send."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _StubHandler)
        self.requests: list[dict[str, Any]] = []


class _StubHandler(BaseHTTPRequestHandler):
    """One OpenAI-compatible endpoint; answers from the canned result set."""

    def do_POST(self) -> None:
        server = cast("_StubServer", self.server)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            document = json.loads(self.rfile.read(length))
        except ValueError:
            document = None
        server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": document,
            }
        )
        if self.path != "/chat/completions" or not isinstance(document, dict):
            self._answer(404, {"error": {"code": "not_found"}})
            return
        messages = document.get("messages")
        system = messages[0].get("content") if isinstance(messages, list) else None
        name = str(system).removeprefix("m6.7-suite binding ").removesuffix(
            ": answer with one JSON value."
        )
        canned = STUB_RESULTS.get(name)
        if canned is None:
            self._answer(400, {"error": {"code": "unknown_binding"}})
            return
        result, prompt_tokens, completion_tokens = canned
        self._answer(
            200,
            {
                "id": "chatcmpl-m67-stub",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": document.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(result)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )

    def _answer(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Bodies are never logged — the request line is not either."""


class StubProvider:
    """The hand-rolled provider double: stdlib HTTP server on loopback."""

    def __init__(self) -> None:
        self._server = _StubServer(("127.0.0.1", 0))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "StubProvider":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests


# --- Seeding both sides ----------------------------------------------------


def seed_control_plane_tenant(database_url: str, *, tenant_id: UUID) -> None:
    """The vendor's record of this deployment: a tenant, its Standard
    entitlement, and the activation token's digest (the plaintext never
    reaches the vendor's database — ADR-0014)."""
    now = datetime.now(UTC)
    sync_engine = create_engine(database_url)
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenants (id, status, deployment_fingerprint)"
                    " VALUES (:id, 'active', :fingerprint)"
                ),
                {"id": tenant_id, "fingerprint": f"m67-{uuid4()}"},
            )
            connection.execute(
                text(
                    "INSERT INTO entitlements (tenant_id, tier, entitlement_version)"
                    " VALUES (:id, 'standard', 1)"
                ),
                {"id": tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO activation_tokens"
                    " (id, tenant_id, token_digest, issued_at, expires_at)"
                    " VALUES (:id, :tenant_id, :digest, :issued_at, :expires_at)"
                ),
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "digest": hashlib.sha256(ACTIVATION_TOKEN.encode("utf-8")).hexdigest(),
                    "issued_at": now,
                    "expires_at": now + timedelta(days=90),
                },
            )
    finally:
        sync_engine.dispose()


def publish_prompts(vendor: VendorService) -> tuple[list[str], int]:
    """Author the five task bindings through the vendor's own registry path:
    append each version, then the one publication act that marks the set
    published and cuts the ``prompt_pack`` release."""
    prompt_ids: list[str] = []
    for name in TASK_PROMPT_NAMES.values():
        created = httpx.post(
            f"{vendor.base_url}/api/v1/prompts",
            json={"name": name, "language": LANGUAGE, "body": prompt_body(name)},
            headers=vendor_headers(vendor),
            timeout=10.0,
        )
        assert created.status_code == 201, created.text
        prompt_ids.append(str(created.json()["id"]))
    published = httpx.post(
        f"{vendor.base_url}/api/v1/prompt-packs",
        json={"prompt_ids": prompt_ids, "published_by": "m67_suite"},
        headers=vendor_headers(vendor),
        timeout=10.0,
    )
    assert published.status_code == 201, published.text
    return prompt_ids, int(published.json()["version"])


async def seed_execution_plane(
    session: AsyncSession, activation_credential_file: Path, *, cp_tenant_id: UUID
) -> tuple[Tenant, Company]:
    """A tenant and company, plus the deployment facts the pre-flight reads:
    the activation credential (whose token the vendor's authentication
    checks), a freshly cached Standard lease, and the operator's
    acknowledgment of the risk statement recorded through the real acceptance
    seam (the 2026-10-04 rulings — this suite pins the channel downstream of
    that layer)."""
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
        ai_disabled=False,
    )
    session.add(company)
    now = datetime.now(UTC)
    ActivationStore(activation_credential_file).save(
        ActivationState(
            credential=ActivationCredential(
                token=ACTIVATION_TOKEN,
                token_id=uuid4(),
                control_plane_tenant_id=cp_tenant_id,
                expires_at=now + timedelta(days=1),
            )
        )
    )
    session.add(
        LeaseCache(
            control_plane_tenant_id=cp_tenant_id,
            tier="standard",
            license_expires_at=now + timedelta(days=30),
            entitlement_version=1,
            fetched_at=now,
            last_attempt_at=now,
        )
    )
    await session.flush()
    return tenant, company


# --- Reading and sweeping the vendor's own record --------------------------


def sweep_control_plane_tables(database_url: str, needle: str) -> int:
    """Read every row of every vendor table; assert the needle is in none.

    Returns the number of rows swept, so the assertion doubles as evidence
    the sweep actually read the database.
    """
    swept = 0
    sync_engine = create_engine(database_url)
    try:
        with sync_engine.connect() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            ).scalars()
            for table in tables:
                rows = connection.execute(text(f'SELECT * FROM "{table}"')).all()
                for row in rows:
                    swept += 1
                    assert needle not in str(tuple(row)), f"{needle!r} found in {table}"
        return swept
    finally:
        sync_engine.dispose()


def metering_rows(database_url: str, tenant_id: UUID) -> list[dict[str, Any]]:
    sync_engine = create_engine(database_url)
    try:
        with sync_engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT * FROM inference_metering WHERE tenant_id = :id"
                        " ORDER BY created_at, task_type"
                    ),
                    {"id": tenant_id},
                ).mappings()
            ]
    finally:
        sync_engine.dispose()


def remove_vendor_rows(
    database_url: str, *, tenant_id: UUID, prompt_ids: list[str], pack_version: int
) -> None:
    """Delete what this run created on the vendor side, children first.

    The control plane's own suites roll back; this one drives a separate
    process, so its commits are real. The rows are exactly the ones this
    run registered or authored. This cleanup is scoped by id (the tenant
    seeded directly), not by time, so it keeps its own statement list; the
    engine lifecycle is the harness's.
    """
    statements = [
        *(
            (f"DELETE FROM {table} WHERE tenant_id = :id", {"id": tenant_id})
            for table in ("inference_metering", "activation_tokens", "entitlements")
        ),
        ("DELETE FROM tenants WHERE id = :id", {"id": tenant_id}),
        *(("DELETE FROM prompts WHERE id = :id", {"id": prompt_id}) for prompt_id in prompt_ids),
        (
            "DELETE FROM pack_releases WHERE pack_type = 'prompt_pack' AND version = :version",
            {"version": pack_version},
        ),
    ]
    delete_vendor_rows(database_url, statements)


# --- The exit gate ----------------------------------------------------------


async def test_one_inference_call_per_task_type_round_trips_with_zero_pii(
    session: AsyncSession,
    control_plane_database: str,
    activation_credential_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cp_tenant_id = uuid4()
    prompt_ids: list[str] = []
    pack_version = 0
    seed_control_plane_tenant(control_plane_database, tenant_id=cp_tenant_id)
    try:
        with StubProvider() as stub, VendorService(
            control_plane_database,
            admin_key=VENDOR_ADMIN_KEY,
            extra_env={
                "AICS_CP_LLM_PROVIDER": STUB_PROVIDER_ID,
                "AICS_CP_LLM_BASE_URL": stub.base_url,
                "AICS_CP_LLM_API_KEY": STUB_API_KEY,
                "AICS_CP_LLM_MODEL": STUB_MODEL_ID,
            },
            capture_stderr=True,
        ) as vendor:
            prompt_ids, pack_version = publish_prompts(vendor)
            tenant, company = await seed_execution_plane(
                session, activation_credential_file, cp_tenant_id=cp_tenant_id
            )
            monkeypatch.setattr(settings, "ai_q13_gate", GATE)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)

            with caplog.at_level(logging.DEBUG):
                restored = [
                    (
                        await ai_gateway.infer(
                            session,
                            tenant_id=tenant.id,
                            company_id=company.id,
                            task_type=task,
                            language=LANGUAGE,
                            content=content,
                        )
                    ).result
                    for task, content, _, _, _ in TASK_CALLS
                ]

            # (1) The calls: one per task type, restored correctly. The
            # reply-parsing result carries the officer's real name again —
            # locally restored, and the only place it appears.
            expected_results = [
                {
                    "events": [{"officer": OFFICER, "note": "resigned"}],
                },
                TASK_CALLS[1][3],
                TASK_CALLS[2][3],
                TASK_CALLS[3][3],
            ]
            assert restored == expected_results

            # (2) The provider seam saw each composed snippet exactly as
            # scrubbed and tokenized: the user message is the expected
            # bytes, the key rode the Authorization header only, and the
            # audit entry's digest below binds the same bytes by hash.
            assert len(stub.requests) == len(TASK_CALLS)
            wire_by_task: dict[str, str] = {}
            for task, _, expected_outbound, _, _ in TASK_CALLS:
                (request,) = [
                    item
                    for item in stub.requests
                    if item["body"]["messages"][1]["content"] == expected_outbound
                ]
                assert request["path"] == "/chat/completions"
                assert request["authorization"] == f"Bearer {STUB_API_KEY}"
                assert request["body"]["model"] == STUB_MODEL_ID
                assert request["body"]["messages"][0]["content"] == prompt_body(
                    TASK_PROMPT_NAMES[task]
                )
                wire_by_task[task.value] = expected_outbound
            assert OFFICER not in wire_by_task[TaskType.REPLY_PARSING.value]
            assert "[PERSON_01]" in wire_by_task[TaskType.REPLY_PARSING.value]

            # (3) Exactly one llm_call audit entry per call, the closed
            # seven-key shape, the digest binding the exact bytes the stub
            # saw, and none of the raw content in any stored field. (The
            # chain's first entry is the seed's operator acknowledgment.)
            rows = (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == tenant.id)
                    .order_by(AuditLog.seq)
                )
            ).all()
            llm_calls = [row for row in rows if row.action == "llm_call"]
            assert len(llm_calls) == len(TASK_CALLS)
            for entry, (task, _, _, _, marker) in zip(llm_calls, TASK_CALLS, strict=True):
                assert entry.action == "llm_call"
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
                assert entry.llm_call["task_type"] == task.value
                assert entry.llm_call["provider"] == STUB_PROVIDER_ID
                assert entry.llm_call["model_id"] == STUB_MODEL_ID
                canned = STUB_RESULTS[TASK_PROMPT_NAMES[task]]
                assert entry.llm_call["prompt_tokens"] == canned[1]
                assert entry.llm_call["completion_tokens"] == canned[2]
                assert entry.llm_call["snippet_sha256"] == hashlib.sha256(
                    wire_by_task[task.value].encode("utf-8")
                ).hexdigest()
                assert marker in entry.llm_call["snippet_summary"]

            # (4) Metering on the vendor side matches the calls one for
            # one: four rows, all succeeded, each bound to the task's
            # prompt, language and the stub's token counts.
            metered = metering_rows(control_plane_database, cp_tenant_id)
            assert len(metered) == len(TASK_CALLS)
            by_task = {row["task_type"]: row for row in metered}
            assert set(by_task) == {task.value for task, *_ in TASK_CALLS}
            for task, _, _, _, _ in TASK_CALLS:
                row = by_task[task.value]
                canned = STUB_RESULTS[TASK_PROMPT_NAMES[task]]
                assert row["status"] == "succeeded"
                assert row["model_id"] == STUB_MODEL_ID
                assert row["prompt_name"] == TASK_PROMPT_NAMES[task]
                assert row["prompt_version"] == 1
                assert row["language"] == LANGUAGE
                assert row["prompt_tokens"] == canned[1]
                assert row["completion_tokens"] == canned[2]

            # (5) The custody sweep. The raw-PII plant, the raw reply
            # sentence, the activation token's plaintext and the provider
            # key survive nowhere: not in any vendor table, not in any
            # execution-plane audit field, not in any log either side
            # captured.
            stored = " ".join(
                str(piece)
                for row in rows
                for piece in (row.details, row.llm_call, row.action, row.entity_type)
            )
            for needle in CUSTODY_NEEDLES:
                assert needle not in stored
                assert needle not in caplog.text
                assert needle not in vendor.stderr_text()
            for needle in CUSTODY_NEEDLES:
                swept = sweep_control_plane_tables(control_plane_database, needle)
                assert swept > 0  # the sweep actually read the database
    finally:
        # Unconditional, mirroring the command-channel suite: the seeded
        # tenant rows exist from the moment the seed commits, whatever later
        # step failed. An empty prompt-id list no-ops and pack_version 0
        # matches nothing (release versions start at 1).
        remove_vendor_rows(
            control_plane_database,
            tenant_id=cp_tenant_id,
            prompt_ids=prompt_ids,
            pack_version=pack_version,
        )


async def test_the_split_call_round_trips_company_less_with_zero_pii(
    session: AsyncSession,
    control_plane_database: str,
    activation_credential_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fifth task type's real-channel proof (workstream ③, ADR-0019 §3):
    the split is company-less by design (ruling D2) — the wire carries one
    tokenized ``content`` field and NO anchor line; the system message
    resolves the ``content_splitter`` binding; the answer comes back
    UNRESTORED (the caller restores spans locally after the span grammar
    passes, so nothing re-identifies here); the audit entry is the split
    parent's own event with ``company_id`` NULL (ruling D10); and the
    vendor's metering row records ``task_type='split'`` — the CHECK widening
    migration 0009 admits. The custody sweep hunts the two company names and
    the officer name this call planted raw."""
    cp_tenant_id = uuid4()
    prompt_ids: list[str] = []
    pack_version = 0
    seed_control_plane_tenant(control_plane_database, tenant_id=cp_tenant_id)
    try:
        with StubProvider() as stub, VendorService(
            control_plane_database,
            admin_key=VENDOR_ADMIN_KEY,
            extra_env={
                "AICS_CP_LLM_PROVIDER": STUB_PROVIDER_ID,
                "AICS_CP_LLM_BASE_URL": stub.base_url,
                "AICS_CP_LLM_API_KEY": STUB_API_KEY,
                "AICS_CP_LLM_MODEL": STUB_MODEL_ID,
            },
            capture_stderr=True,
        ) as vendor:
            prompt_ids, pack_version = publish_prompts(vendor)
            tenant, _company = await seed_execution_plane(
                session, activation_credential_file, cp_tenant_id=cp_tenant_id
            )
            monkeypatch.setattr(settings, "ai_q13_gate", GATE)
            monkeypatch.setattr(settings, "cp_base_url", vendor.base_url)

            with caplog.at_level(logging.DEBUG):
                call = await ai_gateway.infer_split(
                    session,
                    tenant_id=tenant.id,
                    parent_id=uuid4(),
                    language=LANGUAGE,
                    content=SPLIT_CONTENT,
                    tokenize_values={
                        "COMPANY": [SPLIT_COMPANY_ALPHA, SPLIT_COMPANY_BETA],
                        "PERSON": [SPLIT_OFFICER],
                    },
                )

            # (1) The wire: one request, the company-less shape — the content
            # field scrubbed-and-tokenized, no company anchor line, and the
            # content_splitter binding on the system message.
            assert len(stub.requests) == 1
            (request,) = stub.requests
            assert request["path"] == "/chat/completions"
            assert request["authorization"] == f"Bearer {STUB_API_KEY}"
            wire_text = request["body"]["messages"][1]["content"]
            assert wire_text == SPLIT_WIRE_TEXT
            assert not wire_text.startswith("company:")
            assert request["body"]["messages"][0]["content"] == prompt_body(
                "content_splitter"
            )

            # (2) The answer is UNRESTORED spans over the call's tokens — the
            # officer's real name appears nowhere in it, and the coverage
            # text the caller will grammar-check is the tokenized content.
            assert call.result == SPLIT_SPANS
            assert SPLIT_OFFICER not in json.dumps(call.result)
            assert call.text == SPLIT_TOKENIZED
            assert call.prompt_name == "content_splitter"
            assert call.prompt_version == 1

            # (3) Exactly one llm_call audit entry — the split parent's own
            # event, company_id NULL, the closed seven-key shape, the digest
            # binding the exact bytes the stub saw.
            rows = (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == tenant.id)
                    .order_by(AuditLog.seq)
                )
            ).all()
            llm_calls = [row for row in rows if row.action == "llm_call"]
            assert len(llm_calls) == 1
            entry = llm_calls[0]
            assert entry.company_id is None
            assert entry.entity_type == "split_parent"
            assert entry.llm_call["task_type"] == "split"
            assert entry.llm_call["provider"] == STUB_PROVIDER_ID
            assert entry.llm_call["model_id"] == STUB_MODEL_ID
            assert entry.llm_call["prompt_tokens"] == 37
            assert entry.llm_call["completion_tokens"] == 14
            assert entry.llm_call["snippet_sha256"] == hashlib.sha256(
                SPLIT_WIRE_TEXT.encode("utf-8")
            ).hexdigest()

            # (4) The vendor's metering row admits the fifth task type (the
            # 0009 widening): one row, split, bound to content_splitter.
            metered = metering_rows(control_plane_database, cp_tenant_id)
            assert len(metered) == 1
            row = metered[0]
            assert row["task_type"] == "split"
            assert row["status"] == "succeeded"
            assert row["prompt_name"] == "content_splitter"
            assert row["prompt_version"] == 1
            assert row["model_id"] == STUB_MODEL_ID
            assert row["prompt_tokens"] == 37
            assert row["completion_tokens"] == 14

            # (5) The custody sweep for the split's needles: the two company
            # names and the officer's name survive nowhere — not in any
            # vendor table, not in any execution-plane audit field, not in
            # any log either side captured.
            needles = (SPLIT_COMPANY_ALPHA, SPLIT_COMPANY_BETA, SPLIT_OFFICER)
            stored = " ".join(
                str(piece)
                for row in rows
                for piece in (row.details, row.llm_call, row.action, row.entity_type)
            )
            for needle in needles:
                assert needle not in stored
                assert needle not in caplog.text
                assert needle not in vendor.stderr_text()
            for needle in needles:
                swept = sweep_control_plane_tables(control_plane_database, needle)
                assert swept > 0  # the sweep actually read the database
    finally:
        remove_vendor_rows(
            control_plane_database,
            tenant_id=cp_tenant_id,
            prompt_ids=prompt_ids,
            pack_version=pack_version,
        )
