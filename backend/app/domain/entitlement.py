"""Entitlement vocabulary and the tier/licence rules derived from it
(ADR-0012, ADR-0016; data-model §2.2 payload shape, §4.3 licence state
machine).

Capabilities, seat/company caps, and licence state are **derived, never
stored** (entitlement design D3/D4): the payload is the frozen shape the
control plane will push, and every accessor here computes what it means at
read time, so the source can be swapped — deployment configuration today,
an activation lease later — without changing this module or its callers.

Two rules worth stating outright:

- Unknown or invalid tier values **fail closed to ``free``** (a misconfigured
  deployment gets no licensed automation, never the benefit of the doubt).
- A ``free`` licence state degrades **any** tier to the Free capability set
  (§4.3), which is what makes Standard → Free degradation observable; the
  ``grace`` state retains the tier's full capabilities. Caps follow the
  provisioned tier (per-subscription overrides win) rather than the state:
  they bound creation, and a lapsed lease is never a data lock-out (ADR-0012).

The second source of a payload — the control plane's lease, which replaces
deployment configuration once a deployment activates (ADR-0016) — is
validated here too (``parse_lease_payload``): what the vendor sends is
refused rather than interpreted when it is not the frozen shape, because the
alternative is a malformed body silently granting or removing automation.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.domain.service_packages import AutomationType

# §4.3: definitive per founder direction 2026-08-19 (superseding the
# original 30-day proposal).
GRACE_PERIOD = timedelta(days=7)

_LICENSED_CAPABILITIES = frozenset(AutomationType)


class Tier(StrEnum):
    """The §2.2 provisioning tiers. A tier is provisioned, never set through
    the product API (design D5) — commercial gating is not user-editable."""

    FREE = "free"
    STANDARD = "standard"
    ENTERPRISE_OFFLINE = "enterprise_offline"


class LicenseState(StrEnum):
    """The §4.3 licence states; every state is derived at read time (D4)."""

    ACTIVE = "active"
    GRACE = "grace"
    FREE = "free"


class EntitlementSource(StrEnum):
    """Where the entitlement in force came from (the CP1b precedence rule).

    A deployment that has activated takes its entitlement from the lease the
    control plane issued and never from deployment configuration again; one
    that has not activated (or has deactivated) reads configuration, exactly
    as it did before the control plane existed.
    """

    CONFIGURATION = "configuration"
    LEASE = "lease"


def coerce_tier(value: Tier | str) -> Tier:
    """The tier a configured or leased value names; unknown → ``free``.

    Fail-closed by construction: the values reaching this function come from
    deployment configuration (later, from a pushed lease), never from client
    input, so an unrecognized value must not fall through to a licensed tier.
    """
    try:
        return Tier(value)
    except ValueError:
        return Tier.FREE


def capabilities_for_tier(tier: Tier | str) -> frozenset[AutomationType]:
    """The §2.2 capability flags a tier grants: none on Free, all on
    Standard/Enterprise Offline (every capability is Standard+)."""
    if coerce_tier(tier) is Tier.FREE:
        return frozenset()
    return _LICENSED_CAPABILITIES


def caps_for_tier(tier: Tier | str) -> tuple[int | None, int | None]:
    """The tier's ``(seat_cap, company_cap)``.

    Free = 2 seats (the bootstrap Administrator counts) and 100 companies
    (ADR-0012, finalized 2026-09-25, superseding the 2026-08-14 numbers).
    This is the fallback a never-activated deployment — or a lease whose caps
    are ``NULL`` — enforces, and it agrees with the free plan the control
    plane pins into every new tenant's lease (ADR-0018). A lease's own caps
    still win; ``None`` here means "per subscription/license".
    """
    if coerce_tier(tier) is Tier.FREE:
        return (2, 100)
    return (None, None)


def _as_utc(value: datetime) -> datetime:
    """``value`` as an aware UTC datetime (a naive one is read as UTC)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def license_state_for(license_expires_at: datetime | None, now: datetime) -> LicenseState:
    """The §4.3 state for a lease expiry evaluated at ``now``.

    ``None`` (no expiry) is perpetual → ``active``. The lease covers its
    expiry instant; ``grace`` then runs for exactly ``GRACE_PERIOD`` and the
    state falls to ``free`` after that. A naive ``license_expires_at`` (a
    deployment value without a timezone) is read as UTC, matching the clock
    the execution plane uses (``datetime.now(UTC)``).
    """
    if license_expires_at is None:
        return LicenseState.ACTIVE
    expires_at = (
        license_expires_at
        if license_expires_at.tzinfo is not None
        else license_expires_at.replace(tzinfo=UTC)
    )
    if now <= expires_at:
        return LicenseState.ACTIVE
    if now - expires_at <= GRACE_PERIOD:
        return LicenseState.GRACE
    return LicenseState.FREE


def refresh_due(
    *,
    fetched_at: datetime | None,
    last_attempt_at: datetime | None,
    now: datetime,
    interval: timedelta,
    backoff: timedelta,
) -> bool:
    """Whether the read path should attempt a lease refresh now.

    Two windows, both read from the lease cache (ADR-0016 point 2, CP1b):
    the lease is due once it is older than ``interval`` (or was never
    fetched), and an attempt made within ``backoff`` holds the next one off.
    The backoff is what keeps a control-plane outage from making every read
    wait on a timeout — and because ``fetched_at`` only moves on success, a
    deployment whose refreshes keep failing stays due and retries at the
    backoff cadence until the lease ages out through §4.3.
    """
    if last_attempt_at is not None and now - _as_utc(last_attempt_at) < backoff:
        return False
    return fetched_at is None or now - _as_utc(fetched_at) >= interval


@dataclass(frozen=True)
class EntitlementPayload:
    """The frozen entitlement shape (contract §2.2, 2026-09-21).

    ``seat_cap``/``company_cap`` are per-subscription overrides — ``None``
    derives from the tier. ``entitlement_version`` is the ordering token for
    pushed leases; under configuration injection it is constant. The lease's
    ``issued_at``/``signature`` are reserved and deliberately absent from this
    dataclass — the lease client accepts and ignores them until the
    signed-lease slice gives them meaning.
    """

    tier: Tier
    license_expires_at: datetime | None = None
    seat_cap: int | None = None
    company_cap: int | None = None
    entitlement_version: int = 1

    def __post_init__(self) -> None:
        # Configured values arrive as plain strings: coerce once here so every
        # accessor and caller can rely on a valid tier (fail closed to free).
        object.__setattr__(self, "tier", coerce_tier(self.tier))

    def license_state_at(self, *, now: datetime) -> LicenseState:
        """The §4.3 state at ``now``."""
        return license_state_for(self.license_expires_at, now)

    def capabilities_at(self, *, now: datetime) -> frozenset[AutomationType]:
        """The capability set the licence actually grants at ``now``.

        A ``free`` state degrades any tier to the Free capability set (§4.3);
        ``active`` and ``grace`` carry the provisioned tier's set.
        """
        if self.license_state_at(now=now) is LicenseState.FREE:
            return capabilities_for_tier(Tier.FREE)
        return capabilities_for_tier(self.tier)

    @property
    def effective_seat_cap(self) -> int | None:
        """The seat cap in force: the payload override, else the tier default."""
        return self.seat_cap if self.seat_cap is not None else caps_for_tier(self.tier)[0]

    @property
    def effective_company_cap(self) -> int | None:
        """The company cap in force: the payload override, else the tier default."""
        return self.company_cap if self.company_cap is not None else caps_for_tier(self.tier)[1]


class LeasePayloadInvalid(Exception):
    """The control plane's lease body is not the frozen §2.2 payload.

    ``reason`` is a stable machine-readable code; the read path records it in
    the lease cache's ``last_error`` and keeps whatever it already had. It is
    never rendered to a client as-is, and it names a field, not a value, so a
    bad body cannot leak its contents back into the deployment's records.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def parse_lease_payload(raw: Mapping[str, object]) -> EntitlementPayload:
    """The frozen entitlement payload from a lease body, or a refusal.

    Fail-closed by construction, and deliberately **not** coercing: a body
    the vendor service could not have produced (a missing field, an unknown
    tier, a cap that is not an integer, an unparsable or absent expiry) is
    refused rather than read as Free or as the licensed tier it names — the
    caller keeps the cached lease, or Free when there is none. The reserved
    fields of the §2.2 shape (``issued_at``, ``signature``) are accepted and
    ignored until the signed-lease slice gives them meaning; unknown extra
    fields are ignored for the same reason (the vendor may add to the shape,
    it may not take away).
    """
    tier_value = raw.get("tier")
    if not isinstance(tier_value, str):
        raise LeasePayloadInvalid("missing_field:tier")
    try:
        tier = Tier(tier_value)
    except ValueError:
        raise LeasePayloadInvalid("unknown_tier") from None

    # The lease always expires: the CP's own response model is non-nullable,
    # and a perpetual licence is Enterprise Offline's signed file, not this
    # endpoint (ADR-0016).
    return EntitlementPayload(
        tier=tier,
        license_expires_at=_required_timestamp(raw, "license_expires_at"),
        seat_cap=_optional_cap(raw, "seat_cap"),
        company_cap=_optional_cap(raw, "company_cap"),
        entitlement_version=_required_int(raw, "entitlement_version"),
    )


def free_entitlement() -> EntitlementPayload:
    """The fail-closed payload of an activated deployment with no cached lease.

    Activation is what makes the lease the source: once a deployment holds a
    credential, an absent or refused lease means no licensed automation —
    never the tier the local configuration still names, which only a
    deployment that has not activated is entitled to read.
    """
    return EntitlementPayload(tier=Tier.FREE)


def _required_timestamp(raw: Mapping[str, object], field: str) -> datetime:
    value = raw.get(field)
    if not isinstance(value, str):
        raise LeasePayloadInvalid(f"missing_field:{field}")
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        raise LeasePayloadInvalid(f"invalid_field:{field}") from None


def _required_int(raw: Mapping[str, object], field: str) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LeasePayloadInvalid(f"invalid_field:{field}")
    return value


def _optional_cap(raw: Mapping[str, object], field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    return _required_int(raw, field)
