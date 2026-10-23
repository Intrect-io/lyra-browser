"""One DevTools connection, shared by every page of a native browser.

Commands go out with an id and come back to the future that waits on that id; events
fan out to the handlers registered for their method, optionally narrowed to one flat
session (``Target.attachToTarget`` with ``flatten``). It is the same shape as
``cdp_guard``'s reader, deliberately not shared with it: the guard's connection has to
fail closed and kill the browser, while this one has to fail every waiting call with
``TargetClosedError`` and let the session decide.

The endpoint is a bearer credential for an unauthenticated debugging port. It is never
put in an exception, a log line or a repr.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Callable
from typing import Any

from ..cdp_socket import WebSocket, WebSocketError
from .errors import ProtocolError, TargetClosedError

_LOOPBACK = "127.0.0.1"
CONNECT_TIMEOUT_S = 10.0
# A full-page screenshot arrives as one base64 message; the guard's 8 MB cap is for
# events and small replies only.
MAX_MESSAGE = 256 * 1024 * 1024

Handler = Callable[[dict, str | None], None]


class Connection:
    """Commands by id, events by method; see the module docstring."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.Future, str]] = {}
        self._handlers: dict[str, list[tuple[str | None, Handler]]] = {}
        self._close_listeners: list[Callable[[], None]] = []
        self.closed = False
        self.handler_errors: list[str] = []
        """Exceptions raised by event handlers, kept instead of killing the reader."""
        self._reader = asyncio.create_task(self._read_loop())

    @classmethod
    async def open(cls, port: int, path: str, *, timeout: float = CONNECT_TIMEOUT_S) -> Connection:
        try:
            ws = await WebSocket.connect(
                _LOOPBACK, port, path, timeout=timeout, max_message=MAX_MESSAGE
            )
        except WebSocketError as exc:
            raise TargetClosedError(f"could not connect to the browser: {exc}") from None
        return cls(ws)

    # -- commands -----------------------------------------------------------------

    async def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send one command and return its ``result``.

        Raises ``ProtocolError`` when Chrome answers with an error and
        ``TargetClosedError`` when the connection is gone or goes before the answer.
        """
        if self.closed:
            raise TargetClosedError()
        msg_id = next(self._ids)
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = (future, method)
        try:
            self._ws.send_nowait(json.dumps(message))
        except Exception:  # noqa: BLE001 — a closing transport is a closed browser
            self._pending.pop(msg_id, None)
            raise TargetClosedError() from None
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    # -- events -------------------------------------------------------------------

    def on(
        self, method: str, handler: Handler, *, session: str | None = None
    ) -> Callable[[], None]:
        """Call ``handler(params, session_id)`` for every ``method`` event.

        ``session`` narrows it to one flat session; None hears every session and the
        browser's own events. Returns the function that removes the handler.
        """
        entry = (session, handler)
        self._handlers.setdefault(method, []).append(entry)

        def remove() -> None:
            with contextlib.suppress(ValueError, KeyError):
                self._handlers[method].remove(entry)

        return remove

    def on_close(self, listener: Callable[[], None]) -> None:
        if self.closed:
            listener()
        else:
            self._close_listeners.append(listener)

    async def wait_for_event(
        self,
        method: str,
        *,
        session: str | None = None,
        predicate: Callable[[dict], bool] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """The params of the next matching event. Register before causing it."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        def handler(params: dict, _session: str | None) -> None:
            if not future.done() and (predicate is None or predicate(params)):
                future.set_result(params)

        remove = self.on(method, handler, session=session)
        self.on_close(lambda: future.done() or future.set_exception(TargetClosedError()))
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            remove()

    # -- lifecycle ----------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                self._dispatch(json.loads(await self._ws.recv()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — any end of the stream is the browser gone
            pass
        finally:
            self._mark_closed()

    def _dispatch(self, message: dict) -> None:
        msg_id = message.get("id")
        if msg_id is not None:
            pending = self._pending.get(msg_id)
            if pending is None:
                return
            future, method = pending
            if future.done():
                return
            error = message.get("error")
            if error is not None:
                future.set_exception(
                    ProtocolError(method, str(error.get("message", "")), error.get("code"))
                )
            else:
                future.set_result(message.get("result") or {})
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        session = message.get("sessionId")
        params = message.get("params") or {}
        for wanted, handler in list(self._handlers.get(method, ())):
            if wanted is not None and wanted != session:
                continue
            try:
                handler(params, session)
            except Exception as exc:  # noqa: BLE001 — one bad handler must not stop the rest
                self.handler_errors.append(f"{method}: {type(exc).__name__}: {exc}")

    def _mark_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        for future, _method in list(self._pending.values()):
            if not future.done():
                future.set_exception(TargetClosedError())
        self._pending.clear()
        listeners, self._close_listeners = self._close_listeners, []
        for listener in listeners:
            with contextlib.suppress(Exception):
                listener()

    async def close(self) -> None:
        if not self.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        self._mark_closed()
