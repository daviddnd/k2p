"""The clamd client against a fake daemon, without a socket (ADR-0009).

The real client — the real INSTREAM framing, the real reads — driven through
its connection seam by ``tests/fake_clamd.py``, which parses what the client
writes and answers the way clamd does. What is exercised is what a deployment
meets: a scanner that is not running, one that stalls, one whose answer is
truncated, a detection, and a daemon-side error.

The rules under test are the fail-closed ones: a transport that cannot
connect, stalls, or answers something unreadable produces a ``failed``
verdict — never an exception the caller could forget to catch, and never a
``clean``. And one rule about the wire: the whole payload is streamed, in
chunks, terminated by a zero-length chunk, so clamd scans the bytes this
deployment holds rather than a path it would have to share a filesystem for.
"""

import pytest

from app.adapters.scan.clamd import CHUNK_BYTES, ClamdScanner
from app.application import scanning
from app.domain.scanning import ScanVerdict
from tests.fake_clamd import ConnectionRefused, FakeClamd

# Captured before the autouse fixture replaces the seam, so the one test that
# is about the factory can call the deployment's own function.
REAL_SCANNER = scanning.scanner


def scanner_for(clamd: FakeClamd, *, timeout: float = 1.0) -> ClamdScanner:
    return ClamdScanner(host="clamav", port=3310, timeout=timeout, connect=clamd.connect)


async def test_a_clean_payload_is_framed_and_reported_clean() -> None:
    clamd = FakeClamd()

    result = await scanner_for(clamd).scan(b"%PDF-1.7 a clean filing")

    assert result.verdict is ScanVerdict.CLEAN
    assert clamd.connections == 1
    # The fake only answers once it has parsed the command, the chunk lengths,
    # and the zero-length terminator — so this list is the framing check.
    assert clamd.payloads == [b"%PDF-1.7 a clean filing"]


async def test_a_payload_larger_than_one_chunk_arrives_whole() -> None:
    clamd = FakeClamd()
    payload = b"M" * (CHUNK_BYTES * 3 + 17)

    result = await scanner_for(clamd).scan(payload)

    assert result.verdict is ScanVerdict.CLEAN
    assert clamd.payloads == [payload]


async def test_an_empty_payload_is_still_streamed() -> None:
    clamd = FakeClamd()

    result = await scanner_for(clamd).scan(b"")

    assert result.verdict is ScanVerdict.CLEAN
    assert clamd.payloads == [b""]


async def test_a_detection_comes_back_with_its_signature() -> None:
    clamd = FakeClamd(signatures={b"malware": "Eicar-Test-Signature"})

    result = await scanner_for(clamd).scan(b"malware")

    assert result.verdict is ScanVerdict.INFECTED
    assert result.signature == "Eicar-Test-Signature"


async def test_a_daemon_error_is_a_failure_with_its_message() -> None:
    clamd = FakeClamd(error="INSTREAM size limit exceeded.")

    result = await scanner_for(clamd).scan(b"anything")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "INSTREAM size limit exceeded."


async def test_a_scanner_that_is_not_running_fails_closed() -> None:
    clamd = FakeClamd(refuse_connection=True)

    result = await scanner_for(clamd).scan(b"anything")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == f"unavailable:{ConnectionRefused.__name__}"


async def test_a_stalled_scanner_times_out_rather_than_hanging() -> None:
    clamd = FakeClamd(stall=True)

    result = await scanner_for(clamd, timeout=0.05).scan(b"anything")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "timed_out"


async def test_a_truncated_answer_fails_closed() -> None:
    # A response with no terminator and a closed stream: the read cannot
    # complete, and a half-read answer is not an answer.
    clamd = FakeClamd(answer=b"stream: OK")

    result = await scanner_for(clamd).scan(b"anything")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "truncated_response"


async def test_a_response_that_is_not_the_protocol_fails_closed() -> None:
    clamd = FakeClamd(answer=b"nonsense\x00")

    result = await scanner_for(clamd).scan(b"anything")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "unrecognized_response"


def test_the_seam_builds_the_client_from_the_deployment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory every caller uses points at the configured scanner."""
    monkeypatch.setattr(scanning.settings, "scan_host", "clamav.internal")
    monkeypatch.setattr(scanning.settings, "scan_port", 3310)
    monkeypatch.setattr(scanning.settings, "scan_timeout_seconds", 4.5)

    built = REAL_SCANNER()

    assert isinstance(built, ClamdScanner)
    assert built._host == "clamav.internal"
    assert built._port == 3310
    assert built._timeout == 4.5
