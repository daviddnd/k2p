"""Tenant configuration (data-model §2.9 ``TenantConfig``; ADR-0013 item 1).

One row per tenant. This slice implements ``naming_templates`` — the
per-tenant token-based naming/storage templates validated per contract §5.3
(scope §2.15) — ``milestones``, the administrator's milestone panel (scope
§2.4; the founder's ruling of 2026-09-24) validated by the single domain rule
in ``app.domain.milestones``, and **``send_modes``**: who releases a message,
per destination (the founder's ruling of 2026-09-24, contract §5.1.8's M2 note
and §5.3). A tenant without a row reads the shipped defaults of all three
blocks, so rows appear only when an Administrator first PUTs a configuration.
Later ``TenantConfig`` fields (calendar-sync settings) land with their own
phases.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class TenantConfig(Base):
    """Per-tenant configuration.

    ``naming_templates`` holds the §5.3 pair, shape
    ``{"document": "...", "generated": "..."}``, ``milestones`` the
    §2.9 milestone block (``app.domain.milestones.Milestones.as_dict()``), and
    ``send_modes`` the per-destination modes
    (``app.domain.send_modes.SendModes.as_dict()``). Each is validated by its
    own domain rule before every write; none is ever written unvalidated.

    ``milestones`` and ``send_modes`` are NOT NULL with **server defaults** —
    the shipped defaults, written by migrations ``0019`` and ``0023``, which is
    also what backfilled the rows that predate each column. That is why the
    read path has no NULL branch: a row without a stored block (however it was
    written) reads the shipped defaults, and the write paths always supply a
    validated one.
    """

    __tablename__ = "tenant_config"

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    naming_templates: Mapped[dict[str, Any]] = mapped_column(JSON)
    milestones: Mapped[dict[str, Any]] = mapped_column(JSON)
    send_modes: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
