"""Pack refresh, install, and freeze — the deployment's pack lifecycle
(ADR-0012; contract §2.2 ``PackCache``, §4.3 licence state machine).

Three operations, and one deliberate absence:

- ``refresh`` is the only path that downloads: it asks the control plane for
  the latest release of each pack type, verifies what it gets
  (``app.domain.packs``), and installs it. It never raises for a vendor
  failure — a per-type refusal is a reported outcome and an audit entry, so
  an operator sees what happened instead of an error page.
- ``apply_license_state`` is the freeze switch (§4.3). It is called wherever
  the licence state is derived — the entitlement read seam every automation
  trigger and the dashboard already consult — so a lapse freezes the cache
  without a job runner and without a caller having to remember to ask.
  Frozen content stays readable: nothing is deleted or hidden, only
  refreshing stops, exactly as ADR-0012 promises.
- ``cached`` is the read behind ``GET /packs``: what this deployment holds,
  which versions, and whether it is frozen.

**The lease refresh deliberately does not pull packs.** The lease read path
must stay fast and must never do bulk work behind a read that other code
calls in-band (``app.application.entitlement``); a pack is up to a megabyte
per type, and an install is an administrative, audited state change. What the
lease *state* does trigger is the freeze — a local flag flip — which is the
part of "opportunistic" that has to happen without anyone asking.

Audit entries carry pack type, version, digest, and a reason — never payload
bytes, and never prompt bodies: the packs are licensed content and the audit
chain is not a copy of them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import (
    ControlPlaneRefused,
    ControlPlaneUnavailable,
)
from app.adapters.persistence.models import Tenant
from app.adapters.persistence.packs import PackCache
from app.application import audit, lease
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.entitlement import LicenseState, Tier
from app.domain.packs import (
    PackRefusal,
    PackRefused,
    PackRelease,
    PackReleaseInvalid,
    PackType,
    RefreshAction,
    pack_refusal_reason,
    parse_pack_release,
    payload_sha256,
    verify_release,
)

NOT_ACTIVATED = "not_activated"
UNREACHABLE = "control_plane_unreachable"
NOT_PUBLISHED = "pack_not_found"


class PacksNotLicensedError(Exception):
    """The licence does not carry licensed packs: no refresh may run.

    ``reason`` is the domain code (``license_state_free`` for a lapsed lease,
    ``tier_not_licensed`` for a Free tier). The cached packs are frozen
    rather than removed, so this refusal costs the deployment nothing it
    already holds.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PackRef:
    """One cached pack version, as the audit and the read surface name it."""

    pack_type: str
    version: int
    sha256: str


@dataclass(frozen=True)
class RefreshOutcome:
    """What one refresh did about one pack type."""

    pack_type: PackType
    action: RefreshAction
    version: int | None = None
    sha256: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RefreshReport:
    """One refresh cycle's account of itself."""

    refreshed: bool
    outcomes: tuple[RefreshOutcome, ...]
    reason: str | None = None


@dataclass(frozen=True)
class LicenseChange:
    """A freeze or an unfreeze, with what it moved."""

    frozen: bool
    packs: tuple[PackRef, ...]
    at: datetime


async def cached(session: AsyncSession) -> list[PackCache]:
    """Every cached pack version, by type then version."""
    rows = await session.scalars(
        select(PackCache).order_by(PackCache.pack_type, PackCache.version)
    )
    return list(rows.all())


def vendor_public_key() -> Ed25519PublicKey | None:
    """This deployment's pinned vendor public key, or ``None`` when unpinned.

    The key is configuration (``pack_vendor_public_key_file``), read per
    refresh — the file is tiny, the cycle is rare, and reading it each time
    means a rotation takes effect without a restart. A file that does not
    hold an Ed25519 public key is a configuration error, and it refuses
    loudly rather than being read as "no key pinned": that misreading would
    turn fail-closed into silently refusing every signed release.
    """
    path = settings.pack_vendor_public_key_file
    if not path:
        return None
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("pack_vendor_public_key_file: not an Ed25519 public key")
    return key


async def apply_license_state(
    session: AsyncSession,
    *,
    tier: Tier,
    license_state: LicenseState,
    now: datetime | None = None,
) -> LicenseChange | None:
    """Freeze the cache when the licence stops carrying packs; ``None`` if it already agrees.

    One cheap existence check runs on every entitlement read; the rows are
    only touched when the licence actually moved, so the common case costs a
    single count query. The audit entry is written against the deployment's
    tenant — this runs on a read path, so it has no actor: a freeze is a
    consequence of the licence, not a person's action.
    """
    at = now if now is not None else datetime.now(UTC)
    wanted = pack_refusal_reason(tier=tier, license_state=license_state) is not None
    moved_count = await session.scalar(
        select(func.count()).select_from(PackCache).where(PackCache.frozen.is_(not wanted))
    )
    if not moved_count:
        return None
    rows = [row for row in await cached(session) if row.frozen != wanted]
    for row in rows:
        row.frozen = wanted
        row.frozen_at = at if wanted else None
    change = LicenseChange(
        frozen=wanted,
        packs=tuple(_ref(row) for row in rows),
        at=at,
    )
    await session.flush()
    tenant_id = await session.scalar(select(Tenant.id).limit(1))
    if tenant_id is not None:
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.PACKS_FROZEN if wanted else AuditAction.PACKS_UNFROZEN,
            entity_type="pack_cache",
            details={
                "frozen": wanted,
                "packs": [_ref_details(ref) for ref in change.packs],
            },
        )
    return change


async def refresh(
    session: AsyncSession,
    *,
    tier: Tier,
    license_state: LicenseState,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> RefreshReport:
    """Download and install the latest release of every pack type.

    Refuses with ``PacksNotLicensedError`` before asking the control plane
    anything when the licence does not carry packs — that refusal is the
    whole cycle's, so it comes back as a status rather than a per-type
    outcome. Every other failure is an outcome.
    """
    at = now if now is not None else datetime.now(UTC)
    refusal = pack_refusal_reason(tier=tier, license_state=license_state)
    if refusal is not None:
        await _record_refusal(session, actor_user_id=actor_user_id, reason=refusal)
        raise PacksNotLicensedError(refusal)
    credential = ActivationStore(settings.cp_activation_token_file).load().credential
    if credential is None:
        # Not activated: there is no vendor to ask, and configuration is not a
        # pack source. Reported, not raised — nothing is wrong.
        return RefreshReport(refreshed=False, outcomes=(), reason=NOT_ACTIVATED)

    client = lease.client()
    outcomes: list[RefreshOutcome] = []
    installed = False
    types = list(PackType)
    for index, pack_type in enumerate(types):
        try:
            document = await client.fetch_pack(token=credential.token, pack_type=pack_type.value)
        except ControlPlaneUnavailable:
            outcomes.extend(
                _not_attempted(remaining, reason=UNREACHABLE) for remaining in types[index:]
            )
            return RefreshReport(refreshed=installed, outcomes=tuple(outcomes), reason=UNREACHABLE)
        except ControlPlaneRefused as exc:
            if exc.code == NOT_PUBLISHED:
                outcomes.append(
                    RefreshOutcome(pack_type=pack_type, action=RefreshAction.NOT_PUBLISHED)
                )
                continue
            reason = f"control_plane_refused:{exc.reason}"
            outcomes.append(
                RefreshOutcome(pack_type=pack_type, action=RefreshAction.REFUSED, reason=reason)
            )
            await _record_refusal(
                session, actor_user_id=actor_user_id, pack_type=pack_type, reason=reason
            )
            continue
        outcome = await _install(session, document=document, actor_user_id=actor_user_id, now=at)
        installed = installed or outcome.action is RefreshAction.INSTALLED
        outcomes.append(outcome)
    return RefreshReport(refreshed=installed, outcomes=tuple(outcomes))


async def _install(
    session: AsyncSession,
    *,
    document: dict[str, object],
    actor_user_id: UUID | None,
    now: datetime,
) -> RefreshOutcome:
    """Verify one downloaded release and install it, or refuse it — audited either way."""
    try:
        release = parse_pack_release(document)
        verify_release(release, public_key=vendor_public_key())
    except PackReleaseInvalid as exc:
        return await _refuse_download(
            session,
            document=document,
            actor_user_id=actor_user_id,
            reason=f"invalid_release:{exc.reason}",
        )
    except PackRefused as exc:
        return await _refuse_download(
            session,
            document=document,
            actor_user_id=actor_user_id,
            reason=exc.reason.value,
        )

    existing = await session.get(PackCache, (release.pack_type.value, release.version))
    if existing is not None and existing.sha256 != release.sha256:
        # The vendor re-cut a version this deployment already read. Refuse:
        # content under a version number is never swapped silently (the
        # control plane appends versions for exactly this reason).
        reason = PackRefusal.VERSION_CONFLICT.value
        await _record_refusal(
            session,
            actor_user_id=actor_user_id,
            pack_type=release.pack_type,
            reason=reason,
            version=release.version,
            sha256=release.sha256,
        )
        return RefreshOutcome(
            pack_type=release.pack_type,
            action=RefreshAction.REFUSED,
            version=release.version,
            sha256=release.sha256,
            reason=reason,
        )
    if existing is not None:
        return RefreshOutcome(
            pack_type=release.pack_type,
            action=RefreshAction.UP_TO_DATE,
            version=release.version,
            sha256=release.sha256,
        )

    session.add(
        PackCache(
            pack_type=release.pack_type.value,
            version=release.version,
            sha256=release.sha256,
            payload=dict(release.payload),
            downloaded_at=now,
        )
    )
    await session.flush()
    tenant_id = await session.scalar(select(Tenant.id).limit(1))
    if tenant_id is not None:
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.PACK_INSTALLED,
            actor_user_id=actor_user_id,
            entity_type="pack_cache",
            details=_release_details(release),
        )
    return RefreshOutcome(
        pack_type=release.pack_type,
        action=RefreshAction.INSTALLED,
        version=release.version,
        sha256=release.sha256,
    )


async def _record_refusal(
    session: AsyncSession,
    *,
    actor_user_id: UUID | None,
    reason: str,
    pack_type: PackType | None = None,
    version: int | None = None,
    sha256: str | None = None,
) -> None:
    """Audit a refusal — the versions and digests, never the payload bytes."""
    tenant_id = await session.scalar(select(Tenant.id).limit(1))
    if tenant_id is None:
        return
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.PACK_REFRESH_REFUSED,
        actor_user_id=actor_user_id,
        entity_type="pack_cache",
        details={
            "reason": reason,
            "pack_type": pack_type.value if pack_type is not None else None,
            "version": version,
            "sha256": sha256,
        },
    )


async def _refuse_download(
    session: AsyncSession,
    *,
    document: dict[str, object],
    actor_user_id: UUID | None,
    reason: str,
) -> RefreshOutcome:
    """Report and audit a release that did not verify.

    The document may be unparsable, so what is recorded is what it *claimed*
    — a type it does not name reads as the first pack type rather than
    failing the refusal path itself.
    """
    pack_type = _claimed_type(document)
    version = _int_or_none(document.get("version"))
    sha256 = _str_or_none(document.get("sha256"))
    await _record_refusal(
        session,
        actor_user_id=actor_user_id,
        pack_type=pack_type,
        reason=reason,
        version=version,
        sha256=sha256,
    )
    return RefreshOutcome(
        pack_type=pack_type,
        action=RefreshAction.REFUSED,
        version=version,
        sha256=sha256,
        reason=reason,
    )


def _not_attempted(pack_type: PackType, *, reason: str) -> RefreshOutcome:
    return RefreshOutcome(pack_type=pack_type, action=RefreshAction.NOT_ATTEMPTED, reason=reason)


def _claimed_type(document: dict[str, object]) -> PackType:
    try:
        return PackType(str(document.get("pack_type")))
    except ValueError:
        return next(iter(PackType))


def _release_details(release: PackRelease) -> dict[str, object]:
    return {
        "pack_type": release.pack_type.value,
        "version": release.version,
        "sha256": release.sha256,
        "signature": release.signature is not None,
    }


def _ref(row: PackCache) -> PackRef:
    return PackRef(pack_type=row.pack_type, version=row.version, sha256=row.sha256)


def _ref_details(ref: PackRef) -> dict[str, object]:
    return {"pack_type": ref.pack_type, "version": ref.version, "sha256": ref.sha256}


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def verify_cached(row: PackCache) -> bool:
    """Whether a cached payload still hashes to the digest it was installed under.

    Exposed for the integrity read: a payload that no longer matches its
    digest means the stored copy was altered after install, which is worth
    being able to ask about without re-downloading anything.
    """
    return payload_sha256(row.payload) == row.sha256
