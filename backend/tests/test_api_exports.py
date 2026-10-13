"""API-layer tests for the export routes (contract §2.8, §2.12, §4.2, §5.1).

These run without a database: the auth seam, RBAC context and session are
overridden and the application service is monkeypatched, so only
fail-closed authentication, the grant-gated authorization checks, request
validation, the mode vocabulary's named refusals and the typed-error → HTTP
mappings are exercised. The export's own claims — one FilingPackage per
export, the events submitted, the audit chain, the stored artifacts, a
refused export's writes-nothing — live in ``test_drafts_scenarios_db.py``,
and the flatten machinery's rendering claims in ``test_draft_fill_pdf.py``.

One test here is not a route test by shape but by subject: the flatten
claim ("the exported PDF has no live form fields") runs the real fill and
the real flattener over the pinned template, hermetically, and reads the
result back with pypdf.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any
from uuid import uuid4

import pypdfium2 as pdfium
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.forms.registry import (
    load_form_registry,
    page_header_fields,
    sheet_pages,
    template_path,
)
from app.adapters.pdf.export_flatten import flatten_filled_pdf
from app.adapters.pdf.filler import fill_template
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import drafts
from app.application.drafts import (
    DraftForbiddenError,
    DraftNotFoundError,
    DraftTransitionError,
    FilingPackageView,
)
from app.domain.drafts import DraftAction, DraftStatus
from app.domain.export_package import ExportMode
from app.domain.prior_filing import FormCode
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
DRAFT_ID = uuid4()
PACKAGE_ID = uuid4()
DOCUMENT_ID = uuid4()
HELPER_ID = uuid4()
CHECKLIST_ID = uuid4()
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
    company_ids=frozenset({uuid4()}),
)

EXPORTS_URL = f"/api/v1/drafts/{DRAFT_ID}/exports"
TRANSITIONS_URL = f"/api/v1/drafts/{DRAFT_ID}/transitions"

EXPORTED_AT = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)
ENVELOPE = {
    "sheet_counts": {"s1": 1},
    "page_pairs": [[1, 1]],
    "helper_document_id": str(HELPER_ID),
    "checklist_document_id": None,
}


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


def package_view(mode: ExportMode = ExportMode.PAPER) -> FilingPackageView:
    return FilingPackageView(
        id=PACKAGE_ID,
        draft_id=DRAFT_ID,
        draft_version_no=1,
        mode=mode,
        document_id=DOCUMENT_ID,
        envelope=dict(ENVELOPE),
        exported_by=USER_ID,
        exported_at=EXPORTED_AT,
    )


# --- The dedicated export route -----------------------------------------------


def test_the_export_route_fails_closed_without_a_credential() -> None:
    client = build_client()
    assert client.post(EXPORTS_URL, json={"mode": "paper"}).status_code == 401


def test_an_export_returns_the_filing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /drafts/{id}/exports`` answers 201 with the FilingPackage: the
    mode, the stored document, the envelope and the exporting actor."""
    captured: dict[str, Any] = {}

    async def fake_export(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        captured.update(kwargs)
        captured["store"], captured["scanner"] = store, scanner
        return package_view()

    monkeypatch.setattr(drafts, "export_draft", fake_export)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "id": str(PACKAGE_ID),
        "draft_id": str(DRAFT_ID),
        "draft_version_no": 1,
        "mode": "paper",
        "document_id": str(DOCUMENT_ID),
        "envelope": ENVELOPE,
        "exported_by": str(USER_ID),
        "exported_at": EXPORTED_AT.isoformat().replace("+00:00", "Z"),
    }
    assert captured["tenant_id"] == TENANT_ID
    assert captured["context"] is ADMIN_CONTEXT
    assert captured["draft_id"] == DRAFT_ID
    assert captured["mode"] is ExportMode.PAPER
    assert captured["actor_user_id"] == USER_ID
    assert captured["store"] is not None
    assert captured["scanner"] is not None


@pytest.mark.parametrize(
    "mode", ["e_registry_prep", "paper", "api_ready"]
)
def test_all_three_modes_are_accepted(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    async def fake_export(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        return package_view(mode=ExportMode(mode))

    monkeypatch.setattr(drafts, "export_draft", fake_export)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": mode})
    assert response.status_code == 201, response.text
    assert response.json()["mode"] == mode


def test_an_unknown_mode_is_a_named_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the vocabulary is refused by name before the service — the
    route validates the mode string itself (``export_mode_invalid``)."""

    async def never(*args: object, **kwargs: object) -> FilingPackageView:  # pragma: no cover
        raise AssertionError("the service must not be reached with an invalid mode")

    monkeypatch.setattr(drafts, "export_draft", never)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "fax"})
    assert response.status_code == 422
    assert response.json()["detail"] == "export_mode_invalid"


def test_an_unknown_body_field_is_a_422() -> None:
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "paper", "rationale": "go"})
    assert response.status_code == 422


def test_a_cross_tenant_or_unknown_draft_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        raise DraftNotFoundError

    monkeypatch.setattr(drafts, "export_draft", missing)
    client = build_client(ADMIN, GENERAL_WITHOUT_GRANT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 404
    assert response.json()["detail"] == "draft not found"


def test_a_draft_without_the_grant_is_a_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        raise DraftForbiddenError

    monkeypatch.setattr(drafts, "export_draft", forbidden)
    client = build_client(ADMIN, GENERAL_WITHOUT_GRANT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


def test_an_export_from_a_non_approved_draft_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """The §4.2 guard: only ``approved`` admits ``export`` — and a refused
    export writes nothing (the scenario suite pins the empty store and the
    unmoved chain against the real executor)."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        raise DraftTransitionError(status=DraftStatus.GENERATED, action=DraftAction.EXPORT)

    monkeypatch.setattr(drafts, "export_draft", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 409
    assert response.json()["detail"] == "transition_not_allowed"


def test_an_unconfirmed_statement_is_a_named_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NAR1 item-16 gate on the way out (ruling R2's other half): the
    domain rule ``ensure_exportable`` names the refusal, and it lands before
    anything is written."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        raise drafts.DraftExportRefusedError("statement_not_confirmed")

    monkeypatch.setattr(drafts, "export_draft", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 409
    assert response.json()["detail"] == "statement_not_confirmed"


def test_a_second_export_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review Focus 4: one export per draft — the second lands on
    ``exported``, which admits nothing."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> FilingPackageView:
        raise DraftTransitionError(status=DraftStatus.EXPORTED, action=DraftAction.EXPORT)

    monkeypatch.setattr(drafts, "export_draft", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(EXPORTS_URL, json={"mode": "paper"})
    assert response.status_code == 409
    assert response.json()["detail"] == "transition_not_allowed"


# --- The transitions door (ruling R4: one executor, two doors) -----------------


def test_the_transitions_door_performs_the_export_with_its_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``action: export`` through the transitions POST is the same executor
    with the mode in the body — the response is the moved draft."""

    async def fake_transition(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> drafts.TransitionResult:
        assert kwargs["action"] is DraftAction.EXPORT
        assert kwargs["mode"] is ExportMode.E_REGISTRY_PREP
        return drafts.TransitionResult(
            view=drafts.DraftView(
                current=_approved_row(status=DraftStatus.EXPORTED.value), history=[]
            )
        )

    monkeypatch.setattr(drafts, "transition", fake_transition)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(TRANSITIONS_URL, json={"action": "export", "mode": "e_registry_prep"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "exported"


def test_the_transitions_export_without_a_mode_is_a_named_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> drafts.TransitionResult:
        raise drafts.DraftInvalidError(["export_mode_required"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(TRANSITIONS_URL, json={"action": "export"})
    assert response.status_code == 422
    assert response.json()["detail"] == "export_mode_required"


def test_a_mode_on_any_other_action_is_a_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode is ``export``'s body's shape alone — the ``confirm_statement``
    pattern verbatim: a ``mark_reviewed`` carrying it is refused by name."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> drafts.TransitionResult:
        raise drafts.DraftInvalidError(["mark_reviewed: carries mode"])

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(
        TRANSITIONS_URL, json={"action": "mark_reviewed", "mode": "paper"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "mark_reviewed: carries mode"


def test_an_invalid_mode_string_on_the_transitions_body_is_a_named_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args: object, **kwargs: object) -> drafts.TransitionResult:  # pragma: no cover
        raise AssertionError("the service must not be reached with an invalid mode")

    monkeypatch.setattr(drafts, "transition", never)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(TRANSITIONS_URL, json={"action": "export", "mode": "fax"})
    assert response.status_code == 422
    assert response.json()["detail"] == "export_mode_invalid"


def test_the_transitions_export_statement_gate_maps_the_named_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both doors answer the same gate: through the transitions POST the
    statement refusal still maps 409 ``statement_not_confirmed`` (the
    executor's ``DraftExportRefusedError``, mapped in ``_transition_refusal``)."""

    async def refused(
        session: object, store: object, scanner: object, **kwargs: Any
    ) -> drafts.TransitionResult:
        raise drafts.DraftExportRefusedError("statement_not_confirmed")

    monkeypatch.setattr(drafts, "transition", refused)
    client = build_client(ADMIN, ADMIN_CONTEXT)
    response = client.post(TRANSITIONS_URL, json={"action": "export", "mode": "paper"})
    assert response.status_code == 409
    assert response.json()["detail"] == "statement_not_confirmed"


def _approved_row(**overrides: Any) -> Any:
    """The current-version row the transitions fake returns."""
    from app.adapters.persistence.drafts import Draft

    values: dict[str, Any] = {
        "id": DRAFT_ID,
        "version_no": 1,
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "form_type": "nar1",
        "target_date": date(2026, 9, 30),
        "status": DraftStatus.APPROVED.value,
        "resolved_state": {"company": {"br_number": "12345678"}},
        "field_provenance": {},
        "qc_flags": [],
        "filing_metadata": {"return": {"made_up_date": "2026-03-01"}},
        "applied_event_ids": [],
        "form_version": {"specification_no": "2/2025 (May 2025)", "template_sha256": "a" * 64},
        "private_company_statement": True,
        "statement_confirmed_by": USER_ID,
        "statement_confirmed_at": EXPORTED_AT,
        "signature_status": "signature_received",
        "reviewed_by": USER_ID,
        "reviewed_at": EXPORTED_AT,
        "approved_by": USER_ID,
        "approved_at": EXPORTED_AT,
        "rationale": None,
        "comments": None,
        "invalidated_at": None,
        "invalidation_reason": None,
        "created_by": USER_ID,
        "created_at": EXPORTED_AT,
        "updated_at": EXPORTED_AT,
    }
    values.update(overrides)
    return Draft(**values)


# --- The flatten claim (the real fill, the real flattener, pypdf read-back) ---


def _filled_nar1() -> bytes:
    """The release's own output over the pinned template: a fill a human can
    open, sign and return — live widgets, CJK on the overlay."""
    from app.application.drafts import fill_plan_for
    from app.domain.drafts import filing_metadata_template

    resolved_state = {
        "company": {
            "br_number": "76543210",
            "cr_number": "1234567",
            "type": "private",
            "date_of_incorporation": "2010-03-01",
            "anniversary_date": "2026-03-01",
            "status": "active",
            "name": {
                "name_en": "SYNTHETIC HARBOUR TRADING LIMITED",
                "name_zh": "合成海港貿易有限公司",
                "business_name": None,
            },
            "registered_office": {
                "flat": "ROOM 1201, 12/F",
                "building": "HARBOUR CENTRE",
                "street": "25 HARBOUR ROAD",
                "district": "WAN CHAI",
            },
            "business_nature": {
                "business_nature_code": "045",
                "business_nature_description": "IMPORT AND EXPORT TRADE",
            },
            "contact": {"email": "compliance@example.hk", "contact_phone_hk": "2345 6789"},
            "filing": {"mortgages_and_charges_total": "0.00", "member_count": None},
            "records_kept": [],
            "share_capital": {
                "classes": [
                    {
                        "class_name": "ORDINARY",
                        "currency": "HKD",
                        "total_shares": 10000,
                        "total_amount": "10000.00",
                        "total_paid_up": "10000.00",
                    }
                ],
                "total": {
                    "derived": True,
                    "currency": "HKD",
                    "total_shares": 10000,
                    "total_amount": "10000.00",
                    "total_paid_up": "10000.00",
                },
            },
        },
        "officers": [],
        "shareholders": [],
        "changes": [],
    }
    plan = fill_plan_for(
        form_type="nar1",
        resolved_state=resolved_state,
        filing_metadata=filing_metadata_template(made_up_date=date(2026, 3, 1)),
        private_company_statement=None,
    )
    registry = load_form_registry(FormCode.NAR1)
    return fill_template(
        template_path(FormCode.NAR1),
        plan,
        header_fields=page_header_fields(registry),
        sheet_pages=sheet_pages(registry),
    )


def test_the_exported_package_has_no_live_form_fields_and_keeps_its_values() -> None:
    """ADR-0004's "flatten only at the final export step", read back: the
    release's filled PDF carries live AcroForm fields; the exported package
    carries none (``get_fields()`` is empty, the root has no ``/AcroForm``,
    no page annotates a widget), the page count is unchanged, and the drawn
    values — Latin and CJK alike — are what the fill wrote."""
    filled = _filled_nar1()
    filled_reader = PdfReader(BytesIO(filled))
    assert filled_reader.get_fields()  # the control: the release is editable
    page_count = len(filled_reader.pages)

    flat = flatten_filled_pdf(filled)
    reader = PdfReader(BytesIO(flat))
    assert reader.get_fields() in (None, {})
    assert "/AcroForm" not in reader.trailer["/Root"]
    assert len(reader.pages) == page_count
    for page in reader.pages:
        assert "/Widget" not in [
            str(annot.get_object().get("/Subtype")) for annot in (page.get("/Annots") or [])
        ]
    # The values still print: the overlay drew them, the widgets are gone.
    # pdfium's renderer is the check (pypdf's extractor does not descend into
    # the merged overlay XObject — the filler suite's own two-layers rule).
    document = pdfium.PdfDocument(flat)
    rendered = "\n".join(
        document[index].get_textpage().get_text_range() for index in range(len(document))
    )
    assert "76543210" in rendered
    assert "SYNTHETIC HARBOUR TRADING LIMITED" in rendered
    assert "合成海港貿易有限公司" in rendered
