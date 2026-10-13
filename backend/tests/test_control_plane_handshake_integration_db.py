"""The two deployables, actually talking (opt-in; needs the control-plane DB).

``tests/control_plane_driver.py`` runs the real vendor service (imported from
``control_plane/``) and the real execution-plane lease client (loaded from its
file) in one subprocess, with the fake Google identity provider behind the
service, and drives the whole ADR-0016 handshake over ASGI: register →
authorize → Google → callback → code redemption with PKCE → lease.

The subprocess is not a stylistic choice. Both deployables name their package
``app``, so whichever is imported first owns the name and the second one
cannot be imported at all; inside this suite that is the execution plane, so
the vendor service is unreachable by import from here. A fresh interpreter
holds both real artifacts at once — and the client is loadable by file only
because it deliberately imports nothing from ``app.*``.

What this test is evidence for, and what it is not: it proves the client's
wire format, PKCE material, and payload expectations are the ones the vendor
service actually accepts and produces — the first end-to-end evidence that
the two deployables talk. It is not a substitute for the execution plane's own
scenarios (``test_entitlement_lease_scenarios_db.py``), which cover what the
deployment does with the lease; and it runs against the *test* configuration
of the vendor service (fake IdP), not a hosted deployment.

The command channel's own two-deployable evidence is next door
(``test_command_envelope_integration_db.py``), which serves the same service
on a loopback socket instead — the worker it has to exercise lives in this
process, so the *vendor* side is the one that has to leave it.

Skipped unless a migrated control-plane database is reachable: it takes the
URL from ``AICS_CP_TEST_DATABASE_URL``, or derives ``aics_control`` on the
same server as ``AICS_TEST_DATABASE_URL`` (locally, both live in
``deploy-db-1``). No test touches the network.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.control_plane_integration import (
    CONTROL_PLANE_DIR,
    LEASE_CLIENT_PATH,
    REPOSITORY_ROOT,
)

DRIVER_PATH = Path(__file__).resolve().parent / "control_plane_driver.py"
CLIENT_PATH = LEASE_CLIENT_PATH

# The §2.2 field set the vendor service renders (asserted exactly).
FROZEN_PAYLOAD_FIELDS = {
    "tier",
    "license_expires_at",
    "seat_cap",
    "company_cap",
    "entitlement_version",
    "issued_at",
    "signature",
}
TIERS = {"free", "standard", "enterprise_offline"}


def test_the_real_client_completes_the_handshake_with_the_real_service(
    control_plane_database: str,
) -> None:
    environment = {
        **os.environ,
        "AICS_CP_DIR": str(CONTROL_PLANE_DIR),
        "AICS_CP_DATABASE_URL": control_plane_database,
        "AICS_LEASE_CLIENT": str(CLIENT_PATH),
    }
    environment.pop("PYTHONPATH", None)  # the driver decides what "app" means
    completed = subprocess.run(
        [sys.executable, str(DRIVER_PATH)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    lease = result["lease"]

    # The deployment's client registered, redeemed its code with its PKCE
    # verifier, and was handed a credential — all against the vendor app.
    assert result["token_length"] > 20
    assert result["tenant_id"]
    # And the lease it read is the frozen §2.2 payload.
    assert set(lease) == FROZEN_PAYLOAD_FIELDS
    assert lease["tier"] in TIERS
    assert lease["tier"] == "free"  # a freshly activated tenant starts on Free
    assert lease["entitlement_version"] >= 1
    assert lease["license_expires_at"]
    # A fresh free tenant's lease pins the free plan's caps (2 seats /
    # 100 companies) — the deployment never has to derive them.
    assert lease["seat_cap"] == 2
    assert lease["company_cap"] == 100


def test_the_real_client_activates_silently_with_a_registration_code(
    control_plane_database: str,
) -> None:
    """The 2026-10-11 amendment over the real pair: a valid registration code
    is the authorization — no authorize step, no Google callback, no code
    redemption. The credential answers directly and the lease reads on it."""
    environment = {
        **os.environ,
        "AICS_CP_DIR": str(CONTROL_PLANE_DIR),
        "AICS_CP_DATABASE_URL": control_plane_database,
        "AICS_LEASE_CLIENT": str(CLIENT_PATH),
        "AICS_HANDSHAKE_MODE": "silent",
    }
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(DRIVER_PATH)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    lease = result["lease"]

    assert result["silent"] is True
    assert result["token_length"] > 20
    assert result["tenant_id"]
    assert set(lease) == FROZEN_PAYLOAD_FIELDS
    assert lease["tier"] == "free"
    assert lease["entitlement_version"] >= 1
    assert lease["seat_cap"] == 2
    assert lease["company_cap"] == 100
