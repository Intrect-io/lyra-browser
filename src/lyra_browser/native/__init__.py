"""lyra's native driver: Chrome over raw DevTools, with no Playwright, Node or chromedriver.

``session.load_driver("native")`` returns ``async_native``, shaped like
``playwright.async_api.async_playwright`` so ``BrowserSession`` drives it unchanged:
``await async_native().start()`` gives an object whose ``chromium`` launches a persistent
context and whose ``stop()`` releases it.

Only the slice of Playwright's API that lyra uses exists here. It runs with the ``cdp``
guard backend only (``Config`` enforces that), sends no ``Runtime.enable``, keeps
Chrome's sandbox on, and evaluates in an isolated world unless a script asks for the
main one. See ``launcher.py`` for the switches and ``context.py`` for how tabs are found.
"""

from __future__ import annotations

from .context import NativeContext
from .errors import Error, TargetClosedError, TimeoutError  # noqa: A004 — Playwright's names


class _Chromium:
    async def launch_persistent_context(self, user_data_dir: str, **options) -> NativeContext:
        return await NativeContext.launch({"user_data_dir": user_data_dir, **options})


class NativeDriver:
    """What ``async_playwright().start()`` returns, for the native driver."""

    def __init__(self) -> None:
        self.chromium = _Chromium()

    async def stop(self) -> None:
        """Each context owns its browser and closes it itself; nothing is shared."""


class _Starter:
    async def start(self) -> NativeDriver:
        return NativeDriver()


def async_native() -> _Starter:
    return _Starter()


__all__ = [
    "Error",
    "NativeContext",
    "NativeDriver",
    "TargetClosedError",
    "TimeoutError",
    "async_native",
]
