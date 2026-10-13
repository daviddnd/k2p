"""The one integrity-verification seam (ADR-0009/0011; scope §8; Review Focus 5).

``run_integrity_check`` is the deployment's honest read over its own records,
two legs on one session:

1. The tenant's **full audit chain**, read in ``seq`` order and verified with
   the single domain rule ``app.domain.audit.verify_chain`` — every problem
   becomes an ``audit_chain_break`` mismatch (a company filter could not
   re-verify a subset; the chain links span companies, ruling R6).
2. The tenant's **registered documents**, iterated in deterministic order
   (``uploaded_at``, ``id``) with ``limit`` honored for an operator's spot
   checks; each blob is re-read through the store's ``get_bytes``, its
   SHA-256 recomputed and compared to the registry row. A blob that cannot
   be read is a ``document_unreadable`` mismatch — an alert, never an abort:
   a storage outage mid-check degrades the document leg to unreadable
   findings and still finishes, which is the honest result (only a database
   the check cannot read at all fails the check, and that refusal lives in
   the API layer).

The seam **writes nothing and emits no audit entry** — the caller owns the
single ``integrity_check`` audit write (the route: actor = the administrator;
the loop: actor NULL = system). A mismatch alerts; it never repairs, flips,
or rewrites anything.
"""

import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.documents import Document
from app.application.documents import DocumentStore
from app.domain.audit import verify_chain
from app.domain.integrity import IntegrityResult, Mismatch, MismatchKind, classify


async def run_integrity_check(
    session: AsyncSession,
    store: DocumentStore,
    *,
    tenant_id: UUID,
    now: datetime,
    limit: int | None = None,
) -> IntegrityResult:
    """Verify the tenant's audit chain and re-hash its registered documents.

    ``now`` is the caller's clock read — the check compares nothing against a
    time, but the seam keeps the caller-owns-the-clock convention the other
    sweeps share. Returns the classified result; writes nothing.
    """
    chain = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.seq)
            )
        ).all()
    )
    chain_problems = verify_chain(chain)
    mismatches = [
        Mismatch(MismatchKind.AUDIT_CHAIN_BREAK, None, problem)
        for problem in chain_problems
    ]
    documents = select(Document).where(Document.tenant_id == tenant_id).order_by(
        Document.uploaded_at, Document.id
    )
    if limit is not None:
        documents = documents.limit(limit)
    checked = 0
    for document in (await session.scalars(documents)).all():
        checked += 1
        try:
            data = await store.get_bytes(document.storage_key)
        except Exception:  # any storage failure is a finding, not an abort
            mismatches.append(
                Mismatch(
                    MismatchKind.DOCUMENT_UNREADABLE,
                    document.id,
                    f"blob {document.storage_key} could not be read",
                )
            )
            continue
        if hashlib.sha256(data).hexdigest() != document.sha256:
            mismatches.append(
                Mismatch(
                    MismatchKind.DOCUMENT_HASH_MISMATCH,
                    document.id,
                    "recomputed sha256 does not match the registry",
                )
            )
    return classify(not chain_problems, checked, mismatches)
