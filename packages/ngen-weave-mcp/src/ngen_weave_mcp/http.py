"""Streamable-HTTP MCP transport entry point: workflow tools served at /mcp."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from ngen_weave.manifest import MANIFEST_NAME
from ngen_weave.wiring import build_service, merged_registry
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from ngen_weave_mcp.fake_provider import fake_provider_from_env
from ngen_weave_mcp.stdio import EPILOG, _package_version
from ngen_weave_mcp.tools import DEFAULT_TOOL_TIMEOUT_S, register_workflow_tools

MCP_HTTP_HOST = "127.0.0.1"  # local-only by design; TLS/auth are out of scope
MCP_HTTP_PORT = 8000


def create_http_app(
    root: Path,
    *,
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    models_file: Path | None = None,
    db_path: Path | None = None,
) -> ASGIApp:
    """Build a Starlette app exposing the registered workflows at /mcp.

    Assembles the stack exactly like the stdio console script — manifest
    discovery, LocalRunService wiring, and register_workflow_tools unchanged
    — anchoring stores and the manifest at `root`, and swaps only the
    transport for the mcp SDK's streamable-http session manager in stateless
    JSON mode.

    Raises:
        ConfigError: A workflow lacks a description or tool names collide.
    """
    os.chdir(root)  # manifest discovery and stores are cwd-anchored
    provider = fake_provider_from_env()
    service = build_service(provider=provider, models_file=models_file, db_path=db_path)
    server = Server("ngen-weave-mcp-http", version=_package_version())

    register_workflow_tools(server, merged_registry(), service, tool_timeout_s=tool_timeout_s)

    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        """Run the session manager for the application's lifetime."""
        async with session_manager.run():
            yield

    return Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", app=_SessionEndpoint(session_manager))],
    )


class _SessionEndpoint:
    """Raw ASGI callable handing every request to the session manager."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def main(argv: list[str] | None = None) -> None:
    """Parse flags, assemble the local stack, serve streamable-http via uvicorn."""
    parser = argparse.ArgumentParser(
        prog="ngen-weave-mcp-http",
        description=(
            "Expose this project's ngen-weave workflows as MCP tools over "
            f"streamable HTTP at /mcp. Workflows come from entry points plus "
            f"{MANIFEST_NAME} at --root."
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

    app = create_http_app(
        args.root,
        tool_timeout_s=args.timeout,
        models_file=args.models,
        db_path=args.db,
    )
    uvicorn.run(app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT)
