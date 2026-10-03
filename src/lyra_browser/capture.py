"""Captures: screenshots and element images written to disk for the model.

A capture is handed over as a *file*, not as base64 inside the tool result.
Measured on VEGA: the client concatenates the text blocks of a tool result and
shows the model that string, so a base64 PNG arrives as tens of thousands of
tokens of prose and no picture. VEGA does have an image path — a local file
under its uploads root is attached to the next model turn as an image block —
and the in-app browser runs on the same machine, so a file on disk is the one
handoff both surfaces can use without a protocol in between.

Files are named so they sort by time, written atomically so a reader never
sees a partial PNG, and pruned oldest-first so a long session cannot fill the
disk one screenshot per turn.
"""

from __future__ import annotations

import asyncio
import os
import struct
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import Config

MIME_PNG = "image/png"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Chrome answers Page.captureScreenshot with this while the renderer has not
# presented a frame yet. Measured with Chrome 152 headless on a fresh context:
# the first capture after a navigation to example.com fails, and the same call
# ~1s later succeeds. It is a transient, not a broken page, so it is retried.
_NO_FRAME_YET = "Unable to capture screenshot"
_RETRY_DELAY_S = 0.15
_RETRY_BUDGET_S = 3.0


async def take(shoot: Callable[[], Awaitable[bytes]]) -> bytes:
    """Run ``shoot`` and retry the no-frame-yet transient within a small budget.

    Any other failure — element detached, page closed — is raised as it is;
    only the one message Chrome uses for "nothing painted yet" is retried, and
    even that gives up after ``_RETRY_BUDGET_S`` so a page that never paints
    still errors instead of hanging the tool.
    """
    deadline = asyncio.get_running_loop().time() + _RETRY_BUDGET_S
    while True:
        try:
            return await shoot()
        except Exception as exc:  # noqa: BLE001 — filtered on the message below
            if _NO_FRAME_YET not in str(exc) or asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(_RETRY_DELAY_S)


def png_size(png: bytes) -> tuple[int, int] | None:
    """Width and height from the IHDR chunk, or None when this is not a PNG.

    The IHDR chunk is mandatory and always first, so the dimensions sit at a
    fixed offset: signature (8) + length (4) + type (4) = 16.
    """
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def save_capture(config: Config, png: bytes, kind: str) -> Path:
    """Write ``png`` under the capture dir and return its path.

    ``kind`` names what was captured (``page``, ``element``) and leads the file
    name so a directory listing reads as a log. The write goes through a temp
    file and ``os.replace`` so a concurrent reader — VEGA attaching the image
    while we are still writing — cannot observe a truncated file.
    """
    directory = config.capture_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}.png"
    tmp = final.with_suffix(".png.part")
    with tmp.open("wb") as handle:
        handle.write(png)
    os.replace(tmp, final)
    _prune(directory, keep=config.capture_keep)
    return final


def _prune(directory: Path, keep: int) -> None:
    """Delete the oldest captures beyond ``keep``. ``keep <= 0`` keeps everything.

    Ordered by name, not mtime: names carry a UTC timestamp, and mtime is what
    a copy or a backup restore rewrites.
    """
    if keep <= 0:
        return
    files = sorted(p for p in directory.glob("*.png") if p.is_file())
    for stale in files[: max(0, len(files) - keep)]:
        try:
            stale.unlink()
        except OSError:
            # Someone else's problem — a capture we cannot delete is not a
            # reason to fail the one we just took.
            pass


def envelope(path: Path, png: bytes, *, url: str, inline: bool) -> dict:
    """The tool result for a capture.

    The path is the handoff. ``inline`` adds the base64 for a client with no
    access to this filesystem — it is opt-in because on the client that does
    (VEGA) the string would be shown to the model as text.
    """
    result: dict = {
        "status": "ok",
        "url": url,
        "image_path": str(path),
        "mime_type": MIME_PNG,
        "bytes": len(png),
    }
    size = png_size(png)
    if size is not None:
        result["width"], result["height"] = size
    if inline:
        import base64

        result["base64"] = base64.b64encode(png).decode("ascii")
    return result
