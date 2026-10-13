"""Records-registry endpoints (ADR-0009; data-model §5.1 Ingestion row).

``POST /companies/{id}/documents`` ingests one uploaded file (raw body +
query-param metadata, the CSV-import pattern — no multipart dependency):
validated by type and size (scope §8), **scanned**, hashed, stored under the
tenant's naming-template key, and registered with the scan outcome in the row
— ``clean``, ``infected``, or ``pending`` when the scanner could not answer.
``GET .../documents`` lists metadata; ``GET .../documents/{id}/download``
issues a short-lived pre-signed URL (ADR-0009 API-mediated access — MinIO
is never exposed to clients) and refuses a document that is not scanned
clean.

``POST /documents/scan-pending`` is the deployment-level maintenance route
that closes the interim posture: it re-scans documents still ``pending`` —
the backlog registered before ClamAV landed, plus anything whose scan failed
since — through the same seam and the store's own ``get_bytes``.

The four routes are Administrator-only or grant-gated: uploading and
retrieving prior filings is ordinary staff work (same rule as fact appends —
founder decision 2026-08-24), while the retrospective scan sweeps every
company in the tenant and is administrative plumbing, like the pack cache.
Every mutation and every sensitive-class download emits its audit event in
the same transaction (ADR-0009).

**The download route also carries the client-bound release gate** (ADR-0017;
the founder's ruling of 2026-09-24): a document the product produced for a
client — today the stored confirmation ``.eml`` — is refused until the
company's assigned package covers its work (and the coverage period holds)
**and** the period's record says the client is continuing and the fee has
arrived. It is the TCSP's own commercial precondition on what goes *out* to a
client, never on a staff member's own working copy or an internal read, and it
is a second filter on top of ADR-0009 rather than a replacement for it:
availability is decided first.
"""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.documents import Document
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import DEFAULT_PRESIGN_TTL_SECONDS, S3Storage
from app.api.deps import ActorRef, get_current_actor, require_admin, require_company_access
from app.application import documents, scanning, service_renewals
from app.application.company_master import CompanyNotFoundError
from app.domain.documents import (
    MAX_UPLOAD_BYTES,
    ContentTypeMismatchError,
    DocumentKind,
    RetentionClass,
    UnsupportedContentTypeError,
    UploadTooLargeError,
)
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/companies", tags=["documents"])

# The retrospective scan is not company-scoped: the interim posture left
# unscanned documents across the whole tenant, and one sweep is what closes it.
scan_router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# The per-request bound on the sweep, so a large backlog is a sequence of
# bounded requests rather than one that can time out.
MAX_SCAN_BATCH = 500
DEFAULT_SCAN_BATCH = 100

def get_storage() -> S3Storage:
    """The process-wide blob-store client; the factory lives in the application
    layer (``app.application.documents``) so the background cycles can share it."""
    return documents.get_storage()


async def _read_capped_body(request: Request) -> bytes:
    """The request body, bounded: 413 as soon as the per-file cap is exceeded.

    Same two-layer pattern as the CSV import: a present over-cap
    ``Content-Length`` is rejected before any bytes are read; the stream
    itself is then capped.
    """
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > MAX_UPLOAD_BYTES
    ):
        raise HTTPException(status_code=413, detail="file exceeds the 25 MiB limit")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file exceeds the 25 MiB limit")
    return bytes(body)


class DocumentResponse(BaseModel):
    """The registry metadata for one stored document (never the blob)."""

    id: UUID
    # NULL only for a company-less document; this company-scoped surface never
    # lists one (migration 0035), and the field stays honest about the column.
    company_id: UUID | None
    kind: str
    origin: str
    retention_class: str
    sha256: str
    storage_key: str
    byte_size: int
    content_type: str
    scan_status: str
    supersedes_document_id: UUID | None
    uploaded_by: UUID | None
    uploaded_at: datetime


class DownloadResponse(BaseModel):
    url: str
    expires_in: int


def _document_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        company_id=document.company_id,
        kind=document.kind,
        origin=document.origin,
        retention_class=document.retention_class,
        sha256=document.sha256,
        storage_key=document.storage_key,
        byte_size=document.byte_size,
        content_type=document.content_type,
        scan_status=document.scan_status,
        supersedes_document_id=document.supersedes_document_id,
        uploaded_by=document.uploaded_by,
        uploaded_at=document.uploaded_at,
    )


@router.post("/{company_id}/documents", status_code=201)
async def upload_document(
    request: Request,
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
    kind: Annotated[DocumentKind, Query()],
    form: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    retention_class: Annotated[RetentionClass | None, Query()] = None,
) -> DocumentResponse:
    """Ingest one uploaded file for the company.

    The body is the raw file bytes; the ``Content-Type`` header is the
    declared type and must match the body's magic bytes (scope §8).
    ``retention_class`` is required only for ``client_document`` — every
    other kind defaults per the single domain rule. The response's
    ``scan_status`` is the scan's outcome: ``clean``, ``infected`` (stored as
    the record of what arrived, never served or processed), or ``pending``
    when the scanner could not answer. 404 for unknown or cross-tenant
    companies; 413 over the 25 MiB cap; 422 on type/size validation failure.
    """
    data = await _read_capped_body(request)
    content_type = (request.headers.get("content-type") or "").partition(";")[0].strip()
    try:
        document = await documents.register_upload(
            session,
            store,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            kind=kind,
            content_type=content_type,
            data=data,
            scanner=scanning.scanner(),
            form=form,
            retention_class=retention_class,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except (UnsupportedContentTypeError, ContentTypeMismatchError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except documents.RetentionClassRequiredError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _document_response(document)


@router.get("/{company_id}/documents")
async def list_documents(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DocumentResponse]:
    """The company's registered documents (metadata only)."""
    try:
        rows = await documents.list_documents(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_document_response(row) for row in rows]


@router.get("/{company_id}/documents/{document_id}/download")
async def download_document(
    company_id: UUID,
    document_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> DownloadResponse:
    """Issue a short-lived pre-signed GET URL for one document.

    Only scanned-clean documents are served (ADR-0009): 409
    ``document_not_available: <scan_status>`` for one that is still
    ``pending`` or is ``infected``. Sensitive-class downloads (director ID
    documents, signed drafts) are audit-logged.

    **The client-bound release gate.** A document the product produced *for a
    client* — today the stored confirmation ``.eml``, the letter of a
    confirmation request — is refused until the company's assigned package
    covers its work and the coverage period holds (409
    ``service_not_in_scope: <reason>`` with ``no_assignment`` /
    ``assignment_ended`` / ``out_of_coverage`` / ``form_not_covered``) **and**
    the period's record says the client is continuing and the fee has arrived —
    ``409 continuation_not_recorded: <year>`` when the answer is not on file,
    ``409 payment_not_received: <year>`` when the client confirmed and the fee
    is the missing piece (ADR-0017; the founder's revision of 2026-09-24). What
    the gate covers is exactly that: a staff member's own working copy, an
    internal upload, a prior filing and the portfolio reads are never blocked
    by it.
    """
    try:
        document = await documents.get_document(
            session, tenant_id=actor.tenant_id, company_id=company_id, document_id=document_id
        )
        # Availability first, then the release gate: a document that may not be
        # served at all (ADR-0009) is a different answer from one that may not
        # be released to this client yet, and neither depends on the other.
        documents.ensure_usable(document)
        await service_renewals.ensure_release_permitted(
            session,
            tenant_id=actor.tenant_id,
            document=document,
            as_of=date.today(),
            actor_user_id=actor.user_id,
        )
        grant = await documents.issue_download_url(
            session,
            store,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            document_id=document_id,
            actor_user_id=actor.user_id,
            expires_in=DEFAULT_PRESIGN_TTL_SECONDS,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except documents.DocumentNotFoundError:
        raise HTTPException(status_code=404, detail="document not found") from None
    except documents.DocumentNotAvailableError as exc:
        raise HTTPException(
            status_code=409, detail=f"document_not_available: {exc.scan_status}"
        ) from None
    except service_renewals.RenewalGateRefusedError as exc:
        # The refusal is a record, and the entry the gate wrote is on this
        # session: a request that ends in a raised refusal is rolled back at
        # the edge (``app.adapters.persistence.session``), so without this
        # commit the chain would show no refusal at all. The entry is the only
        # pending write on this path — nothing was served, and nothing else was
        # written.
        await session.commit()
        raise HTTPException(status_code=409, detail=exc.detail) from None
    return DownloadResponse(url=grant.url, expires_in=grant.expires_in)


class ScanOutcomeResponse(BaseModel):
    """One document's retro-scan outcome — ids and scan results, never content."""

    document_id: UUID
    company_id: UUID | None
    scan_status: str
    signature: str | None
    reason: str | None


class ScanSweepResponse(BaseModel):
    """What one sweep did, and how much of the backlog is left."""

    scanned: int
    clean: int
    infected: int
    failed: int
    remaining: int
    outcomes: list[ScanOutcomeResponse]


@scan_router.post("/scan-pending")
async def scan_pending_documents(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
    limit: Annotated[int, Query(ge=1, le=MAX_SCAN_BATCH)] = DEFAULT_SCAN_BATCH,
) -> ScanSweepResponse:
    """Re-scan documents still ``pending``, oldest first (Administrator-only).

    The ADR-0009 interim posture is closed: only scanned-clean documents are
    served or processed, so the backlog that registered ``pending`` before
    ClamAV landed has to be resolved. Each document is re-read from the
    object store, scanned through the same seam a fresh upload uses, flipped
    to ``clean`` or ``infected``, and audited. A scan that fails again leaves
    the document ``pending`` and reports its reason; ``remaining`` says how
    much is left, so an operator loops until it is zero.
    """
    sweep = await documents.scan_pending_documents(
        session,
        store,
        tenant_id=actor.tenant_id,
        scanner=scanning.scanner(),
        limit=limit,
    )
    outcomes = [
        ScanOutcomeResponse(
            document_id=outcome.document_id,
            company_id=outcome.company_id,
            scan_status=outcome.scan_status.value,
            signature=outcome.signature,
            reason=outcome.reason,
        )
        for outcome in sweep.outcomes
    ]
    return ScanSweepResponse(
        scanned=len(outcomes),
        clean=sum(1 for outcome in outcomes if outcome.scan_status == "clean"),
        infected=sum(1 for outcome in outcomes if outcome.scan_status == "infected"),
        failed=sum(1 for outcome in outcomes if outcome.scan_status == "pending"),
        remaining=sweep.remaining,
        outcomes=outcomes,
    )
