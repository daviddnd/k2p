"""DB-backed pack-cache scenarios (opt-in; needs a migrated PostgreSQL).

The execution plane's half of CP2 (ADR-0012; contract §2.2 ``PackCache``),
against a real database and a faithful fake of the vendor service
(``tests/fake_control_plane.py``, no socket): a refresh that downloads,
verifies, and installs; the refusals that install nothing (a digest that does
not match, a signature this build cannot verify, a version the vendor re-cut,
and the vendor's own refusals); the §4.3 freeze on a lapsed licence with the
frozen content still readable and refreshes refused with a reason; recovery
unfreezing the cache and refresh resuming; and the ungated paths untouched
throughout.

Two things are asserted rather than assumed: a refusal leaves no cached row
behind, and no audit entry ever carries payload bytes or prompt bodies — only
pack type, version, digest, and reason.

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back; the
credential file is per-test (``conftest.py``).
"""

import base64
import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import ControlPlaneClient
from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.entitlement import LeaseCache
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.packs import PackCache
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.application import lease
from app.core.config import settings
from app.domain.activation import ActivationCredential, ActivationState
from app.domain.audit import AuditAction
from app.main import create_app
from tests.fake_control_plane import FakeControlPlane, UnreachableTransport

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

CONTROL_PLANE_URL = "http://control-plane.test"
TOKEN = "pack-deployment-credential"
ENTITLEMENT_URL = "/api/v1/entitlement"
PACKS_URL = "/api/v1/packs"
REFRESH_URL = "/api/v1/packs/refresh"

# Text that must never reach the audit chain: a pack payload and a prompt body
# are licensed content, and the audit records versions and digests only.
PROMPT_TEXT = "PROMPT-TEXT-THAT-MUST-NOT-BE-AUDITED"

FORM_MAPPING_PAYLOAD: dict[str, Any] = {
    "forms": {
        "nar1": {
            "manifest": {
                "form_code": "NAR1",
                "specification_no": "2/2025",
                "template": "nar1-2-2025.pdf",
                "version": {"sha256": "a" * 64},
            },
            "mapping": {"items": []},
            "geometry": {"fields": []},
        }
    }
}
PROMPT_PACK_PAYLOAD: dict[str, Any] = {
    "prompts": [
        {
            "name": "reply_parser",
            "language": "en",
            "version": 1,
            "body": PROMPT_TEXT,
            "sha256": hashlib.sha256(PROMPT_TEXT.encode("utf-8")).hexdigest(),
        }
    ]
}
DEADLINE_RULES_PAYLOAD: dict[str, Any] = {
    "rules": [{"code": "nar1_due", "offset_days": 42}]
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


# --- Harness --------------------------------------------------------------


def make_client(session: AsyncSession, actor: ActorRef | None = None) -> TestClient:
    """The real app with only the auth seam and session overridden."""
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def use_control_plane(monkeypatch: pytest.MonkeyPatch, control_plane: FakeControlPlane) -> None:
    """Point the pack transport's client seam at the fake vendor service."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL,
            timeout=1.0,
            transport=httpx.ASGITransport(app=control_plane.app),
        ),
    )


def use_down_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every call to the vendor service fail like a dead host."""
    monkeypatch.setattr(settings, "cp_base_url", CONTROL_PLANE_URL)
    monkeypatch.setattr(
        lease,
        "client",
        lambda: ControlPlaneClient(
            base_url=CONTROL_PLANE_URL, timeout=0.1, transport=UnreachableTransport()
        ),
    )


async def seed_tenant_with_admin(session: AsyncSession) -> tuple[Tenant, User]:
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


async def seed_general_user(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Staff",
        role="general",
    )
    session.add(user)
    await session.flush()
    return user


def credential_file() -> Path:
    return settings.cp_activation_token_file


def activate(
    session: AsyncSession,
    control_plane: FakeControlPlane,
    *,
    tier: str | None = "standard",
    expires_in: timedelta = timedelta(days=30),
) -> None:
    """Leave the deployment as a completed activation does: credential + lease.

    The 0600 file and the cached lease are CP1b's own shapes (its suite covers
    how they get written); this one starts from them so its subject — the pack
    cache — is what it exercises.
    """
    now = datetime.now(UTC)
    control_plane.tokens[TOKEN] = now + timedelta(days=90)
    ActivationStore(credential_file()).save(
        ActivationState(
            deployment_fingerprint="pack-cache-test-deployment",
            credential=ActivationCredential(
                token=TOKEN,
                token_id=uuid4(),
                control_plane_tenant_id=control_plane.tenant_id,
                expires_at=now + timedelta(days=90),
            ),
        )
    )
    if tier is not None:
        session.add(
            LeaseCache(
                control_plane_tenant_id=control_plane.tenant_id,
                tier=tier,
                license_expires_at=now + expires_in,
                entitlement_version=1,
                fetched_at=now,
                last_attempt_at=now,
            )
        )


async def expire_lease(session: AsyncSession, *, days_ago: int) -> None:
    """Move the cached lease into (or past) the §4.3 grace window."""
    row = await session.scalar(select(LeaseCache).limit(1))
    assert row is not None
    row.license_expires_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()


async def cache_rows(session: AsyncSession) -> list[PackCache]:
    rows = await session.scalars(
        select(PackCache).order_by(PackCache.pack_type, PackCache.version)
    )
    return list(rows.all())


async def audit_rows(session: AsyncSession) -> list[AuditLog]:
    rows = await session.scalars(select(AuditLog).order_by(AuditLog.seq))
    return list(rows.all())


async def audit_entries(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    return [row for row in await audit_rows(session) if row.action == action.value]


def refresh(client: TestClient):
    return client.post(REFRESH_URL)


def published(control_plane: FakeControlPlane) -> None:
    """Fill the fake's catalog: two types published, two never shipped."""
    control_plane.publish_pack("form_mapping", FORM_MAPPING_PAYLOAD)
    control_plane.publish_pack("prompt_pack", PROMPT_PACK_PAYLOAD)


def outcomes(response: httpx.Response) -> dict[str, dict[str, Any]]:
    return {outcome["pack_type"]: outcome for outcome in response.json()["outcomes"]}


# --- Download, verify, install -------------------------------------------


async def test_a_refresh_downloads_verifies_and_installs(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = refresh(client)

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["refreshed"] is True
    assert report["reason"] is None
    by_type = outcomes(response)
    assert by_type["form_mapping"]["action"] == "installed"
    assert by_type["prompt_pack"]["action"] == "installed"
    assert by_type["prompt_pack"]["version"] == 1
    assert len(by_type["prompt_pack"]["sha256"]) == 64
    # A type the vendor has never published is not a failure.
    assert by_type["deadline_rules"]["action"] == "not_published"
    assert by_type["fee_schedule"]["action"] == "not_published"
    assert by_type["deadline_rules"]["reason"] is None

    # What was cached carries the digest it was verified under.
    rows = {row.pack_type: row for row in await cache_rows(session)}
    assert set(rows) == {"form_mapping", "prompt_pack"}
    assert rows["prompt_pack"].payload == PROMPT_PACK_PAYLOAD
    assert rows["form_mapping"].frozen is False and rows["form_mapping"].frozen_at is None
    assert rows["form_mapping"].downloaded_at is not None

    # The read surface names them, verifies each stored copy, and returns no
    # payload at all.
    listing = client.get(PACKS_URL).json()
    assert listing["license_state"] == "active"
    assert listing["frozen"] is False
    assert {entry["pack_type"] for entry in listing["packs"]} == {"form_mapping", "prompt_pack"}
    assert all(entry["payload_verified"] is True for entry in listing["packs"])
    # The read names what is cached; it never serves a payload.
    assert all(
        set(entry)
        == {
            "pack_type",
            "version",
            "sha256",
            "downloaded_at",
            "frozen",
            "frozen_at",
            "payload_verified",
        }
        for entry in listing["packs"]
    )

    # Every install is audited with its version and digest — and with nothing
    # else: no payload bytes, no prompt body.
    installs = await audit_entries(session, AuditAction.PACK_INSTALLED)
    assert [entry.details["pack_type"] for entry in installs] == ["form_mapping", "prompt_pack"]
    assert all(
        entry.details["sha256"] == rows[entry.details["pack_type"]].sha256 for entry in installs
    )
    assert all(entry.details["signature"] is False for entry in installs)
    chain = json.dumps([entry.details for entry in await audit_rows(session)])
    assert PROMPT_TEXT not in chain
    assert "nar1-2-2025.pdf" not in chain

    # A second refresh re-downloads, sees the same digests, and installs
    # nothing: an unchanged catalog is not an event.
    again = refresh(client)
    assert again.json()["refreshed"] is False
    assert {action["action"] for action in again.json()["outcomes"]} != {"installed"}
    assert len(await audit_entries(session, AuditAction.PACK_INSTALLED)) == 2


async def test_a_digest_that_does_not_match_refuses_to_install(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.publish_pack("form_mapping", FORM_MAPPING_PAYLOAD)
    control_plane.pack_digest_overrides["form_mapping"] = "0" * 64
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = refresh(client)

    assert response.status_code == 200, response.text
    outcome = outcomes(response)["form_mapping"]
    assert outcome["action"] == "refused"
    assert outcome["reason"] == "sha256_mismatch"
    assert outcome["version"] == 1
    assert outcome["sha256"] == "0" * 64
    assert await cache_rows(session) == []

    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert len(refusals) == 1
    assert refusals[0].details == {
        "reason": "sha256_mismatch",
        "pack_type": "form_mapping",
        "version": 1,
        "sha256": "0" * 64,
    }
    assert (await audit_entries(session, AuditAction.PACK_INSTALLED)) == []


async def test_a_signed_release_is_refused_rather_than_trusted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed posture, unchanged: no pinned key, a signature, a refusal.

    This deployment has not pinned the vendor's public key, so a release
    that carries a signature installs nothing — the digest matching is not
    enough once authorship has been claimed and cannot be checked here.
    """
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.publish_pack(
        "form_mapping", FORM_MAPPING_PAYLOAD, signature="c2lnbmF0dXJl" * 8
    )
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    outcome = outcomes(refresh(client))["form_mapping"]

    assert outcome["action"] == "refused"
    assert outcome["reason"] == "signature_unverifiable"
    assert await cache_rows(session) == []
    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert [entry.details["reason"] for entry in refusals] == ["signature_unverifiable"]


def _vendor_signs(payload: dict[str, Any], private: Ed25519PrivateKey) -> str:
    """The control plane's signature over a release digest, built without
    the vendor module: canonical JSON, SHA-256, Ed25519, base64."""
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return base64.b64encode(private.sign(digest.encode("ascii"))).decode("ascii")


def _pin_vendor_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private: Ed25519PrivateKey
) -> Path:
    """Point ``pack_vendor_public_key_file`` at the paired public key PEM."""
    key_file = tmp_path / "vendor-public-key.pem"
    key_file.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(settings, "pack_vendor_public_key_file", str(key_file))
    return key_file


async def test_a_signed_release_with_the_pinned_vendor_key_installs(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CP2 signing seam end to end at this seam: sign → pin → verify → install."""
    vendor = Ed25519PrivateKey.generate()
    _pin_vendor_key(monkeypatch, tmp_path, vendor)
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.publish_pack(
        "form_mapping", FORM_MAPPING_PAYLOAD, signature=_vendor_signs(FORM_MAPPING_PAYLOAD, vendor)
    )
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    outcome = outcomes(refresh(client))["form_mapping"]

    assert outcome["action"] == "installed"
    assert await cache_rows(session) != []
    (install,) = await audit_entries(session, AuditAction.PACK_INSTALLED)
    assert install.details["signature"] is True


async def test_a_signed_release_from_the_wrong_key_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pinned key is the trust anchor: another key's signature installs nothing."""
    pinned = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()
    _pin_vendor_key(monkeypatch, tmp_path, pinned)
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.publish_pack(
        "form_mapping",
        FORM_MAPPING_PAYLOAD,
        signature=_vendor_signs(FORM_MAPPING_PAYLOAD, stranger),
    )
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    outcome = outcomes(refresh(client))["form_mapping"]

    assert outcome["action"] == "refused"
    assert outcome["reason"] == "signature_unverifiable"
    assert await cache_rows(session) == []
    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert [entry.details["reason"] for entry in refusals] == ["signature_unverifiable"]


async def test_a_version_the_vendor_recut_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    control_plane.publish_pack("form_mapping", FORM_MAPPING_PAYLOAD)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    refresh(client)

    # Same version, different payload: the vendor re-cut a release. The
    # cached bytes must not move under a version this deployment has read.
    control_plane.publish_pack("form_mapping", {"forms": {}, "notes": "re-cut"})
    outcome = outcomes(refresh(client))["form_mapping"]

    assert outcome["action"] == "refused"
    assert outcome["reason"] == "version_conflict"
    (cached,) = await cache_rows(session)
    assert cached.payload == FORM_MAPPING_PAYLOAD


async def test_a_vendor_refusal_is_reported_per_type(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 from the vendor (Free tier there) is a reason, not a crash."""
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(packs_status=403)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = refresh(client)

    assert response.status_code == 200, response.text
    assert response.json()["refreshed"] is False
    assert {outcome["action"] for outcome in response.json()["outcomes"]} == {"refused"}
    assert {outcome["reason"] for outcome in response.json()["outcomes"]} == {
        "control_plane_refused:entitlement_denied:tier_not_licensed"
    }
    assert await cache_rows(session) == []
    assert len(await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)) == 4


async def test_an_unreachable_control_plane_does_not_fail_the_route(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    activate(session, control_plane)
    use_down_control_plane(monkeypatch)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = refresh(client)

    assert response.status_code == 200, response.text
    assert response.json()["refreshed"] is False
    assert response.json()["reason"] == "control_plane_unreachable"
    # Nothing was attempted after the first failure, and the report says so
    # rather than implying the other types were checked.
    assert {outcome["action"] for outcome in response.json()["outcomes"]} == {"not_attempted"}
    assert await cache_rows(session) == []


async def test_a_refresh_without_activation_reports_why(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A licensed-but-unactivated deployment has no vendor to ask.

    Configuration is the entitlement source before activation, and it says
    Standard here: there is nothing wrong and nothing to refuse — the cycle
    reports ``not_activated`` without inventing a catalog.
    """
    tenant, admin = await seed_tenant_with_admin(session)
    monkeypatch.setattr(settings, "license_tier", "standard")
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    response = refresh(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"refreshed": False, "reason": "not_activated", "outcomes": []}
    assert control_plane.pack_fetches == []


async def test_a_free_tier_is_refused_before_the_vendor_is_asked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Free tenant's refusal is its own: licensed packs were never in its tier."""
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane, tier="free")
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    refused = refresh(client)

    assert refused.status_code == 403, refused.text
    assert refused.json() == {"code": "packs_not_licensed", "reason": "tier_not_licensed"}
    assert control_plane.pack_fetches == []
    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert [entry.details["reason"] for entry in refusals] == ["tier_not_licensed"]


# --- The §4.3 freeze -----------------------------------------------------


async def test_a_lapsed_licence_freezes_the_cache_and_refuses_refreshes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    refresh(client)
    assert len(await cache_rows(session)) == 2

    # The lease runs out and ages past the 7-day grace window: §4.3 free.
    await expire_lease(session, days_ago=8)
    view = client.get(ENTITLEMENT_URL).json()
    assert view["license_state"] == "free"

    # The freeze rides on the entitlement read, not on the pack routes: it was
    # applied by a plain licence read above.
    frozen = await cache_rows(session)
    assert [row.frozen for row in frozen] == [True, True]
    assert all(row.frozen_at is not None for row in frozen)
    change = await audit_entries(session, AuditAction.PACKS_FROZEN)
    assert len(change) == 1
    assert change[0].actor_user_id is None  # a consequence of the licence
    assert {entry["pack_type"] for entry in change[0].details["packs"]} == {
        "form_mapping",
        "prompt_pack",
    }
    assert all(len(entry["sha256"]) == 64 for entry in change[0].details["packs"])

    # Refresh is refused with a reason, and the refusal is audited even though
    # the route answers 403 (the entry commits with the response).
    refused = refresh(client)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"code": "packs_not_licensed", "reason": "license_state_free"}
    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert [entry.details["reason"] for entry in refusals] == ["license_state_free"]
    assert refusals[0].details["pack_type"] is None
    # The vendor was not asked anything.
    assert control_plane.pack_fetches == [
        ("form_mapping", None),
        ("deadline_rules", None),
        ("fee_schedule", None),
        ("prompt_pack", None),
    ]

    # Frozen content stays readable: still listed, still verifiable, still in
    # the database — ADR-0012's promise that nothing local is taken away.
    listing = client.get(PACKS_URL).json()
    assert listing["frozen"] is True
    assert listing["license_state"] == "free"
    assert all(entry["frozen"] is True for entry in listing["packs"])
    assert all(entry["payload_verified"] is True for entry in listing["packs"])
    assert len(await cache_rows(session)) == 2

    # The ungated paths never depended on any of this.
    assert client.get("/api/v1/companies").status_code == 200


async def test_recovery_unfreezes_and_refresh_resumes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    refresh(client)
    await expire_lease(session, days_ago=8)
    assert client.get(ENTITLEMENT_URL).json()["license_state"] == "free"
    assert refresh(client).status_code == 403

    # The subscription is restored and the deployment reads the lease again.
    lease_row = await session.scalar(select(LeaseCache).limit(1))
    assert lease_row is not None
    lease_row.license_expires_at = datetime.now(UTC) + timedelta(days=30)
    await session.flush()

    recovered = client.get(ENTITLEMENT_URL).json()
    assert recovered["license_state"] == "active"
    rows = await cache_rows(session)
    assert [row.frozen for row in rows] == [False, False]
    assert all(row.frozen_at is None for row in rows)
    unfrozen = await audit_entries(session, AuditAction.PACKS_UNFROZEN)
    assert len(unfrozen) == 1

    # Refresh works again, and installs what moved while the cache was frozen.
    control_plane.publish_pack(
        "form_mapping", {**FORM_MAPPING_PAYLOAD, "specification": "3/2025"}, version=2
    )
    response = refresh(client)
    assert response.status_code == 200, response.text
    assert outcomes(response)["form_mapping"]["action"] == "installed"
    assert outcomes(response)["form_mapping"]["version"] == 2
    versions = [row.version for row in await cache_rows(session) if row.pack_type == "form_mapping"]
    assert versions == [1, 2]  # the old version is kept, never replaced


async def test_a_freeze_happens_once_per_state_change(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated reads at the same licence state write nothing."""
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    refresh(client)
    await expire_lease(session, days_ago=8)

    for _ in range(3):
        assert client.get(ENTITLEMENT_URL).json()["license_state"] == "free"

    assert len(await audit_entries(session, AuditAction.PACKS_FROZEN)) == 1


# --- Authorization and the deployment's own shape ------------------------


async def test_the_pack_routes_are_administrator_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is deployment-level, like the lease: the role gate is the rule.

    There is no per-company grant to scope — a licensed pack belongs to the
    deployment's licence, not to a client — so a General account is refused
    outright rather than filtered.
    """
    tenant, _ = await seed_tenant_with_admin(session)
    staff = await seed_general_user(session, tenant)
    control_plane = FakeControlPlane()
    published(control_plane)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=staff.id, tenant_id=tenant.id))

    assert client.get(PACKS_URL).status_code == 403
    assert refresh(client).status_code == 403
    assert await cache_rows(session) == []


async def test_a_refusal_leaves_nothing_cached_but_the_audit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one invariant every refusal shares, asserted across all of them."""
    tenant, admin = await seed_tenant_with_admin(session)
    control_plane = FakeControlPlane(packs_status=403)
    use_control_plane(monkeypatch, control_plane)
    activate(session, control_plane)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))

    refresh(client)

    assert await cache_rows(session) == []
    refusals = await audit_entries(session, AuditAction.PACK_REFRESH_REFUSED)
    assert len(refusals) == 4
    # Reasons and types only: a refused download never reached a payload.
    for entry in refusals:
        assert set(entry.details) == {"reason", "pack_type", "version", "sha256"}
