"""``Dialog``: an alert, confirm, prompt or beforeunload raised by a page."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from .errors import ProtocolError

if TYPE_CHECKING:
    from .page import NativePage


class Dialog:
    """What ``DialogHandler`` reads and answers (Playwright's ``Dialog``)."""

    def __init__(self, page: NativePage, params: dict) -> None:
        self.page = page
        self.type: str = params.get("type", "alert")
        self.message: str = params.get("message", "")
        self.default_value: str = params.get("defaultPrompt", "") or ""
        self._handled = False

    async def accept(self, prompt_text: str | None = None) -> None:
        params: dict = {"accept": True}
        if prompt_text is not None:
            params["promptText"] = prompt_text
        await self._answer(params)

    async def dismiss(self) -> None:
        await self._answer({"accept": False})

    async def _answer(self, params: dict) -> None:
        if self._handled:
            return
        self._handled = True
        # A dialog the page closed itself, or one answered meanwhile, is "No dialog
        # is showing": the aim (no dialog in the way) already holds.
        with contextlib.suppress(ProtocolError):
            await self.page._send("Page.handleJavaScriptDialog", params)
