"""stdio MCP transport entry point: local service behind workflow tools."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

import mcp.server.stdio as stdio_server_module
from mcp.server import Server
from ngen_weave.manifest import MANIFEST_NAME
from ngen_weave.wiring import build_service, merged_registry

from ngen_weave_mcp.fake_provider import fake_provider_from_env
from ngen_weave_mcp.tools import DEFAULT_TOOL_TIMEOUT_S, register_workflow_tools

EPILOG = """\
Test-only environment hooks (not for production):
  NGEN_WEAVE_FAKE_PROVIDER=1     replace real model calls with canned replies
  NGEN_WEAVE_FAKE_REPLIES=PATH   JSON array of reply strings to replay in order
"""


def main(argv: list[str] | None = None) -> None:
    """Parse flags, assemble the local stack, register tools, serve on stdio."""
    parser = argparse.ArgumentParser(
        prog="ngen-weave-mcp",
        description=(
            "Expose this project's ngen-weave workflows as MCP tools over stdio. "
            f"Workflows come from entry points plus {MANIFEST_NAME} at --root."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help=f"project root holding {MANIFEST_NAME} and .ngen-weave/ (default: cwd)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="YAML/JSON run config loaded before serving (run.budget limits apply)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TOOL_TIMEOUT_S,
        help="seconds a tool call polls before returning the run id, which stays resumable",
    )
    parser.add_argument(
        "--models",
        type=Path,
        help="override path to models.json (default: <root>/models.json)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="override LangGraph checkpoint database path",
    )
    args = parser.parse_args(argv)

    os.chdir(args.root)  # manifest discovery and stores are cwd-anchored
    provider = fake_provider_from_env()
    service = build_service(
        config_path=args.config, provider=provider, models_file=args.models, db_path=args.db
    )
    server = Server("ngen-weave-mcp", version=_package_version())

    register_workflow_tools(server, merged_registry(), service, tool_timeout_s=args.timeout)

    async def serve() -> None:
        with contextlib.suppress(KeyboardInterrupt):
            async with stdio_server_module.stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(serve())


def _package_version() -> str:
    """Best-effort package version for the initialize handshake."""
    try:
        return package_version("ngen-weave-mcp")
    except PackageNotFoundError:
        return "0.0.0"
