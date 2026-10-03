#!/usr/bin/env python3
"""Probe: does answering documents from Node change what bot defences do?

``probe_redirect_hops.py`` measures what ``route.fetch(max_redirects=0)`` +
``route.fulfill(response=...)`` ("relay") changes on the wire against a loopback server.
This one asks the question that decides whether the idea could ship: do DataDome
(etsy.com) and Kasada (hyatt.com) still let the browser in when every judged document
comes from Node instead of Chrome?

It reuses ``probe_document_intercept.py`` (its cells, its fresh profile per visit, its
verdict heuristics, its ``measure`` loop) and adds two cells whose guard relays:

  v1-route          playwright driver, like ``b-route`` (today's route + guard) but relayed
  e-patch-v1-route  patchright driver, like ``e-patch-route`` but relayed

Cell modifiers still work: ``v1-route+rm:swift`` drops ``--enable-unsafe-swiftshader``
(on a GPU-less host that switch is the other known cause of the DataDome refusal, so a
relay cell is only informative about the document request with it dropped).

Run, headful, N=3, 12 s apart (the host needs ``xvfb-run``; patchright cells need a venv
that has it, which also has playwright)::

    xvfb-run -a -s "-screen 0 1920x1080x24" $PYPR scripts/probe_redirect_sites.py \\
      --cells "a-plain,b-route,b-route+rm:swift,v1-route,v1-route+rm:swift" --sites etsy
    xvfb-run -a -s "-screen 0 1920x1080x24" $PYPR scripts/probe_redirect_sites.py \\
      --cells "a-plain,e-patch-route,e-patch-v1-route" --sites hyatt

Nothing is asserted; the table at the end is ok/N per cell. Be polite: one visit at a
time, never more than a handful per site per hour from one address.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_document_intercept as probe  # noqa: E402

import lyra_browser.context as vctx  # noqa: E402
from lyra_browser.enforcement import NavigationGuard, classify  # noqa: E402

RELAYED = ("v1-route", "e-patch-v1-route")


class RelayRoute:
    """Stands in for the route: ``continue_()`` becomes fetch-without-redirects + fulfill.

    Only what the guard would judge is relayed; every other request is continued as
    usual. A failed fetch is continued natively (this is a measurement of the page, not
    a proposal for how a shipped relay would fail).
    """

    def __init__(self, route: Any, request: Any) -> None:
        self._route = route
        self._request = request

    async def continue_(self, **kwargs: Any) -> None:
        if classify(self._request) is None:
            return await self._route.continue_(**kwargs)
        try:
            response = await self._route.fetch(max_redirects=0)
        except Exception as exc:  # noqa: BLE001 - measurement only
            print(f"    relay: fetch failed {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            return await self._route.continue_()
        await self._route.fulfill(response=response)

    async def fulfill(self, **kwargs: Any) -> None:
        return await self._route.fulfill(**kwargs)

    async def abort(self, *args: Any, **kwargs: Any) -> None:
        return await self._route.abort(*args, **kwargs)


class RelayGuard(NavigationGuard):
    """The real guard, answering every request it lets through via ``RelayRoute``."""

    async def __call__(self, route: Any, request: Any) -> None:
        await super().__call__(RelayRoute(route, request), request)


probe.CELLS["v1-route"] = probe.Cell(
    "v1-route", route=True, note="route + guard, documents relayed via route.fetch"
)
probe.CELLS["e-patch-v1-route"] = probe.Cell(
    "e-patch-v1-route", route=True, driver="patchright", note="patchright + v1-route"
)

_visit = probe.visit


async def _visit_with_guard(cell: Any, site: str, headless: bool, user_agent: str | None) -> dict:
    """The guard is picked per visit: ``ServerContext`` builds it from this module's name."""
    relayed = cell.name.split("+")[0] in RELAYED
    vctx.NavigationGuard = RelayGuard if relayed else NavigationGuard
    return await _visit(cell, site, headless, user_agent)


probe.visit = _visit_with_guard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=["headless", "headful"], default="headful")
    parser.add_argument("--cells", default="a-plain,b-route,b-route+rm:swift,v1-route")
    parser.add_argument("--sites", default="etsy")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--gap", type=float, default=12.0, help="seconds between visits")
    parser.add_argument("--timeout", type=float, default=150.0, help="seconds per visit")
    parser.add_argument("--seed", type=int, default=4649)
    parser.add_argument("--out", default="", help="JSONL results (default: the probe's own dir)")
    args = parser.parse_args()
    asyncio.run(probe.cmd_measure(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
