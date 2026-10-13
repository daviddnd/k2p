"""Records-registry persistence (ADR-0009; migration 0009).

One ``documents`` row per stored object; blobs live in MinIO. Writes go
through ``app.application.documents`` — the only path that hashes,
renders the storage key, and registers the row together.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class Document(Base):
    """A registered stored object (WORM blob + metadata row).

    One ``tenant_id`` per row: the composite foreign keys pin the company
    and the uploader to that tenant (F1 hardening pattern), so a
    cross-tenant row is uninsertable at the database level. Rows are
    mutable (scan status, supersession metadata); only the blobs are WORM.
    """

    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
        ForeignKeyConstraint(["uploaded_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # The F1 composite target a child table names (migration 0030's
        # filing_packages is the first): (id, tenant_id) unique, like every
        # other table the single-row pattern references.
        UniqueConstraint("id", "tenant_id"),
        # The vocabularies are app.domain.documents' StrEnums; the migration
        # created these CHECKs and they are declared here too, or a future
        # autogenerate would read them as strays and drop them.
        CheckConstraint(
            "kind IN ('prior_filing', 'client_reply', 'signed_draft', 'generated_draft', "
            "'export_package', 'client_document')",
            name="documents_kind_check",
        ),
        CheckConstraint(
            "origin IN ('upload', 'email_attachment', 'generated')",
            name="documents_origin_check",
        ),
        CheckConstraint(
            "retention_class IN ('statutory_filing', 'client_communication', "
            "'cdd_id_document', 'generated_draft', 'export_package')",
            name="documents_retention_class_check",
        ),
        CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected')",
            name="documents_scan_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the company-less generated drafts the clarification loop's
    # asks store (migration 0035 — the letter is no company's document; the
    # composite FK above is not checked on a NULL, and the company-bound
    # rows keep their F1 tenant pinning).
    company_id: Mapped[UUID | None] = mapped_column(index=True)
    # Vocabularies: app.domain.documents (DocumentKind / DocumentOrigin /
    # RetentionClass / ScanStatus); DB CHECKs are the backstop.
    kind: Mapped[str] = mapped_column(String(30))
    origin: Mapped[str] = mapped_column(String(20))
    retention_class: Mapped[str] = mapped_column(String(30))
    sha256: Mapped[str] = mapped_column(String(64))
    # Rendered from the tenant's naming template at write time; never
    # rewritten (WORM — ADR-0009/0013).
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100))
    scan_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    # Version chain: new versions are new objects (ADR-0009).
    supersedes_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id")
    )
    # NULL = system (generated documents in later phases).
    uploaded_by: Mapped[UUID | None] = mapped_column()
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
