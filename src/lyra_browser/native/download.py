"""``Download``: a file a page was offered, kept by Chrome under a temporary name.

The context sets ``Browser.setDownloadBehavior(allowAndName)`` into a private directory,
so Chrome saves each file as its GUID there and reports progress with
``Browser.downloadProgress``. Nothing reaches ``download_dir`` until lyra's own
``downloads`` module decides to ``save_as`` it; the directory is removed when the
browser closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import Error, ProtocolError

if TYPE_CHECKING:
    from .page import NativePage


class Download:
    """What lyra's ``downloads`` module reads and does (Playwright's ``Download``)."""

    def __init__(self, page: NativePage, params: dict, directory: Path) -> None:
        self.page = page
        self.url: str = params.get("url", "")
        self.suggested_filename: str = params.get("suggestedFilename", "") or ""
        self.guid: str = params["guid"]
        self._file = directory / self.guid
        self._done: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    # -- fed by the context -------------------------------------------------------

    def _progress(self, state: str) -> None:
        if self._done.done():
            return
        if state == "completed":
            self._done.set_result(None)
        elif state == "canceled":
            self._done.set_result("canceled")

    def _browser_gone(self) -> None:
        if not self._done.done():
            self._done.set_result("browser closed")

    # -- the Playwright surface ---------------------------------------------------

    async def failure(self) -> str | None:
        return await asyncio.shield(self._done)

    async def path(self) -> Path:
        failure = await asyncio.shield(self._done)
        if failure is not None:
            raise Error(f"Download.path: {failure}")
        return self._file

    async def save_as(self, path: str | Path) -> None:
        failure = await asyncio.shield(self._done)
        if failure is not None:
            raise Error(f"Download.save_as: {failure}")
        target = Path(path)
        await asyncio.to_thread(shutil.copyfile, self._file, target)

    async def cancel(self) -> None:
        if self._done.done():
            return
        with contextlib.suppress(ProtocolError):
            await self.page.context._browser_send("Browser.cancelDownload", {"guid": self.guid})

    async def delete(self) -> None:
        if not self._done.done():
            await self.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._done), 5)
        with contextlib.suppress(OSError):
            self._file.unlink()
