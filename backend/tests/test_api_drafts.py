"""API-layer tests for the draft routes (contract §4.2, §5.1).

These run without a database: the auth seam, RBAC context and session are
overridden and the application service is monkeypatched, so only fail-closed
authentication, the grant-gated authorization checks, request validation and the
typed-error → HTTP mappings are exercised. The lifecycle itself — the version
rows, the resolution, the event moves, the audit chain — lives in
``test_drafts_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.pdf.filler import DraftFontUnavailableError, GlyphUnavailableError
from app.adapters.persistence.documents import Document
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import drafts
from app.application.company_master import CompanyNotFoundError
from app.application.drafts import (
    AutomationDeniedError,
    DraftActionNotWiredError,
    DraftConflictError,
    DraftForbiddenError,
    DraftInvalidError,
    DraftNotFoundError,
    DraftRationaleRequiredError,
    DraftTransitionError,
    DraftTransitions,
    DraftView,
    SignaturePackage,
    TransitionResult,
)
from app.domain import change_events as change_events_domain
from app.domain.change_events import ChangeEventAction, ChangeEventStatus
from app.domain.compliance import DueDateRuleNotEstablishedError
from app.domain.documents import ContentTypeMismatchError, UnsupportedContentTypeError
from app.domain.draft_fill import MISSING_VALUE, Escalation
from app.domain.drafts import DraftAction, DraftStatus, SignatureStatus
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_packages import GatingReason
from app.main import create_app

COMPANY_ID = uuid4()
DOCUMENT_ID = uuid4()
OTHER_COMPANY_ID = uuid4()
DRAFT_ID = uuid4()
USER_ID = uuid4()
TENANT_ID = uuid4()

ADMIN = ActorRef(user_id=USER_ID, tenant_id=TENANT_ID)
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({OTHER_COMPANY_ID}),
)

MARK = {"action": "mark_under_review"}
COMPANY_DRAFTS_URL = f"/api/v1/companies/{COMPANY_ID}/drafts"
DRAFT_URL = f"/api/v1/drafts/{DRAFT_ID}"
DRAFT_TRANSITIONS_URL = f"/api/v1/drafts/{DRAFT_ID}/transitions"
SIGNATURE_URL = f"/api/v1/drafts/{DRAFT_ID}/signature"

FORM_VERSION = {"specification_no": "2/2025 (May 2025)", "template_sha256": "a" * 64}


def build_client(
    actor: ActorRef | None = None,
    context: ActorContext | None = None,
    store: object | None = None,
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    if store is not None:
        app.dependency_overrides[get_storage] = lambda: store

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def stored_draft(**overrides: object) -> Draft:
    """A draft version row as the service returns it (never persisted here)."""
    values: dict[str, Any] = {
        "id": DRAFT_ID,
        "version_no": 1,
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "form_type": "nar1",
        "target_date": date(2026, 9, 30),
        "status": DraftStatus.GENERATED.value,
        "resolved_state": {"company": {"br_number": "12345678"}},
        "field_provenance": {
            "HDR-BRN": {
                "target": "company.br_number",
                "source_type": "master_record_id",
                "source_id": str(COMPANY_ID),
                "changed": False,
            }
        },
        "qc_flags": [{"kind": "missing_source", "path": "company.name_en"}],
        "filing_metadata": {"return": {"made_up_date": "2026-03-01"}},
        "applied_event_ids": [],
        "form_version": FORM_VERSION,
        "private_company_statement": True,
        "statement_confirmed_by": None,
        "statement_confirmed_at": None,
        "signature_status": SignatureStatus.NOT_SENT.value,
        "reviewed_by": None,
        "reviewed_at": None,
        "approved_by": None,
        "approved_at": None,
        "rationale": None,
        "comments": None,
        "invalidated_at": None,
        "invalidation_reason": None,
        "created_by": USER_ID,
        "created_at": datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return Draft(**values)


def view(**overrides: object) -> DraftView:
    return DraftView(current=stored_draft(**overrides), history=[])


def stored_document(**overrides: object) -> Document:
    """The WORM row a release's stored package becomes (never persisted here)."""
    values: dict[str, Any] = {
        "id": DOCUMENT_ID,
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "kind": "generated_draft",
        "origin": "generated",
        "retention_class": "generated_draft",
        "sha256": "b" * 64,
        "storage_key": "1234567/2026/nar1_draft_1234567_20260928_v1.pdf",
        "byte_size": 1024,
        "content_type": "application/pdf",
        "scan_status": "clean",
        "supersedes_document_id": None,
        "uploaded_by": USER_ID,
        "uploaded_at": datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return Document(**values)


# --- Authentication and authorization ---------------------------------------


def test_every_draft_route_fails_closed_without_a_credential() -> None:
    client = build_client()
    generation = {"form_type": "nar1", "target_date": "2026-09-30"}
    assert client.post(COMPANY_DRAFTS_URL, json=generation).status_code == 401
    assert client.get(COMPANY_DRAFTS_URL).status_code == 401
    assert client.get(DRAFT_URL).status_code == 401
    assert client.get(DRAFT_TRANSITIONS_URL).status_code == 401
    assert client.post(DRAFT_TRANSITIONS_URL, json=MARK).status_code == 401
    assert client.post(SIGNATURE_URL, content=b"%PDF-1.4").status_code == 401


def test_a_general_account_without_the_grant_is_refused_on_company_scoped_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant dependency refuses before the service is reached: a company
    path names the company, so no read of the draft is needed to deny."""

    async def never(*args: object, **kwargs: object) -> DraftView:  # pragma: no cover
        raise AssertionError("the service must not be reached without the company grant")

    monkeypatch.setattr(drafts, "generate_draft", never)
    monkeypatch.setattr(drafts, "list_drafts", never)
    client = build_client(ADMIN, GENERAL_WITHOUT_GRANT)
    assert client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    ).status_code == 403
    assert client.get(COMPANY_DRAFTS_URL).status_code == 403


def test_the_id_scoped_routes_apply_the_same_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id-scoped paths carry no company, so the application layer resolves it
    from the row and asks the same single decision — the service's refusal is
    the route's 403."""

    async def forbidden(*args: object, **kwargs: object) -> DraftView:
        raise DraftForbiddenError

    monkeypatch.setattr(drafts, "get_draft", forbidden)
    monkeypatch.setattr(drafts, "transitions_for", forbidden)
    monkeypatch.setattr(drafts, "transition", forbidden)
    monkeypatch.setattr(drafts, "record_signed_upload", forbidden)
    client = build_client(ADMIN, GENERAL_WITHOUT_GRANT)
    assert client.get(DRAFT_URL).status_code == 403
    assert client.get(DRAFT_TRANSITIONS_URL).status_code == 403
    assert client.post(DRAFT_TRANSITIONS_URL, json=MARK).status_code == 403
    assert client.post(SIGNATURE_URL, content=b"%PDF-1.4").status_code == 403


def test_the_reads_carry_no_permission_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A General account with the company grant but none of the four flags may
    read a draft and its transitions (ADR-0012: the customer's own data)."""

    async def get_service(*args: object, **kwargs: object) -> DraftView:
        return view()

    async def transitions_service(*args: object, **kwargs: object) -> DraftTransitions:
        return DraftTransitions(
            row=stored_draft(),
            actions=drafts.allowed_actions(
                status=DraftStatus.GENERATED,
                may_review=False,
                may_approve=False,
                has_grant=True,
            ),
        )

    grant_only = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset(),
        company_ids=frozenset({COMPANY_ID}),
    )
    monkeypatch.setattr(drafts, "get_draft", get_service)
    monkeypatch.setattr(drafts, "transitions_for", transitions_service)
    client = build_client(ADMIN, grant_only)
    assert client.get(DRAFT_URL).status_code == 200
    body = client.get(DRAFT_TRANSITIONS_URL).json()
    assert body["status"] == "generated"
    # No flags: the two reviewer rows are not offered, and nothing unwired is.
    assert body["actions"] == []


# --- Generation --------------------------------------------------------------


def test_generation_returns_the_read_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_generate(session: object, **kwargs: Any) -> DraftView:
        captured.update(kwargs)
        return view()

    monkeypatch.setattr(drafts, "generate_draft", fake_generate)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(DRAFT_ID)
    assert body["form_type"] == "nar1"
    assert body["status"] == "generated"
    assert body["signature_status"] == "not_sent"
    assert body["applied_event_ids"] == []
    assert body["form_version"] == FORM_VERSION
    assert body["private_company_statement"] is True
    assert body["history"] == []
    assert captured["tenant_id"] == TENANT_ID
    assert captured["company_id"] == COMPANY_ID
    assert captured["form_type"].value == "nar1"
    assert captured["target_date"] == date(2026, 9, 30)


def test_a_denied_gate_is_a_403_naming_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def denied(session: object, **kwargs: Any) -> DraftView:
        raise AutomationDeniedError(GatingReason.SERVICE_NOT_INCLUDED)

    monkeypatch.setattr(drafts, "generate_draft", denied)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: service_not_included"


def test_an_unknown_company_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(session: object, **kwargs: Any) -> DraftView:
        raise CompanyNotFoundError

    monkeypatch.setattr(drafts, "generate_draft", missing)
    monkeypatch.setattr(drafts, "list_drafts", missing)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    assert client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    ).status_code == 404
    assert client.get(COMPANY_DRAFTS_URL).status_code == 404


def test_an_unestablished_nar1_rule_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(session: object, **kwargs: Any) -> DraftView:
        raise DueDateRuleNotEstablishedError("public")

    monkeypatch.setattr(drafts, "generate_draft", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "nar1_rule_not_established"


def test_a_lost_version_race_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def raced(session: object, **kwargs: Any) -> DraftView:
        raise DraftConflictError

    monkeypatch.setattr(drafts, "generate_draft", raced)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nar1", "target_date": "2026-09-30"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "draft_conflict"


def test_an_unknown_form_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        COMPANY_DRAFTS_URL, json={"form_type": "nn3", "target_date": "2026-09-30"}
    )
    assert response.status_code == 422


def test_the_company_read_lists_the_lineages(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(session: object, **kwargs: Any) -> list[DraftView]:
        return [view()]

    monkeypatch.setattr(drafts, "list_drafts", fake_list)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.get(COMPANY_DRAFTS_URL)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(DRAFT_ID)]


# --- The draft read and the transitions read ---------------------------------


def test_the_detail_read_carries_the_history_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    history = stored_draft(version_no=1)
    current = stored_draft(version_no=2, status=DraftStatus.UNDER_REVIEW.value)

    async def fake_get(session: object, **kwargs: Any) -> DraftView:
        return DraftView(current=current, history=[history])

    monkeypatch.setattr(drafts, "get_draft", fake_get)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    body = client.get(DRAFT_URL).json()
    assert body["version_no"] == 2
    assert body["status"] == "under_review"
    assert [entry["version_no"] for entry in body["history"]] == [1]
    assert body["history"][0]["status"] == "generated"
    assert body["history"][0]["created_by"] == str(USER_ID)


def test_an_unknown_draft_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``store``/``scanner`` default so the same fake can stand in for the
    # reads, which the route calls without them.
    async def missing(
        session: object, store: object = None, scanner: object = None, **kwargs: Any
    ) -> TransitionResult:
        raise DraftNotFoundError

    monkeypatch.setattr(drafts, "get_draft", missing)
    monkeypatch.setattr(drafts, "transitions_for", missing)
    monkeypatch.setattr(drafts, "transition", missing)
    monkeypatch.setattr(drafts, "record_signed_upload", missing)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    assert client.get(DRAFT_URL).status_code == 404
    assert client.get(DRAFT_TRANSITIONS_URL).status_code == 404
    assert client.post(DRAFT_TRANSITIONS_URL, json=MARK).status_code == 404
    assert client.post(SIGNATURE_URL, content=b"%PDF-1.4").status_code == 404


def test_a_forbidden_draft_is_a_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(
        session: object, store: object = None, scanner: object = None, **kwargs: Any
    ) -> TransitionResult:
        raise DraftForbiddenError

    monkeypatch.setattr(drafts, "get_draft", forbidden)
    monkeypatch.setattr(drafts, "transition", forbidden)
    monkeypatch.setattr(drafts, "record_signed_upload", forbidden)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    assert client.get(DRAFT_URL).status_code == 403
    assert client.post(DRAFT_TRANSITIONS_URL, json=MARK).status_code == 403
    assert client.post(SIGNATURE_URL, content=b"%PDF-1.4").status_code == 403


def test_the_transitions_read_offers_the_wired_rows_for_the_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reachable rows of §4.2 for a reviewer at ``generated``: ``edit`` and
    ``mark_under_review``. The signature rows and ``invalidate`` are absent —
    unwired and the system's — so no button can 409 or act by itself."""

    async def fake_transitions(session: object, **kwargs: Any) -> DraftTransitions:
        return DraftTransitions(
            row=stored_draft(),
            actions=drafts.allowed_actions(
                status=DraftStatus.GENERATED,
                may_review=True,
                may_approve=False,
                has_grant=True,
            ),
        )

    monkeypatch.setattr(drafts, "transitions_for", fake_transitions)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    body = client.get(DRAFT_TRANSITIONS_URL).json()
    assert body["draft_id"] == str(DRAFT_ID)
    assert body["company_id"] == str(COMPANY_ID)
    assert body["status"] == "generated"
    assert body["actions"] == [
        {"action": "edit", "to_status": "under_review", "rationale_required": False},
        {"action": "mark_under_review", "to_status": "under_review", "rationale_required": False},
    ]


def test_apply_is_never_offered_by_the_change_event_transitions_read() -> None:
    """F2's system ``apply`` is wired, and still nobody's to press: the row's
    leg is ``None``, so the allowed-actions read drops it for every actor."""
    for status in ChangeEventStatus:
        offered = {
            item.action
            for item in change_events_domain.allowed_actions(
                status=status, may_review=True, may_approve=True
            )
        }
        assert ChangeEventAction.APPLY not in offered


# --- The transitions POST ----------------------------------------------------


def test_a_wired_transition_returns_the_updated_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        captured["store"], captured["scanner"] = store, scanner
        return TransitionResult(view=view(status=DraftStatus.UNDER_REVIEW.value))

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL,
        json={
            "action": "edit",
            "filing_metadata": {"return": {"fs_period_start": "2025-04-01"}},
            "private_company_statement": True,
            "rationale": "checked against the register",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "under_review"
    assert captured["action"] is DraftAction.EDIT
    assert captured["stated"] == {"filing_metadata", "private_company_statement"}
    assert captured["filing_metadata"] == {"return": {"fs_period_start": "2025-04-01"}}
    assert captured["private_company_statement"] is True
    assert captured["rationale"] == "checked against the register"


def test_an_absent_edit_field_is_not_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        return TransitionResult(view=view())

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json=MARK)
    assert response.status_code == 200
    assert captured["stated"] == set()
    assert captured["filing_metadata"] is None


def test_an_unwired_action_answers_its_named_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftActionNotWiredError(
            action=kwargs["action"], reason="export_not_wired"
        )

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    # The executor's unwired-action refusal maps to the named 409 verbatim
    # (pinned against the real executor's partition in ``test_drafts_domain``
    # — since P6-4 the map is empty and this is a mapping test alone).
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "export"})
    assert response.status_code == 409
    assert response.json()["detail"] == "export_not_wired"


def test_regenerate_returns_the_fresh_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """P6-3: ``regenerate`` is wired — the executor dispatches it (the API
    carries the action and maps the refusals; the version append itself is the
    scenario suite's claim)."""
    captured: dict[str, Any] = {}

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        return TransitionResult(
            view=DraftView(
                current=stored_draft(
                    version_no=2,
                    status=DraftStatus.GENERATED.value,
                    signature_status=SignatureStatus.NOT_SENT.value,
                ),
                history=[stored_draft(version_no=1, status=DraftStatus.REJECTED.value)],
            )
        )

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "regenerate"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version_no"] == 2
    assert body["status"] == "generated"
    assert body["signature_status"] == "not_sent"
    assert [entry["version_no"] for entry in body["history"]] == [1]
    assert body["history"][0]["status"] == "rejected"
    assert body["signature_package"] is None
    assert captured["action"] is DraftAction.REGENERATE


def test_a_rationale_on_regenerate_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``regenerate`` row is rationale-free (the rejection's own row
    carried it): a stated rationale is refused by name — the
    ``confirm_statement`` body's-shape pattern — before anything is built.
    The refusal is the executor's (pinned against the real one in
    ``test_drafts_scenarios_db.py``); this test pins the route's mapping."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(["regenerate: carries rationale"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL,
        json={"action": "regenerate", "rationale": "client clarified the address"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "regenerate: carries rationale"


def test_a_rationale_on_export_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``export`` row is rationale-free, the ``regenerate`` precedent: an
    export goes out or it does not, and a stated note is refused by name
    (422 ``export: carries rationale``) rather than silently dropped by the
    dispatch's early return — the route mapping; the unmoved draft and the
    untouched chain are the scenario suite's claim."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(["export: carries rationale"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL,
        json={"action": "export", "mode": "paper", "rationale": "partner asked"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "export: carries rationale"


def test_a_denied_gate_on_regenerate_is_a_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling R3: regeneration rides the generation seam and consults the
    same §5.1 gate — a denial answers 403 with the gate's own reason, exactly
    like generation's route."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise AutomationDeniedError(GatingReason.SERVICE_NOT_INCLUDED)

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "regenerate"})
    assert response.status_code == 403
    assert response.json()["detail"] == "automation_denied: service_not_included"


def test_a_lost_version_race_on_regenerate_is_a_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raced(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftConflictError

    monkeypatch.setattr(drafts, "transition", raced)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "regenerate"})
    assert response.status_code == 409
    assert response.json()["detail"] == "draft_conflict"


def test_a_status_that_does_not_admit_the_action_is_a_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftTransitionError(
            status=DraftStatus.UNDER_REVIEW, action=DraftAction.MARK_UNDER_REVIEW
        )

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json=MARK)
    assert response.status_code == 409
    assert response.json()["detail"] == "transition_not_allowed"


def test_an_invalid_edit_answers_the_domain_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(
            [
                "filing_metadata_invalid: member_particulars_delivery",
                "filing_metadata_invalid: return.fs_period_end: before fs_period_start",
            ]
        )

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL,
        json={
            "action": "edit",
            "filing_metadata": {
                "member_particulars_delivery": "carrier_pigeon",
                "return": {"fs_period_start": "2026-04-01", "fs_period_end": "2026-03-31"},
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "filing_metadata_invalid: member_particulars_delivery; "
        "filing_metadata_invalid: return.fs_period_end: before fs_period_start"
    )


def test_an_unknown_action_name_is_a_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "bless"})
    assert response.status_code == 422


def test_an_unknown_body_field_is_a_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "mark_under_review", "filing_metdata": {}}
    )
    assert response.status_code == 422


def test_the_release_answers_with_its_signature_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """``send_for_signature``'s response carries the document and the escalations."""

    async def released(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        return TransitionResult(
            view=view(
                status=DraftStatus.SENT_FOR_SIGNATURE.value,
                signature_status=SignatureStatus.SENT_FOR_SIGNATURE.value,
            ),
            signature_package=SignaturePackage(
                document=stored_document(),
                escalations=(Escalation(MISSING_VALUE, "filing_metadata.presentor: not stated"),),
            ),
        )

    monkeypatch.setattr(drafts, "transition", released)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "send_for_signature"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "sent_for_signature"
    assert body["signature_status"] == "sent_for_signature"
    assert body["signature_package"] == {
        "document_id": str(DOCUMENT_ID),
        "escalations": [
            {"kind": MISSING_VALUE, "detail": "filing_metadata.presentor: not stated"}
        ],
    }


def test_every_other_response_carries_no_package(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        return TransitionResult(view=view())

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    assert client.post(DRAFT_TRANSITIONS_URL, json=MARK).json()["signature_package"] is None


def test_a_missing_font_is_a_named_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftFontUnavailableError(Path("/nonexistent/NotoSansHK.ttf"))

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "send_for_signature"})
    assert response.status_code == 503
    assert response.json()["detail"] == "draft_font_unavailable"


def test_a_character_the_font_cannot_render_is_a_named_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise GlyphUnavailableError("乐们")

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "send_for_signature"})
    assert response.status_code == 409
    assert response.json()["detail"] == "font_glyph_unavailable: 乐们"


# --- The review/approval gate (scope §2.11, P6-2) -----------------------------


def test_mark_reviewed_returns_the_reviewed_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """The review row lands the draft on ``reviewed`` with the review stamp;
    the response is the moved draft and the body carries no confirmation."""
    captured: dict[str, Any] = {}

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        return TransitionResult(
            view=view(
                status=DraftStatus.REVIEWED.value,
                reviewed_by=USER_ID,
                reviewed_at=datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
            )
        )

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "mark_reviewed"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reviewed"
    assert body["reviewed_by"] == str(USER_ID)
    assert body["reviewed_at"] is not None
    assert body["approved_by"] is None
    assert captured["action"] is DraftAction.MARK_REVIEWED
    assert captured["confirm_statement"] is None


def test_approve_without_the_statement_confirmation_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NAR1 item 16's declaration must be human-confirmed at approval (ruling
    R2): the executor refuses before anything is written, and the refusal is
    named — the draft never moves (the scenario suite pins the unmoved row)."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(["statement_confirmation_required"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "approve"})
    assert response.status_code == 422
    assert response.json()["detail"] == "statement_confirmation_required"


def test_approve_with_the_confirmation_returns_the_approved_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        return TransitionResult(
            view=view(
                status=DraftStatus.APPROVED.value,
                approved_by=USER_ID,
                approved_at=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
                statement_confirmed_by=USER_ID,
                statement_confirmed_at=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
            )
        )

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "approve", "confirm_statement": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(USER_ID)
    assert body["statement_confirmed_by"] == str(USER_ID)
    assert body["statement_confirmed_at"] is not None
    assert captured["confirm_statement"] is True


def test_a_confirmation_on_a_non_statement_draft_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft that makes no item-16 statement (flag false or absent — ND2A
    has none) cannot be confirmed: the caller has misunderstood the draft."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(["statement_not_made"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "approve", "confirm_statement": True}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "statement_not_made"


def test_confirm_statement_on_any_other_action_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmation is ``approve``'s body's shape alone — the edit-fields
    refusal pattern verbatim: a ``mark_reviewed`` carrying it is a request the
    caller misunderstood, not a key to ignore."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftInvalidError(["mark_reviewed: carries confirm_statement"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "mark_reviewed", "confirm_statement": True}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "mark_reviewed: carries confirm_statement"


def test_a_reject_without_a_rationale_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both reject legs are §4.2 "rationale required" rows: a rejection without
    the reason is refused, never recorded (the change-events precedent)."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        raise DraftRationaleRequiredError

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(DRAFT_TRANSITIONS_URL, json={"action": "reject"})
    assert response.status_code == 422
    assert response.json()["detail"] == "rationale_required"


def test_a_reject_with_the_wrong_flag_is_a_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject from ``signature_received`` is a reviewer's row: an actor holding
    only ``approve_drafts`` is refused 403 — their authority, not the table's
    legality (that mismatch would be a 409)."""
    captured: dict[str, Any] = {}

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        captured.update(kwargs)
        raise DraftForbiddenError

    monkeypatch.setattr(drafts, "transition", refused)
    approver_only = ActorContext(
        is_active=True,
        role=Role.GENERAL,
        permissions=frozenset({Permission.APPROVE_DRAFTS}),
        company_ids=frozenset({COMPANY_ID}),
    )
    client = build_client(ADMIN, approver_only)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "reject", "rationale": "wrong person"}
    )
    assert response.status_code == 403
    assert captured["action"] is DraftAction.REJECT


def test_the_reviewer_and_the_approver_may_be_the_same_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four-eyes is permissive per scope §2.11: one account may review and
    approve the same draft — no same-actor check exists, by ruling."""

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> TransitionResult:
        return TransitionResult(
            view=view(
                status=DraftStatus.APPROVED.value,
                reviewed_by=USER_ID,
                approved_by=USER_ID,
                statement_confirmed_by=USER_ID,
            )
        )

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        DRAFT_TRANSITIONS_URL, json={"action": "approve", "confirm_statement": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reviewed_by"] == body["approved_by"] == str(USER_ID)


# --- The client signature upload (scope §2.10, P6-1) --------------------------


class FakeStore:
    """The route's injected blob store: nothing here may be written on a
    refusal (the double-upload / writes-nothing claims themselves live in
    ``test_drafts_scenarios_db.py``, which has a real database)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


SIGNED_BYTES = b"%PDF-1.4\n%signed-by-the-client\n"


def test_the_signed_upload_returns_the_moved_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /drafts/{id}/signature`` hands the raw body and the declared
    Content-Type to the application seam with the actor's ids, and answers
    with the moved draft (status and signature lifecycle together)."""
    captured: dict[str, Any] = {}

    async def fake_record(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> DraftView:
        captured.update(kwargs)
        captured["store"], captured["scanner"] = store, scanner
        return view(
            status=DraftStatus.SIGNATURE_RECEIVED.value,
            signature_status=SignatureStatus.SIGNATURE_RECEIVED.value,
        )

    monkeypatch.setattr(drafts, "record_signed_upload", fake_record)
    store = FakeStore()
    client = build_client(ADMIN, GENERAL_WITH_GRANT, store=store)
    response = client.post(
        SIGNATURE_URL,
        content=SIGNED_BYTES,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(DRAFT_ID)
    assert body["status"] == "signature_received"
    assert body["signature_status"] == "signature_received"
    assert body["signature_package"] is None
    assert captured["tenant_id"] == TENANT_ID
    assert captured["context"] is GENERAL_WITH_GRANT
    assert captured["draft_id"] == DRAFT_ID
    assert captured["actor_user_id"] == USER_ID
    assert captured["data"] == SIGNED_BYTES
    assert captured["content_type"] == "application/pdf"
    assert captured["store"] is store
    assert captured["scanner"] is not None


def test_an_upload_from_any_other_status_is_a_409_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard refuses before the store: a draft not awaiting the signature
    answers ``transition_not_allowed`` and the blob store stays empty."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> DraftView:
        raise DraftTransitionError(
            status=DraftStatus.GENERATED, action=DraftAction.UPLOAD_SIGNED
        )

    monkeypatch.setattr(drafts, "record_signed_upload", refused)
    store = FakeStore()
    client = build_client(ADMIN, GENERAL_WITH_GRANT, store=store)
    response = client.post(
        SIGNATURE_URL, content=SIGNED_BYTES, headers={"Content-Type": "application/pdf"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "transition_not_allowed"
    assert store.objects == {}


def test_an_upload_over_the_cap_is_413_before_any_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args: object, **kwargs: object) -> DraftView:  # pragma: no cover
        raise AssertionError("the service must not be reached past the cap")

    monkeypatch.setattr(drafts, "record_signed_upload", never)
    client = build_client(ADMIN, GENERAL_WITH_GRANT)
    response = client.post(
        SIGNATURE_URL,
        content=SIGNED_BYTES,
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(26 * 1024 * 1024),
        },
    )
    assert response.status_code == 413


def test_an_unsupported_declared_type_is_the_seams_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documents seam's own refusal, verbatim: the registry accepts the
    kinds §8 names, and the route does not translate the vocabulary."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> DraftView:
        raise UnsupportedContentTypeError("content type 'application/msword' is not allowed")

    monkeypatch.setattr(drafts, "record_signed_upload", refused)
    client = build_client(ADMIN, GENERAL_WITH_GRANT)
    response = client.post(
        SIGNATURE_URL,
        content=SIGNED_BYTES,
        headers={"Content-Type": "application/msword"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "content type 'application/msword' is not allowed"


def test_a_declared_type_the_bytes_belie_is_the_seams_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The magic-bytes leg: a body declared ``application/pdf`` whose bytes
    are not a PDF answers the seam's mismatch refusal, verbatim."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> DraftView:
        raise ContentTypeMismatchError("body does not match declared type 'application/pdf'")

    monkeypatch.setattr(drafts, "record_signed_upload", refused)
    client = build_client(ADMIN, GENERAL_WITH_GRANT)
    response = client.post(
        SIGNATURE_URL,
        content=b"this is not a PDF at all",
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "body does not match declared type 'application/pdf'"
