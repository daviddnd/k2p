"""An in-memory scanner: bytes in, a staged verdict out.

The suite's stand-in for the malware scanner
(``app.application.scanning.scanner``), installed by ``tests/conftest.py`` so
every test that stores or re-scans a file gets a verdict without a socket and
without depending on a scanner being installed on the machine. The real
transport is covered directly in ``test_clamd_client.py``.

Verdicts are staged per payload, with one default for everything else:
``clean`` unless a test says otherwise. ``scanned`` records what was handed to
the scanner, in order, which is how the ingestion-order tests check that the
scan happened *before* anything was written.
"""

from dataclasses import dataclass, field

from app.domain.scanning import ScanResult, ScanVerdict

CLEAN = ScanResult(verdict=ScanVerdict.CLEAN)

# The archived sample signature; a name ClamAV really reports, so a test that
# asserts on a detection is asserting on the shape a deployment would see.
EICAR_SIGNATURE = "Eicar-Test-Signature"


def infected(signature: str = EICAR_SIGNATURE) -> ScanResult:
    return ScanResult(verdict=ScanVerdict.INFECTED, signature=signature)


def failed(reason: str = "unavailable:ConnectionRefusedError") -> ScanResult:
    return ScanResult(verdict=ScanVerdict.FAILED, reason=reason)


@dataclass
class FakeScanner:
    """A ``Scanner`` whose answers a test sets before the call."""

    default: ScanResult = CLEAN
    by_content: dict[bytes, ScanResult] = field(default_factory=dict)
    scanned: list[bytes] = field(default_factory=list)

    async def scan(self, data: bytes) -> ScanResult:
        self.scanned.append(data)
        return self.by_content.get(data, self.default)
