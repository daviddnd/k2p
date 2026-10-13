"""API-layer tests for the records-registry endpoints.

These run without a database or object store: the auth seam, RBAC context,
session, and storage are overridden, and the application services are
monkeypatched, so only fail-closed authentication, the grant-gated
authorization check (``require_company_access``), request validation, and
the typed-error → HTTP mappings are exercised. Real flows against
PostgreSQL live in ``test_documents_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.documents import Document
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.api.documents import get_storage
from app.application import documents, service_renewals
from app.application.company_master import CompanyNotFoundError
from app.domain.documents import UnsupportedContentTypeError
from app.domain.rbac import ActorContext, Permission, Role
from app.domain.service_renewals import ReleaseBlockReason
from app.main import create_app

COMPANY_ID = uuid4()
DOCUMENT_ID = uuid4()
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

UPLOAD_URL = f"/api/v1/companies/{COMPANY_ID}/documents"
SCAN_PENDING_URL = "/api/v1/documents/scan-pending"


class FakeStore:
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


class FakeSession:
    """A session that answers the one call the refusal path makes.

    The download route commits the release gate's refusal entry itself (a
    request that ends in a raised refusal is rolled back at the edge), so the
    session has to be able to commit — and counting the calls is how these
    tests see that it did.
    """

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def stored_document(scan_status: str = "clean") -> Document:
    """One document row as the download route's read returns it."""
    return Document(
        id=DOCUMENT_ID,
        tenant_id=ADMIN.tenant_id,
        company_id=COMPANY_ID,
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="0" * 64,
        storage_key="letters/2027/nar1.eml",
        byte_size=12,
        content_type="message/rfc822",
        scan_status=scan_status,
        uploaded_by=ADMIN.user_id,
        uploaded_at=datetime(2026, 9, 24, 8, 0, tzinfo=UTC),
    )


def _returning(document: Document):
    async def fake(*args: object, **kwargs: object) -> Document:
        return document

    return fake


def allow_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the release gate pass: its own refusals are exercised separately."""

    async def permitted(*args: object, **kwargs: object) -> None: ...

    monkeypatch.setattr(service_renewals, "ensure_release_permitted", permitted)


def build_client(
    actor: ActorRef | None = None,
    context: ActorContext | None = None,
    session: AsyncSession | FakeSession | None = None,
) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context
    app.dependency_overrides[get_storage] = FakeStore

    async def no_session() -> AsyncIterator[AsyncSession | FakeSession | None]:
        yield session  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


# --- Fail closed; all three routes grant-gated ---


def test_unauthenticated_fails_closed_on_all_routes() -> None:
    client = build_client()
    assert client.post(f"{UPLOAD_URL}?kind=prior_filing", content=b"%PDF-").status_code == 401
    assert client.get(UPLOAD_URL).status_code == 401
    assert client.get(f"{UPLOAD_URL}/{uuid4()}/download").status_code == 401


def test_general_without_company_grant_is_forbidden() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITHOUT_GRANT
    )
    assert client.post(f"{UPLOAD_URL}?kind=prior_filing", content=b"%PDF-").status_code == 403
    assert client.get(UPLOAD_URL).status_code == 403
    assert client.get(f"{UPLOAD_URL}/{uuid4()}/download").status_code == 403


# --- Request validation ---


def test_upload_requires_the_kind_query_parameter() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post(UPLOAD_URL, content=b"%PDF-").status_code == 422
    assert client.post(f"{UPLOAD_URL}?kind=not_a_kind", content=b"%PDF-").status_code == 422


def test_upload_over_the_cap_is_413_before_any_service_call() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"{UPLOAD_URL}?kind=prior_filing",
        content=b"%PDF-",
        headers={"Content-Type": "application/pdf", "Content-Length": str(26 * 1024 * 1024)},
    )
    assert response.status_code == 413


# --- Service error mappings ---


def test_upload_maps_domain_validation_errors_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_register(*args: object, **kwargs: object) -> object:
        raise UnsupportedContentTypeError("content type 'text/plain' is not allowed")

    monkeypatch.setattr(documents, "register_upload", fake_register)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"{UPLOAD_URL}?kind=prior_filing", content=b"hello", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]


def test_upload_maps_unknown_company_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_register(*args: object, **kwargs: object) -> object:
        raise CompanyNotFoundError

    monkeypatch.setattr(documents, "register_upload", fake_register)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"{UPLOAD_URL}?kind=prior_filing",
        content=b"%PDF-",
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 404


def test_download_maps_unknown_document_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(*args: object, **kwargs: object) -> object:
        raise documents.DocumentNotFoundError

    monkeypatch.setattr(documents, "get_document", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(f"{UPLOAD_URL}/{uuid4()}/download").status_code == 404


def test_granted_general_staff_can_reach_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "get_document", _returning(stored_document()))
    allow_release(monkeypatch)

    async def fake_issue(*args: object, **kwargs: object) -> documents.DownloadGrant:
        return documents.DownloadGrant(url="https://storage.invalid/x?ttl=900", expires_in=900)

    monkeypatch.setattr(documents, "issue_download_url", fake_issue)
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
    )
    response = client.get(f"{UPLOAD_URL}/{uuid4()}/download")
    assert response.status_code == 200
    assert response.json()["expires_in"] == 900


def test_download_of_a_document_that_is_not_scanned_clean_is_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Availability is decided by the document's own scan status, before the
    release gate is consulted at all — and the gate is not reached, so a
    pending document is never reported as a renewal refusal."""
    monkeypatch.setattr(documents, "get_document", _returning(stored_document("infected")))

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the release gate must not be consulted for an unusable document")

    monkeypatch.setattr(service_renewals, "ensure_release_permitted", never)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(f"{UPLOAD_URL}/{uuid4()}/download")

    assert response.status_code == 409
    assert response.json()["detail"] == "document_not_available: infected"


# --- The client-bound release gate ---


def test_a_blocked_release_answers_the_reason_the_period_and_commits_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's refusal on the wire: 409 with the contract's own detail, and
    the session committed **before** the answer — the entry the application
    wrote is the record of the refusal, and a request that ends in a raised
    refusal is rolled back at the edge (the DB suite asserts the entry survives;
    this asserts the route asks for it).

    The detail is the error's own (``app.application.service_renewals``), which
    is what makes this test worth having: a route that composed its own string
    could answer something the application never said.
    """
    monkeypatch.setattr(documents, "get_document", _returning(stored_document()))
    session = FakeSession()

    async def refused(*args: object, **kwargs: object) -> None:
        raise service_renewals.RenewalGateRefusedError(
            reason=ReleaseBlockReason.OUT_OF_COVERAGE, anniversary_year=2027
        )

    monkeypatch.setattr(service_renewals, "ensure_release_permitted", refused)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT, session=session)

    response = client.get(f"{UPLOAD_URL}/{uuid4()}/download")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "service_not_in_scope: out_of_coverage"
    assert session.commits == 1


def test_the_period_refusals_carry_the_period_and_the_scope_ones_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire shapes asserted side by side: the scope legs name the reason
    (the remedy is about coverage), the two period legs name the year (the
    remedy is a period's record — the answer, or the fee). Both come from the
    error, so a caller cannot get one shape's text under the other's status,
    and the unanswered period cannot arrive as a missing payment.
    """
    monkeypatch.setattr(documents, "get_document", _returning(stored_document()))
    client = build_client(
        actor=ADMIN, context=ADMIN_CONTEXT, session=FakeSession()
    )

    details = {}
    for reason in (
        ReleaseBlockReason.NO_ASSIGNMENT,
        ReleaseBlockReason.ASSIGNMENT_ENDED,
        ReleaseBlockReason.OUT_OF_COVERAGE,
        ReleaseBlockReason.FORM_NOT_COVERED,
        ReleaseBlockReason.CONTINUATION_NOT_RECORDED,
        ReleaseBlockReason.PAYMENT_NOT_RECEIVED,
    ):

        async def refused(*args: object, _reason=reason, **kwargs: object) -> None:
            raise service_renewals.RenewalGateRefusedError(
                reason=_reason, anniversary_year=2027
            )

        monkeypatch.setattr(service_renewals, "ensure_release_permitted", refused)
        response = client.get(f"{UPLOAD_URL}/{uuid4()}/download")
        assert response.status_code == 409, response.text
        details[reason] = response.json()["detail"]

    assert details[ReleaseBlockReason.NO_ASSIGNMENT] == "service_not_in_scope: no_assignment"
    assert details[ReleaseBlockReason.ASSIGNMENT_ENDED] == "service_not_in_scope: assignment_ended"
    assert details[ReleaseBlockReason.OUT_OF_COVERAGE] == "service_not_in_scope: out_of_coverage"
    assert details[ReleaseBlockReason.FORM_NOT_COVERED] == "service_not_in_scope: form_not_covered"
    assert details[ReleaseBlockReason.CONTINUATION_NOT_RECORDED] == (
        "continuation_not_recorded: 2027"
    )
    assert details[ReleaseBlockReason.PAYMENT_NOT_RECEIVED] == "payment_not_received: 2027"


def test_a_permitted_download_issues_a_url_and_commits_nothing_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: the gate passes, the URL is issued, and the route has
    nothing of its own to commit — what the deployment commits at the request
    edge is the session dependency's, and the DB suite is where "no audit entry
    on a permitted download" is asserted against the real chain."""
    monkeypatch.setattr(documents, "get_document", _returning(stored_document()))
    allow_release(monkeypatch)
    session = FakeSession()

    async def fake_issue(*args: object, **kwargs: object) -> documents.DownloadGrant:
        return documents.DownloadGrant(url="https://storage.invalid/y?ttl=900", expires_in=900)

    monkeypatch.setattr(documents, "issue_download_url", fake_issue)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT, session=session)

    response = client.get(f"{UPLOAD_URL}/{uuid4()}/download")

    assert response.status_code == 200, response.text
    assert session.commits == 0


# --- The retrospective scan ---


def test_the_retro_scan_is_administrator_only() -> None:
    assert build_client().post(SCAN_PENDING_URL).status_code == 401
    assert (
        build_client(
            actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_WITH_GRANT
        )
        .post(SCAN_PENDING_URL)
        .status_code
        == 403
    )


def test_the_retro_scan_reports_each_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.application.documents import ScanOutcome, ScanSweep
    from app.domain.documents import ScanStatus

    document_id, company_id = uuid4(), uuid4()

    async def fake_sweep(*args: object, **kwargs: object) -> ScanSweep:
        return ScanSweep(
            outcomes=(
                ScanOutcome(
                    document_id=document_id,
                    company_id=company_id,
                    scan_status=ScanStatus.INFECTED,
                    signature="Eicar-Test-Signature",
                    reason=None,
                ),
            ),
            remaining=2,
        )

    monkeypatch.setattr(documents, "scan_pending_documents", fake_sweep)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = client.post(SCAN_PENDING_URL).json()

    assert body == {
        "scanned": 1,
        "clean": 0,
        "infected": 1,
        "failed": 0,
        "remaining": 2,
        "outcomes": [
            {
                "document_id": str(document_id),
                "company_id": str(company_id),
                "scan_status": "infected",
                "signature": "Eicar-Test-Signature",
                "reason": None,
            }
        ],
    }
