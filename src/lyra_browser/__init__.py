"""lyra-browser — a headful, collaborative browser MCP server for VEGA."""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config
from .server import build_server

__all__ = ["Config", "build_server", "__version__"]
