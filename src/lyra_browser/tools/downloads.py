"""Downloads: what this session has saved.

Saving is declared on the tool that starts it (``click``, ``press_key`` or
``navigate`` with ``download=true``) and done by ``downloads.py``. This is the read
side, for a model that needs the path of a file it saved earlier.
"""

from __future__ import annotations

from fastmcp import FastMCP

from ..context import ServerContext, claim_session


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def list_downloads() -> dict:
        """List the files this session has downloaded, oldest first.

        Each entry is ``filename``, ``path``, ``bytes`` and ``url_origin`` (the site
        the file came from), the same as the ``download`` a call returned when it
        saved one. A download that was blocked or failed left no file and is not
        listed. This only reads: it asks nobody, works during a takeover and does
        not open the browser.
        """
        # Reading is not driving: check who holds the browser without becoming the
        # holder. No page is needed, so none is acquired (and no browser launched).
        conflict = await claim_session(ctx, claim=False)
        if conflict is not None:
            return conflict
        files = ctx.downloads.saved()
        return {
            "status": "ok",
            "download_dir": str(ctx.config.download_dir.expanduser().absolute()),
            "count": len(files),
            "downloads": files,
        }
