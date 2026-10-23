"""URL globs and observer event objects of the native driver.

Real-browser behaviour (waits that actually wait, the five observer events, Runtime.enable
sent only once something observes console output) is in
``testing/native_wait_observe_smoke_261023_v1.py``.
"""

from __future__ import annotations

import re

import pytest

from lyra_browser.native.events import (
    ConsoleMessage,
    ObservedResponse,
    PageError,
    Request,
    glob_to_regex,
    url_matcher,
)


@pytest.mark.parametrize(
    ("glob", "url", "hit"),
    [
        ("**/step/*?x=1", "http://h:1/step/2?x=1", True),
        ("**/step/*?x=1", "http://h:1/step/2/3?x=1", False),
        ("https://a.com/*", "https://a.com/login", True),
        ("https://a.com/*", "https://a.com/a/b", False),
        ("https://a.com/**", "https://a.com/a/b", True),
        ("**/{login,signin}", "https://a.com/signin", True),
        ("**/login", "https://a.com/login?next=1", False),
        ("**/a\\*b", "https://x/a*b", True),
    ],
)
def test_globs_follow_playwright(glob, url, hit):
    assert (glob_to_regex(glob).match(url) is not None) is hit


def test_url_matcher_takes_a_glob_a_regex_or_a_predicate():
    assert url_matcher("**/done")("https://x/done")
    assert url_matcher(re.compile(r"/done$"))("https://x/done")
    assert url_matcher(lambda u: u.endswith("ok"))("https://x/ok")


def test_events_carry_what_the_observer_reads():
    req = Request(
        {"request": {"url": "https://x/api", "method": "POST", "postData": "a=1"}, "type": "XHR"}
    )
    assert (req.url, req.method, req.resource_type, req.post_data) == (
        "https://x/api",
        "POST",
        "xhr",
        "a=1",
    )
    resp = ObservedResponse({"response": {"url": "https://x/api", "status": 503}}, req)
    assert resp.status == 503 and resp.request is req
    msg = ConsoleMessage(
        {
            "type": "error",
            "args": [{"value": "boom"}, {"value": 42}],
            "stackTrace": {"callFrames": [{"url": "https://x/app.js"}]},
        }
    )
    assert (msg.type, msg.text, msg.location["url"]) == ("error", "boom 42", "https://x/app.js")
    err = PageError({"exceptionDetails": {"exception": {"description": "Error: kaboom"}}})
    assert err.message == "Error: kaboom" and str(err) == "Error: kaboom"
