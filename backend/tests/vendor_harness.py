"""The cross-deployable suites' shared vendor-service harness.

Five integration suites drive the real control plane — the vendor app in a
subprocess, reachable over loopback only (``tests/control_plane_server.py``).
Each carried its own hand-rolled copy of the same two pieces; this module is
the one copy. Each suite passes its own per-suite drift:

- ``admin_key`` — the one credential the suite hands the vendor service,
  through the environment, so no secret is printed or parsed out of a
  subprocess. Every suite's value is its own.
- ``extra_env`` — the inference-channel suite additionally environment-points
  the vendor's LLM provider at its loopback stub.
- ``capture_stderr`` — the inference-channel suite keeps the subprocess's
  stderr in a file so its custody sweep can read every log line the vendor
  side captured.
- the cleanup's statement lists — ``leading`` (the registration-claim suite
  deletes its ``registrations`` row first, because it references the tenant
  the claim created), ``children`` (the tenant-scoped tables, in FK order),
  and ``vendor_level`` (the pack suites' ``pack_releases``, which has no
  tenant to scope by and is matched on the release instant).

The inference-channel suite's cleanup is the one shape that cannot join the
time-scoped builder: its rows are scoped by id (the tenant it seeded
directly), not by ``since``. It keeps its own statement list in the suite and
calls :func:`delete_vendor_rows` for the shared engine lifecycle.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy import create_engine, text

from tests.control_plane_integration import CONTROL_PLANE_DIR, REPOSITORY_ROOT

SERVER_PATH = Path(__file__).resolve().parent / "control_plane_server.py"
READY_TIMEOUT_SECONDS = 15.0

# The tenant-scoped tables every time-scoped cleanup deletes, children first
# (FK order), scoped by the tenants the run registered.
_TENANT_CHILDREN = (
    "command_queue",
    "deployment_reports",
    "activation_tokens",
    "tenant_memberships",
    "entitlements",
)


class VendorService:
    """The real control plane, in a subprocess, reachable over loopback only.

    The script binds its own socket and prints the base URL. Suites that ask
    for ``capture_stderr`` keep the subprocess's stderr in a file (readable
    through :meth:`stderr_text`) instead of a pipe.
    """

    def __init__(
        self,
        database_url: str,
        *,
        admin_key: str,
        extra_env: Mapping[str, str] | None = None,
        capture_stderr: bool = False,
    ) -> None:
        self._database_url = database_url
        self.admin_key = admin_key
        self._extra_env = dict(extra_env) if extra_env is not None else {}
        self._process: subprocess.Popen[str] | None = None
        self._files = ExitStack()
        self._stderr: IO[str] | None = None
        if capture_stderr:
            # The ExitStack above is the context manager for this file;
            # SIM115 cannot see past the assignment.
            self._stderr = self._files.enter_context(
                tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
            )
        self.base_url = ""

    def __enter__(self) -> "VendorService":
        environment = {
            **os.environ,
            "AICS_CP_DIR": str(CONTROL_PLANE_DIR),
            "AICS_CP_DATABASE_URL": self._database_url,
            "AICS_CP_VENDOR_ADMIN_KEY": self.admin_key,
            **self._extra_env,
        }
        environment.pop("PYTHONPATH", None)  # the script decides what "app" means
        self._process = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=self._stderr if self._stderr is not None else subprocess.PIPE,
            text=True,
        )
        line = "" if self._process.stdout is None else self._process.stdout.readline()
        if not line:
            if self._stderr is not None:
                detail = self.stderr_text()
            else:
                detail = "" if self._process.stderr is None else self._process.stderr.read()
            raise RuntimeError(f"the vendor service did not start: {detail}")
        self.base_url = str(json.loads(line)["base_url"])
        self._wait_until_ready()
        return self

    def __exit__(self, *_: object) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck server
                self._process.kill()
                self._process.wait(timeout=10)
        self._files.close()

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("the vendor service never answered /healthz")

    def stderr_text(self) -> str:
        """The captured stderr; only meaningful with ``capture_stderr=True``."""
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        return self._stderr.read()


def vendor_headers(vendor: VendorService) -> dict[str, str]:
    return {"X-Vendor-Admin-Key": vendor.admin_key}


def remove_vendor_rows(
    database_url: str,
    *,
    since: datetime,
    leading: Sequence[str] = (),
    children: Sequence[str] = _TENANT_CHILDREN,
    vendor_level: Sequence[str] = (),
) -> None:
    """Delete what this run created on the vendor side, children first.

    The control plane's own suites roll back; the cross-deployable ones drive
    a separate process, so its commits are real. The rows are exactly the
    ones registered during the run: ``leading`` statements first (a row that
    references the tenant the run created, deleted ahead of its children),
    then the tenant-scoped ``children`` in FK order, then ``vendor_level``
    statements for tables with no tenant to scope by, then the tenant row
    itself and the pre-activation leftovers.
    """
    statements = [
        *leading,
        *(
            f"DELETE FROM {table} WHERE tenant_id IN "
            "(SELECT id FROM tenants WHERE registered_at >= :since)"
            for table in children
        ),
        *vendor_level,
        "DELETE FROM tenants WHERE registered_at >= :since",
        "DELETE FROM activation_requests WHERE created_at >= :since",
        "DELETE FROM accounts WHERE created_at >= :since",
    ]
    delete_vendor_rows(database_url, [(statement, {"since": since}) for statement in statements])


def delete_vendor_rows(
    database_url: str, statements: Sequence[tuple[str, Mapping[str, Any]]]
) -> None:
    """The shared engine lifecycle: every statement, in order, in one transaction."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            for statement, params in statements:
                connection.execute(text(statement), params)
    finally:
        engine.dispose()
