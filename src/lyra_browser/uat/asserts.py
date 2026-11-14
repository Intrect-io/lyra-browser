"""Deterministic checks on the page a run ended on.

The persona's ``finish`` says what it believes happened; an assertion says what the page
shows. Assertions are part of the spec, evaluated by the server — not the model — once:
when ``finish`` is called, on the page the persona finished on, or, for a run that never
calls it, when the runner closes the browser. The same spec on the same page gives the
same answer, which is what makes a run comparable with its replay and with a benchmark's
expected result.
"""

from __future__ import annotations

import re
from typing import Any

from .spec import Assertion

# One call for every text/count assertion: the page's visible text (all of it, or what a
# selector matches) and how many elements a selector matches. ``innerText`` is what a
# person sees; hidden text does not count.
_PAGE_STATE_JS = """
(selector) => {
  if (!selector) {
    return {text: document.body ? document.body.innerText : "", count: null, error: null};
  }
  let nodes;
  try {
    nodes = Array.from(document.querySelectorAll(selector));
  } catch (e) {
    return {text: "", count: 0, error: String(e && e.message || e)};
  }
  return {text: nodes.map(n => n.innerText || n.textContent || "").join("\\n"),
          count: nodes.length, error: null};
}
"""


def _result(a: Assertion, passed: bool, observed: Any, error: str | None = None) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "status": "error" if error else ("passed" if passed else "failed"),
        "expected": a.describe(),
        "observed": observed,
        **({"error": error} if error else {}),
    }


async def _state(page: Any, selector: str | None) -> dict[str, Any]:
    state = await page.evaluate(_PAGE_STATE_JS, selector)
    if not isinstance(state, dict):
        raise RuntimeError(f"the page answered {state!r}")
    return state


async def evaluate(page: Any, assertions: list[Assertion]) -> list[dict[str, Any]]:
    """Every assertion against ``page``; one that cannot be evaluated is an ``error`` row,
    never an exception (the report must still be written)."""
    results: list[dict[str, Any]] = []
    for a in assertions:
        try:
            if a.kind == "url_matches":
                url = str(page.url)
                results.append(_result(a, re.search(a.value, url) is not None, url))
            elif a.kind == "title_contains":
                title = str(await page.title())
                results.append(_result(a, a.value in title, title))
            elif a.kind in ("text_present", "text_absent"):
                state = await _state(page, a.selector)
                if state.get("error"):
                    results.append(_result(a, False, None, f"selector: {state['error']}"))
                    continue
                found = a.value in str(state.get("text") or "")
                results.append(_result(a, found == (a.kind == "text_present"), found))
            elif a.kind == "selector_count":
                state = await _state(page, a.selector)
                if state.get("error"):
                    results.append(_result(a, False, None, f"selector: {state['error']}"))
                    continue
                count = int(state.get("count") or 0)
                ok = (a.min is None or count >= a.min) and (a.max is None or count <= a.max)
                results.append(_result(a, ok, count))
        except Exception as exc:  # noqa: BLE001 - a dead page is a result, not a crash
            results.append(_result(a, False, None, f"{type(exc).__name__}: {exc}"[:300]))
    return results


async def evaluate_once(ctx: Any, recorder: Any) -> None:
    """Evaluate the run's assertions on the current page and record them, once per run.

    Never launches a browser: a run whose browser is gone records every assertion as an
    ``error`` saying so."""
    if not recorder.assertions or recorder.asserts_done:
        return
    recorder.asserts_done = True
    try:
        page = await ctx.session.page(revive=False)
    except Exception as exc:  # noqa: BLE001 - no page is a result
        reason = f"no page to check: {type(exc).__name__}: {exc}"[:300]
        rows = [_result(a, False, None, reason) for a in recorder.assertions]
    else:
        rows = await evaluate(page, recorder.assertions)
    recorder.add_asserts(rows)
