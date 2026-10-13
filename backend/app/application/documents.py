"""Records-registry orchestration (ADR-0009): the only write/read path for
stored documents.

- ``register_upload`` runs the ingestion order — validate (type/size/magic
  bytes, scope §8) → **scan** → hash → render key → store blob → register row
  → audit — all metadata writes on the caller's session, so the row and its
  audit entry commit or roll back together at the request edge. The blob
  store is reached only through the ``DocumentStore`` port below (the real
  client built once by ``get_storage``), the scanner only through
  ``app.application.scanning``.
- ``list_documents`` / ``get_document`` are the metadata read paths.
- ``issue_download_url`` is the only application access path to a blob
  (ADR-0009: API-mediated access via short-lived pre-signed URLs); it and
  ``ensure_usable`` enforce the other half of ADR-0009 — only scanned-clean
  content is served or processed. Downloads of sensitive classes (director
  ID documents, signed drafts) are audit-logged per the single domain rule
  ``is_sensitive_class``.
- ``scan_pending_documents`` is the retro-scan: it re-reads documents that
  are still ``pending`` (uploads from the interim posture, and any file whose
  scan failed) through the store's ``get_bytes`` and flips them to ``clean``
  or ``infected``.

The scan is the reason ``pending`` exists at all: a scanner that cannot
answer leaves the document quarantined — stored, audited, and never usable —
and the retro-scan is what resolves it once the scanner is back. Storage keys
come from the tenant's naming template at write time (ADR-0013); a stored key
is never rewritten, and a same-key re-upload gets a content-hash suffix
instead (WORM — no overwrite).

**Company-less registration (migration ``0035``, the clarification loop's
letters).** A generated draft that belongs to no company — the ask letter a
company-less attribution case stores — registers through this same seam with
``company_id = None``: the row, the audit entry (``company_id = NULL``, ruling
D10) and the read all accept the company-less shape, and only a generated
origin may register company-less (a client file or an inbound reply has its
company by the time it reaches the registry). The naming templates are
per-company, with CR/BRN tokens a company-less letter has no values for, so
the key renders from a fixed tenant-level fallback template instead —
``_correspondence/{Year}/{Form}_{DocType}_{YYYYMMDD}.{Ext}`` (for the ask
letter: ``_correspondence/2026/DOC_draft_20261009.eml``) — one deterministic,
operator-readable segment a store browser can open (ADR-0009/0013), rendered
through the same ``render_key`` machinery so every safety check (token value
hygiene, length caps) still applies, and collision-suffixed like any other
key. The company-less read (``get_document`` with ``company_id = None``) is
tenant-scoped: it returns exactly the company-less rows, never a company's.

Services flush but never commit — the API session dependency commits at
the request edge.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.documents import Document
from app.adapters.storage.s3 import S3Storage
from app.application import audit, company_master, tenant_config
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.documents import (
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
    ScanStatus,
    default_retention_class,
    is_sensitive_class,
    is_usable,
    storage_key_values,
    validate_upload,
)
from app.domain.naming_templates import render_key
from app.domain.scanning import ScanResult, scan_status_for

# The tenant-level fallback template for company-less generated drafts (the
# clarification loop's letters): the per-company templates carry CR/BRN
# tokens a company-less letter has no values for, so its key renders from
# this fixed shape instead — deterministic and operator-readable
# (ADR-0009/0013), still through render_key's safety checks.
CORRESPONDENCE_TEMPLATE = "_correspondence/{Year}/{Form}_{DocType}_{YYYYMMDD}.{Ext}"


class DocumentStore(Protocol):
    """The blob-storage port. Implemented by ``app.adapters.storage.s3``;
    tests substitute an in-memory fake."""

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


_storage: S3Storage | None = None


def get_storage() -> S3Storage:
    """The process-wide blob-store client, built from settings on first use.

    The one factory every caller — API routes and background cycles alike —
    builds its store through, so a deployment has one client and the suite
    one thing to replace. It lives in the application layer because the
    background cycles (ingestion, parsing, reminders, sending, the command
    worker's composed sweep) reach it too; the API delegates rather than
    owning a second copy.
    """
    global _storage
    if _storage is None:
        _storage = S3Storage(
            endpoint_url=settings.s3_endpoint_url,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    return _storage


class DocumentNotFoundError(Exception):
    """No document with this id exists on the company (cross-tenant → unknown)."""


class DocumentNotAvailableError(Exception):
    """The document exists but is not usable: its scan status is not ``clean``.

    ``scan_status`` is ``pending`` or ``infected`` — ADR-0009 refuses to serve
    or process either, and the caller is told which one it was.
    """

    def __init__(self, scan_status: str) -> None:
        self.scan_status = scan_status
        super().__init__(f"document is not available (scan_status={scan_status})")


class RetentionClassRequiredError(Exception):
    """The document kind has no default retention class; the caller must choose."""


@dataclass(frozen=True)
class DownloadGrant:
    """A short-lived pre-signed URL for one document."""

    url: str
    expires_in: int


def _with_collision_suffix(key: str, sha256: str) -> str:
    """A deterministic variant key when the rendered key is already taken.

    Two uploads of the same form on the same day render the same key; the
    content-hash suffix keeps both (WORM — the first object is never
    overwritten).
    """
    stem, dot, suffix = key.rpartition(".")
    return f"{stem}-{sha256[:8]}{dot}{suffix}" if dot else f"{key}-{sha256[:8]}"


async def _key_taken(session: AsyncSession, *, storage_key: str) -> bool:
    return (
        await session.scalar(select(Document.id).where(Document.storage_key == storage_key))
    ) is not None


async def register_upload(
    session: AsyncSession,
    store: DocumentStore,
    *,
    tenant_id: UUID,
    company_id: UUID | None,
    actor_user_id: UUID | None,
    kind: DocumentKind,
    content_type: str,
    data: bytes,
    scanner: Scanner,
    origin: DocumentOrigin = DocumentOrigin.UPLOAD,
    form: str | None = None,
    retention_class: RetentionClass | None = None,
    version: int | None = None,
) -> Document:
    """Ingest one file: validate → scan → hash → key → store → register.

    The scan sits between validation and everything that writes: the row
    registers carrying the outcome, so nothing reaches the registry without a
    verdict, and ``scan_status_for`` never yields ``clean`` on doubt — an
    infected file registers as ``infected`` (kept as the record of what
    arrived, never served or processed) and a scanner that could not answer
    leaves the document ``pending``, which the retro-scan resolves later. A
    failed scan is therefore not an exception here; the outcome is in the row
    and in the audit entry.

    ``retention_class`` defaults from the kind via the single domain rule;
    kinds without a default (``client_document``) require an explicit value
    (``RetentionClassRequiredError``). ``origin`` is ``upload`` unless the
    caller is ingesting a file that arrived by email. Raises the domain
    validation errors (``UnsupportedContentTypeError``,
    ``ContentTypeMismatchError``, ``UploadTooLargeError``) before anything is
    written; ``CompanyNotFoundError`` via the shared seam for unknown or
    cross-tenant companies.

    ``version`` is the draft version of a system-generated document — the
    confirmation-request generator's lineage is its first caller. When it is
    set, the key is rendered from the tenant's **``generated``** template
    (whose ``{Version}`` token §5.3 makes mandatory) instead of the
    ``document`` template, and is handed to ``storage_key_values`` with it.
    When it is not, the key is rendered exactly as it always was.

    ``company_id = None`` registers a company-less document under the tenant
    fallback segment (the module docstring's shape): the clarification
    loop's ask letter (migration ``0035``, a generated draft), and the split
    parent's raw inbound message (workstream ③, migration ``0036`` — the
    one inbound record with no company, the parent row carrying the company
    context on its items). A client **upload** with no company is a caller
    bug — staff uploads always act on a company — refused with a
    ``ValueError`` before anything is written. The company-bound path is
    unchanged: the company resolves first, the key renders from the tenant's
    templates with its CR/BRN tokens, and a company-less read never sees the
    row.
    """
    if company_id is None and origin is DocumentOrigin.UPLOAD:
        raise ValueError("company-less registration is only for generated and inbound documents")
    company = (
        None
        if company_id is None
        else await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    )
    validate_upload(content_type=content_type, data=data)
    if retention_class is None:
        retention_class = default_retention_class(kind)
        if retention_class is None:
            raise RetentionClassRequiredError(
                f"kind {kind.value!r} has no default retention class; supply one"
            )
    scan = await scanner.scan(data)
    scan_status = scan_status_for(scan.verdict)
    digest = hashlib.sha256(data).hexdigest()

    today = datetime.now(UTC).date()
    if company is None:
        # The tenant-level fallback segment (module docstring): the per-company
        # templates carry CR/BRN tokens a company-less letter has no values
        # for, so its key renders from the fixed correspondence shape instead.
        key = render_key(
            CORRESPONDENCE_TEMPLATE,
            storage_key_values(
                cr_number=None,
                br_number="",
                form=form,
                kind=kind,
                governing_date=today,
                content_type=content_type,
            ),
        )
    else:
        templates = await tenant_config.get_naming_templates(session, tenant_id=tenant_id)
        template = (
            templates.naming_templates.generated
            if version is not None
            else templates.naming_templates.document
        )
        key = render_key(
            template,
            storage_key_values(
                cr_number=company.cr_number,
                br_number=company.br_number,
                form=form,
                kind=kind,
                governing_date=today,
                content_type=content_type,
                version=version,
            ),
        )
    if await _key_taken(session, storage_key=key):
        key = _with_collision_suffix(key, digest)

    await store.put(key, data, content_type)
    document = Document(
        tenant_id=tenant_id,
        company_id=None if company is None else company.id,
        kind=kind.value,
        origin=origin.value,
        retention_class=retention_class.value,
        sha256=digest,
        storage_key=key,
        byte_size=len(data),
        content_type=content_type,
        scan_status=scan_status.value,
        uploaded_by=actor_user_id,
    )
    session.add(document)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DOCUMENT_REGISTERED,
        actor_user_id=actor_user_id,
        company_id=None if company is None else company.id,
        entity_type="document",
        entity_id=document.id,
        details={
            "kind": document.kind,
            "origin": document.origin,
            "retention_class": document.retention_class,
            "sha256": digest,
            "storage_key": key,
            "byte_size": document.byte_size,
            **_scan_details(scan),
        },
    )
    return document


def _scan_details(scan: ScanResult) -> dict[str, str]:
    """The auditable half of one scan: outcome, plus the signature or reason.

    Neither carries file content: a signature name is ClamAV's own label and
    a reason is a transport or daemon code.
    """
    details = {"scan_status": scan_status_for(scan.verdict).value}
    if scan.signature is not None:
        details["scan_signature"] = scan.signature
    if scan.reason is not None:
        details["scan_reason"] = scan.reason
    return details


def ensure_usable(document: Document) -> None:
    """Refuse to serve or process a document that is not scanned clean.

    The reader-side half of ADR-0009, called by every path that would hand out
    or read a blob, so the rule has one implementation and one message.
    """
    if not is_usable(ScanStatus(document.scan_status)):
        raise DocumentNotAvailableError(document.scan_status)


async def list_documents(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[Document]:
    """The company's registered documents, newest first."""
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = await session.scalars(
        select(Document).where(Document.company_id == company.id).order_by(Document.uploaded_at)
    )
    return list(rows.all())


async def get_document(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID | None, document_id: UUID
) -> Document:
    """One document, or ``DocumentNotFoundError`` (unknown and cross-tenant ids
    are indistinguishable).

    ``company_id = None`` is the company-less read (migration ``0035``): the
    tenant's company-less documents — the clarification loop's letters — by id
    alone. It returns exactly the company-less rows: a company's document is
    not reachable through it, and the company-scoped read below never returns
    a company-less row."""
    if company_id is None:
        company_less = await session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.company_id.is_(None),
                Document.tenant_id == tenant_id,
            )
        )
        if company_less is None:
            raise DocumentNotFoundError
        return company_less
    await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    document: Document | None = await session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.company_id == company_id,
            Document.tenant_id == tenant_id,
        )
    )
    if document is None:
        raise DocumentNotFoundError
    return document


async def issue_download_url(
    session: AsyncSession,
    store: DocumentStore,
    *,
    tenant_id: UUID,
    company_id: UUID,
    document_id: UUID,
    actor_user_id: UUID | None,
    expires_in: int,
) -> DownloadGrant:
    """Issue a short-lived pre-signed GET URL (ADR-0009 API-mediated access).

    Only scanned-clean content is served (``ensure_usable`` → the typed
    ``DocumentNotAvailableError``), and downloads of sensitive classes
    (director ID documents, signed drafts — the single domain rule
    ``is_sensitive_class``) are written to the audit log in the same
    transaction.
    """
    document = await get_document(
        session, tenant_id=tenant_id, company_id=company_id, document_id=document_id
    )
    ensure_usable(document)
    url = await store.presign_get(document.storage_key, expires_in=expires_in)
    if is_sensitive_class(DocumentKind(document.kind), RetentionClass(document.retention_class)):
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.DOCUMENT_DOWNLOADED,
            actor_user_id=actor_user_id,
            company_id=document.company_id,
            entity_type="document",
            entity_id=document.id,
            details={"kind": document.kind, "retention_class": document.retention_class},
        )
    return DownloadGrant(url=url, expires_in=expires_in)


@dataclass(frozen=True)
class ScanOutcome:
    """One retro-scan result, as the operator reads it — never content."""

    document_id: UUID
    # NULL for a company-less document (migration 0035 — the clarification
    # loop's letter): its scan outcome names no company.
    company_id: UUID | None
    scan_status: ScanStatus
    signature: str | None
    reason: str | None


@dataclass(frozen=True)
class ScanSweep:
    """What one retro-scan pass did, and how much of the backlog is left."""

    outcomes: tuple[ScanOutcome, ...]
    remaining: int


async def scan_pending_documents(
    session: AsyncSession,
    store: DocumentStore,
    *,
    tenant_id: UUID,
    scanner: Scanner,
    limit: int,
) -> ScanSweep:
    """Re-scan up to ``limit`` still-``pending`` documents, oldest first.

    The backlog the interim posture left behind — uploads registered before
    ClamAV landed — plus anything whose scan failed since. The stored bytes
    come back out of the registry's own store (``get_bytes``) and go through
    the same seam as a fresh ingestion; the row flips to ``clean`` or
    ``infected``, and a scan that fails again leaves it ``pending``. Every
    document scanned is audited, whatever the verdict.

    ``remaining`` counts what this pass did not reach, so an operator can
    loop until it is zero; a missing blob raises rather than being skipped —
    that is an integrity problem, not a document to skip past.
    """
    pending = list(
        (
            await session.scalars(
                select(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.scan_status == ScanStatus.PENDING.value,
                )
                .order_by(Document.uploaded_at, Document.id)
                .limit(limit)
            )
        ).all()
    )
    outcomes = [
        await _rescan(session, store, document=document, scanner=scanner) for document in pending
    ]
    remaining = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.tenant_id == tenant_id, Document.scan_status == ScanStatus.PENDING.value)
    )
    return ScanSweep(outcomes=tuple(outcomes), remaining=int(remaining or 0))


async def _rescan(
    session: AsyncSession, store: DocumentStore, *, document: Document, scanner: Scanner
) -> ScanOutcome:
    """Scan one stored document, record the flip, and audit the outcome."""
    scan = await scanner.scan(await store.get_bytes(document.storage_key))
    scan_status = scan_status_for(scan.verdict)
    document.scan_status = scan_status.value
    await audit.record_event(
        session,
        tenant_id=document.tenant_id,
        action=AuditAction.DOCUMENT_SCANNED,
        company_id=document.company_id,
        entity_type="document",
        entity_id=document.id,
        details={"previous_scan_status": ScanStatus.PENDING.value, **_scan_details(scan)},
    )
    return ScanOutcome(
        document_id=document.id,
        company_id=document.company_id,
        scan_status=scan_status,
        signature=scan.signature,
        reason=scan.reason,
    )


__all__ = [
    "DocumentNotAvailableError",
    "DocumentNotFoundError",
    "DocumentStore",
    "DownloadGrant",
    "RetentionClassRequiredError",
    "ScanOutcome",
    "ScanSweep",
    "ensure_usable",
    "get_document",
    "issue_download_url",
    "list_documents",
    "register_upload",
    "scan_pending_documents",
]
