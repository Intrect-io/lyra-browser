"""DevTools tools on fakes: the log, the gates, and what never leaves redacted."""

from __future__ import annotations

import json

import pytest

from conftest import FakeConsoleMessage, FakePage, FakeRequest, FakeResponse
from lyra_browser.config import Config
from lyra_browser.consent import ConsentBroker
from lyra_browser.devtools import REDACTED, DevtoolsRecorder, redact_headers
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore


def _traffic(page: FakePage) -> tuple[FakeRequest, FakeRequest]:
    ok = FakeRequest("https://api.example/data?token=abc", resource_type="fetch")
    page.emit("request", ok)
    page.emit("response", FakeResponse(ok, 200))
    ok.timing.update(responseStart=12.0, responseEnd=34.5)
    page.emit("requestfinished", ok)
    bad = FakeRequest("https://start.example/missing.png", resource_type="image")
    page.emit("request", bad)
    bad.failure = "net::ERR_NAME_NOT_RESOLVED"
    page.emit("requestfailed", bad)
    return ok, bad


# --- the recorder -----------------------------------------------------------------


def test_headers_are_redacted_by_name_and_by_word():
    out = redact_headers(
        {
            "Cookie": "sid=1",
            "Authorization": "Bearer x",
            "X-Api-Key": "k",
            "X-CSRF-Token": "t",
            "Accept": "*/*",
        }
    )
    assert out == {
        "cookie": REDACTED,
        "authorization": REDACTED,
        "x-api-key": REDACTED,
        "x-csrf-token": REDACTED,
        "accept": "*/*",
    }


def test_network_is_recorded_without_console(page):
    recorder = DevtoolsRecorder()
    recorder.attach(page)
    _traffic(page)

    assert "console" not in page.listeners, "console must stay off until asked for"
    rows = recorder.network()["entries"]
    assert [r["state"] for r in rows] == ["finished", "failed"]
    assert rows[0]["duration_ms"] == 34.5 and rows[0]["ttfb_ms"] == 12.0
    assert rows[0]["status"] == 200
    assert "abc" not in rows[0]["url"], "query values never reach the log"
    assert rows[1]["failure"] == "net::ERR_NAME_NOT_RESOLVED"
    assert rows[1]["duration_ms"] is not None


def test_attaching_twice_does_not_double_the_log(page):
    recorder = DevtoolsRecorder()
    recorder.attach(page)
    recorder.attach(page)
    page.emit("request", FakeRequest("https://start.example/x"))
    assert len(recorder.network_log) == 1


def test_the_log_is_bounded_and_counts_what_it_dropped(page):
    recorder = DevtoolsRecorder(network_limit=3)
    recorder.attach(page)
    for i in range(5):
        page.emit("request", FakeRequest(f"https://start.example/{i}"))
    out = recorder.network()
    assert len(out["entries"]) == 3 and out["dropped"] == 2


def test_paging_with_since(page):
    recorder = DevtoolsRecorder()
    recorder.attach(page)
    for i in range(5):
        page.emit("request", FakeRequest(f"https://start.example/{i}"))
    first = recorder.network(limit=2)
    assert first["more"] and len(first["entries"]) == 2
    second = recorder.network(since=first["next_since"], limit=10)
    assert [e["url"] for e in second["entries"]] == [
        f"https://start.example/{i}" for i in range(2, 5)
    ]
    assert not second["more"]


def test_console_on_reaches_open_and_later_tabs_and_off_unhooks(page):
    recorder = DevtoolsRecorder()
    recorder.attach(page)
    assert recorder.set_console(True) == 1
    later = FakePage("https://start.example/two")
    recorder.attach(later)
    page.emit("console", FakeConsoleMessage("boom", "error"))
    later.emit("pageerror", Exception("Uncaught TypeError: x is undefined"))

    rows = recorder.console()["entries"]
    assert [(r["level"], r["tab"]) for r in rows] == [("error", 1), ("exception", 2)]
    assert rows[0]["source"] == "https://start.example/app.js?<redacted: v>:3"

    recorder.set_console(False)
    page.emit("console", FakeConsoleMessage("after"))
    assert len(recorder.console()["entries"]) == 2
    assert recorder.console(levels=["exception"])["entries"][0]["tab"] == 2


# --- the tools --------------------------------------------------------------------


async def test_console_messages_is_off_until_asked(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    off = await tools["console_messages"]()
    assert off["status"] == "console_off" and off["capture"] is False
    assert "console" not in page.listeners

    on = await tools["console_messages"](capture=True)
    assert on["capture"] is True
    page.emit("console", FakeConsoleMessage("hello", "warning"))
    got = await tools["console_messages"](levels=["warning"])
    assert got["status"] == "ok" and got["entries"][0]["text"] == "hello"


async def test_console_messages_rejects_unknown_levels(make_ctx, tools_of):
    out = await (await tools_of(make_ctx()))["console_messages"](levels=["loud"])
    assert out["status"] == "invalid_argument"


async def test_network_requests_lists_and_filters(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    ctx.devtools.attach(page)  # what the session does for each tab it adopts
    _traffic(page)
    everything = await tools["network_requests"]()
    assert len(everything["entries"]) == 2
    assert everything["counts"]["failed"] == 1
    failed = await tools["network_requests"](failed_only=True)
    assert [e["type"] for e in failed["entries"]] == ["image"]
    images = await tools["network_requests"](resource_type="fetch")
    assert len(images["entries"]) == 1


async def test_network_request_redacts_headers_and_gates_the_body(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    ctx.devtools.attach(page)  # what the session does for each tab it adopts
    _traffic(page)
    entry_id = (await tools["network_requests"]())["entries"][0]["id"]

    plain = await tools["network_request"](id=entry_id)
    assert plain["request_headers"]["cookie"] == REDACTED
    assert plain["response_headers"]["set-cookie"] == REDACTED
    assert "body" not in plain and "token=abc" not in json.dumps(plain)

    refused = await tools["network_request"](id=entry_id, body=True)
    assert refused["status"] == "needs_approval"

    shown = await tools["network_request"](id=entry_id, body=True, confirm=True)
    assert shown["body"] == '{"ok": true}'
    assert shown["url"] == "https://api.example/data?token=abc"
    assert shown["request_headers"]["cookie"] == REDACTED, "INSPECT does not unredact headers"


async def test_network_request_unknown_id(make_ctx, tools_of):
    out = await (await tools_of(make_ctx()))["network_request"](id="r999")
    assert out["status"] == "not_found"


async def test_page_performance_strips_resource_queries(make_ctx, tools_of, page):
    out = await (await tools_of(make_ctx()))["page_performance"]()
    assert out["status"] == "ok"
    assert out["vitals"]["lcp_ms"] == 55.0
    assert "secret" not in out["resources"]["slowest"][0]["url"]


async def test_storage_lists_names_and_gates_values(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    cookies = await tools["storage"]()
    assert cookies["items"][0]["name"] == "sid" and "value" not in cookies["items"][0]
    local = await tools["storage"](kind="local")
    assert local["items"] == [{"name": "theme", "size": 4}]

    refused = await tools["storage"](reveal=True)
    assert refused["status"] == "needs_approval"
    revealed = await tools["storage"](reveal=True, confirm=True)
    assert revealed["items"][0]["value"] == "s3cr3t"


async def test_storage_rejects_unknown_kind(make_ctx, tools_of):
    out = await (await tools_of(make_ctx()))["storage"](kind="indexeddb")
    assert out["status"] == "invalid_argument"


async def test_export_har_is_redacted_and_bodies_are_gated(make_ctx, tools_of, page, tmp_path):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    ctx.devtools.attach(page)
    _traffic(page)
    out = await tools["export_har"]()
    assert out["status"] == "ok" and out["entries"] == 2
    har = json.loads(open(out["har_path"], encoding="utf-8").read())
    first = har["log"]["entries"][0]
    assert har["log"]["version"] == "1.2"
    assert {"name": "cookie", "value": REDACTED} in first["request"]["headers"]
    assert "abc" not in first["request"]["url"]
    assert "text" not in first["response"]["content"]
    assert first["time"] == 34.5

    refused = await tools["export_har"](include_bodies=True)
    assert refused["status"] == "needs_approval"
    full = await tools["export_har"](include_bodies=True, confirm=True)
    assert full["bodies"] == 1
    har = json.loads(open(full["har_path"], encoding="utf-8").read())
    assert har["log"]["entries"][0]["response"]["content"]["text"] == '{"ok": true}'


async def test_evaluate_asks_every_time(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    page.eval_answers["document.title"] = "Start"

    refused = await tools["evaluate"](expression="document.title")
    assert refused["status"] == "needs_approval"
    assert not any(c[0] == "evaluate" for c in page.calls), "a refused expression never runs"

    first = await tools["evaluate"](expression="document.title", confirm=True)
    assert first["status"] == "ok" and json.loads(first["result"]) == "Start"
    again = await tools["evaluate"](expression="document.title")
    assert again["status"] == "needs_approval", "EVALUATE is never leased"


async def test_evaluate_reports_the_page_exception(make_ctx, tools_of, page):
    page.eval_answers["boom()"] = RuntimeError("ReferenceError: boom is not defined")
    out = await (await tools_of(make_ctx()))["evaluate"](expression="boom()", confirm=True)
    assert out["status"] == "exception" and "boom is not defined" in out["error"]


async def test_evaluate_stands_down_for_a_takeover(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.collab.takeover = True
    out = await (await tools_of(ctx))["evaluate"](expression="1", confirm=True)
    assert out["status"] == "takeover_active"
    assert not any(c[0] == "evaluate" for c in page.calls)


async def test_evaluate_is_audited_with_the_expression(make_ctx, tools_of, page):
    ctx = make_ctx()
    await (await tools_of(ctx))["evaluate"](expression="1 + 1", confirm=True)
    rows = [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines()]
    assert any(r.get("tool") == "evaluate" and "1 + 1" in json.dumps(r) for r in rows)


# --- consent ----------------------------------------------------------------------


@pytest.mark.parametrize("capability", [Capability.EVALUATE, Capability.INSPECT])
async def test_autonomous_never_approves_evaluate_or_inspect(tmp_path, capability):
    from lyra_browser.audit import AuditLog

    cfg = Config(consent_channel="autonomous")
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    broker = ConsentBroker(cfg, PermissionStore(), AuditLog(tmp_path / "a.jsonl"))
    decision = await broker.request(
        action="x", origin=parse_origin("https://shop.example/"), capability=capability
    )
    assert not decision.allowed


def test_evaluate_is_single_use_and_inspect_is_leased():
    store = PermissionStore()
    origin = parse_origin("https://shop.example/")
    assert store.is_one_shot(origin, Capability.EVALUATE)
    assert not store.is_one_shot(origin, Capability.INSPECT)
    store.grant("s", origin, Capability.NAVIGATE)
    assert not store.check("s", origin, Capability.INSPECT), "visiting a site does not inspect it"
