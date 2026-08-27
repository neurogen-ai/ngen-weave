"""Streamable-HTTP e2e: real MCP-over-HTTP against the canonical code-review example."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from contextlib import asynccontextmanager, chdir
from pathlib import Path

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from ngen_weave.registry import reset as registry_reset
from ngen_weave.wiring import build_service, reset_merged_registry
from ngen_weave_mcp.fake_provider import FakeReplyProvider
from ngen_weave_mcp.http import create_http_app

EXAMPLE_DIR = Path(__file__).resolve().parents[4] / "examples" / "code_review"
EXAMPLE_SRC = EXAMPLE_DIR / "src"
TOOL_NAME = "code_review-workflows-CodeReview"


@pytest.fixture()
def example_project(tmp_path):
    """Copy the example's manifest next to its importable src under tmp_path."""
    shutil.copy(EXAMPLE_DIR / "ngen-weave.json", tmp_path / "ngen-weave.json")
    return tmp_path


def fake_replies_file(tmp_path, replies) -> Path:
    """Write a canned-reply JSON array; mirrors tests/e2e/test_code_review.py."""
    replies_file = tmp_path / "fake-replies.json"
    replies_file.write_text(json.dumps(replies))
    return replies_file


@asynccontextmanager
async def running_server(root: Path):
    """Serve create_http_app on uvicorn in-process at an OS-chosen port."""
    with chdir(root):
        app = create_http_app(root)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.fixture()
def _discovery(example_project):
    """Point discovery at the copied project's modules for this process."""
    sys.path.insert(0, str(EXAMPLE_SRC))
    reset_merged_registry()
    yield
    del sys.path[0]
    reset_merged_registry()
    # Discovery registered manifest workflows globally; drop them so other
    # tests in the same process can re-register without collisions.
    registry_reset()


def review_diff() -> str:
    return json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]


async def test_http_run_completes_via_tool_call(example_project, tmp_path, monkeypatch, _discovery):
    diff = review_diff()
    # Store and checkpoint paths are cwd-anchored; stay in the project root
    # for the whole test so the served stack and the resume service agree.
    monkeypatch.chdir(example_project)
    replies = fake_replies_file(
        tmp_path,
        [
            json.dumps({"review": "Looks good overall.", "diff": diff}),
            json.dumps({"reviewed_diff": diff, "verdict": "approve"}),
        ],
    )
    # Test-only hooks documented in stdio.py's epilog and fake_provider.py:
    # make the served stack replay canned model replies instead of calling a
    # real provider.
    monkeypatch.setenv("NGEN_WEAVE_FAKE_PROVIDER", "1")
    monkeypatch.setenv("NGEN_WEAVE_FAKE_REPLIES", str(replies))

    async with (
        running_server(example_project) as url,
        streamable_http_client(url) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        by_name = {t.name: t for t in listing.tools}
        assert TOOL_NAME in by_name
        tool = by_name[TOOL_NAME]
        assert tool.description == ("Code review: draft, gate, human approval, finalize.")
        assert tool.input_schema["properties"].keys() == {"diff"}

        result = await session.call_tool(TOOL_NAME, {"diff": diff}, read_timeout_seconds=120)

    assert result.is_error is False
    assert result.structured_content == {
        "reviewed_diff": diff,
        "verdict": "approve",
    }


async def test_http_human_pause_returns_run_id_then_resumes(
    example_project, tmp_path, monkeypatch, _discovery
):
    diff = review_diff()
    monkeypatch.chdir(example_project)
    replies = fake_replies_file(
        tmp_path,
        # An empty review fails the gate, routing to the human approval node,
        # which parks the run; no further replies are consumed before resume.
        [json.dumps({"review": "", "diff": diff})],
    )
    monkeypatch.setenv("NGEN_WEAVE_FAKE_PROVIDER", "1")
    monkeypatch.setenv("NGEN_WEAVE_FAKE_REPLIES", str(replies))

    async with (
        running_server(example_project) as url,
        streamable_http_client(url) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(TOOL_NAME, {"diff": diff})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "waiting_human"
    assert payload["run_id"]
    assert payload["waiting_on"]

    # Resumption goes through the runs service exactly like ngen-weave resume.
    resumer = build_service(
        provider=FakeReplyProvider([json.dumps({"reviewed_diff": diff, "verdict": "approve"})])
    )
    handle = await resumer.resume(payload["run_id"], payload={"verdict": "approve", "notes": ""})
    assert handle.status == "completed"
    final = await resumer.status(payload["run_id"])
    assert final.output == {"reviewed_diff": diff, "verdict": "approve"}
