"""API-layer tests for the calendar-push routes (slice M8; contract §5.1.5's
extension; rulings R4-R6).

Without a database: the auth seam, the RBAC context and the session are
overridden; the provider adapter seam (``calendar_sync.build_provider``) is
stubbed the way the reminder route tests stub the sweep, so what runs is the
fail-closed authentication, the Administrator-only checks, the credential
shape validation, the probe's per-leg report mapping, the push-status mapping
with ``probe_passed`` surfaced beside the mode (nothing user-visible
overclaims an unprobed credential), the static guide read, and the audit
contract — create and revoke append their entries through
``application.audit.record_event`` naming the credential id, read and probe
append nothing, and no response ever carries the client secret or the
refresh token.

The credential file itself is real: the autouse custody fixture points the
store at a per-test directory, so the create/probe/revoke walks exercise the
ADR-0014 file store end to end with no provider on the wire.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.calendar.credential_store import CalendarCredentialStore
from app.adapters.calendar.probe import (
    CALENDAR_LIST,
    PROBE_DELETE,
    PROBE_WRITE,
    TOKEN_REFRESH,
    ProbeLeg,
    ProbeReport,
)
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import calendar_sync
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.calendar_push import CalendarProviderName, SyncMode
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

CREDENTIALS_URL = "/api/v1/calendar/provider-credentials"
PROBE_URL = "/api/v1/calendar/probe"
PUSH_STATUS_URL = "/api/v1/calendar/push-status"
GUIDE_URL = "/api/v1/calendar/onboarding-guide"

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
SECRET = "test-client-secret-never-on-the-wire"
REFRESH_TOKEN = "test-refresh-token-never-on-the-wire"

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),  # even holding every grantable permission
    company_ids=frozenset(),
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

PUSH_ROUTES = [
    ("POST", CREDENTIALS_URL),
    ("GET", CREDENTIALS_URL),
    ("DELETE", CREDENTIALS_URL),
    ("POST", PROBE_URL),
    ("GET", PUSH_STATUS_URL),
    ("GET", GUIDE_URL),
]

GOOGLE_PAYLOAD = {
    "provider": "google",
    "client_id": "google-client-id",
    "client_secret": SECRET,
    "refresh_token": REFRESH_TOKEN,
    "calendar_id": "primary",
}
MICROSOFT_PAYLOAD = {
    "provider": "microsoft",
    "client_id": "microsoft-client-id",
    "client_secret": SECRET,
    "refresh_token": REFRESH_TOKEN,
    "calendar_id": "calendar-1",
    "directory_tenant_id": "directory-tenant-1",
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


def store() -> CalendarCredentialStore:
    return CalendarCredentialStore(settings.calendar_credential_dir)


class FakeProvider:
    """The adapter seam: answers the probe, never touches a network."""

    def __init__(self, report: ProbeReport) -> None:
        self._report = report

    async def probe(self) -> ProbeReport:
        return self._report


def passed_report() -> ProbeReport:
    legs = (TOKEN_REFRESH, CALENDAR_LIST, PROBE_WRITE, PROBE_DELETE)
    return ProbeReport(legs=tuple(ProbeLeg(name=name, ok=True) for name in legs))


def failed_report() -> ProbeReport:
    """The refresh leg failed: everything after it is reported as skipped."""
    return ProbeReport(
        legs=(
            ProbeLeg(TOKEN_REFRESH, ok=False, detail="invalid_grant"),
            ProbeLeg(CALENDAR_LIST, ok=False, detail="skipped"),
            ProbeLeg(PROBE_WRITE, ok=False, detail="skipped"),
            ProbeLeg(PROBE_DELETE, ok=False, detail="skipped"),
        )
    )


@pytest.fixture
def audit_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Captures record_event's keyword arguments; the session is never used."""
    captured: list[dict[str, object]] = []

    async def fake_record_event(session: object, **kwargs: object) -> object:
        captured.append(kwargs)
        return None

    monkeypatch.setattr(calendar_sync.audit, "record_event", fake_record_event)
    return captured


def credential_view(**overrides: object) -> calendar_sync.ProviderCredentialView:
    values: dict[str, object] = {
        "credential_id": uuid4(),
        "provider": CalendarProviderName.GOOGLE,
        "calendar_id": "primary",
        "probe_passed": False,
        "created_at": datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return calendar_sync.ProviderCredentialView(**values)  # type: ignore[arg-type]


# --- Fail closed ---


def test_the_push_routes_need_a_session() -> None:
    client = build_client()
    for method, url in PUSH_ROUTES:
        assert client.request(method, url).status_code == 401, url


@pytest.mark.parametrize("context", [GENERAL_CONTEXT, INACTIVE_ADMIN_CONTEXT])
def test_the_push_routes_are_administrator_only(context: ActorContext) -> None:
    client = build_client(actor=ADMIN, context=context)
    for method, url in PUSH_ROUTES:
        assert client.request(method, url).status_code == 403, url


# --- The credential lifecycle ---


def test_create_stores_the_credential_and_returns_metadata_only(
    audit_spy: list[dict[str, object]],
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(CREDENTIALS_URL, json=GOOGLE_PAYLOAD)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["connected"] is True
    assert body["provider"] == "google"
    assert body["calendar_id"] == "primary"
    assert body["probe_passed"] is False
    assert body["created_at"] == body["updated_at"]
    # The file is the credential: a read back sees exactly what was stored.
    stored = store().load(ADMIN.tenant_id)
    assert stored is not None
    assert stored.provider is CalendarProviderName.GOOGLE
    assert stored.probe_passed is False
    # Custody is audited: the entry names the credential id and provider
    # metadata, never the tokens.
    assert len(audit_spy) == 1
    (entry,) = audit_spy
    assert entry["action"] is AuditAction.CALENDAR_PROVIDER_CONNECTED
    assert entry["entity_type"] == "calendar_provider_credential"
    assert entry["entity_id"] == UUID(body["credential_id"])
    assert entry["details"] == {"provider": "google", "calendar_id": "primary"}
    # No secret anywhere on the wire.
    assert SECRET not in response.text
    assert REFRESH_TOKEN not in response.text


def test_create_validates_the_shape() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    malformed = [
        {**GOOGLE_PAYLOAD, "provider": "dropbox"},
        {**GOOGLE_PAYLOAD, "client_id": ""},
        {**GOOGLE_PAYLOAD, "refresh_token": ""},
        {**MICROSOFT_PAYLOAD, "directory_tenant_id": None},
        {**GOOGLE_PAYLOAD, "directory_tenant_id": "directory-tenant-1"},
        {**GOOGLE_PAYLOAD, "calendar_id": ""},
    ]
    for payload in malformed:
        response = client.post(CREDENTIALS_URL, json=payload)
        assert response.status_code == 422, payload
    # Nothing malformed was ever stored.
    assert store().load(ADMIN.tenant_id) is None


def test_the_read_returns_metadata_only_and_audits_nothing(
    audit_spy: list[dict[str, object]],
) -> None:
    store().save(
        ADMIN.tenant_id,
        calendar_sync.ProviderCredential(
            provider=CalendarProviderName.MICROSOFT,
            client_id="microsoft-client-id",
            client_secret=SECRET,
            refresh_token=REFRESH_TOKEN,
            calendar_id="calendar-1",
            directory_tenant_id="directory-tenant-1",
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(CREDENTIALS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is True
    assert body["provider"] == "microsoft"
    assert body["calendar_id"] == "calendar-1"
    assert set(body) == {
        "connected",
        "credential_id",
        "provider",
        "calendar_id",
        "probe_passed",
        "created_at",
        "updated_at",
    }
    assert SECRET not in response.text
    assert REFRESH_TOKEN not in response.text
    # A read is not a custody mutation: no audit entry.
    assert audit_spy == []


def test_the_read_without_a_credential_reports_disconnected(
    audit_spy: list[dict[str, object]],
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(CREDENTIALS_URL)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "connected": False,
        "credential_id": None,
        "provider": None,
        "calendar_id": None,
        "probe_passed": False,
        "created_at": None,
        "updated_at": None,
    }
    assert audit_spy == []


def test_revoke_deletes_the_file_and_audits_the_disconnect(
    audit_spy: list[dict[str, object]],
) -> None:
    stored = store().save(
        ADMIN.tenant_id,
        calendar_sync.ProviderCredential(
            provider=CalendarProviderName.GOOGLE,
            client_id="google-client-id",
            client_secret=SECRET,
            refresh_token=REFRESH_TOKEN,
            calendar_id="primary",
        ),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.request("DELETE", CREDENTIALS_URL)

    assert response.status_code == 200, response.text
    assert response.json()["connected"] is False
    assert store().load(ADMIN.tenant_id) is None
    assert len(audit_spy) == 1
    (entry,) = audit_spy
    assert entry["action"] is AuditAction.CALENDAR_PROVIDER_DISCONNECTED
    assert entry["entity_type"] == "calendar_provider_credential"
    assert entry["entity_id"] == stored.credential_id
    assert SECRET not in str(entry["details"])
    assert REFRESH_TOKEN not in str(entry["details"])


def test_revoke_without_a_credential_is_a_silent_noop(
    audit_spy: list[dict[str, object]],
) -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.request("DELETE", CREDENTIALS_URL)

    assert response.status_code == 200, response.text
    assert response.json()["connected"] is False
    assert audit_spy == []


# --- The probe (ruling R5) ---


def test_the_probe_runs_every_leg_and_marks_the_credential(
    monkeypatch: pytest.MonkeyPatch,
    audit_spy: list[dict[str, object]],
) -> None:
    store().save(
        ADMIN.tenant_id,
        calendar_sync.ProviderCredential(
            provider=CalendarProviderName.GOOGLE,
            client_id="google-client-id",
            client_secret=SECRET,
            refresh_token=REFRESH_TOKEN,
            calendar_id="primary",
        ),
    )
    fake = FakeProvider(passed_report())
    monkeypatch.setattr(calendar_sync, "build_provider", lambda credential: fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(PROBE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["passed"] is True
    assert [(leg["name"], leg["ok"], leg["detail"]) for leg in body["legs"]] == [
        (TOKEN_REFRESH, True, None),
        (CALENDAR_LIST, True, None),
        (PROBE_WRITE, True, None),
        (PROBE_DELETE, True, None),
    ]
    # The flag moved: the file now records that the probe passed.
    stored = store().load(ADMIN.tenant_id)
    assert stored is not None
    assert stored.probe_passed is True
    # The probe is not a custody mutation and never echoes the token.
    assert audit_spy == []
    assert REFRESH_TOKEN not in response.text


def test_a_failed_leg_leaves_the_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store().save(
        ADMIN.tenant_id,
        calendar_sync.ProviderCredential(
            provider=CalendarProviderName.MICROSOFT,
            client_id="microsoft-client-id",
            client_secret=SECRET,
            refresh_token=REFRESH_TOKEN,
            calendar_id="calendar-1",
            directory_tenant_id="directory-tenant-1",
            probe_passed=True,  # a credential that probed green once before
        ),
    )
    fake = FakeProvider(failed_report())
    monkeypatch.setattr(calendar_sync, "build_provider", lambda credential: fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(PROBE_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["passed"] is False
    assert body["legs"][0] == {"name": TOKEN_REFRESH, "ok": False, "detail": "invalid_grant"}
    # Legs after the failed one name what the probe did not do.
    assert [leg["detail"] for leg in body["legs"][1:]] == ["skipped", "skipped", "skipped"]
    stored = store().load(ADMIN.tenant_id)
    assert stored is not None
    assert stored.probe_passed is False


def test_the_probe_refuses_while_no_credential_is_connected() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(PROBE_URL)

    assert response.status_code == 409
    assert response.json()["detail"] == "not_connected"


# --- The push-status read ---


def test_push_status_surfaces_probe_passed_beside_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covered, denied = uuid4(), uuid4()
    status = calendar_sync.TenantPushStatus(
        credential=credential_view(provider=CalendarProviderName.GOOGLE),
        mode=SyncMode.PUSH,
        probe_passed=False,  # an unprobed credential must not read as "pushing"
        companies=(
            calendar_sync.CompanyPushState(company_id=covered, state="feed", reason=None),
            calendar_sync.CompanyPushState(
                company_id=denied, state="refused", reason="service_not_included"
            ),
        ),
    )

    async def fake_status(
        session: AsyncSession, *, tenant_id: object
    ) -> calendar_sync.TenantPushStatus:
        return status

    monkeypatch.setattr(calendar_sync, "tenant_push_status", fake_status)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(PUSH_STATUS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "push"
    assert body["probe_passed"] is False  # next to the mode, not hidden by it
    assert body["credential"]["provider"] == "google"
    (covered_item,) = [item for item in body["companies"] if item["company_id"] == str(covered)]
    assert covered_item == {"company_id": str(covered), "state": "feed", "reason": None}
    (denied_item,) = [item for item in body["companies"] if item["company_id"] == str(denied)]
    assert denied_item == {
        "company_id": str(denied),
        "state": "refused",
        "reason": "service_not_included",
    }


# --- The static onboarding guide (ruling R6) ---


def test_the_guides_are_static_and_name_the_registration_steps() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    microsoft = client.get(GUIDE_URL, params={"provider": "microsoft"})
    assert microsoft.status_code == 200, microsoft.text
    body = microsoft.json()
    assert body["provider"] == "microsoft"
    for phrase in (
        "Entra",
        "client secret",
        "Calendars.ReadWrite",
        "refresh token",
        "calendar id",
        "probe",
    ):
        assert phrase in body["guide"]

    google = client.get(GUIDE_URL, params={"provider": "google"})
    assert google.status_code == 200, google.text
    body = google.json()
    assert body["provider"] == "google"
    for phrase in (
        "Google Cloud",
        "Google Calendar API",
        "OAuth",
        "refresh token",
        "calendar ID",
        "probe",
    ):
        assert phrase in body["guide"]


def test_an_unknown_provider_is_a_422() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.get(GUIDE_URL, params={"provider": "dropbox"})

    assert response.status_code == 422
