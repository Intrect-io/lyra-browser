"""drag, evaluate_readonly, read_download, save_deliverable and set_emulation on fakes.

The real-browser half (that Chrome refuses a write under ``throwOnSideEffect``, that an
override survives a navigation and reaches a new tab, that a drag moves a slider) is
``scripts/verify_task_tools_e2e.py``.
"""

from __future__ import annotations

import json

import pytest

from lyra_browser.tools import tasks


async def test_drag_presses_on_the_source_and_releases_on_the_target(make_ctx, tools_of, page):
    ctx = make_ctx()
    drag = (await tools_of(ctx))["drag"]

    result = await drag(source="#card", target="#done")

    assert result["status"] == "ok"
    moves = [c for c in page.calls if c[0] in ("hover", "mouse_down", "mouse_up")]
    assert moves == [("hover", "#card"), ("mouse_down",), ("hover", "#done"), ("mouse_up",)]
    rows = [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines()]
    assert any(r["tool"] == "drag" and r["args"].get("target") == "#done" for r in rows)


async def test_drag_checks_both_ends_before_touching_the_page(make_ctx, tools_of, page):
    page.present = {"#card"}  # the target does not exist
    drag = (await tools_of(make_ctx()))["drag"]

    result = await drag(source="#card", target="#nowhere")

    assert result["status"] == "not_found"
    assert not [c for c in page.calls if c[0] in ("mouse_down", "mouse_up")]


async def test_drag_off_site_needs_approval_and_does_nothing(make_ctx, tools_of, page):
    drag = (await tools_of(make_ctx(on_site=False)))["drag"]

    result = await drag(source="#a", target="#b")

    assert result["status"] == "needs_approval"
    assert not [c for c in page.calls if c[0] in ("mouse_down", "mouse_up")]


async def test_evaluate_readonly_asks_the_engine_to_refuse_side_effects(make_ctx, tools_of, page):
    page.context.cdp_answers["Runtime.evaluate"] = {"result": {"type": "number", "value": 3}}
    ev = (await tools_of(make_ctx()))["evaluate_readonly"]

    result = await ev(expression="document.querySelectorAll('li').length")

    assert result == {
        "status": "ok",
        "result": "3",
        "truncated": False,
        "type": "number",
        "url": page.url,
    }
    method, params = page.context.cdp_calls[-1]
    assert method == "Runtime.evaluate" and params["throwOnSideEffect"] is True


async def test_evaluate_readonly_reports_a_refused_write(make_ctx, tools_of, page):
    page.context.cdp_answers["Runtime.evaluate"] = {
        "result": {"type": "object"},
        "exceptionDetails": {
            "text": "Uncaught",
            "exception": {"description": "EvalError: Possible side-effect in debug-evaluate"},
        },
    }
    ev = (await tools_of(make_ctx()))["evaluate_readonly"]

    result = await ev(expression="document.title = 'x'")

    assert result["status"] == "refused_side_effect"


async def test_evaluate_readonly_does_not_read_storage(make_ctx, tools_of, page):
    ev = (await tools_of(make_ctx()))["evaluate_readonly"]

    for expression in ("document.cookie", "localStorage.getItem('t')", "window.sessionStorage"):
        assert (await ev(expression=expression))["status"] == "refused_storage"
    assert page.context.cdp_calls == []


async def test_read_download_reads_only_what_was_saved(make_ctx, tools_of, tmp_path):
    ctx = make_ctx()
    saved = tmp_path / "downloads" / "report.csv"
    saved.parent.mkdir(parents=True)
    saved.write_text("a,b\n1,2\n")
    binary = saved.with_name("logo.png")
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    ctx.downloads._saved[ctx.downloads._session_key()] = [
        {"filename": "report.csv", "path": str(saved), "bytes": 8, "url_origin": "x"},
        {"filename": "logo.png", "path": str(binary), "bytes": 10, "url_origin": "x"},
    ]
    read = (await tools_of(ctx))["read_download"]

    text = await read(filename="report.csv")
    assert text["status"] == "ok" and text["text"] == "a,b\n1,2\n" and not text["truncated"]
    cut = await read(filename=str(saved), max_chars=3)
    assert cut["text"] == "a,b" and cut["truncated"]
    image = await read(filename="logo.png")
    assert image["binary"] is True and image["head_hex"].startswith("89504e47")
    assert (await read(filename="/etc/passwd"))["status"] == "not_found"


async def test_save_deliverable_writes_a_plain_name_once(make_ctx, tools_of, tmp_path):
    ctx = make_ctx()
    save = (await tools_of(ctx))["save_deliverable"]

    first = await save(name="prices.csv", content="plan,price\nStudio,29\n")
    assert first["status"] == "ok"
    assert (tmp_path / "deliverables" / "prices.csv").read_text() == "plan,price\nStudio,29\n"
    assert (await save(name="prices.csv", content="x"))["status"] == "exists"
    assert (await save(name="prices.csv", content="x", overwrite=True))["status"] == "ok"
    for bad in ("../escape.txt", "a/b.txt", ".hidden", "", "x" * 200):
        assert (await save(name=bad, content="x"))["status"] == "invalid_argument"
    assert (await save(name="big.txt", content="x" * 1_000_001))["status"] == "invalid_argument"
    rows = [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines()]
    assert all("content" not in r.get("args", {}) for r in rows if r["tool"] == "save_deliverable")


async def test_set_emulation_overrides_and_grants_geolocation(make_ctx, tools_of, page):
    emulate = (await tools_of(make_ctx()))["set_emulation"]

    result = await emulate(
        latitude=52.52, longitude=13.405, locale="de-DE", timezone="Europe/Berlin"
    )

    assert result["status"] == "ok"
    sent = dict(page.context.cdp_calls)
    assert sent["Emulation.setGeolocationOverride"]["latitude"] == 52.52
    assert sent["Emulation.setLocaleOverride"] == {"locale": "de-DE"}
    assert sent["Emulation.setTimezoneOverride"] == {"timezoneId": "Europe/Berlin"}
    assert page.context.granted == [["geolocation"]]

    page.context.cdp_calls.clear()
    cleared = await emulate(clear=True)
    assert cleared["status"] == "ok" and cleared["emulation"] == {}
    assert dict(page.context.cdp_calls)["Emulation.setLocaleOverride"] == {"locale": ""}


async def test_set_emulation_reaches_a_tab_opened_afterwards(make_ctx, tools_of, page):
    import asyncio

    from conftest import FakePage

    ctx = make_ctx()
    emulate = (await tools_of(ctx))["set_emulation"]
    await emulate(locale="ko-KR")
    popup = FakePage("https://start.example/popup")
    for observer in ctx.session.page_observers:
        observer(popup)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ("Emulation.setLocaleOverride", {"locale": "ko-KR"}) in popup.context.cdp_calls


@pytest.mark.parametrize(
    "kwargs",
    [{"latitude": 1.0}, {"latitude": 91.0, "longitude": 0.0}, {}],
)
async def test_set_emulation_refuses_incomplete_requests(make_ctx, tools_of, kwargs):
    emulate = (await tools_of(make_ctx()))["set_emulation"]
    assert (await emulate(**kwargs))["status"] == "invalid_argument"


def test_the_storage_names_are_matched_as_words():
    assert tasks._STORAGE_NAMES.search("document.cookie")
    assert not tasks._STORAGE_NAMES.search("cookies_accepted_banner_count")
