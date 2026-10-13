"""API-layer tests for the prior-filing ingestion and review-queue endpoints.

These run without a database or object store: the auth seam, RBAC context,
session, and storage are overridden, and the application services are
monkeypatched, so only fail-closed authentication, the grant-gated
authorization check (``require_company_access``), request validation, and the
typed-error → HTTP mappings are exercised. Real flows against PostgreSQL live
in ``test_prior_filing_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import prior_filing
from app.application.company_master import BrnAlreadyInUseError, CompanyNotFoundError
from app.application.documents import DocumentNotFoundError
from app.domain.prior_filing import UnknownTargetError
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import GatingReason
from app.main import create_app

COMPANY_ID = uuid4()
BATCH_ID = uuid4()
RECORD_ID = uuid4()
ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset(),
)

BATCHES_URL = f"/api/v1/companies/{COMPANY_ID}/import-batches"
BATCH_URL = f"{BATCHES_URL}/{BATCH_ID}"
CONFIRM_URL = f"{BATCH_URL}/records/{RECORD_ID}/confirm"
REJECT_URL = f"{BATCH_URL}/records/{RECORD_ID}/reject"


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def _raise(error: Exception):
    async def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


# --- Fail closed on every route ---


def test_unauthenticated_fails_closed_on_all_routes() -> None:
    client = build_client()
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"}
    ).status_code == 401
    assert client.get(BATCHES_URL).status_code == 401
    assert client.get(BATCH_URL).status_code == 401
    assert client.post(CONFIRM_URL).status_code == 401
    assert client.post(REJECT_URL).status_code == 401


def test_general_without_company_grant_is_forbidden() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITHOUT_GRANT
    )
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"}
    ).status_code == 403
    assert client.get(BATCHES_URL).status_code == 403
    assert client.get(BATCH_URL).status_code == 403
    assert client.post(CONFIRM_URL).status_code == 403
    assert client.post(REJECT_URL).status_code == 403


# --- Request validation ---


def test_trigger_requires_a_known_form_code() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar2"}
    ).status_code == 422
    assert client.post(BATCHES_URL, json={"form": "nar1"}).status_code == 422


# --- Service error mappings ---


def test_gate_denial_surfaces_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prior_filing,
        "extract_prior_filing",
        _raise(prior_filing.AutomationDeniedError(GatingReason.TIER_CAPABILITY_MISSING)),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"})
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: tier_capability_missing"


def test_trigger_maps_unknown_company_and_document_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(prior_filing, "extract_prior_filing", _raise(CompanyNotFoundError))
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"}
    ).status_code == 404
    monkeypatch.setattr(prior_filing, "extract_prior_filing", _raise(DocumentNotFoundError))
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"}
    ).status_code == 404


def test_trigger_maps_wrong_document_kind_and_form_to_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(
        prior_filing, "extract_prior_filing", _raise(prior_filing.NotAPriorFilingError("nope"))
    )
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nar1"}
    ).status_code == 422
    monkeypatch.setattr(
        prior_filing, "extract_prior_filing", _raise(prior_filing.UnsupportedFormError("nope"))
    )
    assert client.post(
        BATCHES_URL, json={"document_id": str(uuid4()), "form": "nd2a"}
    ).status_code == 422


def test_read_maps_unknown_batch_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prior_filing, "get_batch", _raise(prior_filing.BatchNotFoundError))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(BATCH_URL).status_code == 404


def test_batch_list_maps_unknown_company_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prior_filing, "list_batches", _raise(CompanyNotFoundError))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(BATCHES_URL).status_code == 404


def test_confirm_maps_its_error_families(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    for error, expected in (
        (prior_filing.RecordNotFoundError(), 404),
        (prior_filing.RecordAlreadyDecidedError("already confirmed"), 409),
        (BrnAlreadyInUseError(), 409),
        (prior_filing.EffectiveDateRequiredError("no date"), 422),
        (prior_filing.IncompleteFactGroupError("registered_office", ("district",)), 422),
        (UnknownTargetError("outside the vocabulary"), 422),
    ):
        monkeypatch.setattr(prior_filing, "confirm_record", _raise(error))
        response = client.post(CONFIRM_URL)
        assert response.status_code == expected, (error, response.text)


def test_confirming_a_static_without_administrative_rights_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service refuses; the route maps it to 403 for a General account."""
    monkeypatch.setattr(
        prior_filing,
        "confirm_record",
        _raise(prior_filing.StaticTargetConfirmationError("company.br_number is a static")),
    )
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    response = client.post(CONFIRM_URL)
    assert response.status_code == 403
    assert "static" in response.json()["detail"]


def test_reject_maps_unknown_record_and_double_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(prior_filing, "reject_record", _raise(prior_filing.RecordNotFoundError))
    assert client.post(REJECT_URL).status_code == 404
    monkeypatch.setattr(
        prior_filing, "reject_record", _raise(prior_filing.RecordAlreadyDecidedError("rejected"))
    )
    assert client.post(REJECT_URL).status_code == 409


# --- Happy paths (services monkeypatched) ---


class _StubRecord:
    id = RECORD_ID
    target = "company.email"
    value = "staff@tcsp.example"
    confidence = 0.99
    status = "confirmed"
    effective_date = date(2025, 3, 31)
    decided_by = ADMIN.user_id
    decided_at = None


class _StubBatch:
    id = BATCH_ID
    company_id = COMPANY_ID
    document_id = uuid4()
    form = "nar1"
    shape = "acroform"
    created_by = None
    created_at = datetime(2026, 9, 21, tzinfo=UTC)


def test_batch_list_returns_run_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The company's batch list carries counts, never the records."""

    async def fake_list(*args: object, **kwargs: object) -> list[prior_filing.BatchSummary]:
        return [
            prior_filing.BatchSummary(
                batch=_StubBatch(),
                records_total=15,
                records_pending=2,
                records_confirmed=12,
                records_rejected=1,
            )
        ]

    monkeypatch.setattr(prior_filing, "list_batches", fake_list)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(BATCHES_URL)

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": str(BATCH_ID),
            "company_id": str(COMPANY_ID),
            "document_id": str(_StubBatch.document_id),
            "form": "nar1",
            "shape": "acroform",
            "created_by": None,
            "created_at": "2026-09-21T00:00:00Z",
            "records_total": 15,
            "records_pending": 2,
            "records_confirmed": 12,
            "records_rejected": 1,
        }
    ]


def test_granted_general_staff_can_confirm_a_proposed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_confirm(*args: object, **kwargs: object) -> _StubRecord:
        captured.update(kwargs)
        return _StubRecord()

    monkeypatch.setattr(prior_filing, "confirm_record", fake_confirm)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    response = client.post(CONFIRM_URL, json={"effective_date": "2025-01-15"})

    assert response.status_code == 200, response.text
    assert response.json()["target"] == "company.email"
    assert response.json()["confidence"] == 0.99
    assert captured["effective_date"] == date(2025, 1, 15)
    # A General account never carries the administrative right (the single
    # RBAC decision behind require_admin).
    assert captured["can_write_statics"] is False


def test_administrator_confirm_carries_the_statics_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_confirm(*args: object, **kwargs: object) -> _StubRecord:
        captured.update(kwargs)
        return _StubRecord()

    monkeypatch.setattr(prior_filing, "confirm_record", fake_confirm)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(CONFIRM_URL)

    assert response.status_code == 200
    assert captured["can_write_statics"] is True
    assert captured["effective_date"] is None
    assert captured["tenant_id"] == ADMIN.tenant_id
    assert captured["batch_id"] == BATCH_ID
    assert captured["record_id"] == RECORD_ID


def test_batch_read_returns_records_and_the_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubAssessment:
        id = uuid4()
        subject_type = "document"
        subject_id = _StubBatch.document_id
        component = "document_extractor"
        model = "pdf-geometry/v1"
        field_scores: ClassVar[dict[str, float]] = {"company.email": 0.99}
        signal_log: ClassVar[dict[str, object]] = {
            "shape": "acroform",
            "fields": {},
            "document": [],
        }
        overall_score = 0.99
        routing = "auto_accept"
        created_at = datetime(2026, 9, 21, tzinfo=UTC)

    async def fake_get_batch(*args: object, **kwargs: object) -> prior_filing.ExtractionRun:
        return prior_filing.ExtractionRun(
            batch=_StubBatch(), records=[_StubRecord()], assessment=_StubAssessment()
        )

    monkeypatch.setattr(prior_filing, "get_batch", fake_get_batch)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    body = client.get(BATCH_URL).json()

    assert body["id"] == str(BATCH_ID)
    assert body["shape"] == "acroform"
    assert [record["target"] for record in body["records"]] == ["company.email"]
    assert body["assessment"]["routing"] == "auto_accept"
    assert body["assessment"]["field_scores"] == {"company.email": 0.99}


def test_granted_general_staff_can_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reject(*args: object, **kwargs: object) -> _StubRecord:
        return _StubRecord()

    monkeypatch.setattr(prior_filing, "reject_record", fake_reject)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    response = client.post(REJECT_URL)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"  # the stub's status, echoed


def test_company_id_is_never_taken_from_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigger reads the company from the path and the tenant from the actor."""
    captured: dict[str, object] = {}

    async def fake_extract(*args: object, **kwargs: object) -> prior_filing.ExtractionRun:
        captured.update(kwargs)
        raise prior_filing.UnsupportedFormError("stop here")

    monkeypatch.setattr(prior_filing, "extract_prior_filing", fake_extract)
    actor = ActorRef(user_id=uuid4(), tenant_id=uuid4())
    client = build_client(actor=actor, context=ADMIN_CONTEXT)
    other = uuid4()
    client.post(
        BATCHES_URL,
        json={
            "document_id": str(uuid4()),
            "form": "nar1",
            "company_id": str(other),
            "tenant_id": str(other),
        },
    )
    assert captured["company_id"] == COMPANY_ID
    assert captured["tenant_id"] == actor.tenant_id
    assert isinstance(captured["document_id"], UUID)
