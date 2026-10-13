"""Configuration-backed entitlement source (entitlement design D2/D4).

One of the two entitlement sources (CP1b): deployment configuration is what a
deployment that has never activated reads, and what it returns to after a
deactivation. Once activation completes, the control plane's lease is the
source and this one is not consulted ([ADR-0016], precedence in
``app.application.entitlement``).

The deployed ``AICS_LICENSE_TIER`` / ``AICS_LICENSE_EXPIRES_AT`` settings are
read through the settings singleton so a deployment — or a test — provisions
the tenant by configuration. Unconfigured means Free: the fail-safe default
is no licensed automation.

No seat/company cap overrides are read from configuration: they are
per-subscription values that arrive with the pushed lease, and the payload
shape already carries the fields for them.
"""

from app.core.config import settings
from app.domain.entitlement import EntitlementPayload, coerce_tier

# Under configuration injection there is no push ordering to track; the
# version only becomes meaningful once a lease can be replaced (D1).
CONFIGURED_ENTITLEMENT_VERSION = 1


def load_entitlement() -> EntitlementPayload:
    """The entitlement configured for this deployment (Free when unset)."""
    return EntitlementPayload(
        tier=coerce_tier(settings.license_tier),
        license_expires_at=settings.license_expires_at,
        entitlement_version=CONFIGURED_ENTITLEMENT_VERSION,
    )
