"""UAT mode on a fake page: what the server records, refuses and attaches.

Every test drives the server the way a brain does — through an in-memory
FastMCP client — so the middleware, the UAT tools and the disabled tools are
exercised exactly as a model would meet them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from mcp.types import ImageContent

from conftest import FakePage, FakeSession
from lyra_browser.uat.recorder import looks_like_card_number
from lyra_browser.uat.server import build_uat_server
from lyra_browser.uat.spec import RunSpec

ENTRY = "https://start.example/"


async def make(
    tmp_path: Path,
    page: FakePage,
    *,
    budget: int = 5,
    uploads: Path | None = None,
    prior: tuple[str, ...] = (),
    vision: str = "on_demand",
    auto_screenshot: bool = True,
):
    persona = {
        "id": "p1",
        "name": "Tester",
        "entry_url": ENTRY,
        "goal": "look around",
        "step_budget": budget,
    }
    if uploads is not None:
        persona["uploads_allowed_dir"] = str(uploads)
    spec = RunSpec.model_validate(
        {
            "persona": persona,
            "target": {
                "trusted_origins": ["start.example"],
                "trusted_send_origins": ["start.example"],
            },
            "brain": {"backend": "scripted", "script": [{"tool": "finish", "args": {}}]},
            "prior_findings": [{"id": pid, "actual": "was broken"} for pid in prior],
            "limits": {"vision": vision, "auto_screenshot": auto_screenshot},
            "out_dir": str(tmp_path),
        }
    )
    run_dir = tmp_path / "run"
    mcp, ctx, recorder = await build_uat_server(
        spec, run_dir, session_factory=lambda cfg: FakeSession(page)
    )
    # As conftest.make_ctx does: no settle window, and the download handler the
    # real session would put on every tab.
    ctx.config.download_settle_s = 0.0
    page.on_download = ctx.downloads.on_download
    return mcp, ctx, recorder, run_dir


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def call(client: Client, name: str, **args) -> dict:
    result = await client.call_tool(name, args, raise_on_error=False)
    return result.structured_content or {}


def test_card_number_detection_uses_luhn():
    assert looks_like_card_number("4242 4242 4242 4242")
    assert looks_like_card_number("Card: 4111-1111-1111-1111 exp 12/30")
    assert not looks_like_card_number("4242 4242 4242 4243")  # fails Luhn
    assert not looks_like_card_number("order 1234567890")  # too short
    assert not looks_like_card_number("hello")


@pytest.mark.asyncio
async def test_entry_navigation_is_recorded_but_free(tmp_path, page):
    mcp, _ctx, recorder, run_dir = await make(tmp_path, page, budget=1)
    async with Client(mcp) as client:
        first = await call(client, "navigate", url=ENTRY)
        assert first["status"] == "ok"
        # The budget of one is still whole: the entry move was given.
        second = await call(client, "click", selector="#plain")
        assert second["status"] == "ok"
        third = await call(client, "click", selector="#plain")
        assert third["status"] == "budget_exhausted"
    trace = rows(run_dir / "trace.jsonl")
    assert [(r["tool"], r["system"], r["counted"]) for r in trace] == [
        ("navigate", True, False),
        ("click", False, True),
        ("click", False, False),
    ]
    assert trace[0]["url_after"] == ENTRY
    assert recorder.steps_used == 1


@pytest.mark.asyncio
async def test_budget_refusal_leaves_reads_working_and_is_a_policy_event(tmp_path, page):
    mcp, _ctx, _recorder, run_dir = await make(tmp_path, page, budget=1)
    async with Client(mcp) as client:
        assert (await call(client, "click", selector="#a"))["status"] == "ok"
        refused = await call(client, "type_text", selector="#f", value="hi")
        assert refused["status"] == "budget_exhausted"
        assert refused["steps_used"] == 1 and refused["step_budget"] == 1
        read = await call(client, "read_page")
        assert "text" in read
        assert (await call(client, "get_url"))["url"] == page.url
    events = rows(run_dir / "events.jsonl")
    assert [e["kind"] for e in events if e["type"] == "policy"] == ["budget_exhausted"]
    # The refused type_text never reached the page.
    assert not any(c[0] == "fill" for c in page.calls)


@pytest.mark.asyncio
async def test_failed_attempts_do_not_spend_the_budget(tmp_path, page):
    page.present = {"#real"}
    mcp, _ctx, recorder, _run_dir = await make(tmp_path, page, budget=1)
    async with Client(mcp) as client:
        assert (await call(client, "click", selector="#missing"))["status"] == "not_found"
        assert recorder.steps_used == 0
        assert (await call(client, "click", selector="#real"))["status"] == "ok"
        assert recorder.steps_used == 1


@pytest.mark.asyncio
async def test_typing_a_card_number_is_refused_by_policy(tmp_path, page):
    mcp, _ctx, _recorder, run_dir = await make(tmp_path, page)
    async with Client(mcp) as client:
        refused = await call(client, "type_text", selector="#card", value="4242 4242 4242 4242")
        assert refused["status"] == "blocked_by_uat_policy"
        assert not any(c[0] == "fill" for c in page.calls)
        allowed = await call(client, "type_text", selector="#name", value="Jane")
        assert allowed["status"] == "ok"
        assert any(c[0] == "fill" for c in page.calls)
    events = rows(run_dir / "events.jsonl")
    assert any(e["type"] == "policy" and e["kind"] == "guard_blocked_input" for e in events)
    # The audit-style redaction applies to the trace too: the typed value is not on disk.
    trace = rows(run_dir / "trace.jsonl")
    assert all("Jane" not in json.dumps(r) for r in trace)


@pytest.mark.asyncio
async def test_uploads_are_confined_to_the_persona_directory(tmp_path, page):
    assets = tmp_path / "assets"
    assets.mkdir()
    sample = assets / "sample.wav"
    sample.write_bytes(b"RIFF")
    elsewhere = tmp_path / "secret.txt"
    elsewhere.write_text("x")
    mcp, _ctx, _recorder, _run_dir = await make(tmp_path, page, uploads=assets)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert "upload_file" in names
        refused = await call(client, "upload_file", selector="#file", paths=[str(elsewhere)])
        assert refused["status"] == "blocked_by_uat_policy"
        assert refused["rejected"] == [str(elsewhere)]
        assert page.attached == []
        ok = await call(client, "upload_file", selector="#file", paths=[str(sample)])
        assert ok["status"] == "ok", ok
        assert page.attached == [str(sample)]


@pytest.mark.asyncio
async def test_without_an_upload_directory_the_tool_is_not_offered(tmp_path, page):
    mcp, _ctx, _recorder, _run_dir = await make(tmp_path, page)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "upload_file" not in names
    # Nor are the runner's, the editor's, or the collaboration tools.
    assert names.isdisjoint(
        {"open_browser", "close_browser", "publish", "save_draft", "set_editor", "read_draft"}
    )
    assert names.isdisjoint({"ask_user_to_do", "request_takeover", "highlight_element"})
    assert {"report_finding", "verdict", "note", "finish", "navigate", "read_page"} <= names


@pytest.mark.asyncio
async def test_findings_verdicts_notes_and_finish_are_events(tmp_path, page):
    mcp, _ctx, recorder, run_dir = await make(tmp_path, page, prior=("R1-03",))
    async with Client(mcp) as client:
        bad = await call(
            client, "report_finding", severity="huge", url=ENTRY, step=1, expected="a", actual="b"
        )
        assert bad["status"] == "error"
        found = await call(
            client,
            "report_finding",
            severity="major",
            url=ENTRY,
            step=1,
            expected="a price",
            actual="no price",
            evidence=["/tmp/shot.png"],
        )
        assert found == {"status": "ok", "id": "F1", "findings": 1}
        unknown = await call(client, "verdict", finding_id="nope", status="FIXED")
        assert unknown["status"] == "error" and unknown["known"] == ["R1-03"]
        judged = await call(
            client, "verdict", finding_id="R1-03", status="STILL_THERE", evidence="same banner"
        )
        assert judged["status"] == "ok" and judged["missing"] == []
        assert (await call(client, "note", text="going back to pricing"))["status"] == "ok"
        done = await call(
            client, "finish", outcome="partial", summary="Got halfway.", what_worked=["clear hero"]
        )
        assert done["status"] == "ok" and done["findings"] == 1 and done["verdicts"] == 1
        # The run is over: browser tools and the report tools alike say so.
        assert (await call(client, "click", selector="#plain"))["status"] == "finished"
        assert (await call(client, "finish", outcome="partial", summary="again"))[
            "status"
        ] == "finished"
        assert (await call(client, "note", text="late"))["status"] == "finished"
    assert recorder.finished
    events = rows(run_dir / "events.jsonl")
    kinds = [e["type"] for e in events]
    assert kinds == ["finding", "verdict", "note", "finish"]
    finding = events[0]
    assert finding["id"] == "F1" and finding["severity"] == "major"
    assert finding["evidence"] == ["/tmp/shot.png"]
    assert events[-1]["outcome"] == "partial" and events[-1]["what_worked"] == ["clear hero"]
    # The attempt after finish is still in the trace, refused.
    trace = rows(run_dir / "trace.jsonl")
    assert trace[-1]["tool"] == "note" and trace[-1]["status"] == "finished"


@pytest.mark.asyncio
async def test_actions_get_a_screenshot_for_the_trace(tmp_path, page):
    mcp, _ctx, _recorder, run_dir = await make(tmp_path, page)
    async with Client(mcp) as client:
        await call(client, "click", selector="#plain")
        await call(client, "read_page")
    trace = rows(run_dir / "trace.jsonl")
    click, read = trace
    assert click["screenshot"] and Path(click["screenshot"]).is_file()
    assert Path(click["screenshot"]).parent == run_dir / "captures"
    assert read["screenshot"] is None  # reads are not illustrated
    # The trace capture is not itself a step.
    assert [r["tool"] for r in trace] == ["click", "read_page"]


@pytest.mark.asyncio
async def test_auto_screenshot_can_be_turned_off(tmp_path, page):
    mcp, _ctx, _recorder, run_dir = await make(tmp_path, page, auto_screenshot=False)
    async with Client(mcp) as client:
        await call(client, "click", selector="#plain")
    assert rows(run_dir / "trace.jsonl")[0]["screenshot"] is None


@pytest.mark.asyncio
async def test_screenshot_results_carry_the_image_when_vision_is_on(tmp_path, page):
    mcp, _ctx, _recorder, _run_dir = await make(tmp_path, page)
    async with Client(mcp) as client:
        result = await client.call_tool("screenshot", {}, raise_on_error=False)
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1 and images[0].mimeType == "image/png"
    assert result.structured_content["image_path"]

    mcp, _ctx, _recorder, _run_dir = await make(tmp_path / "b", page, vision="never")
    async with Client(mcp) as client:
        result = await client.call_tool("screenshot", {}, raise_on_error=False)
    assert not any(isinstance(c, ImageContent) for c in result.content)


@pytest.mark.asyncio
async def test_leaving_the_target_is_refused_even_with_confirm(tmp_path, page):
    """The self-approval hole: with consent on ``auto`` an in-memory client that cannot
    be asked falls back to honouring the model's confirm=true. UAT pins ``elicit``."""
    mcp, ctx, _recorder, run_dir = await make(tmp_path, page)
    assert ctx.config.consent_channel == "elicit"
    async with Client(mcp) as client:
        first = await call(client, "navigate", url="https://other.example/")
        assert first["status"] == "needs_approval"
        again = await call(client, "navigate", url="https://other.example/", confirm=True)
        assert again["status"] == "needs_approval"
        home = await call(client, "navigate", url="https://start.example/pricing")
        assert home["status"] == "ok"
    assert page.url == "https://start.example/pricing"
    trace = rows(run_dir / "trace.jsonl")
    assert [r["status"] for r in trace] == ["needs_approval", "needs_approval", "ok"]
    assert [r["counted"] for r in trace] == [False, False, True]
