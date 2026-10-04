"""Network and console observers: what is kept whole, what is summarised, what is counted."""

from __future__ import annotations

import json
from types import SimpleNamespace

from lyra_browser.uat.observe import PageObservers


class Page:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_matched_requests_keep_query_and_body_others_are_summarised(tmp_path):
    obs = PageObservers(tmp_path, capture_patterns=[r"/g/collect"])
    page = Page()
    obs.attach(page)
    assert set(page.handlers) == {"request", "response", "requestfailed", "console", "pageerror"}

    page.handlers["request"](
        SimpleNamespace(
            url="https://www.google-analytics.com/g/collect?v=2&en=page_view&tt=internal",
            method="POST",
            resource_type="xhr",
            post_data="en=purchase&cu=USD",
        )
    )
    page.handlers["request"](
        SimpleNamespace(
            url="https://example.com/search?q=secret+words&token=abc",
            method="GET",
            resource_type="document",
            post_data=None,
        )
    )
    obs.flush()
    hit, doc = rows(tmp_path / "network.jsonl")
    assert hit["matched"] == "/g/collect"
    assert "en=page_view" in hit["url"] and hit["post_data"] == "en=purchase&cu=USD"
    assert "secret" not in doc["url"] and "abc" not in doc["url"]
    assert doc["url"].startswith("https://example.com/search?")  # names survive, values do not
    assert "post_data" not in doc
    assert obs.counts["requests"] == 2 and obs.matched["/g/collect"] == 1


def test_responses_are_kept_for_documents_matches_and_errors_only(tmp_path):
    obs = PageObservers(tmp_path, capture_patterns=[r"/api/"])
    page = Page()
    obs.attach(page)
    respond = page.handlers["response"]
    respond(
        SimpleNamespace(
            url="https://example.com/a.css",
            status=200,
            request=SimpleNamespace(resource_type="stylesheet"),
        )
    )
    respond(
        SimpleNamespace(
            url="https://example.com/",
            status=200,
            request=SimpleNamespace(resource_type="document"),
        )
    )
    respond(
        SimpleNamespace(
            url="https://example.com/img.png",
            status=500,
            request=SimpleNamespace(resource_type="image"),
        )
    )
    respond(
        SimpleNamespace(
            url="https://example.com/api/me?x=1",
            status=401,
            request=SimpleNamespace(resource_type="fetch"),
        )
    )
    obs.flush()
    kept = rows(tmp_path / "network.jsonl")
    assert [(r["url"], r["status"]) for r in kept] == [
        ("https://example.com/", 200),
        ("https://example.com/img.png", 500),
        ("https://example.com/api/me?x=1", 401),
    ]
    assert obs.counts["http_errors"] == 2


def test_console_and_page_errors_are_counted_and_truncated(tmp_path):
    obs = PageObservers(tmp_path)
    page = Page()
    obs.attach(page)
    page.handlers["console"](
        SimpleNamespace(type="error", text="x" * 2000, location={"url": "https://e.com/app.js?v=9"})
    )
    page.handlers["console"](SimpleNamespace(type="warning", text="careful", location=None))
    page.handlers["pageerror"](SimpleNamespace(message="TypeError: boom"))
    page.handlers["requestfailed"](
        SimpleNamespace(
            url="https://e.com/x", method="GET", resource_type="fetch", failure="net::ERR_FAILED"
        )
    )
    summary = obs.summary()
    console = rows(tmp_path / "console.jsonl")
    assert [c["kind"] for c in console] == ["console", "console", "pageerror"]
    assert len(console[0]["text"]) == 500
    # Query values are stripped from console locations too; the name survives.
    assert console[0]["url"].startswith("https://e.com/app.js?") and "v=9" not in console[0]["url"]
    assert summary["counts"] == {
        "requests": 0,
        "failed": 1,
        "http_errors": 0,
        "console_errors": 1,
        "console_warnings": 1,
        "page_errors": 1,
    }
    assert summary["pages"] == 1 and summary["network_file"] and summary["console_file"]


def test_a_page_without_on_is_skipped_not_fatal(tmp_path):
    obs = PageObservers(tmp_path)
    obs.attach(object())
    assert obs.attached_pages == 0
    assert obs.summary()["network_file"] is None
