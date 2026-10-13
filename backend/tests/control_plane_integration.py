"""Where the execution plane's integration tests find the vendor service.

The vendor-backed integration suites drive the real control plane — the
vendor app in a subprocess, over ASGI or served on loopback. They all answer
the same two questions first — which database does the vendor service use,
and is it migrated — and none may touch anything but loopback, so the
answers live here (the fixture that skips when the database is missing is in
``conftest.py``):

- ``AICS_CP_TEST_DATABASE_URL`` names it directly;
- otherwise ``aics_control`` is derived on the same server as
  ``AICS_TEST_DATABASE_URL`` (locally both databases live in ``deploy-db-1``).

It also owns the mechanism every vendor-backed suite shares at the
activation callback: the vendor commits its one-time code in a
yield-dependency teardown, after the 302 back to the deployment has already
left, so the redemption is polled rather than shot once (see
``redeem_activation_code``).
"""

import os
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_DIR = REPOSITORY_ROOT / "control_plane"
# The one execution-plane module the driver subprocess may load by file: it
# deliberately imports nothing from ``app.*`` (see its docstring).
LEASE_CLIENT_PATH = (
    REPOSITORY_ROOT / "backend" / "app" / "adapters" / "entitlement" / "control_plane_client.py"
)
# The redemption bound for the activation callback: the vendor commits its
# one-time code in a yield-dependency teardown, after the 302 back to the
# deployment has already left (control_plane/app/adapters/persistence/
# session.py:21-22), so at loopback speed the callback can outrun the commit
# and meet ``code_refused:invalid_code``. A code refused as unknown was not
# consumed — the deployment keeps its pending attempt, and the handshake
# itself treats a lost response as retryable — so the redemption is polled
# on exactly that refusal, with a hard deadline.
CALLBACK_REDEEM_ATTEMPTS = 10
CALLBACK_REDEEM_WAIT_SECONDS = 0.2
# Owned here so there is exactly one redemption mechanism; the suites keep
# their own ACTIVATION_URL constants for their browser legs.
ACTIVATION_CALLBACK_URL = "/api/v1/entitlement/activation/callback"


def redeem_activation_code(client: TestClient, code: str, state: str) -> None:
    """Redeem the one-time code, tolerating the vendor's post-302 commit lag.

    Only ``code_refused:invalid_code`` is retried — the one outcome the
    not-yet-committed race produces; any other refusal fails on its first
    occurrence. When the deadline passes the final response is asserted with
    its body, so an exceeded bound is a clear failure, never a hang.
    """
    url = f"{ACTIVATION_CALLBACK_URL}?code={code}&state={state}"
    completed: httpx.Response | None = None
    for attempt in range(CALLBACK_REDEEM_ATTEMPTS):
        completed = client.get(url)
        if completed.status_code == 200:
            return
        if "code_refused:invalid_code" not in completed.text:
            break
        if attempt < CALLBACK_REDEEM_ATTEMPTS - 1:
            time.sleep(CALLBACK_REDEEM_WAIT_SECONDS)
    assert completed is not None
    bound = CALLBACK_REDEEM_ATTEMPTS * CALLBACK_REDEEM_WAIT_SECONDS
    assert completed.status_code == 200, (
        "the vendor never committed the one-time code within "
        f"~{bound:.1f}s of its redirect — {completed.status_code}: {completed.text}"
    )


def control_plane_database_url() -> str | None:
    """The control-plane database, named directly or derived from the local pair."""
    explicit = os.environ.get("AICS_CP_TEST_DATABASE_URL")
    if explicit:
        return explicit
    execution_plane = os.environ.get("AICS_TEST_DATABASE_URL")
    if execution_plane is None:
        return None
    # render_as_string, not str(): URL.__str__ masks the password, and the
    # masked form cannot connect.
    return make_url(execution_plane).set(database="aics_control").render_as_string(
        hide_password=False
    )


def is_migrated(url: str) -> bool:
    """Whether the control-plane schema is there (an empty database is not)."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return (
                connection.execute(
                    text("SELECT to_regclass('public.activation_requests')")
                ).scalar()
                is not None
            )
    except (OperationalError, SQLAlchemyError):
        return False
    finally:
        engine.dispose()
