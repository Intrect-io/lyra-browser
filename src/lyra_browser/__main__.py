"""CLI entry point: ``lyra-browser`` launches the MCP server over stdio.

VEGA (mcp.json) and Hermes (config.yaml ``mcp_servers``) spawn this as a stdio
MCP server. Pass ``--http`` to instead serve over Streamable HTTP for
remote/dev use.
"""

from __future__ import annotations

import argparse

from .config import CLIENTS, Config
from .server import build_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lyra-browser", description=__doc__)
    parser.add_argument("--http", action="store_true", help="Serve over HTTP instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (with --http).")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (with --http).")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Run without a visible window. Nobody can watch or take over, so the "
            "collaboration tools refuse with 'unattended'. Omit to follow "
            "LYRA_BROWSER_HEADLESS, else the client's default (headless for "
            "hermes, and on Linux with no display)."
        ),
    )
    parser.add_argument(
        "--client",
        choices=sorted(CLIENTS),
        default=None,
        help=(
            "The agent harness spawning this server. Picks where the profile and "
            "captures live and whether a window is expected. Omit to follow "
            "LYRA_BROWSER_CLIENT, else detect (VEGA_DATA_DIR -> vega, "
            "HERMES_HOME -> hermes)."
        ),
    )
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    """Build the runtime config: environment first, explicit flags on top.

    A flag beats the environment so a developer can force either mode without
    editing the VEGA install's mcp.json env block.
    """
    cfg = Config.from_env(client=args.client)
    if args.headless is not None:
        cfg.headless = args.headless
    return cfg


def main() -> None:
    args = build_parser().parse_args()
    mcp = build_server(config_from_args(args))
    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
