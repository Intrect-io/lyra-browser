"""Tool registration modules.

Each module exposes ``register(mcp, ctx)`` which attaches its tools to the
FastMCP server. ``register_all`` wires them in a fixed, reviewable order.
"""

from __future__ import annotations

from fastmcp import FastMCP

from ..context import ServerContext
from . import (
    collaboration,
    dialogs,
    downloads,
    forms,
    interaction,
    navigation,
    reading,
    tabs,
    waiting,
)


def register_all(mcp: FastMCP, ctx: ServerContext) -> None:
    navigation.register(mcp, ctx)
    interaction.register(mcp, ctx)
    dialogs.register(mcp, ctx)
    forms.register(mcp, ctx)
    reading.register(mcp, ctx)
    tabs.register(mcp, ctx)
    waiting.register(mcp, ctx)
    downloads.register(mcp, ctx)
    collaboration.register(mcp, ctx)
