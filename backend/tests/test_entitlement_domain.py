"""Domain tests for the entitlement vocabulary and its derived rules
(ADR-0012/0016; data-model §2.2 payload shape, §4.3 licence state machine).

Pure and database-free: the tier → capability/cap matrices, the §4.3
``active → grace (7 days) → free`` derivation at its boundaries, the
payload's derived accessors (per-subscription overrides win over the tier
defaults), the fail-closed coercion of unknown tier values, and — with the
control-plane lease client (CP1b) — the defensive parse of a lease body and
the two windows that pace a refresh.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.entitlement import (
    GRACE_PERIOD,
    EntitlementPayload,
    LeasePayloadInvalid,
    LicenseState,
    Tier,
    capabilities_for_tier,
    caps_for_tier,
    coerce_tier,
    free_entitlement,
    license_state_for,
    parse_lease_payload,
    refresh_due,
)
from app.domain.service_packages import AutomationType

ALL_CAPABILITIES = frozenset(AutomationType)
LICENSED_TIERS = (Tier.STANDARD, Tier.ENTERPRISE_OFFLINE)
EXPIRY = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


# --- tier → capability / cap matrices ---


def test_free_tier_carries_no_capabilities() -> None:
    assert capabilities_for_tier(Tier.FREE) == frozenset()


def test_licensed_tiers_carry_the_full_vocabulary() -> None:
    for tier in LICENSED_TIERS:
        assert capabilities_for_tier(tier) == ALL_CAPABILITIES


def test_document_extraction_is_a_licenced_capability() -> None:
    """Founder decision 2026-09-21: storage is ungated, extraction is not."""
    assert AutomationType.DOCUMENT_EXTRACTION.value == "document_extraction"
    assert AutomationType.DOCUMENT_EXTRACTION in capabilities_for_tier(Tier.STANDARD)


def test_cap_matrix() -> None:
    # Free = 2 seats (the bootstrap Administrator counts) and 100 companies
    # (ADR-0012, finalized 2026-09-25); licensed tiers are per
    # subscription/license (no fixed cap).
    assert caps_for_tier(Tier.FREE) == (2, 100)
    for tier in LICENSED_TIERS:
        assert caps_for_tier(tier) == (None, None)


# --- §4.3 licence state derivation at the boundaries ---


def test_no_expiry_is_perpetual_active() -> None:
    assert license_state_for(None, now=datetime(2030, 1, 1, tzinfo=UTC)) is LicenseState.ACTIVE


def test_not_yet_expired_is_active() -> None:
    assert license_state_for(EXPIRY, now=EXPIRY - timedelta(seconds=1)) is LicenseState.ACTIVE
    # The lease covers its expiry instant; grace begins just after it.
    assert license_state_for(EXPIRY, now=EXPIRY) is LicenseState.ACTIVE


def test_expired_by_exactly_seven_days_is_grace() -> None:
    assert license_state_for(EXPIRY, now=EXPIRY + timedelta(microseconds=1)) is LicenseState.GRACE
    assert license_state_for(EXPIRY, now=EXPIRY + GRACE_PERIOD) is LicenseState.GRACE


def test_expired_by_seven_days_and_one_second_is_free() -> None:
    now = EXPIRY + GRACE_PERIOD + timedelta(seconds=1)
    assert license_state_for(EXPIRY, now=now) is LicenseState.FREE


def test_naive_expiry_is_interpreted_as_utc() -> None:
    """A deployment value without a timezone reads as the repo's UTC clock."""
    naive = EXPIRY.replace(tzinfo=None)
    assert license_state_for(naive, now=EXPIRY - timedelta(hours=1)) is LicenseState.ACTIVE
    assert license_state_for(naive, now=EXPIRY + GRACE_PERIOD) is LicenseState.GRACE
    assert (
        license_state_for(naive, now=EXPIRY + GRACE_PERIOD + timedelta(seconds=1))
        is LicenseState.FREE
    )


# --- unknown values fail closed ---


def test_unknown_tier_fails_closed_to_free() -> None:
    assert coerce_tier("bogus") is Tier.FREE
    assert coerce_tier("") is Tier.FREE
    assert coerce_tier(Tier.STANDARD) is Tier.STANDARD
    assert capabilities_for_tier("bogus") == frozenset()
    assert caps_for_tier("bogus") == (2, 100)


# --- the frozen payload's derived accessors ---


def test_payload_derives_state_capabilities_and_caps() -> None:
    payload = EntitlementPayload(tier=Tier.STANDARD, license_expires_at=EXPIRY)

    assert payload.license_state_at(now=EXPIRY - timedelta(days=1)) is LicenseState.ACTIVE
    assert payload.capabilities_at(now=EXPIRY - timedelta(days=1)) == ALL_CAPABILITIES
    assert payload.effective_seat_cap is None
    assert payload.effective_company_cap is None
    assert payload.entitlement_version == 1


def test_payload_free_tier_derives_the_free_caps() -> None:
    payload = EntitlementPayload(tier=Tier.FREE)
    assert payload.effective_seat_cap == 2
    assert payload.effective_company_cap == 100
    assert payload.capabilities_at(now=EXPIRY) == frozenset()


def test_payload_overrides_win_over_the_tier_defaults() -> None:
    payload = EntitlementPayload(
        tier=Tier.STANDARD, seat_cap=5, company_cap=250, entitlement_version=7
    )
    assert payload.effective_seat_cap == 5
    assert payload.effective_company_cap == 250
    assert payload.entitlement_version == 7
    # An explicit override also applies to the Free tier; the cap it does not
    # override still derives from the tier default.
    payload = EntitlementPayload(tier=Tier.FREE, seat_cap=5)
    assert payload.effective_seat_cap == 5
    assert payload.effective_company_cap == 100


def test_grace_keeps_the_tier_capabilities_and_free_degrades_to_the_free_set() -> None:
    payload = EntitlementPayload(tier=Tier.STANDARD, license_expires_at=EXPIRY)

    in_grace = EXPIRY + timedelta(days=3)
    assert payload.license_state_at(now=in_grace) is LicenseState.GRACE
    assert payload.capabilities_at(now=in_grace) == ALL_CAPABILITIES

    degraded = EXPIRY + GRACE_PERIOD + timedelta(seconds=1)
    assert payload.license_state_at(now=degraded) is LicenseState.FREE
    assert payload.capabilities_at(now=degraded) == frozenset()


def test_payload_coerces_unknown_tiers_to_free() -> None:
    payload = EntitlementPayload(tier="enterprise")  # type: ignore[arg-type]
    assert payload.tier is Tier.FREE
    assert payload.capabilities_at(now=EXPIRY) == frozenset()
    assert payload.effective_seat_cap == 2


# --- the leased payload: what the control plane sends (CP1b) ---


def test_a_lease_body_parses_into_the_frozen_payload() -> None:
    payload = parse_lease_payload(
        {
            "tier": "standard",
            "license_expires_at": EXPIRY.isoformat(),
            "seat_cap": 5,
            "company_cap": 250,
            "entitlement_version": 4,
            # Reserved fields the signed-lease slice will define; accepted
            # and ignored today.
            "issued_at": EXPIRY.isoformat(),
            "signature": None,
        }
    )

    assert payload.tier is Tier.STANDARD
    assert payload.license_expires_at == EXPIRY
    assert payload.effective_seat_cap == 5
    assert payload.effective_company_cap == 250
    assert payload.entitlement_version == 4
    assert payload.capabilities_at(now=EXPIRY - timedelta(days=1)) == ALL_CAPABILITIES


def test_a_lease_body_without_caps_derives_them_from_the_tier() -> None:
    payload = parse_lease_payload(
        {
            "tier": "free",
            "license_expires_at": EXPIRY.isoformat(),
            "seat_cap": None,
            "company_cap": None,
            "entitlement_version": 1,
        }
    )

    assert payload.effective_seat_cap == 2
    assert payload.effective_company_cap == 100


@pytest.mark.parametrize(
    "body",
    [
        # Missing fields, one at a time.
        {"license_expires_at": EXPIRY.isoformat(), "entitlement_version": 1},
        {"tier": "standard", "entitlement_version": 1},
        {"tier": "standard", "license_expires_at": EXPIRY.isoformat()},
        # An unknown tier is refused, never coerced (the config path coerces;
        # a leased payload that names a tier nobody defines must not).
        {
            "tier": "premium",
            "license_expires_at": EXPIRY.isoformat(),
            "entitlement_version": 1,
        },
        # Types the payload cannot carry.
        {"tier": 7, "license_expires_at": EXPIRY.isoformat(), "entitlement_version": 1},
        {"tier": "standard", "license_expires_at": 0, "entitlement_version": 1},
        {
            "tier": "standard",
            "license_expires_at": "yesterday",
            "entitlement_version": 1,
        },
        # A lease always expires: null is not a perpetual licence here.
        {"tier": "standard", "license_expires_at": None, "entitlement_version": 1},
        {
            "tier": "standard",
            "license_expires_at": EXPIRY.isoformat(),
            "entitlement_version": "one",
        },
        {
            "tier": "standard",
            "license_expires_at": EXPIRY.isoformat(),
            "entitlement_version": True,
        },
        {
            "tier": "standard",
            "license_expires_at": EXPIRY.isoformat(),
            "entitlement_version": 1,
            "seat_cap": "five",
        },
    ],
)
def test_an_unusable_lease_body_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(LeasePayloadInvalid) as refusal:
        parse_lease_payload(body)

    # The reason names a field, never a value: it is stored in the lease
    # cache and shown to an administrator.
    assert ":" in refusal.value.reason or refusal.value.reason in {
        "unknown_tier",
        "missing_field:tier",
    }


def test_the_free_fallback_is_free() -> None:
    payload = free_entitlement()

    assert payload.tier is Tier.FREE
    assert payload.capabilities_at(now=EXPIRY) == frozenset()
    assert payload.license_expires_at is None


# --- refresh pacing (job-free, so the read path does the pacing) ---

INTERVAL = timedelta(hours=24)
BACKOFF = timedelta(minutes=30)


def test_a_lease_that_was_never_fetched_is_due() -> None:
    assert refresh_due(
        fetched_at=None, last_attempt_at=None, now=EXPIRY, interval=INTERVAL, backoff=BACKOFF
    )


def test_a_lease_inside_the_interval_is_not_due() -> None:
    assert not refresh_due(
        fetched_at=EXPIRY - timedelta(hours=23),
        last_attempt_at=EXPIRY - timedelta(hours=23),
        now=EXPIRY,
        interval=INTERVAL,
        backoff=BACKOFF,
    )


def test_a_stale_lease_is_due_once_the_backoff_has_elapsed() -> None:
    stale = EXPIRY - timedelta(days=30)

    assert not refresh_due(
        fetched_at=stale,
        last_attempt_at=EXPIRY - timedelta(minutes=29),
        now=EXPIRY,
        interval=INTERVAL,
        backoff=BACKOFF,
    )
    assert refresh_due(
        fetched_at=stale,
        last_attempt_at=EXPIRY - timedelta(minutes=31),
        now=EXPIRY,
        interval=INTERVAL,
        backoff=BACKOFF,
    )
    # fetched_at moves on success only, so a deployment whose refreshes keep
    # failing stays due — and keeps retrying at the backoff cadence — until
    # the §4.3 state machine has degraded it.
    assert refresh_due(
        fetched_at=None,
        last_attempt_at=EXPIRY - timedelta(minutes=31),
        now=EXPIRY,
        interval=INTERVAL,
        backoff=BACKOFF,
    )


def test_a_naive_timestamp_is_read_as_utc() -> None:
    naive_attempt = (EXPIRY - timedelta(minutes=5)).replace(tzinfo=None)
    assert not refresh_due(
        fetched_at=None,
        last_attempt_at=naive_attempt,
        now=EXPIRY,
        interval=INTERVAL,
        backoff=BACKOFF,
    )
