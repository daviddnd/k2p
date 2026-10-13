"""Pack-cache persistence (ADR-0012; contract §2.2 ``PackCache``, migration
0014).

What this deployment has downloaded, and whether it is still allowed to
refresh. Deployment-level rather than tenant-scoped, like ``lease_cache`` and
for the same reason: a licensed pack is an artifact of the deployment's own
licence, not of any client's data, and there is exactly one licence per
deployment (ADR-0002's single-tenant install).

``(pack_type, version)`` is the natural key — a type keeps its old versions
alongside a new one rather than replacing it, because ADR-0012's safety line
is that content the customer already holds is never taken away. ``frozen`` is
the §4.3 downgrade switch: frozen rows stay readable and stop being
refreshed, and ``frozen_at`` is set exactly when the flag is, by check
constraint.

The payloads here are licensed content (form mappings, deadline rules, fee
schedules, prompt sets): vendor-authored, never client data, and never
written to an audit entry — the audit records versions and digests, never
bytes.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

_PACK_TYPE_CHECK = "pack_type IN ('form_mapping', 'deadline_rules', 'fee_schedule', 'prompt_pack')"


class PackCache(Base):
    """One downloaded pack version and its freeze state (§4.3)."""

    __tablename__ = "pack_cache"
    __table_args__ = (
        CheckConstraint(_PACK_TYPE_CHECK, name="pack_cache_type_check"),
        CheckConstraint("version > 0", name="pack_cache_version_check"),
        # A pack is frozen with a timestamp or not at all: the pair moving
        # together is what lets "when did this stop refreshing" be answered.
        CheckConstraint(
            "(frozen AND frozen_at IS NOT NULL) OR (NOT frozen AND frozen_at IS NULL)",
            name="pack_cache_frozen_check",
        ),
    )

    pack_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The digest the payload was accepted under — kept so a re-verification
    # reads the digest the deployment actually checked, not one it infers.
    sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    frozen: Mapped[bool] = mapped_column(server_default="false")
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
