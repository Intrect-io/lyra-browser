#!/usr/bin/env python3
"""Does bot defence let the product's browser in? One guard backend against another.

DataDome (etsy.com) refuses a browser launched with ``context.route`` (its cache-disable
shows on the document request) and, on a host with no GPU, one launched with Playwright's
``--enable-unsafe-swiftshader``. The ``cdp`` guard backend has neither. This measures that
with the product itself, not a harness: every visit is a fresh ``build_tools`` (new profile,
``BrowserSession``, ``ServerContext``, ``NavigationGuard`` in enforce mode), then the
agent's own ``navigate``, then the same page-state heuristic as ``probe_document_intercept``
(the earlier survey), so numbers stay comparable.

Cells:
  cdp            ``guard_backend="cdp"``: no route, swiftshader dropped (the new backend)
  route-master   ``guard_backend="route"`` with every launch default kept: what master ships

Visits are shuffled within each round and ``--gap`` seconds apart. A site that has been
visited from one IP many times in an afternoon stops being a fair test (the probe's control
fell from 11/11 to 2/5 after about 130 fresh-profile visits), so keep ``--reps`` small and
run it once.

    xvfb-run -a -s "-screen 0 1920x1080x24" .venv/bin/python scripts/measure_compat.py \\
        --headful --sites etsy,hyatt --reps 5 --gap 12
    <venv with patchright>/bin/python scripts/measure_compat.py --driver patchright --headful

Rows go to ``testing/compat_out/guard-<time>.jsonl`` (gitignored scratch).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from probe_document_intercept import CLASSIFY_JS, verdict  # noqa: E402
from verify_browser_e2e import build_tools  # noqa: E402

SITES = {
    "etsy": "https://www.etsy.com/signin",
    "hyatt": "https://www.hyatt.com/",
    "tripadvisor": "https://www.tripadvisor.com/",
}
CELLS = ("cdp", "route-master")
OUT = REPO / "testing" / "compat_out"


async def settle(page, seconds: float = 15.0) -> dict:
    """Poll until a challenge resolves or time runs out; return the last page state."""
    deadline = time.monotonic() + seconds
    state: dict = {}
    await asyncio.sleep(4)
    while True:
        try:
            state = await page.evaluate(CLASSIFY_JS) or {}
        except Exception as exc:  # noqa: BLE001 — mid-navigation
            state = {"error": str(exc)[:120]}
        if time.monotonic() >= deadline or verdict(state) == "ok":
            return state
        await asyncio.sleep(2.5)


async def visit(cell: str, site: str, *, headless: bool, driver: str) -> dict:
    from lyra_browser import session as session_mod

    backend = "cdp" if cell == "cdp" else "route"
    os.environ["LYRA_BROWSER_GUARD"] = backend
    original = session_mod.launch_kwargs
    if cell == "route-master":
        # Master launches with every Playwright default; this branch drops one for cdp only,
        # but a later change may drop it for both, and this cell must stay what master is.
        def master(*args: object, **kwargs: object) -> dict:
            kw = original(*args, **kwargs)
            kw.pop("ignore_default_args", None)
            return kw

        session_mod.launch_kwargs = master
    started = time.monotonic()
    row: dict = {"cell": cell, "site": site, "mode": "headless" if headless else "headful"}
    tmp = tempfile.TemporaryDirectory(prefix="compat-", ignore_cleanup_errors=True)
    ctx = None
    try:
        ctx, tools = await build_tools(Path(tmp.name), headless=headless)
        ctx.config.driver = driver
        opened = await tools["open_browser"]()
        row["driver"] = opened.get("driver")
        reply = await tools["navigate"](
            url=SITES[site], reason="compat measurement", confirm=True, wait_until="commit"
        )
        row["navigate"] = reply.get("status")
        page = await ctx.session.page()
        state = await settle(page)
        row.update(
            verdict=verdict(state),
            status=state.get("status"),
            title=state.get("title"),
            final=state.get("url"),
            flags=[k for k, v in state.get("m", {}).items() if v],
            error=state.get("error"),
        )
    except Exception as exc:  # noqa: BLE001 — a failed visit is a result, not a crash
        row.update(verdict="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        session_mod.launch_kwargs = original
        if ctx is not None:
            try:
                await ctx.session.stop()
            except Exception:  # noqa: BLE001 — already gone
                pass
        tmp.cleanup()
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


async def main_async(args: argparse.Namespace) -> None:
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"guard-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rows: list[dict] = []
    first = True
    for rep in range(args.reps):
        order = [(cell, site) for cell in cells for site in sites]
        random.shuffle(order)
        for cell, site in order:
            if not first:
                await asyncio.sleep(args.gap)
            first = False
            row = await visit(cell, site, headless=not args.headful, driver=args.driver)
            row["rep"] = rep
            rows.append(row)
            with path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(
                f"  rep {rep} {cell:13} {site:12} {row['verdict']:22} "
                f"status={row.get('status')} title={str(row.get('title'))[:30]!r} "
                f"({row['seconds']}s)",
                flush=True,
            )
    table: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for row in rows:
        table[(row["site"], row["cell"])].append(row["verdict"] == "ok")
    print(f"\n{'site':12} {'cell':14} ok/N   (mode={'headful' if args.headful else 'headless'})")
    for (site, cell), oks in sorted(table.items()):
        print(f"{site:12} {cell:14} {sum(oks)}/{len(oks)}")
    print(f"\nrows: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sites", default="etsy,hyatt", help=f"comma list of {sorted(SITES)}")
    parser.add_argument("--cells", default=",".join(CELLS), help=f"comma list of {CELLS}")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--gap", type=float, default=12.0, help="seconds between visits")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="add or override a site, e.g. to rehearse the harness against a loopback page",
    )
    args = parser.parse_args()
    for item in args.url:
        name, _, url = item.partition("=")
        SITES[name] = url
    unknown = [s for s in args.sites.split(",") if s.strip() not in SITES]
    if unknown or any(c.strip() not in CELLS for c in args.cells.split(",")):
        parser.error(f"unknown site or cell: {unknown or args.cells}")
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
