"""The CDP transport, pinned against frames built by hand.

``cdp_socket.WebSocket`` is the entire WebSocket client the guard has to talk to Chrome, so
what it puts on the wire and what it refuses to read *is* the contract. An in-test server
performs the handshake (computing ``Sec-WebSocket-Accept`` itself, not through the client's
helper) and then speaks raw frames the test assembles byte by byte. Where the ``websockets``
package is installed, its server is the independent witness at the end.

Every breach of the protocol must surface as ``WebSocketError`` and nothing else: the guard
reads any of them exactly like the peer hanging up, and fails closed. The messages are fixed
strings because the endpoint (port, path) is a bearer credential for an unauthenticated
debugging port: no exception from this module may name it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import struct
import traceback
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from lyra_browser import cdp_socket
from lyra_browser.cdp_socket import WebSocket, WebSocketError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
UUID = "0b0b0b0b-1111-2222-3333-444444444444"
PATH = f"/devtools/browser/{UUID}"
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
# Nothing here should take this long; it only turns a hang into a failure.
WAIT = 5.0

# The payload sizes on either side of a length-encoding boundary (RFC 6455 5.2): 7 bits,
# the 126 marker with 16 bits, the 127 marker with 64 bits.
SIZES = [0, 125, 126, 65535, 65536]
LENGTH_CODE = {0: 0, 125: 125, 126: 126, 65535: 126, 65536: 127}


def text_of(size: int) -> str:
    """ASCII text of ``size`` bytes whose period (90) is coprime with the 4-byte mask."""
    return "".join(chr(33 + (i * 7) % 90) for i in range(size))


def xor(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def accept_for(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


def upgrade(
    key: str,
    *,
    status: str = "101 Switching Protocols",
    headers: dict[str, str | None] | None = None,
) -> bytes:
    """The server's handshake answer; a header set to ``None`` is left out."""
    fields: dict[str, str | None] = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": accept_for(key),
        **(headers or {}),
    }
    lines = [f"HTTP/1.1 {status}", *(f"{k}: {v}" for k, v in fields.items() if v is not None)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def frame(
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    rsv: int = 0,
    masked: bool = False,
    declared: int | None = None,
) -> bytes:
    """One frame as a server sends it: minimal length encoding, flags as asked.

    ``declared`` lies about the payload length, for headers whose payload never comes.
    """
    size = len(payload) if declared is None else declared
    first = (0x80 if fin else 0) | rsv | opcode
    flag = 0x80 if masked else 0
    if size < 126:
        head = struct.pack("!BB", first, flag | size)
    elif size < 1 << 16:
        head = struct.pack("!BBH", first, flag | 126, size)
    else:
        head = struct.pack("!BBQ", first, flag | 127, size)
    if masked:
        key = b"\x0f\x1e\x2d\x3c"
        return head + key + xor(payload, key)
    return head + payload


@dataclass
class ClientFrame:
    """A frame the client sent, as the server decodes it."""

    fin: bool
    opcode: int
    masked: bool
    length_code: int
    key: bytes
    payload: bytes


class Peer:
    """The server end of one accepted connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, head: bytes):
        self.reader = reader
        self.writer = writer
        self.head = head

    async def frame(self) -> ClientFrame:
        async def take(n: int) -> bytes:
            return await asyncio.wait_for(self.reader.readexactly(n), WAIT)

        b0, b1 = await take(2)
        code = size = b1 & 0x7F
        if code == 126:
            (size,) = struct.unpack("!H", await take(2))
        elif code == 127:
            (size,) = struct.unpack("!Q", await take(8))
        masked = bool(b1 & 0x80)
        key = await take(4) if masked else b""
        body = await take(size) if size else b""
        payload = xor(body, key) if masked else body
        return ClientFrame(bool(b0 & 0x80), b0 & 0x0F, masked, code, key, payload)

    def send(self, data: bytes) -> None:
        self.writer.write(data)

    async def dribble(self, data: bytes, chunk: int = 1) -> None:
        """Send ``data`` a few bytes per write, yielding in between."""
        for i in range(0, len(data), chunk):
            self.writer.write(data[i : i + chunk])
            await self.writer.drain()
            await asyncio.sleep(0)

    def hang_up(self) -> None:
        self.writer.close()

    def reset(self) -> None:
        """Drop the connection with a TCP reset instead of an orderly close."""
        sock = self.writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.writer.transport.abort()

    async def rest(self) -> bytes:
        """Everything the client still sends until it hangs up (a reset counts as nothing)."""
        try:
            return await asyncio.wait_for(self.reader.read(), WAIT)
        except ConnectionResetError:
            return b""


class Server:
    """A loopback WebSocket server whose handshake answer the test chooses."""

    def __init__(self, respond: Callable[[str], bytes | list[bytes] | None]) -> None:
        self.respond = respond
        self.peers: list[Peer] = []
        self.sockets: list[WebSocket] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        lines = head.decode("latin-1").split("\r\n")
        key = next(
            line.split(":", 1)[1].strip()
            for line in lines
            if line.lower().startswith("sec-websocket-key:")
        )
        self.peers.append(Peer(reader, writer, head))
        answer = self.respond(key)
        if answer is None:
            writer.close()
            return
        chunks = [answer] if isinstance(answer, bytes) else answer
        for i, chunk in enumerate(chunks):
            writer.write(chunk)
            await writer.drain()
            if i < len(chunks) - 1:
                await asyncio.sleep(0.01)

    async def connect(self, path: str = PATH, **options) -> tuple[WebSocket, Peer]:
        options.setdefault("timeout", WAIT)
        seen = len(self.peers)
        ws = await WebSocket.connect("127.0.0.1", self.port, path, **options)
        self.sockets.append(ws)
        return ws, self.peers[seen]

    async def stop(self) -> None:
        for ws in self.sockets:
            ws.abort()
        for peer in self.peers:
            peer.writer.close()
        assert self._server is not None
        self._server.close()
        for peer in self.peers:
            try:
                await peer.writer.wait_closed()
            except OSError:
                pass


@pytest.fixture
async def serve():
    servers: list[Server] = []

    async def start(respond: Callable = upgrade) -> Server:
        server = Server(respond)
        await server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.stop()


@pytest.fixture
async def pair(serve):
    """A connected client and the server end of that connection."""
    return await (await serve()).connect()


async def recv(ws: WebSocket) -> str:
    return await asyncio.wait_for(ws.recv(), WAIT)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def printed_chain(exc: BaseException | None):
    """The exceptions a traceback shows: the cause, or the context unless it is suppressed."""
    while exc is not None:
        yield exc
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)


def assert_no_endpoint(exc: BaseException, port: int, path: str = PATH) -> None:
    """Nothing a log of this exception would print names where Chrome is listening."""
    for shown in printed_chain(exc):
        text = f"{shown} {shown!r} {''.join(traceback.format_exception_only(shown))}"
        for secret in ("127.0.0.1", str(port), path, path.rsplit("/", 1)[-1]):
            assert secret not in text


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------


async def test_the_handshake_asks_for_a_plain_upgrade_and_offers_nothing_chrome_refuses(serve):
    server = await serve()
    _ws, peer = await server.connect()

    request, *lines = peer.head.decode("latin-1").rstrip("\r\n").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines)}
    assert request == f"GET {PATH} HTTP/1.1"
    # Chrome's debugging server only serves a Host that is a loopback address.
    assert headers["host"] == f"127.0.0.1:{server.port}"
    assert headers["upgrade"].lower() == "websocket"
    assert "upgrade" in headers["connection"].lower()
    assert headers["sec-websocket-version"] == "13"
    assert len(base64.b64decode(headers["sec-websocket-key"])) == 16
    # An Origin makes Chrome refuse the handshake; nothing is negotiated, so nothing is offered.
    assert not {"origin", "sec-websocket-extensions", "sec-websocket-protocol"} & headers.keys()


async def test_the_answer_is_read_case_insensitively_and_connection_may_list_tokens(serve):
    server = await serve(
        lambda key: (
            f"HTTP/1.1 101 Switching Protocols\r\nupgrade: WebSocket\r\n"
            f"CONNECTION: keep-alive, Upgrade\r\nsec-websocket-accept: {accept_for(key)}\r\n\r\n"
        ).encode()
    )
    ws, peer = await server.connect()
    peer.send(frame(TEXT, b"hello"))
    assert await recv(ws) == "hello"


async def test_an_answer_that_arrives_in_pieces_is_assembled(serve):
    def in_pieces(key: str) -> list[bytes]:
        answer = upgrade(key)
        return [answer[:7], answer[7:50], answer[50:-2], answer[-2:]]

    ws, peer = await (await serve(in_pieces)).connect()
    peer.send(frame(TEXT, b"hello"))
    assert await recv(ws) == "hello"


async def test_a_frame_sent_together_with_the_answer_is_not_lost(serve):
    ws, _peer = await (await serve(lambda key: upgrade(key) + frame(TEXT, b"early"))).connect()
    assert await recv(ws) == "early"


def reply(**kwargs) -> Callable[[str], bytes | None]:
    return lambda key: upgrade(key, **kwargs)


# Each is a valid upgrade with exactly one thing wrong, so a refusal can only be for that.
REFUSED = {
    "status 200": reply(status="200 OK"),
    "status 403": reply(status="403 Forbidden"),
    "wrong accept key": reply(headers={"Sec-WebSocket-Accept": accept_for("not the key sent")}),
    "no accept key": reply(headers={"Sec-WebSocket-Accept": None}),
    "no Upgrade": reply(headers={"Upgrade": None}),
    "Upgrade is not websocket": reply(headers={"Upgrade": "h2c"}),
    "no Connection": reply(headers={"Connection": None}),
    "Connection is not upgrade": reply(headers={"Connection": "keep-alive"}),
    "extension negotiated": reply(headers={"Sec-WebSocket-Extensions": "permessage-deflate"}),
    "subprotocol chosen": reply(headers={"Sec-WebSocket-Protocol": "chat"}),
    "header block over the limit": reply(headers={"X-Padding": "a" * 20_000}),
    "hung up without answering": lambda key: None,
}


@pytest.mark.parametrize("respond", REFUSED.values(), ids=REFUSED.keys())
async def test_a_handshake_that_is_not_exactly_our_upgrade_is_refused(serve, respond):
    server = await serve(respond)

    with pytest.raises(WebSocketError) as info:
        await server.connect()

    assert_no_endpoint(info.value, server.port)
    assert await server.peers[0].rest() == b"", "the connection is not left open"


@pytest.mark.parametrize(
    "path",
    [
        "devtools/browser/abc",
        "/devtools/browser/abc def",
        "/devtools/browser/abc\r\nOrigin: http://evil.example",
        "/devtools/browser/abc\nX: y",
    ],
    ids=["no leading slash", "space", "CRLF injection", "bare LF injection"],
)
async def test_a_path_that_could_rewrite_the_request_is_refused_before_connecting(serve, path):
    server = await serve()

    with pytest.raises(WebSocketError) as info:
        await server.connect(path)

    assert_no_endpoint(info.value, server.port, path)
    await asyncio.sleep(0.05)  # an accepted connection would have shown up by now
    assert server.peers == []


async def test_a_refused_connection_does_not_name_where_it_tried():
    port = closed_port()

    with pytest.raises(WebSocketError) as info:
        await WebSocket.connect("127.0.0.1", port, PATH, timeout=WAIT)

    assert_no_endpoint(info.value, port)


# --------------------------------------------------------------------------
# What the client sends
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
async def test_a_client_frame_is_masked_final_and_uses_the_shortest_length_encoding(pair, size):
    ws, peer = pair
    text = text_of(size)

    ws.send_nowait(text)

    sent = await peer.frame()
    assert (sent.fin, sent.opcode, sent.masked) == (True, TEXT, True)
    assert sent.length_code == LENGTH_CODE[size]
    assert sent.payload.decode() == text, "unmasking with the frame's own key restores the text"


async def test_each_client_frame_gets_its_own_mask_key(pair):
    ws, peer = pair
    for _ in range(4):
        ws.send_nowait("same text every time")

    keys = {(await peer.frame()).key for _ in range(4)}

    assert len(keys) > 1, "a constant key is no mask at all"


async def test_text_is_sent_as_utf8_and_sized_in_bytes_not_characters(pair):
    ws, peer = pair
    wide = "é" * 70  # 70 characters, 140 bytes: past the one-byte length
    mixed = "https://例え.example/日本語?q=✓🙂"

    ws.send_nowait(wide)
    ws.send_nowait(mixed)

    first, second = await peer.frame(), await peer.frame()
    assert first.length_code == 126
    assert first.payload == wide.encode("utf-8")
    assert second.payload.decode("utf-8") == mixed


# --------------------------------------------------------------------------
# What the client reads
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
async def test_a_text_message_of_every_length_class_arrives_whole(pair, size):
    ws, peer = pair
    text = text_of(size)

    peer.send(frame(TEXT, text.encode()))

    assert await recv(ws) == text


async def test_frames_that_arrive_together_or_a_byte_at_a_time_are_read_correctly(pair):
    ws, peer = pair
    peer.send(frame(TEXT, b"one") + frame(TEXT, b"two") + frame(TEXT, b"three"))
    assert [await recv(ws) for _ in range(3)] == ["one", "two", "three"]

    big = text_of(300)  # a 16-bit length: the header itself is split across writes
    reader = asyncio.ensure_future(recv(ws))
    await peer.dribble(frame(TEXT, big.encode()), chunk=1)
    assert await reader == big


async def test_a_fragmented_message_is_reassembled(pair):
    ws, peer = pair

    peer.send(
        frame(TEXT, b'{"id":', fin=False)
        + frame(CONT, b"1,", fin=False)
        + frame(CONT, b'"ok":true}')
    )

    assert await recv(ws) == '{"id":1,"ok":true}'


async def test_a_character_split_across_fragments_is_decoded_after_reassembly(pair):
    ws, peer = pair
    encoded = "café ✓".encode()
    cut = encoded.index(b"\xc3") + 1  # between the two bytes of é

    peer.send(frame(TEXT, encoded[:cut], fin=False) + frame(CONT, encoded[cut:]))

    assert await recv(ws) == "café ✓"


@pytest.mark.parametrize(
    "payload", [b"", b"are you there", b"p" * 125], ids=["empty", "short", "max"]
)
async def test_a_ping_inside_a_fragmented_message_is_answered_and_the_message_survives(
    pair, payload
):
    ws, peer = pair

    peer.send(frame(TEXT, b"one ", fin=False) + frame(PING, payload) + frame(CONT, b"two"))

    assert await recv(ws) == "one two"
    pong = await peer.frame()
    assert (pong.opcode, pong.fin, pong.masked, pong.payload) == (PONG, True, True, payload)


async def test_a_pong_from_the_server_is_ignored_and_provokes_no_answer(pair):
    ws, peer = pair

    peer.send(frame(PONG, b"unsolicited") + frame(TEXT, b"after"))
    assert await recv(ws) == "after"
    # The first thing the client ever sends is the answer to this ping, not to the pong.
    peer.send(frame(PING, b"p") + frame(TEXT, b"last"))
    assert await recv(ws) == "last"

    first = await peer.frame()
    assert (first.opcode, first.payload) == (PONG, b"p")


# What a peer gone wrong can send. Each one is a connection that cannot be used.
BREACHES = {
    "continuation with nothing to continue": frame(CONT, b"x"),
    "new text message inside an unfinished one": frame(TEXT, b"a", fin=False) + frame(TEXT, b"b"),
    "binary frame inside an unfinished text message": (
        frame(TEXT, b"a", fin=False) + frame(BINARY, b"b")
    ),
    "reserved bit 1": frame(TEXT, b"x", rsv=0x40),
    "reserved bit 2": frame(TEXT, b"x", rsv=0x20),
    "reserved bit 3": frame(TEXT, b"x", rsv=0x10),
    "masked server frame": frame(TEXT, b"x", masked=True),
    "fragmented ping": frame(PING, b"", fin=False),
    "ping over 125 bytes": frame(PING, b"p" * 126),
    "close over 125 bytes": frame(CLOSE, b"c" * 126),
    "binary message": frame(BINARY, b"\x00\x01"),
    "invalid utf-8": frame(TEXT, b"\xff\xfe"),
    "reserved data opcode": frame(0x3, b"x"),
    "reserved control opcode": frame(0xB),
    "64-bit length with the top bit set": frame(TEXT, b"", declared=1 << 63),
}


@pytest.mark.parametrize("wire", BREACHES.values(), ids=BREACHES.keys())
async def test_a_breach_of_the_protocol_is_a_websocket_error(serve, wire):
    server = await serve()
    ws, peer = await server.connect()
    peer.send(wire)  # the server stays connected: the frame alone is what fails

    with pytest.raises(WebSocketError) as info:
        await recv(ws)

    assert_no_endpoint(info.value, server.port)


CUTS = {
    "half a header": (frame(TEXT, b"0123456789"), 1),
    "a header and no payload": (frame(TEXT, b"0123456789"), 2),
    "half a payload": (frame(TEXT, b"0123456789"), 7),
    "inside a 64-bit length": (frame(TEXT, b"x" * 65536), 5),
}


@pytest.mark.parametrize(("wire", "cut"), CUTS.values(), ids=CUTS.keys())
async def test_a_frame_cut_short_by_the_peer_hanging_up_is_a_websocket_error(pair, wire, cut):
    ws, peer = pair
    peer.send(wire[:cut])
    peer.hang_up()

    with pytest.raises(WebSocketError):
        await recv(ws)


@pytest.mark.parametrize("sent", [b"", frame(TEXT, b"0123456789")[:5]], ids=["idle", "mid frame"])
async def test_a_connection_reset_is_a_websocket_error(pair, sent):
    ws, peer = pair
    waiting = asyncio.ensure_future(recv(ws))
    await asyncio.sleep(0)  # the reader is parked in recv when the reset arrives
    peer.send(sent)
    peer.reset()

    with pytest.raises(WebSocketError):
        await waiting


async def test_a_message_exactly_at_the_limit_is_accepted_and_one_byte_more_is_not(serve):
    ws, peer = await (await serve()).connect(max_message=100)
    peer.send(frame(TEXT, b"a" * 100))
    assert await recv(ws) == "a" * 100

    peer.send(frame(TEXT, b"a" * 101))
    with pytest.raises(WebSocketError):
        await recv(ws)


async def test_fragments_that_add_up_past_the_limit_are_refused(serve):
    ws, peer = await (await serve()).connect(max_message=100)

    # Each part is within the limit; the message is not.
    peer.send(frame(TEXT, b"a" * 60, fin=False) + frame(CONT, b"a" * 60))

    with pytest.raises(WebSocketError):
        await recv(ws)


@pytest.mark.parametrize(
    ("opcode", "hooked"),
    [(TEXT, False), (TEXT, True), (PING, False)],
    ids=["data", "data with a hook", "control"],
)
async def test_a_header_announcing_a_huge_payload_is_refused_without_waiting_for_it(
    serve, opcode, hooked
):
    ws, peer = await (await serve()).connect(max_message=100)
    if hooked:
        ws.shrink = Hook()

    # The payload never comes. Waiting for it (or allocating for it) would hang or exhaust us.
    peer.send(frame(opcode, b"", declared=2**40))

    with pytest.raises(WebSocketError):
        await recv(ws)


# --------------------------------------------------------------------------
# A message above the limit: its first and last bytes, or an error
# --------------------------------------------------------------------------

MIB = 1024 * 1024
HEAD, TAIL = 256 * 1024, 64 * 1024  # what is kept of a message that is not kept whole


def big_text(size: int) -> str:
    return (text_of(90) * (size // 90 + 1))[:size]


# Not a multiple of the 1 MiB read size, so the last chunk is smaller than what is kept.
BIG = big_text(3 * MIB + 12_345)


class Hook:
    """A ``shrink`` hook that records what it was handed."""

    def __init__(self, result: str | None = "what the hook made of it") -> None:
        self.result = result
        self.calls: list[tuple[bytes, bytes]] = []

    def __call__(self, head: bytes, tail: bytes) -> str | None:
        self.calls.append((head, tail))
        return self.result


def one_frame(data: bytes) -> bytes:
    return frame(TEXT, data)


def three_fragments(data: bytes) -> bytes:
    a, b = len(data) // 5, len(data) * 3 // 5
    return (
        frame(TEXT, data[:a], fin=False) + frame(CONT, data[a:b], fin=False) + frame(CONT, data[b:])
    )


def tiny_last_fragments(data: bytes) -> bytes:
    """The end of the message, which is what is kept, spread over three frames."""
    return (
        frame(TEXT, data[:-15], fin=False)
        + frame(CONT, data[-15:-5], fin=False)
        + frame(CONT, data[-5:])
    )


FRAMINGS = {
    "one frame": one_frame,
    "three fragments": three_fragments,
    "tiny last fragments": tiny_last_fragments,
}


@pytest.mark.parametrize("framing", FRAMINGS.values(), ids=FRAMINGS.keys())
async def test_a_message_above_the_limit_goes_to_the_hook_as_its_first_and_last_bytes(
    serve, framing
):
    ws, peer = await (await serve()).connect(max_message=100_000)
    ws.shrink = hook = Hook()
    data = BIG.encode()

    peer.send(framing(data))

    assert await recv(ws) == "what the hook made of it"
    assert hook.calls == [(data[:HEAD], data[-TAIL:])]


@pytest.mark.parametrize("framing", FRAMINGS.values(), ids=FRAMINGS.keys())
async def test_without_a_hook_a_message_above_the_limit_is_an_error(serve, framing):
    ws, peer = await (await serve()).connect(max_message=100_000)

    peer.send(framing(BIG.encode()))

    with pytest.raises(WebSocketError):
        await recv(ws)


@pytest.mark.parametrize("framing", FRAMINGS.values(), ids=FRAMINGS.keys())
async def test_a_hook_that_has_no_message_to_give_leaves_an_error(serve, framing):
    ws, peer = await (await serve()).connect(max_message=100_000)
    ws.shrink = hook = Hook(None)

    peer.send(framing(BIG.encode()))

    with pytest.raises(WebSocketError):
        await recv(ws)
    assert len(hook.calls) == 1


@pytest.mark.parametrize("framing", FRAMINGS.values(), ids=FRAMINGS.keys())
async def test_the_frames_after_an_oversize_message_are_read_in_step(serve, framing):
    ws, peer = await (await serve()).connect(max_message=100_000)
    ws.shrink = Hook("shrunk")

    after = text_of(300)
    peer.send(framing(BIG.encode()) + frame(TEXT, b"next one") + frame(TEXT, after.encode()))

    assert await recv(ws) == "shrunk"
    assert await recv(ws) == "next one"
    assert await recv(ws) == after


async def test_a_ping_inside_an_oversize_message_is_answered_and_changes_nothing_kept(serve):
    ws, peer = await (await serve()).connect(max_message=100_000)
    ws.shrink = hook = Hook("shrunk")
    data, cut = BIG.encode(), 2 * MIB

    peer.send(
        frame(TEXT, data[:cut], fin=False)
        + frame(PING, b"alive?")
        + frame(CONT, data[cut:])
        + frame(TEXT, b"next")
    )

    assert await recv(ws) == "shrunk"
    assert await recv(ws) == "next"
    assert hook.calls == [(data[:HEAD], data[-TAIL:])]
    assert (await peer.frame()).payload == b"alive?"


async def test_the_hook_is_for_messages_above_the_limit_only(serve):
    ws, peer = await (await serve()).connect(max_message=100)
    ws.shrink = hook = Hook("shrunk")

    peer.send(frame(TEXT, b"a" * 100))
    assert await recv(ws) == "a" * 100
    assert hook.calls == [], "a message at the limit is delivered as it is"

    peer.send(frame(TEXT, b"b" * 101))
    assert await recv(ws) == "shrunk"
    assert len(hook.calls) == 1


@pytest.mark.parametrize("framing", [one_frame, three_fragments], ids=["one frame", "fragments"])
async def test_a_message_beyond_the_hard_ceiling_is_refused_even_with_a_hook(
    serve, monkeypatch, framing
):
    monkeypatch.setattr(cdp_socket, "HARD_MAX", 2 * MIB)
    ws, peer = await (await serve()).connect(max_message=100_000)
    ws.shrink = hook = Hook()

    peer.send(framing(big_text(3 * MIB).encode()))

    with pytest.raises(WebSocketError):
        await recv(ws)
    assert hook.calls == []


# --------------------------------------------------------------------------
# Ending the connection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "echoed"),
    [
        (b"", b""),
        (struct.pack("!H", 1000), struct.pack("!H", 1000)),
        (struct.pack("!H", 1001) + b"going away", struct.pack("!H", 1001)),
    ],
    ids=["no status", "normal", "status and reason"],
)
async def test_a_close_from_the_server_is_answered_once_and_ends_the_connection(
    pair, payload, echoed
):
    ws, peer = pair
    peer.send(frame(CLOSE, payload))

    with pytest.raises(WebSocketError):
        await recv(ws)

    answer = await peer.frame()
    assert (answer.opcode, answer.fin, answer.masked, answer.payload) == (CLOSE, True, True, echoed)
    await ws.close()
    assert await peer.rest() == b"", "the answer is the only close frame there is"


async def test_a_close_in_the_middle_of_a_message_ends_it_without_a_partial_message(pair):
    ws, peer = pair
    peer.send(frame(TEXT, b"half of a ", fin=False) + frame(CLOSE, struct.pack("!H", 1000)))

    with pytest.raises(WebSocketError):
        await recv(ws)


async def test_close_says_goodbye_once_and_may_be_repeated(pair):
    ws, peer = pair

    await ws.close()
    await ws.close()

    goodbye = await peer.frame()
    assert (goodbye.opcode, goodbye.masked, goodbye.payload) == (
        CLOSE,
        True,
        struct.pack("!H", 1000),
    )
    assert await peer.rest() == b""
    assert ws.closing


async def test_close_on_a_connection_the_peer_already_dropped_completes(pair):
    ws, peer = pair
    peer.hang_up()
    with pytest.raises(WebSocketError):
        await recv(ws)

    await asyncio.wait_for(ws.close(), WAIT)

    assert ws.closing


async def test_abort_fails_a_recv_that_is_waiting_and_says_no_goodbye(pair):
    ws, peer = pair
    waiting = asyncio.ensure_future(recv(ws))
    await asyncio.sleep(0.01)
    assert not waiting.done(), "recv is parked, not failing for some other reason"

    ws.abort()

    with pytest.raises(WebSocketError):
        await waiting
    assert ws.closing
    assert await peer.rest() == b"", "an abort is a vanished peer: no close frame"


@pytest.mark.parametrize("how", ["close", "abort"])
async def test_sending_on_a_closed_socket_is_a_websocket_error(pair, how):
    ws, _peer = pair
    if how == "close":
        await ws.close()
    else:
        ws.abort()

    with pytest.raises(WebSocketError):
        ws.send_nowait("late")


# --------------------------------------------------------------------------
# Against the websockets package
# --------------------------------------------------------------------------


async def test_the_websockets_server_agrees_on_large_fragmented_and_ping_traffic():
    """An independent implementation: it rejects a bad mask or length instead of echoing it."""
    ws_server = pytest.importorskip("websockets.asyncio.server")
    big = text_of(70_000)
    seen: dict[str, object] = {}

    async def handler(conn) -> None:
        seen["received"] = await conn.recv()
        await conn.send(big)
        await conn.send(["frag-a|", "frag-b|", "frag-c"])  # one message in three frames
        seen["pong"] = await asyncio.wait_for(await conn.ping(b"hello"), WAIT)
        await conn.close(1000)

    async with ws_server.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await WebSocket.connect("127.0.0.1", port, "/", timeout=WAIT)

        ws.send_nowait(big)
        assert await recv(ws) == big
        assert await recv(ws) == "frag-a|frag-b|frag-c"
        with pytest.raises(WebSocketError):
            await recv(ws)  # the server's close, after it saw our pong
        await ws.close()

    assert seen["received"] == big
    assert seen["pong"] is not None
