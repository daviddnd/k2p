"""Shared fixtures for the scenario suites.

Pytest discovers fixtures from this module. Three, all opt-in:

- ``activation_credential_file`` (autouse): every test gets its own credential
  file path, so no test can read — or write — the activation credential of the
  machine it runs on, and the entitlement source a test sees is always the one
  it set up itself.
- ``mail_credential_file`` (autouse): the same for the mailbox credential.
- ``calendar_credential_dir`` (autouse): the same for the calendar
  provider-credential directory (slice M8) — one 0600 file per tenant, never
  a suite running against the machine's real custody paths.
- ``tenant_key_file`` (autouse): the same for the per-tenant encryption key
  (slice D1) — a test must never read — or write — the key of the machine it
  runs on.
- ``scanner_seam`` (autouse): the malware scanner is replaced by an in-memory
  fake, so no suite needs a scanner installed and no test can reach a real
  clamd. Tests that need a detection or a scanner failure stage it on the
  returned fake; ``test_clamd_client.py`` drives the real client against the
  fake daemon in ``tests/fake_clamd.py``.
- ``control_plane_database``: the migrated vendor-side database, for the suites
  that drive the real control plane (``tests/control_plane_integration.py``
  holds the URL derivation); skips with a reason when it is not there.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.application import login_throttle, scanning
from app.core.config import settings
from tests.control_plane_integration import (
    CONTROL_PLANE_DIR,
    control_plane_database_url,
    is_migrated,
)
from tests.fake_scanning import FakeScanner


@pytest.fixture(autouse=True)
def activation_credential_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the deployment's credential file at a per-test temporary path."""
    path = tmp_path / "activation-credential"
    monkeypatch.setattr(settings, "cp_activation_token_file", path)
    yield path


@pytest.fixture(autouse=True)
def mail_credential_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the mailbox credential file at a per-test temporary path.

    Same reason as the activation file: a test must never read — or write —
    the mailbox credential of the machine it runs on.
    """
    path = tmp_path / "mail-credential"
    monkeypatch.setattr(settings, "mail_credential_file", path)
    yield path


@pytest.fixture(autouse=True)
def calendar_credential_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the calendar provider-credential directory at a per-test path.

    Same reason as the other credential paths: ADR-0014 custody, which a suite
    must never read — or write — on the machine it runs on.
    """
    path = tmp_path / "calendar-credentials"
    monkeypatch.setattr(settings, "calendar_credential_dir", path)
    yield path


@pytest.fixture(autouse=True)
def tenant_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the tenant encryption key at a per-test temporary path.

    Same reason as the credential files: the key is ADR-0014 custody, and a
    suite must never read — or write — the key of the machine it runs on.
    """
    path = tmp_path / "tenant-key"
    monkeypatch.setattr(settings, "tenant_key_file", path)
    yield path


@pytest.fixture(autouse=True)
def scanner_seam(monkeypatch: pytest.MonkeyPatch) -> FakeScanner:
    """Replace the deployment's scanner with an in-memory fake.

    Same reason as the credential files: a suite must not depend on — or talk
    to — anything on the machine it runs on. Every test gets a fresh fake that
    answers ``clean`` until the test says otherwise.
    """
    fake = FakeScanner()
    monkeypatch.setattr(scanning, "scanner", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def login_throttle_state() -> Iterator[None]:
    """Reset the login-throttle registry (slice ②) around every test.

    The registry is process-wide, and the source key is shared by everything
    that posts to ``/auth/login`` from one process: without the reset, one
    suite's failed sign-ins would heat the key another suite's logins must
    pass. Each test starts — and leaves — clean.
    """
    login_throttle._ledgers.clear()
    yield
    login_throttle._ledgers.clear()


@pytest.fixture(scope="module")
def control_plane_database() -> str:
    """The migrated control-plane database, or a skip explaining what is missing."""
    url = control_plane_database_url()
    if url is None:
        pytest.skip("set AICS_TEST_DATABASE_URL (or AICS_CP_TEST_DATABASE_URL)")
    if not CONTROL_PLANE_DIR.is_dir():
        pytest.skip("control_plane/ is not in this checkout")
    if not is_migrated(url):
        pytest.skip(f"control-plane database at {url.rsplit('/', 1)[-1]} is not migrated")
    return url
