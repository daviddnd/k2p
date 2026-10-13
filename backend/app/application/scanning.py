"""The malware-scanning seam (ADR-0009): the one place a scanner is built.

Every caller that stores or re-scans a file obtains its scanner here: the
upload route, the mail-ingestion cycle, and the retro-scan of documents that
are still ``pending``. One seam, so a deployment has one scanner and the
suite has one thing to replace — the tests substitute an in-memory fake
(``tests/fake_scanning.py``), and the real transport is covered directly in
``tests/test_clamd_client.py``.

The transport itself is an adapter detail: ``app.adapters.scan.clamd`` speaks
to clamd over TCP at the configured address, and a deployment that has none
reachable gets ``failed`` verdicts — not clean, never served, never
processed (ADR-0009).
"""

from typing import Protocol

from app.adapters.scan.clamd import ClamdScanner
from app.core.config import settings
from app.domain.scanning import ScanResult


class Scanner(Protocol):
    """What the ingestion path needs of a scanner: bytes in, verdict out.

    Structural per ADR-0006: callers depend on this shape, never on the
    clamd client, so the seam can be replaced without touching a caller.
    """

    async def scan(self, data: bytes) -> ScanResult: ...


def scanner() -> Scanner:
    """The deployment's malware scanner: clamd over TCP, per configuration."""
    return ClamdScanner(
        host=settings.scan_host,
        port=settings.scan_port,
        timeout=settings.scan_timeout_seconds,
    )
