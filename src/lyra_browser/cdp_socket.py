"""A minimal WebSocket client (RFC 6455), just enough for Chrome's DevTools endpoint.

``cdp_guard`` talks to Chrome over a second connection of its own, and that
connection is a WebSocket to ``127.0.0.1``. This is the whole client, instead of the
``websockets`` package, for four reasons:

- **No new runtime dependency.** Nothing lyra-browser depends on pulls ``websockets``
  in, and a missing import would have to fail closed at the moment a browser launches.
  An end user with only VEGA.app has no pip to fix that with.
- **Nothing happens that was not asked for.** One loopback connection, text frames,
  no TLS, no extensions, no subprotocol, no ``Origin`` header (Chrome refuses a
  handshake that carries one), no keep-alive pings. A library's ping timeout would
  drop the connection whenever the event loop stalled for twenty seconds, and a
  dropped connection closes the window.
- **Any surprise is a lost connection.** Malformed framing, a masked server frame,
  reserved bits, an oversize message, invalid UTF-8: every one raises
  ``WebSocketError``, and the guard treats that exactly like the peer hanging up
  (it fails closed). There is no recovery path to get wrong.
- **It is small enough to read**: the framing is checked against hand-built frames
  and, where installed, the ``websockets`` server.

Messages of ``WebSocketError`` are fixed strings. The endpoint (port, path) is a bearer
credential for an unauthenticated debugging port, so it never appears in an exception,
a log line or an audit row.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from collections.abc import Callable

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER = 16 * 1024
# The largest message kept whole. DevTools traffic here is events and small replies; one
# above this is either handed to ``WebSocket.shrink`` or an error.
MAX_MESSAGE = 8 * 1024 * 1024
# The most a message handled by ``shrink`` may weigh, however it is streamed past.
HARD_MAX = 4 * 1024**3
# What is kept of such a message: its first bytes and its last, read in chunks of _CHUNK.
_HEAD = 256 * 1024
_TAIL = 64 * 1024
_CHUNK = 1024 * 1024

_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    """The connection cannot be used: refused, closed, or in breach of the protocol."""


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR ``payload`` with the 4-byte ``key`` repeated; a bignum XOR keeps it in C."""
    n = len(payload)
    if not n:
        return payload
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


def _accept_key(key: bytes) -> bytes:
    return base64.b64encode(hashlib.sha1(key + _GUID).digest())


class WebSocket:
    """One client connection: ``recv`` from a single task, ``send_nowait`` from anywhere."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_message: int = MAX_MESSAGE,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max = max_message
        self._close_sent = False
        self.shrink: Callable[[bytes, bytes], str | None] | None = None
        """Reduces a message above ``max_message`` to one worth delivering; see ``recv``."""

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        path: str,
        *,
        timeout: float = 10.0,
        max_message: int = MAX_MESSAGE,
    ) -> WebSocket:
        if not path.startswith("/") or any(c in path for c in " \r\n"):
            raise WebSocketError("invalid endpoint")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=_MAX_HEADER), timeout
            )
        except (OSError, TimeoutError):
            raise WebSocketError("could not connect") from None
        sock = cls(reader, writer, max_message)
        try:
            await asyncio.wait_for(sock._handshake(host, port, path), timeout)
        except BaseException:
            writer.close()
            raise
        return sock

    async def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16))
        self._writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key.decode()}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        try:
            head = await self._reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            raise WebSocketError("handshake failed") from None
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise WebSocketError("handshake refused")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        if (
            headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("connection", "").lower()
            or headers.get("sec-websocket-accept", "").encode() != _accept_key(key)
            or "sec-websocket-extensions" in headers
            or "sec-websocket-protocol" in headers
        ):
            raise WebSocketError("handshake invalid")

    # -- reading ------------------------------------------------------------------

    async def _read(self, n: int) -> bytes:
        try:
            return await self._reader.readexactly(n)
        except (asyncio.IncompleteReadError, OSError):
            raise WebSocketError("connection closed") from None

    async def _header(self) -> tuple[bool, int, int]:
        b0, b1 = await self._read(2)
        if b0 & 0x70:
            raise WebSocketError("reserved bits set")  # no extension was negotiated
        if b1 & 0x80:
            raise WebSocketError("server frame is masked")
        opcode, length = b0 & 0x0F, b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", await self._read(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", await self._read(8))
            if length >> 63:
                raise WebSocketError("frame length invalid")
        fin = bool(b0 & 0x80)
        if opcode >= _CLOSE and (not fin or length > 125):
            raise WebSocketError("control frame invalid")
        return fin, opcode, length

    async def recv(self) -> str:
        """The next complete text message. Raises ``WebSocketError`` once the connection is over.

        A message above ``max_message`` is an error — unless ``shrink`` is set. Then it is
        read through without being kept: only its first and last bytes are, and ``shrink``
        turns those into the message to deliver (or None: still an error). DevTools sends
        a message that big when a navigation carries an upload, and neither the memory it
        would take nor closing the connection over it is acceptable.
        """
        parts: list[bytes] = []
        size = 0
        started = False
        ends: tuple[bytearray, bytearray] | None = None
        while True:
            fin, opcode, length = await self._header()
            if opcode >= _CLOSE:
                payload = await self._read(length) if length else b""
                if opcode == _PING:
                    self._write(_PONG, payload)
                elif opcode == _CLOSE:
                    # Answer the close handshake, then report the end like any other.
                    if not self._close_sent:
                        self._write(_CLOSE, payload[:2])
                        self._close_sent = True
                    raise WebSocketError("closed by peer")
                elif opcode != _PONG:
                    raise WebSocketError("unknown opcode")
                continue
            if opcode == _CONT:
                if not started:
                    raise WebSocketError("unexpected continuation")
            elif opcode in (_TEXT, _BINARY):
                if started:
                    raise WebSocketError("interleaved message")
                if opcode == _BINARY:
                    raise WebSocketError("binary message")  # DevTools speaks text
                started = True
            else:
                raise WebSocketError("unknown opcode")
            # Refuse on the announced length: a header that promises more than will ever be
            # accepted must not be waited on for its payload.
            if size + length > (self._max if self.shrink is None else HARD_MAX):
                raise WebSocketError("message too large")
            remaining = length
            while remaining:
                chunk = await self._read(min(remaining, _CHUNK))
                remaining -= len(chunk)
                size += len(chunk)
                if ends is None:
                    parts.append(chunk)
                    if size > self._max:
                        if self.shrink is None:
                            raise WebSocketError("message too large")
                        whole = b"".join(parts)
                        parts.clear()
                        ends = (bytearray(whole[:_HEAD]), bytearray(whole[-_TAIL:]))
                        del whole
                else:
                    if size > HARD_MAX:
                        raise WebSocketError("message too large")
                    tail = ends[1]
                    tail += chunk
                    del tail[:-_TAIL]
            if fin:
                break
        if ends is not None:
            assert self.shrink is not None
            text = self.shrink(bytes(ends[0]), bytes(ends[1]))
            if text is None:
                raise WebSocketError("message too large")
            return text
        try:
            return b"".join(parts).decode("utf-8")
        except UnicodeDecodeError:
            raise WebSocketError("message is not utf-8") from None

    # -- writing ------------------------------------------------------------------

    def _write(self, opcode: int, payload: bytes) -> None:
        if self._writer.is_closing():
            raise WebSocketError("connection closed")
        n = len(payload)
        key = os.urandom(4)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self._writer.write(head + key + _mask(payload, key))

    def send_nowait(self, text: str) -> None:
        """Queue one text message. The frame is complete in the transport buffer on return."""
        self._write(_TEXT, text.encode("utf-8"))

    @property
    def closing(self) -> bool:
        return self._writer.is_closing()

    async def close(self) -> None:
        """Say goodbye and drop the connection. Safe to call twice, and on a dead one."""
        if not self._close_sent and not self._writer.is_closing():
            self._close_sent = True
            try:
                self._write(_CLOSE, struct.pack("!H", 1000))
            except WebSocketError:
                pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    def abort(self) -> None:
        """Drop the TCP connection with no goodbye: what a peer that vanished looks like."""
        self._close_sent = True
        transport = self._writer.transport
        if transport is not None:
            transport.abort()
