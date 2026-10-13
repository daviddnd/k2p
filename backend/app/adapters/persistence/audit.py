"""Audit-log persistence (ADR-0009; migration 0008).

One append-only, hash-chained ``audit_log`` table. Writes go exclusively
through ``app.application.audit.record_event`` — the only path that
computes the chain link and entry hash. Immutability (no UPDATE/DELETE) is
enforced by database triggers, not by this model.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class AuditLog(Base):
    """One audit entry; rows are never updated or deleted.

    One ``tenant_id`` per row: the composite foreign keys pin the actor and
    the company scope to that tenant (F1 hardening pattern), so a
    cross-tenant entry is uninsertable at the database level.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        ForeignKeyConstraint(["actor_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Global monotonic insertion order for chain-walk verification.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Application-set: an input to the entry hash, so known before insert.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # NULL = system actor.
    actor_user_id: Mapped[UUID | None] = mapped_column()
    # NULL = tenant-scoped event with no company scope.
    company_id: Mapped[UUID | None] = mapped_column(index=True)
    # Vocabulary: app.domain.audit.AuditAction (no DB CHECK — the append-only
    # table must never be rewritten as the vocabulary grows).
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[UUID | None] = mapped_column()
    # Before/after summary, stored exactly as hashed (jsonable form).
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Reserved for ADR-0009 LLM-call metadata; NULL until Phase 3.
    llm_call: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # NULL = the tenant's genesis entry.
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
