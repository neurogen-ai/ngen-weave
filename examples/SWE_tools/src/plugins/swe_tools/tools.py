"""Filesystem ToolSpecs for the Scout and Dev agents.

Tool roots are anchored at the SWE_TOOLS_REPO_ROOT environment variable
(default: the working directory). These are example-grade tools: paths are
resolved against the root and escapes are rejected, but otherwise this is the
real filesystem — point SWE_TOOLS_REPO_ROOT at a scratch checkout, not at
anything precious.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from ngen_weave.agent.tools import ToolSpec
from ngen_weave.errors import DataError

ROOT_ENV = "SWE_TOOLS_REPO_ROOT"

_READ_LIMIT = 8000


def _root() -> Path:
    return Path(os.environ.get(ROOT_ENV, ".")).resolve()


def _resolve(path: str) -> Path:
    root = _root()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise DataError(f"tool path {path!r} escapes the repo root {root}")
    return resolved


def _tool(name: str, description: str, schema: dict, fn: Callable[[dict], Awaitable[dict]]):
    return ToolSpec(name=name, description=description, parameters_schema=schema, fn=fn)


async def _list_dir(args: dict) -> dict:
    target = _resolve(args["path"])
    if not target.is_dir():
        return {"error": f"not a directory: {target}"}
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return {"entries": entries[:200]}


async def _read_file(args: dict) -> dict:
    target = _resolve(args["path"])
    if not target.is_file():
        return {"error": f"no such file: {target}"}
    text = target.read_text(errors="replace")
    limit = int(args.get("max_chars", _READ_LIMIT))
    return {"path": str(target), "content": text[:limit], "truncated": len(text) > limit}


async def _write_file(args: dict) -> dict:
    target = _resolve(args["path"])
    content = args["content"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"path": str(target), "bytes": len(content.encode())}


LIST_DIR = _tool(
    "list_dir",
    'List directory entries relative to the repo root. Returns {entries: [...]} or {error: "..."}.',
    {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    _list_dir,
)

READ_FILE = _tool(
    "read_file",
    'Read a file relative to the repo root. Returns {path, content, truncated} or {error: "..."}.',
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    },
    _read_file,
)

WRITE_FILE = _tool(
    "write_file",
    "Write (or overwrite) a file relative to the repo root. Returns {path, bytes}.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    _write_file,
)
