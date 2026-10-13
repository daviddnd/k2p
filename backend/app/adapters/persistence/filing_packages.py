"""Filing-package persistence (contract §2.8's FilingPackage; migration ``0030``).

One row per export: the draft version the package was built from, the mode,
the WORM ``export_package`` document the primary artifact was stored as, the
``exported_by/at`` pair, and the **envelope** — the derived counts (sheet
counts, page-number pairs) and the companion documents' ids the assembly
computed (``app.domain.export_package`` owns the shape; the column is JSONB
like the drafts' documents, deliberately not a CHECK — the shape is the
domain's).

The draft linkage names one **version row**: ``(draft_id, draft_version_no)``
references ``drafts``' composite key, with the tenant pinned through the
``drafts`` ``(id, version_no, tenant_id)`` unique target — the child-table
shape ``app.adapters.persistence.drafts``' docstring reserves. The document
and the exporter join the tenant the F1 way; migration ``0030`` adds the
``(id, tenant_id)`` unique constraint on ``documents`` that composite
reference needs (every other table the single-row pattern references already
carries one).

``mode`` is the three-value vocabulary of ``app.domain.export_package``'s
``ExportMode`` as a CHECK — the backstop a caller cannot bypass.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

# The database backstop (app.domain.export_package owns the values).
_MODE_CHECK = "mode IN ('e_registry_prep', 'paper', 'api_ready')"


class FilingPackage(Base):
    """One export: an approved draft's submission package (data-model §2.8).

    Immutable once written — an export is a fact (a filing was prepared), and
    the §4.2 guard plus the executor make a second export of one version
    unreachable, so the row is insert-only in practice. ``envelope`` is the
    derived record a reader audits the package against: sheet counts per
    slug, the footer page-number pairs the numbered sheets actually printed
    (NAR1 Schedule 1's copies alone; unnumbered sheets contribute none), and
    the companion documents' ids (``None`` where the mode carries no
    companion).
    """

    __tablename__ = "filing_packages"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(["exported_by", "tenant_id"], ["users.id", "users.tenant_id"]),
        # The draft linkage names one version row, tenant pinned through the
        # drafts table's (id, version_no, tenant_id) unique target.
        ForeignKeyConstraint(
            ["draft_id", "draft_version_no", "tenant_id"],
            ["drafts.id", "drafts.version_no", "drafts.tenant_id"],
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        CheckConstraint(_MODE_CHECK, name="filing_packages_mode_check"),
        Index("ix_filing_packages_tenant_draft", "tenant_id", "draft_id"),
    )

    id: Mapped[UUID] = mapped_column(default=uuid4, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column()
    # The version this package was built from — the linkage is to the row,
    # not the lineage: a later regeneration's export is a new package row.
    draft_id: Mapped[UUID] = mapped_column()
    draft_version_no: Mapped[int] = mapped_column(Integer)
    # Vocabulary: app.domain.export_package.ExportMode.
    mode: Mapped[str] = mapped_column(String(20))
    # The primary artifact as stored: one WORM export_package document.
    document_id: Mapped[UUID] = mapped_column()
    # The derived counts and the companion documents' ids.
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    exported_by: Mapped[UUID] = mapped_column()
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
