"""Domain rules for the malware scanner's answers (ADR-0009).

Pure: clamd's one line of INSTREAM output becomes a verdict, and a verdict
becomes the document's scan status. The rule that matters is that only an
explicit ``OK`` is clean — a daemon error, an empty read, or anything this
module does not recognise is a failure, and a failure is never served or
processed.
"""

import pytest

from app.domain.documents import ScanStatus
from app.domain.scanning import (
    MAX_SCAN_TEXT_LENGTH,
    ScanVerdict,
    parse_clamd_response,
    scan_status_for,
)


def test_ok_is_clean() -> None:
    result = parse_clamd_response(b"stream: OK\x00")

    assert result.verdict is ScanVerdict.CLEAN
    assert result.signature is None
    assert result.reason is None


def test_a_detection_carries_the_signature_name() -> None:
    result = parse_clamd_response(b"stream: Eicar-Test-Signature FOUND\x00")

    assert result.verdict is ScanVerdict.INFECTED
    assert result.signature == "Eicar-Test-Signature"
    assert result.reason is None


def test_a_signature_containing_spaces_survives_intact() -> None:
    result = parse_clamd_response(b"stream: Win.Trojan.Agent-1234567.Some.Name FOUND\x00")

    assert result.verdict is ScanVerdict.INFECTED
    assert result.signature == "Win.Trojan.Agent-1234567.Some.Name"


def test_a_daemon_error_is_a_failure_carrying_its_message() -> None:
    result = parse_clamd_response(b"stream: INSTREAM size limit exceeded. ERROR\x00")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "INSTREAM size limit exceeded."
    assert result.signature is None


def test_an_empty_answer_is_a_failure_not_an_ok() -> None:
    result = parse_clamd_response(b"")

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason == "empty_response"


def test_an_unrecognised_answer_is_a_failure() -> None:
    assert parse_clamd_response(b"stream: Eicar-Test-Signature\x00").verdict is (
        ScanVerdict.FAILED
    )
    assert parse_clamd_response(b"stream: \x00").verdict is ScanVerdict.FAILED
    assert parse_clamd_response(b"stream:  FOUND\x00").reason == "unrecognized_response"


def test_a_bare_marker_without_the_stream_prefix_is_still_read() -> None:
    # A daemon answer that omits the file-name prefix must not turn every scan
    # into a failure; the verdict is what the line ends with.
    assert parse_clamd_response(b"OK\x00").verdict is ScanVerdict.CLEAN
    assert parse_clamd_response(b"Eicar-Test-Signature FOUND\x00").signature == (
        "Eicar-Test-Signature"
    )


def test_long_daemon_text_is_bounded() -> None:
    result = parse_clamd_response(("stream: " + "x" * 500 + " ERROR").encode())

    assert result.verdict is ScanVerdict.FAILED
    assert result.reason is not None
    assert len(result.reason) == MAX_SCAN_TEXT_LENGTH


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (ScanVerdict.CLEAN, ScanStatus.CLEAN),
        (ScanVerdict.INFECTED, ScanStatus.INFECTED),
        # Fail closed: a scan that could not answer leaves the document
        # quarantined, never clean.
        (ScanVerdict.FAILED, ScanStatus.PENDING),
    ],
)
def test_a_verdict_maps_to_the_document_scan_status(
    verdict: ScanVerdict, expected: ScanStatus
) -> None:
    assert scan_status_for(verdict) is expected
