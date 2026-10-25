"""NativePage's request filter, page.off and expect_download, on a fake connection.

These are what VEGA's web_fetch (one pinned origin) and Drive shared-file download use
in place of Playwright. No browser is launched: the connection records what is sent
and replays events.
"""

from __future__ import annotations

import asyncio

import pytest

from lyra_browser.native.page import NativePage


class _Conn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict | None, str | None]] = []
        self.handlers: list[tuple[str, str | None, object]] = []

    async def send(self, method, params=None, *, session=None, timeout=None):  # noqa: ARG002
        self.sent.append((method, params, session))
        return {}

    def on(self, method, handler, *, session=None):
        self.handlers.append((method, session, handler))
        return lambda: None

    def fire(self, method: str, params: dict, session: str) -> None:
        for name, wanted, handler in list(self.handlers):
            if name == method and wanted in (None, session):
                handler(params, session)


class _Context:
    def __init__(self) -> None:
        self._conn = _Conn()
        self._timeout_default = None


def _page() -> tuple[NativePage, _Conn]:
    context = _Context()
    page = NativePage(context, "T1", "S1", None, "about:blank")
    return page, context._conn


async def test_the_filter_lets_through_what_it_keeps_and_answers_the_rest_204():
    page, conn = _page()
    await page.enable_request_filter(
        lambda paused: paused["request"]["url"].startswith("https://ok.example")
    )
    assert ("Fetch.enable", page_patterns := conn.sent[-1][1], "S1") == conn.sent[-1]
    assert page_patterns["patterns"][0]["resourceType"] == "Document"

    conn.fire(
        "Fetch.requestPaused", {"requestId": "a", "request": {"url": "https://ok.example/"}}, "S1"
    )
    conn.fire(
        "Fetch.requestPaused", {"requestId": "b", "request": {"url": "http://127.0.0.1/"}}, "S1"
    )
    await asyncio.sleep(0)  # the answers are sent without waiting for a reply

    answers = {
        params["requestId"]: method
        for method, params, _ in conn.sent
        if params and "requestId" in params
    }
    assert answers == {"a": "Fetch.continueRequest", "b": "Fetch.fulfillRequest"}
    refused = next(p for m, p, _ in conn.sent if m == "Fetch.fulfillRequest")
    assert refused["responseCode"] == 204


async def test_a_filter_that_raises_refuses_instead_of_opening_the_gate():
    page, conn = _page()

    def broken(_paused):
        raise RuntimeError("bug in the caller's filter")

    await page.enable_request_filter(broken)
    conn.fire("Fetch.requestPaused", {"requestId": "x", "request": {"url": "https://a/"}}, "S1")
    await asyncio.sleep(0)
    assert ("Fetch.fulfillRequest", {"requestId": "x", "responseCode": 204}, "S1") in conn.sent


def test_off_removes_exactly_the_handler_on_added():
    page, _ = _page()
    seen: list[str] = []
    first, second = seen.append, lambda value: seen.append(f"2:{value}")
    page.on("download", first)
    page.on("download", second)
    page.off("download", first)
    page._emit("download", "file")
    assert seen == ["2:file"]
    page.off("download", second)
    page._emit("download", "again")
    assert seen == ["2:file"]


async def test_expect_download_hands_over_the_next_download_and_stops_listening():
    page, _ = _page()
    waiting = asyncio.ensure_future(page.expect_download(timeout=2))
    await asyncio.sleep(0)
    page._emit("download", "the-download")
    assert await waiting == "the-download"
    assert "download" not in page._handlers, "the listener outlived the wait"


async def test_expect_download_times_out_and_still_stops_listening():
    page, _ = _page()
    with pytest.raises(asyncio.TimeoutError):
        await page.expect_download(timeout=0.05)
    assert "download" not in page._handlers
