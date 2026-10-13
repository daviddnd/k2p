"""Lease-cache persistence (ADR-0016 point 2; contract §2.2, migration 0011).

One row per deployment: the last entitlement lease the control plane issued,
when it was fetched, and the bookkeeping that paces the next attempt. **Only
non-secret state is here** — the activation token is a credential and lives
in a file (``app.adapters.entitlement.activation_store``), because a database
backup would leak it; the row holds what the vendor said, never the
credential that lets a refresh be asked for.

The row is keyed by the *control plane's* tenant id, not by a local
``tenants`` row: the two are different id spaces (the local table is this
deployment's own), and the activation callback that first creates the row is
unauthenticated by necessity, so it has no local tenant to resolve. It
carries no client data — a tier, caps, a version, an expiry, and timestamps.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base


class LeaseCache(Base):
    """The deployment's cached lease and its refresh bookkeeping."""

    __tablename__ = "lease_cache"
    __table_args__ = (
        CheckConstraint(
            "tier IS NULL OR tier IN ('free', 'standard', 'enterprise_offline')",
            name="lease_cache_tier_check",
        ),
        # The row is created at activation, before any lease is known: a
        # cached payload and a fetch instant arrive together or not at all.
        CheckConstraint(
            "(tier IS NULL) = (fetched_at IS NULL)",
            name="lease_cache_payload_check",
        ),
    )

    control_plane_tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    tier: Mapped[str | None] = mapped_column(String(30))
    # A lease always expires; NULL here means "nothing fetched yet" (the CP's
    # own response model is non-nullable).
    license_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seat_cap: Mapped[int | None]
    company_cap: Mapped[int | None]
    entitlement_version: Mapped[int | None]
    # Moves on success only: it is what makes the lease "old".
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Moves on every attempt, success or failure: it is the backoff clock.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the last attempt failed (a machine code, never client data).
    last_error: Mapped[str | None] = mapped_column(String(120))
