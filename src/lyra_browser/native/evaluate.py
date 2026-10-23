"""Running lyra's page scripts without ``Runtime.enable``, the way Playwright's ``evaluate`` does.

Two decisions Playwright makes for every ``evaluate`` call, made the same way here:

- **Function or expression.** ``"(el) => el.textContent"`` is called with the argument;
  ``"[window.innerWidth, window.innerHeight]"`` is evaluated as it stands. Playwright
  compiles the text to find out; this reads its head (``is_function``), which is enough
  for every script lyra sends, all written as arrows or ``function``.
- **Which world.** Scripts run in an isolated world (``Page.createIsolatedWorld``) by
  default, as patchright runs them: the page cannot see them, cannot reach their
  variables, and cannot have replaced ``document.querySelector`` under them. A script
  that has to read the page's own globals (``window.CKEDITOR``) asks for ``world="main"``.

No ``Runtime.enable`` is ever sent (it is the call Kasada refuses on). An isolated world
does not survive a navigation, so ``WorldCache`` forgets a frame's world when the frame
navigates and creates a fresh one on the next call.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import Error, ProtocolError

UTILITY_WORLD = "__lyra_utility_world__"
MAIN = "main"
ISOLATED = "isolated"

_FUNCTION_HEAD = re.compile(
    r"""^\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)""",
    re.DOTALL,
)
# What Chrome says when a context id no longer exists: the frame navigated between
# the cache lookup and the call.
_STALE_CONTEXT = ("cannot find context with specified id", "execution context was destroyed")
CONTEXT_DESTROYED = "Execution context was destroyed, most likely because of a navigation"

NO_ARG = object()


def is_function(source: str) -> bool:
    """Whether ``source`` is a function to call rather than an expression to evaluate."""
    return bool(_FUNCTION_HEAD.match(source))


def call_argument(value: Any) -> dict:
    """A ``Runtime.CallArgument`` for a JSON value (all lyra ever passes)."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"unserializableValue": repr(value).replace("inf", "Infinity").replace("nan", "NaN")}
    return {"value": value}


_UNSERIALIZABLE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf, "-0": -0.0}


def decode_result(api: str, reply: dict) -> Any:
    """The JSON value of a ``Runtime.evaluate``/``callFunctionOn`` reply, or raise.

    A script that threw raises ``Error`` worded ``Api: <the JS error>``, as Playwright's
    does; ``undefined`` comes back as None.
    """
    details = reply.get("exceptionDetails")
    if details:
        exception = details.get("exception") or {}
        text = exception.get("description") or details.get("text") or "script failed"
        raise Error(f"{api}: {text}")
    result = reply.get("result") or {}
    if "unserializableValue" in result:
        raw = result["unserializableValue"]
        if raw.endswith("n"):  # BigInt
            return int(raw[:-1])
        return _UNSERIALIZABLE.get(raw)
    return result.get("value")


def is_stale_context(exc: BaseException) -> bool:
    return isinstance(exc, ProtocolError) and any(
        marker in exc.reason.lower() for marker in _STALE_CONTEXT
    )


class WorldCache:
    """The isolated world of each frame of one page, created on first use."""

    def __init__(self) -> None:
        self._contexts: dict[str, int] = {}

    def get(self, frame_id: str) -> int | None:
        return self._contexts.get(frame_id)

    def put(self, frame_id: str, context_id: int) -> None:
        self._contexts[frame_id] = context_id

    def forget(self, frame_id: str) -> None:
        self._contexts.pop(frame_id, None)

    def clear(self) -> None:
        self._contexts.clear()
