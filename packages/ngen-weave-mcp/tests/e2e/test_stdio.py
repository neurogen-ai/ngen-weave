"""Stdio e2e: real MCP-over-stdio against the canonical code-review example."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXAMPLE_DIR = Path(__file__).resolve().parents[4] / "examples" / "code_review"
EXAMPLE_SRC = EXAMPLE_DIR / "src"
TOOL_NAME = "code_review-workflows-CodeReview"


@pytest.fixture()
def example_project(tmp_path):
    """Copy the example's manifest next to its importable src under tmp_path."""
    shutil.copy(EXAMPLE_DIR / "ngen-weave.json", tmp_path / "ngen-weave.json")
    return tmp_path


@pytest.fixture()
def fake_replies(tmp_path):
    """Canned replies mirroring tests/e2e/test_code_review.py's fixture."""
    diff = json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]
    replies = [
        json.dumps({"review": "Looks good overall.", "diff": diff}),
        json.dumps({"reviewed_diff": diff, "verdict": "approve"}),
    ]
    replies_file = tmp_path / "fake-replies.json"
    replies_file.write_text(json.dumps(replies))
    return replies_file


def console_script_command() -> list[str]:
    """Locate the ngen-weave-mcp console script beside the venv interpreter."""
    candidate = Path(sys.executable).with_name("ngen-weave-mcp")
    if candidate.exists():
        return [str(candidate)]
    which = shutil.which("ngen-weave-mcp")
    if which:
        return [which]
    # Fall back to direct entry-point invocation for editable checkouts.
    return [sys.executable, "-c", "from ngen_weave_mcp.stdio import main; main()"]


async def test_stdio_run_completes_via_tool_call(example_project, fake_replies):
    env = {
        **os.environ,
        # Test-only hook documented in stdio.py's --help epilog and README-less
        # plan docs: makes the subprocess replay canned model replies instead
        # of calling a real provider.
        "NGEN_WEAVE_FAKE_PROVIDER": "1",
        "NGEN_WEAVE_FAKE_REPLIES": str(fake_replies),
        "PYTHONPATH": str(EXAMPLE_SRC),
    }
    params = StdioServerParameters(
        command=console_script_command()[0],
        args=[*console_script_command()[1:], "--root", str(example_project)],
        cwd=str(example_project),
        env=env,
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        by_name = {t.name: t for t in listing.tools}
        assert TOOL_NAME in by_name
        tool = by_name[TOOL_NAME]
        assert tool.description == ("Code review: draft, gate, human approval, finalize.")
        assert tool.input_schema["properties"].keys() == {"diff"}

        diff = json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]
        result = await session.call_tool(TOOL_NAME, {"diff": diff}, read_timeout_seconds=120)

    assert result.is_error is False
    assert result.structured_content == {
        "reviewed_diff": diff,
        "verdict": "approve",
    }
