"""A faithful stand-in for clamd, without a socket.

The scanner's transport seam (``ClamdScanner(connect=…)``) is handed a pair of
in-memory streams instead of a TCP connection. This module parses what the
client writes — the ``zINSTREAM`` command, then length-prefixed chunks, then
the zero-length terminator — and answers the way clamd does: one
NUL-terminated line, ``stream: OK``, ``stream: <signature> FOUND``, or
``stream: <message> ERROR``.

Parsing the framing is the point. A client that wrote the wrong chunk length,
forgot the terminator, or sent the wrong command would be caught here rather
than at a customer's deployment — the same argument the Google fake makes for
the token endpoint. The levers stage what a deployment meets: a scanner that
is not running, one that stalls, one whose answer is truncated, and a table of
detections keyed by content.
"""

import asyncio
from dataclasses import dataclass, field

COMMAND = b"zINSTREAM\x00"
TERMINATOR = "\x00"


class ConnectionRefused(OSError):
    """What a dead scanner looks like to the client (``ConnectionRefusedError``)."""


@dataclass
class FakeClamd:
    """A clamd that answers from a table of conclusions."""

    # Payload → ClamAV signature name (a detection); anything else is clean.
    signatures: dict[bytes, str] = field(default_factory=dict)
    # Levers.
    refuse_connection: bool = False
    stall: bool = False
    answer: bytes | None = None
    error: str | None = None

    # What the fake received.
    connections: int = 0
    payloads: list[bytes] = field(default_factory=list)

    async def connect(self, host: str, port: int) -> tuple[asyncio.StreamReader, "_StreamWriter"]:
        """The ``ClamdScanner`` connection seam, in memory."""
        self.connections += 1
        if self.refuse_connection:
            raise ConnectionRefused(f"connection refused to {host}:{port}")
        reader = asyncio.StreamReader()
        writer = _StreamWriter(clamd=self, reader=reader)
        if self.stall:
            writer.silent = True
        return reader, writer


class _StreamWriter:
    """Accepts the INSTREAM framing and answers once the stream ends."""

    def __init__(self, *, clamd: FakeClamd, reader: asyncio.StreamReader) -> None:
        self._clamd = clamd
        self._reader = reader
        self._buffer = bytearray()
        self._payload = bytearray()
        self._expect_command = True
        self._chunk_length = -1
        self.silent = False
        self.closed = False

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._consume()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def _consume(self) -> None:
        """Walk the framing; answer when the zero-length chunk arrives."""
        if self.silent:
            return
        if self._expect_command:
            if not self._buffer.startswith(COMMAND):
                # A wrong command is a daemon-side error, not a hang.
                self._answer(b"UNKNOWN COMMAND\x00")
                return
            del self._buffer[: len(COMMAND)]
            self._expect_command = False
        while True:
            if self._chunk_length < 0:
                if len(self._buffer) < 4:
                    return
                self._chunk_length = int.from_bytes(self._buffer[:4], "big")
                del self._buffer[:4]
                if self._chunk_length == 0:
                    self._complete()
                    return
            if len(self._buffer) < self._chunk_length:
                return
            self._payload.extend(self._buffer[: self._chunk_length])
            del self._buffer[: self._chunk_length]
            self._chunk_length = -1

    def _complete(self) -> None:
        payload = bytes(self._payload)
        self._clamd.payloads.append(payload)
        self._answer(_answer_for(self._clamd, payload))

    def _answer(self, answer: bytes) -> None:
        self._reader.feed_data(answer)
        self._reader.feed_eof()


def _answer_for(clamd: FakeClamd, payload: bytes) -> bytes:
    """The bytes clamd would send for this stream."""
    if clamd.answer is not None:
        return clamd.answer
    if clamd.error is not None:
        return f"stream: {clamd.error} ERROR{TERMINATOR}".encode()
    signature = clamd.signatures.get(payload)
    if signature is not None:
        return f"stream: {signature} FOUND{TERMINATOR}".encode()
    return f"stream: OK{TERMINATOR}".encode()
