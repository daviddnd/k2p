"""DB-backed records-registry scenarios (opt-in; needs a migrated PostgreSQL).

These verify migration ``0009_documents`` and the document services /
endpoints against a real database with an in-memory ``DocumentStore`` fake
(the boto3 adapter is covered separately by the opt-in MinIO round trip in
``test_s3_storage_scenarios.py``): the ingestion order and the scan outcome
it registers, naming-template key rendering, key-collision suffixing,
retention-class derivation, cross-tenant invisibility, audit emission, the
sensitive-class download rule, and the ADR-0009 availability rule that stops
a document which is not scanned clean from being served. Skipped unless
``AICS_TEST_DATABASE_URL`` points at a database migrated to head.

Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import Tenant, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import documents, tenant_config
from app.application.company_master import CompanyNotFoundError
from app.domain.audit import AuditAction
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
    ScanStatus,
)
from app.domain.milestones import default_milestones
from app.main import create_app
from tests.fake_scanning import CLEAN, FakeScanner, failed, infected

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

PDF = b"%PDF-1.7 fake filing body"
PNG = b"\x89PNG\r\n\x1a\n fake director id scan"
COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2026-03-01",
    "type": "private",
}


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes, deterministic URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test."""
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        await connection.rollback()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def make_client(session: AsyncSession, store: FakeStore, actor: ActorRef) -> TestClient:
    """The real app with the auth seam, session, and storage overridden."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store

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


async def make_company(
    session: AsyncSession, tenant: Tenant, admin: User
) -> str:
    """One company through the real endpoint; returns its id."""
    client = make_client(session, FakeStore(), ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post("/api/v1/companies", json=COMPANY)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(
    client: TestClient, company_id: str, query: str = "kind=prior_filing&form=NAR1"
) -> dict:
    response = client.post(
        f"/api/v1/companies/{company_id}/documents?{query}",
        content=PDF,
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def audit_actions(session: AsyncSession, tenant: Tenant) -> list[str]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.seq)
    )
    return [row.action for row in rows.all()]


# --- Ingestion ---


async def test_upload_registers_row_stores_blob_and_audits(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)

    body = upload(client, company_id)

    today = date.today().strftime("%Y%m%d")
    assert body["kind"] == "prior_filing"
    assert body["origin"] == "upload"
    assert body["retention_class"] == "statutory_filing"  # derived from the kind
    # The scan runs before anything is written, so the row registers carrying
    # its verdict — and only a clean verdict makes the document usable.
    assert body["scan_status"] == "clean"
    assert body["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert body["byte_size"] == len(PDF)
    # Default document template: {CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}
    assert body["storage_key"] == f"1234567/{date.today().year}/NAR1_original_1234567_{today}.pdf"
    assert store.objects[body["storage_key"]] == PDF

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == AuditAction.DOCUMENT_REGISTERED.value,
                )
            )
        ).all()
    )
    assert len(entries) == 1
    assert entries[0].details["scan_status"] == "clean"


async def test_a_png_director_id_lands_under_a_key_that_says_png(
    session: AsyncSession, store: FakeStore
) -> None:
    """The CDD ID scan is a PNG, so its key must not claim to be a PDF.

    The storage key is the operator's own view of the object store when they
    read it independently (ADR-0013), so a wrong extension there misnames the
    file type in the record — worst of all in the sensitive class.
    """
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)

    response = client.post(
        f"/api/v1/companies/{company_id}/documents"
        "?kind=client_document&retention_class=cdd_id_document",
        content=PNG,
        headers={"Content-Type": "image/png"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    today = date.today().strftime("%Y%m%d")
    assert body["content_type"] == "image/png"
    assert body["storage_key"] == f"1234567/{date.today().year}/DOC_original_1234567_{today}.png"
    assert store.objects[body["storage_key"]] == PNG


async def test_client_document_requires_an_explicit_retention_class(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)

    url = f"/api/v1/companies/{company_id}/documents?kind=client_document"
    missing = client.post(url, content=PDF, headers={"Content-Type": "application/pdf"})
    assert missing.status_code == 422

    body = upload(client, company_id, "kind=client_document&retention_class=cdd_id_document")
    assert body["retention_class"] == "cdd_id_document"


async def test_same_day_same_form_reupload_gets_a_hash_suffix(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)

    first = upload(client, company_id)
    second = upload(client, company_id)  # same content, same rendered key

    stem = first["storage_key"][: -len(".pdf")]
    assert second["storage_key"] == f"{stem}-{hashlib.sha256(PDF).hexdigest()[:8]}.pdf"
    # WORM: the first object is untouched.
    assert store.objects[first["storage_key"]] == PDF
    assert store.objects[second["storage_key"]] == PDF


async def test_upload_rejects_type_mismatch_through_the_stack(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)

    response = client.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing",
        content=b"MZ not a pdf",
        headers={"Content-Type": "application/pdf"},
    )
    assert response.status_code == 422
    assert await session.scalar(select(Document)) is None
    assert store.objects == {}


# --- The scan decides what is usable (ADR-0009) ---


async def test_an_upload_the_scanner_refuses_is_quarantined_and_not_servable(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)
    scanner_seam.default = failed("unavailable:ConnectionRefusedError")

    body = upload(client, company_id)

    # Fail closed: the file is stored and registered — the interim posture's
    # ``pending`` now means "not successfully scanned" — but it is not clean,
    # so it is neither served nor processed.
    assert body["scan_status"] == ScanStatus.PENDING.value
    assert scanner_seam.scanned == [PDF]
    download = client.get(f"/api/v1/companies/{company_id}/documents/{body['id']}/download")
    assert download.status_code == 409
    assert download.json()["detail"] == "document_not_available: pending"
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == AuditAction.DOCUMENT_REGISTERED.value,
                )
            )
        ).all()
    )
    assert entries[0].details["scan_status"] == "pending"
    assert entries[0].details["scan_reason"] == "unavailable:ConnectionRefusedError"


async def test_an_infected_upload_is_stored_as_infected_and_not_servable(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)
    scanner_seam.default = infected("Eicar-Test-Signature")

    body = upload(client, company_id)

    # Stored as the record of what arrived, and served to nobody.
    assert body["scan_status"] == ScanStatus.INFECTED.value
    assert store.objects[body["storage_key"]] == PDF
    download = client.get(f"/api/v1/companies/{company_id}/documents/{body['id']}/download")
    assert download.status_code == 409
    assert download.json()["detail"] == "document_not_available: infected"
    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == AuditAction.DOCUMENT_REGISTERED.value,
                )
            )
        ).all()
    )
    assert entries[0].details["scan_signature"] == "Eicar-Test-Signature"


async def test_a_manual_upload_is_scanned_like_any_other_file(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)
    scanner_seam.default = CLEAN

    upload(client, company_id)

    assert scanner_seam.scanned == [PDF]


# --- Reads and downloads ---


async def test_list_and_download_round_trip(session: AsyncSession, store: FakeStore) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)
    uploaded = upload(client, company_id)

    listing = client.get(f"/api/v1/companies/{company_id}/documents")
    assert [row["id"] for row in listing.json()] == [uploaded["id"]]

    download = client.get(f"/api/v1/companies/{company_id}/documents/{uploaded['id']}/download")
    assert download.status_code == 200
    assert download.json()["url"].endswith(f"{uploaded['storage_key']}?ttl=900")

    # A statutory filing is not a sensitive class: no download audit event.
    actions = await audit_actions(session, tenant)
    assert AuditAction.DOCUMENT_DOWNLOADED.value not in actions


async def test_sensitive_class_download_is_audited(session: AsyncSession, store: FakeStore) -> None:
    tenant, admin = await make_admin(session)
    client = make_client(session, store, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    company_id = await make_company(session, tenant, admin)
    uploaded = upload(client, company_id, "kind=client_document&retention_class=cdd_id_document")

    download = client.get(f"/api/v1/companies/{company_id}/documents/{uploaded['id']}/download")
    assert download.status_code == 200

    entries = list(
        (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant.id,
                    AuditLog.action == AuditAction.DOCUMENT_DOWNLOADED.value,
                )
            )
        ).all()
    )
    assert len(entries) == 1
    assert str(entries[0].entity_id) == uploaded["id"]
    assert entries[0].actor_user_id == admin.id


# --- Tenant isolation ---


async def test_cross_tenant_company_and_document_are_invisible(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, admin_b = await make_admin(session)
    client_a = make_client(session, store, ActorRef(user_id=admin_a.id, tenant_id=tenant_a.id))
    client_b = make_client(session, store, ActorRef(user_id=admin_b.id, tenant_id=tenant_b.id))
    company_id = await make_company(session, tenant_a, admin_a)
    uploaded = upload(client_a, company_id)

    # Tenant B cannot upload to, list, or download tenant A's documents.
    assert client_b.post(
        f"/api/v1/companies/{company_id}/documents?kind=prior_filing",
        content=PDF,
        headers={"Content-Type": "application/pdf"},
    ).status_code == 404
    assert client_b.get(f"/api/v1/companies/{company_id}/documents").status_code == 404
    assert client_b.get(
        f"/api/v1/companies/{company_id}/documents/{uploaded['id']}/download"
    ).status_code == 404


async def test_service_level_cross_tenant_company_raises_not_found(
    session: AsyncSession, store: FakeStore
) -> None:
    tenant_a, admin_a = await make_admin(session)
    tenant_b, _ = await make_admin(session)
    company_id = await make_company(session, tenant_a, admin_a)

    with pytest.raises(CompanyNotFoundError):
        await documents.register_upload(
            session,
            store,
            tenant_id=tenant_b.id,
            company_id=UUID(company_id),
            actor_user_id=None,
            kind=DocumentKind.PRIOR_FILING,
            content_type="application/pdf",
            data=PDF,
            scanner=FakeScanner(),
        )


# --- The generated template and its {Version} token (contract §5.3) ---

MESSAGE = (
    b"From: tcsp@tcsp.example.hk\r\n"
    b"To: director@client.example.hk\r\n"
    b"Subject: [ABC Limited \xe2\x80\x94 CR 1234567] Annual Return Confirmation\r\n"
    b"\r\nbody\r\n"
)


async def register_generated(
    session: AsyncSession,
    store: FakeStore,
    *,
    tenant: Tenant,
    admin: User,
    company_id: str,
    version: int | None,
    scanner: FakeScanner,
) -> Document:
    """One system-generated draft through the real write path."""
    return await documents.register_upload(
        session,
        store,
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        actor_user_id=admin.id,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=MESSAGE,
        retention_class=RetentionClass.CLIENT_COMMUNICATION,
        form="NAR1",
        version=version,
        scanner=scanner,
    )


async def test_a_versioned_registration_renders_the_generated_template(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """``version=`` keys a system-generated draft from the ``generated`` template.

    §5.3 makes ``{Version}`` mandatory there, so the version is what keeps two
    drafts of one company, form and day off one key; the key still names the
    kind (``draft``) and the type the bytes are (``.eml``).
    """
    tenant, admin = await make_admin(session)
    company_id = await make_company(session, tenant, admin)

    document = await register_generated(
        session, store, tenant=tenant, admin=admin, company_id=company_id, version=1,
        scanner=scanner_seam,
    )

    today = date.today()
    assert document.storage_key == (
        f"1234567/{today.year}/NAR1_draft_1234567_{today.strftime('%Y%m%d')}_v1.eml"
    )
    assert document.content_type == MESSAGE_CONTENT_TYPE
    # The scan is part of the same order on this path: nothing is stored
    # before the scanner answered.
    assert scanner_seam.scanned == [MESSAGE]
    assert document.scan_status == ScanStatus.CLEAN.value
    assert store.objects[document.storage_key] == MESSAGE

    second = await register_generated(
        session, store, tenant=tenant, admin=admin, company_id=company_id, version=2,
        scanner=scanner_seam,
    )
    assert second.storage_key.endswith("_v2.eml")
    assert second.storage_key != document.storage_key


async def test_without_a_version_the_document_template_renders_as_before(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """``version=None`` is the key every existing caller has always got.

    The same registration as the versioned one, minus the version: the
    ``document`` template renders it, with no version suffix and no content-hash
    collision suffix (the two keys differ), which is what pins the new
    parameter as additive.
    """
    tenant, admin = await make_admin(session)
    company_id = await make_company(session, tenant, admin)

    document = await register_generated(
        session, store, tenant=tenant, admin=admin, company_id=company_id, version=None,
        scanner=scanner_seam,
    )

    today = date.today()
    assert document.storage_key == (
        f"1234567/{today.year}/NAR1_draft_1234567_{today.strftime('%Y%m%d')}.eml"
    )
    assert store.objects[document.storage_key] == MESSAGE


async def test_the_tenants_own_templates_are_the_ones_rendered(
    session: AsyncSession, store: FakeStore, scanner_seam: FakeScanner
) -> None:
    """The version selects *which* stored template renders the key.

    Defaults would give a different key from the stored pair as well, so the
    two templates here are deliberately distinguishable from the shipped ones
    as much as from each other.
    """
    tenant, admin = await make_admin(session)
    company_id = await make_company(session, tenant, admin)
    await tenant_config.put_tenant_config(
        session,
        tenant_id=tenant.id,
        actor_user_id=admin.id,
        document="{CompanyCode}/{Form}_{DocType}.{Ext}",
        generated="{CompanyCode}/{Form}_{DocType}_r{Version}.{Ext}",
        milestones=default_milestones().as_dict(),
    )

    versioned = await register_generated(
        session, store, tenant=tenant, admin=admin, company_id=company_id, version=3,
        scanner=scanner_seam,
    )
    plain = await register_generated(
        session, store, tenant=tenant, admin=admin, company_id=company_id, version=None,
        scanner=scanner_seam,
    )

    assert versioned.storage_key == "1234567/NAR1_draft_r3.eml"
    assert plain.storage_key == "1234567/NAR1_draft.eml"
