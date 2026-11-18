#!/usr/bin/env python3
"""Visual eval: how often an agent finishes tasks that need sight, through lyra's tools.

The tasks (``eval_visual_site.py``) keep their answer in pixels: a target drawn on a
canvas, icon-only buttons, a code shown for 2.5 s, a CSS colour, a dialog whose close
button is drawn, a chart value, text spilling out of a box, a panel that changes colour.
The site scores what the page saw the agent do; the agent's own account never counts.

Two modes:

- ``--brain scripted`` (the gate; no model): an oracle self-test. Each task is played
  twice through a real browser, once correctly and once wrongly, using the solution only
  the harness can fetch. The oracle must accept the first and reject the second. Prints
  ``PASS ...`` per check and exits non-zero on the first failure.
- ``--brain claude-code`` (or another ``lyra-uat`` backend): each task x ``--repeats`` is a
  ``lyra-uat`` persona run against a fresh round of the task, and the result file
  (``--out``) records each episode's success, steps and seconds. No home paths are written.

``--suite v2`` picks the harder tasks of ``eval_visual_site_v2.py`` (tiny text, close bars,
below the fold, hover-only labels, a subtle regression, overlap, low contrast, misalignment).

    python scripts/eval_visual.py --brain scripted --headless-only
    python scripts/eval_visual.py --brain scripted --suite v2 --headless-only
    python scripts/eval_visual.py --brain claude-code --model sonnet --repeats 2 \
        --out docs/measurements/visual-eval/baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_visual_site import ALL_GOALS, SUITES, EvalSite, serve  # noqa: E402
from eval_visual_site_v2 import BARS, TINY  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _get(base: str, task: str, what: str, token: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/{task}/{what}?token={token}", timeout=10) as r:
        return json.loads(r.read())


def expect(condition: bool, message: str, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail}")
    print(f"PASS  {message}")


# ---- scripted: the oracle self-test ------------------------------------------------------


async def _canvas_origin(page, selector: str) -> tuple[float, float]:
    box = await page.evaluate(
        f"(() => {{ const b = document.querySelector({json.dumps(selector)})"
        ".getBoundingClientRect(); return [b.left, b.top]; })()"
    )
    return float(box[0]), float(box[1])


async def _play(task: str, correct: bool, *, page, tools, sol: dict, base: str, token: str):
    click = tools["click"]
    type_text = tools["type_text"]

    async def press(selector: str) -> None:
        r = await click(selector=selector)
        assert r.get("status") == "ok", (selector, r)

    async def press_at(css_x: float, css_y: float) -> None:
        # Through the tool, as an agent would: pixels of the latest screenshot.
        shot = await tools["screenshot"]()
        scale = shot.get("scale", 1.0)
        r = await click(x=css_x * scale, y=css_y * scale)
        assert r.get("status") == "ok", r

    if task == "canvas_target":
        target = sol["shapes"][0] if correct else sol["shapes"][1]
        x0, y0 = await _canvas_origin(page, "#c")
        await press_at(x0 + target["x"], y0 + target["y"])
    elif task == "icon_toolbar":
        i = sol["icons"].index("trash")
        await press(f'#bar button[data-i="{i if correct else (i + 1) % len(sol["icons"])}"]')
    elif task == "toast_code":
        await press("#gen")
        await asyncio.sleep(0.3)
        issued = _get(base, task, "solution", token)["issued"]
        value = issued if correct else "WRONG1"
        r = await type_text(selector="#code", value=value)
        assert r.get("status") == "ok", r
        await press("#check")
    elif task == "color_state":
        if correct:
            for _ in range(sol["order"].index("blue")):
                await press("#next")
        await press("#done")
    elif task == "canvas_modal":
        w, h = await page.evaluate("[innerWidth, innerHeight]")
        bx, by = (w - 460) / 2, (h - 260) / 2
        cx = bx + 26 if sol["corner"][1] == "l" else bx + 460 - 26
        cy = by + 26 if sol["corner"][0] == "t" else by + 260 - 26
        if correct:
            await press_at(cx, cy)
            await asyncio.sleep(0.2)
            await press("#cont")
        else:
            # The dialog is still up: a press where Continue is lands on the canvas.
            bx0, by0 = await _canvas_origin(page, "#cont")
            await press_at(bx0 + 10, by0 + 8)
    elif task == "chart_value":
        want = sol["values"][sol["months"].index(sol["target"])]
        r = await type_text(selector="#val", value=str(want if correct else want + 1))
        assert r.get("status") == "ok", r
        await press("#check")
    elif task == "layout_fault":
        i = sol["faulty"] if correct else (sol["faulty"] + 1) % 4
        await press(f'#cards button[data-i="{i}"]')
    elif task == "changed_panel":
        await press("#apply")
        i = sol["changed"] if correct else (sol["changed"] + 1) % 3
        await press(f'.panel button[data-i="{i}"]')
    elif task == "tiny_find":
        i = sol["target"] if correct else (sol["target"] + 1) % len(sol["codes"])
        x0, y0 = await _canvas_origin(page, "#c")
        col, row = i % TINY["cols"], i // TINY["cols"]
        await press_at(
            x0 + TINY["x0"] + col * TINY["dx"] + TINY["w"] / 2,
            y0 + TINY["y0"] + row * TINY["dy"] + TINY["h"] / 2,
        )
    elif task == "tall_bar":
        heights = sol["heights"]
        i = sol["target"] if correct else heights.index(248)
        x0, y0 = await _canvas_origin(page, "#c")
        await press_at(x0 + BARS["x0"] + i * BARS["dx"] + BARS["w"] / 2, y0 + BARS["base"] - 20)
    elif task == "far_target":
        if correct:
            await page.evaluate("document.getElementById('c2').scrollIntoView({block:'center'})")
            await asyncio.sleep(0.2)
            shape = next(s for s in sol["far"] if s["id"] == sol["target"])
            x0, y0 = await _canvas_origin(page, "#c2")
        else:
            shape = next(s for s in sol["top"] if s["shape"] == "hexagon")
            x0, y0 = await _canvas_origin(page, "#c1")
        await press_at(x0 + shape["x"], y0 + shape["y"])
    elif task == "hover_tip":
        i = sol["target"] if correct else (sol["target"] + 1) % len(sol["tips"])
        r = await tools["hover"](selector=f'#bar .rb[data-i="{i}"]')
        assert r.get("status") == "ok", r
        await press(f'#bar .rb[data-i="{i}"]')
    elif task == "regression":
        await press("#deploy")
        i = sol["changed"] if correct else (sol["changed"] + 1) % 12
        await press(f'#grid button[data-i="{i}"]')
    elif task == "overlap":
        i = sol["covered"] if correct else (sol["covered"] + 1) % 4
        await press(f'#cards button[data-i="{i}"]')
    elif task == "faint_text":
        i = sol["faint"] if correct else (sol["faint"] + 1) % 6
        await press(f'#cards button[data-i="{i}"]')
    elif task == "misaligned":
        i = sol["off"] if correct else (sol["off"] + 1) % 16
        await press(f'#grid .bx[data-i="{i}"]')
    else:
        raise KeyError(task)


async def scripted(headless: bool, suite: str = "v1") -> int:
    from verify_browser_e2e import build_tools

    from lyra_browser.context import acquire_page

    site = EvalSite(seed=7)
    server = serve(site)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    tmp = tempfile.TemporaryDirectory(prefix="lyra-eval-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        forbidden = urllib.request.Request(f"{base}/api/toast_code/solution?token=nope")
        try:
            urllib.request.urlopen(forbidden, timeout=5)
            expect(False, "control endpoints refuse a wrong token")
        except urllib.error.HTTPError as exc:
            expect(exc.code == 403, "control endpoints refuse a wrong token", exc.code)
        for task in SUITES[suite]:
            for correct in (True, False):
                _get(base, task, "reset", site.token)
                nav = await tools["navigate"](url=f"{base}/task/{task}")
                expect(nav.get("status") == "ok", f"{task}: page loads", nav)
                text = await tools["read_page"]()
                page, err = await acquire_page(ctx)
                assert err is None, err
                sol = _get(base, task, "solution", site.token)
                await _play(
                    task, correct, page=page, tools=tools, sol=sol, base=base, token=site.token
                )
                await asyncio.sleep(0.3)
                result = _get(base, task, "result", site.token)
                word = "accepts the right answer" if correct else "rejects a wrong one"
                expect(result["success"] is correct, f"{task}: the oracle {word}", result)
                if correct:
                    # The answer must not be text a reader hands over.
                    leaked = _leaks(task, sol, json.dumps(text))
                    expect(not leaked, f"{task}: read_page does not give the answer away", leaked)
    finally:
        await tools["close_browser"]()
        server.shutdown()
        server.server_close()
        tmp.cleanup()
    print("ALL PASS")
    return 0


def _leaks(task: str, sol: dict, text: str) -> str:
    """The answer of a round, if it shows up in read_page's text."""
    if task == "chart_value":
        want = str(sol["values"][sol["months"].index(sol["target"])])
        return want if f" {want} " in f" {text} " or f'"{want}"' in text else ""
    if task == "color_state":
        return "blue" if "blue" in text.lower() else ""
    if task == "icon_toolbar":
        return "trash" if "trash" in text.lower() else ""
    if task == "canvas_target":
        return "star" if "star" in text.lower() else ""
    if task == "tiny_find":
        code = sol["codes"][sol["target"]]
        return code if code in text else ""
    if task == "hover_tip":
        return "archive" if "archive" in text.lower() else ""
    if task == "far_target":
        return "hexagon" if "hexagon" in text.lower() else ""
    return ""


# ---- model mode: the measurement --------------------------------------------------------


def _chrome_version() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium"):
        path = shutil.which(name)
        if path:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
            return out.stdout.strip() or "unknown"
    return "unknown"


def _commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO, capture_output=True, text=True
    )
    return out.stdout.strip() or "unknown"


async def measure(args: argparse.Namespace) -> int:
    from lyra_browser.uat.runner import run_spec
    from lyra_browser.uat.spec import BrainSpec, Limits, Persona, RunSpec, Target

    site = EvalSite()
    server = serve(site)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    tasks = [t for t in SUITES[args.suite] if not args.tasks or t in args.tasks.split(",")]
    episodes: list[dict] = []
    model_seen: str | None = None
    out_root = Path(tempfile.mkdtemp(prefix="lyra-eval-runs-"))
    try:
        for repeat in range(args.repeats):
            for task in tasks:
                _get(base, task, "reset", site.token)
                spec = RunSpec(
                    persona=Persona(
                        id=f"eval-{task}",
                        name="Visual eval",
                        entry_url=f"{base}/task/{task}",
                        goal=ALL_GOALS[task],
                        success_criteria=["The page has registered the requested action."],
                        step_budget=args.steps,
                    ),
                    target=Target(trusted_origins=[base]),
                    brain=BrainSpec(
                        backend=args.brain,
                        model=args.model,
                        max_turns=args.max_turns,
                        max_budget_usd=args.max_budget_usd,
                    ),
                    limits=Limits(wall_s=args.wall_s),
                    out_dir=out_root,
                    headless=True,
                )
                started = time.monotonic()
                report = await run_spec(spec)
                seconds = round(time.monotonic() - started, 1)
                result = _get(base, task, "result", site.token)
                model_seen = model_seen or report.brain.model
                episode = {
                    "task": task,
                    "repeat": repeat,
                    "success": bool(result["success"]),
                    "steps": len(report.trace),
                    "seconds": seconds,
                    "exit": report.outcome.status,
                }
                if report.brain.error:
                    episode["error"] = report.brain.error[:200]
                episodes.append(episode)
                print(json.dumps(episode), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        if not args.keep_runs:
            shutil.rmtree(out_root, ignore_errors=True)
    rate = sum(e["success"] for e in episodes) / len(episodes)
    record = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "lyra_commit": _commit(),
        "chrome_version": _chrome_version(),
        "brain": args.brain,
        "model": args.model or model_seen or "default",
        "model_reported": model_seen,
        "repeats": args.repeats,
        "step_budget": args.steps,
        "tasks": tasks,
        "suite": args.suite,
        "goals": {t: ALL_GOALS[t] for t in tasks},
        "episodes": episodes,
        "success_rate": round(rate, 4),
        "per_task": {
            t: sum(e["success"] for e in episodes if e["task"] == t)
            / max(1, sum(1 for e in episodes if e["task"] == t))
            for t in tasks
        },
    }
    text = json.dumps(record, indent=2) + "\n"
    home = str(Path.home())
    text = text.replace(home, "~")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: success_rate {rate:.3f} over {len(episodes)} episodes")
    else:
        print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--brain", default="scripted")
    parser.add_argument("--model", default=None)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--steps", type=int, default=25, help="step budget per episode")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-budget-usd", type=float, default=None)
    parser.add_argument("--wall-s", type=int, default=480)
    parser.add_argument("--suite", choices=sorted(SUITES), default="v1")
    parser.add_argument("--tasks", default="", help="comma-separated subset")
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-runs", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    args = parser.parse_args()
    if args.brain == "scripted":
        return asyncio.run(scripted(headless=not args.headful_only, suite=args.suite))
    os.environ.setdefault("LYRA_UAT_QUIET", "1")
    return asyncio.run(measure(args))


if __name__ == "__main__":
    raise SystemExit(main())
