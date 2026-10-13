"""clamd over TCP: the scanning seam's only transport (ADR-0009).

ClamAV's daemon speaks a small line protocol on port 3310. The streaming
command is ``INSTREAM``: the client sends the command, then the payload in
length-prefixed chunks (4-byte big-endian, then the bytes), then a zero-length
chunk, and reads one line back — ``stream: OK``, ``stream: <signature>
FOUND``, or ``stream: <message> ERROR``. The ``z`` prefix means the answer is
NUL-terminated, which is why the read below can end on the terminator instead
of guessing at a line ending.

Hand-rolled rather than taken from a package: the protocol is these twenty
lines, and a new production dependency needs founder sign-off (AGENTS.md).
What a package would add — signatures for the rest of clamd's command set —
is not wanted here: this module sends exactly one command, and never a
``SCAN`` over a path it would have to share a filesystem for.

Nothing raises. A transport that cannot connect, stalls, or answers something
unreadable is a ``failed`` verdict, so a deployment without a reachable
scanner fails closed by construction rather than by every caller remembering
to catch — the same reason the response parser returns a verdict instead of
raising one (``app.domain.scanning``).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from app.domain.scanning import TERMINATOR, ScanResult, ScanVerdict, parse_clamd_response

# Chunking the stream keeps the peak write buffer small; 64 KiB is what the
# protocol examples use and is comfortably inside any socket buffer.
CHUNK_BYTES = 64 * 1024

# ``z`` = NUL-terminated answer, ``INSTREAM`` = scan the bytes that follow.
_COMMAND = b"zINSTREAM\x00"
_END_OF_STREAM = b"\x00\x00\x00\x00"

# The connection call, injectable so the suite can drive the protocol without
# a socket (``tests/fake_clamd.py``); production uses asyncio's own.
Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class ClamdScanner:
    """One scan per call: connect, stream the bytes, read the verdict, close."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout: float,
        connect: Connect | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._connect = connect if connect is not None else asyncio.open_connection

    async def scan(self, data: bytes) -> ScanResult:
        """Scan ``data``; every failure is a ``failed`` verdict, never a raise."""
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await self._connect(self._host, self._port)
                try:
                    await self._stream(writer, data)
                    answer = await reader.readuntil(TERMINATOR)
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except TimeoutError:
            return ScanResult(verdict=ScanVerdict.FAILED, reason="timed_out")
        except OSError as exc:
            # Unreachable, refused, DNS, or a reset mid-stream — the class
            # name is the diagnosis and carries no data.
            return ScanResult(
                verdict=ScanVerdict.FAILED, reason=f"unavailable:{exc.__class__.__name__}"
            )
        except asyncio.IncompleteReadError:
            return ScanResult(verdict=ScanVerdict.FAILED, reason="truncated_response")
        except asyncio.LimitOverrunError:
            return ScanResult(verdict=ScanVerdict.FAILED, reason="response_too_long")
        return parse_clamd_response(answer)

    @staticmethod
    async def _stream(writer: asyncio.StreamWriter, data: bytes) -> None:
        """Send the command and the length-prefixed payload, then the end marker."""
        writer.write(_COMMAND)
        for start in range(0, len(data), CHUNK_BYTES):
            chunk = data[start : start + CHUNK_BYTES]
            writer.write(len(chunk).to_bytes(4, "big") + chunk)
        writer.write(_END_OF_STREAM)
        await writer.drain()
