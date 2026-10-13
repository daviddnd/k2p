"""Malware scanning: the verdict vocabulary and clamd's answer (ADR-0009).

ADR-0009 fixes the ingestion order — quarantine → scan → hash → register →
store — and one rule that outlives it: unscanned files are never processed or
served. The interim posture that suspended the rule for internal deployments
(founder-approved 2026-08-25) closed when ClamAV landed with the Phase 3
ingestion slice; from then on a stored file carries the outcome of a real
scan, and a file that could not be scanned stays quarantined.

This module is the pure half: what a scan can conclude, and how clamd's one
line of INSTREAM output becomes one of those conclusions. The transport is
``app.adapters.scan.clamd``.

A verdict of ``failed`` is a value, deliberately not an exception: a scanner
that cannot answer must not be able to abort an ingestion silently, and every
caller that treats "not clean" as "not usable" then stays fail-closed without
error handling of its own.

Pure and framework-free per ADR-0006.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.documents import ScanStatus

# ClamAV signature names and daemon errors are short; a bound keeps a drifted
# or hostile answer from travelling onward, and both travel only into logs,
# audits, and response bodies — never an error message that carries content.
MAX_SCAN_TEXT_LENGTH = 120

# clamd's INSTREAM answer is NUL-terminated when the command is ``zINSTREAM``.
TERMINATOR = b"\x00"

_OK = "OK"
_FOUND = "FOUND"
_ERROR = "ERROR"


class ScanVerdict(StrEnum):
    """What one scan concluded about one file."""

    CLEAN = "clean"
    INFECTED = "infected"
    # Not a verdict at all: the scanner could not be reached, timed out, or
    # answered something this module does not recognise. Fail closed — the
    # caller must treat it exactly like "not clean".
    FAILED = "failed"


@dataclass(frozen=True)
class ScanResult:
    """One scan's outcome: the verdict, plus why when there is a why.

    ``signature`` is ClamAV's own detection name (``Eicar-Signature``) and is
    present only for an infection; ``reason`` is a short code — the daemon's
    error text, or the transport's — and is present only for a failure.
    """

    verdict: ScanVerdict
    signature: str | None = None
    reason: str | None = None


_SCAN_STATUS_BY_VERDICT: dict[ScanVerdict, ScanStatus] = {
    ScanVerdict.CLEAN: ScanStatus.CLEAN,
    ScanVerdict.INFECTED: ScanStatus.INFECTED,
    # Neither clean nor evidence of malware: the file stays ``pending`` — the
    # quarantined state the retro-scan resolves later (ADR-0009).
    ScanVerdict.FAILED: ScanStatus.PENDING,
}


def scan_status_for(verdict: ScanVerdict) -> ScanStatus:
    """The document scan status a verdict implies (never ``clean`` on doubt)."""
    return _SCAN_STATUS_BY_VERDICT[verdict]


def parse_clamd_response(answer: bytes) -> ScanResult:
    """clamd's INSTREAM answer as a verdict.

    The wire shape is ``<stream>: OK``, ``<stream>: <signature> FOUND``, or
    ``<stream>: <message> ERROR`` — one line, NUL-terminated because the
    command was ``zINSTREAM``. Anything else (an empty read, a body this
    module does not recognise, a bare signature without the ``FOUND`` marker)
    is ``failed``: a scanner whose answer cannot be read is a scanner that did
    not say "clean", and that must never be treated as one.
    """
    text = answer.split(TERMINATOR, 1)[0].decode("utf-8", errors="replace").strip()
    if not text:
        return ScanResult(verdict=ScanVerdict.FAILED, reason="empty_response")
    # The prefix is the name this client sent (``stream``); the verdict is
    # after it. A line without a prefix is still read, so a daemon that
    # answers bare does not turn every scan into a failure.
    _, separator, tail = text.partition(": ")
    rest = tail if separator else text
    if rest == _OK:
        return ScanResult(verdict=ScanVerdict.CLEAN)
    if rest.endswith(f" {_FOUND}"):
        signature = rest[: -len(_FOUND) - 1].strip()
        if signature:
            return ScanResult(
                verdict=ScanVerdict.INFECTED, signature=signature[:MAX_SCAN_TEXT_LENGTH]
            )
        return ScanResult(verdict=ScanVerdict.FAILED, reason="unrecognized_response")
    if rest.endswith(f" {_ERROR}"):
        reason = rest[: -len(_ERROR) - 1].strip() or "clamd_error"
        return ScanResult(verdict=ScanVerdict.FAILED, reason=reason[:MAX_SCAN_TEXT_LENGTH])
    return ScanResult(verdict=ScanVerdict.FAILED, reason="unrecognized_response")
