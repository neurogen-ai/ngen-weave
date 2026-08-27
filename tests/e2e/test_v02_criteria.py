"""v0.2 success criteria exercised across the MCP and CLI surfaces.

One test per remaining criterion; criteria 2 and 6 link to their owning D4
suites instead of repeating them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager, chdir
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams
from ngen_weave.agent.node import AgentNode
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolSpec
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.export import dump_run_json
from ngen_weave.workflow import END, START, Worker, Workflow
from ngen_weave_mcp.fake_provider import FAKE_PROVIDER_ENV, FAKE_REPLIES_ENV, FakeReplyProvider
from ngen_weave_mcp.http import create_http_app
from ngen_weave_mcp.tools import register_workflow_tools, tool_name
from pydantic import BaseModel
from typer.testing import CliRunner

runner = CliRunner()

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "code_review"
EXAMPLE_SRC = EXAMPLE_DIR / "src"
TOOL_NAME = "code_review-workflows-CodeReview"
WORKFLOW = "code_review.workflows.CodeReview"

# A2 contract: the v0.1 flat-file keys plus started_at and notes. The strict
# recorded-bytes fixture lives at unit level in
# packages/ngen-weave-core/src/ngen_weave/test_export.py; this file carries
# the cheaper structural assertions over a live completed run.
RUN_JSON_KEYS = frozenset(
    {
        "format",
        "run_id",
        "workflow",
        "status",
        "input",
        "output",
        "error",
        "attempts",
        "submissions",
        "started_at",
        "notes",
        "records",
    }
)

_TERMINAL_ACTIVATIONS = frozenset({"ok", "invalid", "waiting_human"})


def _console_script_command() -> list[str]:
    """Locate the ngen-weave-mcp console script beside the venv interpreter."""
    candidate = Path(sys.executable).with_name("ngen-weave-mcp")
    if candidate.exists():
        return [str(candidate)]
    found = shutil.which("ngen-weave-mcp")
    if found:
        return [found]
    # Fall back to direct entry-point invocation for editable checkouts.
    return [sys.executable, "-c", "from ngen_weave_mcp.stdio import main; main()"]


def _review_diff() -> str:
    """Read the canonical diff the CodeReview workflow consumes."""
    return json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]


def _set_fake_replies(monkeypatch, tmp_path: Path, replies: list[str]) -> None:
    """Write the canned-reply file and enable the documented env hooks."""
    replies_file = tmp_path / "fake-replies.json"
    replies_file.write_text(json.dumps(replies))
    monkeypatch.setenv(FAKE_PROVIDER_ENV, "1")
    monkeypatch.setenv(FAKE_REPLIES_ENV, str(replies_file))


@pytest.fixture(params=["stdio", "http"])
def transport(request) -> str:
    """Which MCP transport the criterion-1 scenario drives, stdio or HTTP."""
    return request.param


@pytest.fixture()
def example_project(tmp_path: Path, monkeypatch) -> Path:
    """Copy the example fixtures into tmp_path and make its src importable.

    Mirrors tests/e2e/test_code_review.py: the manifest copy makes merged
    discovery register the example classes for in-process resumes and CLI
    verbs anchored at the copied project directory.
    """
    for name in ("ngw.yaml", "request.json", "models.json", "ngen-weave.json"):
        shutil.copy(EXAMPLE_DIR / name, tmp_path / name)
    monkeypatch.syspath_prepend(str(EXAMPLE_SRC))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    from ngen_weave.registry import reset as registry_reset
    from ngen_weave.wiring import reset_merged_registry

    registry_reset()
    reset_merged_registry()


@asynccontextmanager
async def stdio_session(project: Path, extra_args: list[str] | None = None):
    """Spawn ngen-weave-mcp against project and yield an initialized session.

    Reuses the F1 e2e pattern: real MCP-over-stdio against a subprocess whose
    runs store, checkpoint database, and manifest anchor at the project dir,
    so an in-process service can pick up where a parked run left off.
    """
    command = _console_script_command()
    params = StdioServerParameters(
        command=command[0],
        args=[*command[1:], "--root", str(project), *(extra_args or [])],
        cwd=str(project),
        env={**os.environ, "PYTHONPATH": str(EXAMPLE_SRC)},
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def http_session(project: Path):
    """Serve create_http_app(project) on uvicorn and yield an initialized session.

    Mirrors the F2 e2e helper: uvicorn in-process at an OS-chosen port, SDK
    streamable-http client above it, stores still anchored at project.
    """
    with chdir(project):
        app = create_http_app(project)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        async with (
            streamable_http_client(url) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            yield session
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def _mcp_surface(transport: str, project: Path, extra_args: list[str] | None = None):
    """Return the async context manager driving the requested transport."""
    if transport == "stdio":
        return stdio_session(project, extra_args)
    return http_session(project)


@asynccontextmanager
async def memory_session(server):
    """Drive one MCP Server over the SDK memory pair; F1's registration path.

    Used by criterion 4, where the AgentNode-bearing workflow exists only
    inside this test process and is handed to register_workflow_tools with an
    explicit discovery map, exactly like the F1 unit suite.
    """
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(
            server.run(*server_streams, server.create_initialization_options())
        )
        try:
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                yield session
        finally:
            task.cancel()


async def test_criterion_1_mcp_tool_pauses_at_human_then_resumes(
    example_project, transport, tmp_path, monkeypatch
) -> None:
    """Criterion 1: a workflow called through MCP parks correctly and resumes.

    The empty draft review fails the gate so the run stops at the human node;
    both transports return the paused structure with a run id, and an
    in-process service resume (the local stack behind the CLI verbs) carries
    the run to its validated terminal output.
    """
    diff = _review_diff()
    _set_fake_replies(monkeypatch, tmp_path, [json.dumps({"review": "", "diff": diff})])

    async with _mcp_surface(transport, example_project) as session:
        result = await session.call_tool(TOOL_NAME, {"diff": diff}, read_timeout_seconds=120)

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "waiting_human"
    assert payload["run_id"]
    assert payload["waiting_on"]

    approval = json.dumps({"reviewed_diff": diff, "verdict": "approve"})
    from ngen_weave.wiring import build_service

    service = build_service(provider=FakeReplyProvider([approval]))
    handle = await service.resume(payload["run_id"], payload={"verdict": "approve", "notes": ""})
    assert handle.status == "completed"
    final = await service.status(payload["run_id"])
    assert final.output == {"reviewed_diff": diff, "verdict": "approve"}


def test_criterion_2_remote_json_review_resumes_identically() -> None:
    """Criterion 2 lives in the D4 parity suite; linked, not repeated.

    See packages/ngen-weave-server/tests/conformance/test_review_parity.py,
    which proves the identical response dict reaches equal outputs and equal
    record streams whether submitted via service.resume or through the local
    YAML artifact path.
    """


def test_criterion_6_conformance_suite_passes_against_fastapi() -> None:
    """Criterion 6 lives in the D4 conformance suite; linked, not repeated.

    See packages/ngen-weave-server/tests/conformance/test_run_service.py,
    which is parametrized over every service factory (including the FastAPI
    backend) and stays green by contract.
    """


class DenialQuestion(BaseModel):
    """Temp-package style input for the denial replay workflow."""

    question: str


class DenialAnswer(BaseModel):
    """Opener output carried into the guarded agent activation."""

    answer: str


class DenialNote(BaseModel):
    """Workflow output; unreachable when the denial fails the run."""

    note: str


_OPENER_REPLY = '{"answer": "first"}'
_TOOL_CALL_REPLY = '{"tool_call": {"name": "forbidden", "args": {}}}'
DENIAL_KEY = "fixtures.g2_denial.G2DeniedFlow"


def _denial_workflow() -> tuple[str, type[Workflow]]:
    """Build the opener-plus-AgentNode composite and register it globally.

    Same temp-package shape as the Branch E acceptance fixtures: generated
    classes register under synthetic class paths and never collide with the
    discovered examples.
    """

    async def forbidden_fn(args: dict) -> dict:
        return {"found": True}

    spec = ToolSpec(
        name="forbidden",
        description="blocked unless allowed",
        parameters_schema={"type": "object", "properties": {}},
        fn=forbidden_fn,
    )
    opener = type(
        "G2DeniedOpener",
        (Worker,),
        {
            "description": "Carry the question forward.",
            "prompt": "carry {question}",
            "input_type": DenialQuestion,
            "output_type": DenialAnswer,
        },
    )
    agent = type(
        "G2DeniedAgent",
        (AgentNode,),
        {
            "description": "Agent whose only tool the permission gate denies.",
            "input_type": DenialAnswer,
            "output_type": DenialNote,
            "permissions": PermissionSet(allowed_tools=()),
            "tools": (spec,),
        },
    )

    def build(self, g):
        g.add_node(opener)
        g.add_node(agent)
        g.add_edge(START, opener)
        g.add_edge(opener, agent)
        g.add_edge(agent, END)

    flow = type(
        "G2DeniedFlow",
        (Workflow,),
        {
            "description": "Replays the denied-tool scenario end to end.",
            "input_type": DenialQuestion,
            "output_type": DenialNote,
            "build": build,
        },
    )
    from ngen_weave.registry import register as registry_register

    for cls in (opener, agent, flow):
        registry_register(cls, "g2-e2e")
    return DENIAL_KEY, flow


async def test_criterion_4_denied_tool_reports_failed_run_and_exports_record(
    tmp_path, monkeypatch
) -> None:
    """Criterion 4: a deny-listed tool through MCP fails and records itself.

    The workflow exposes through the same register_workflow_tools handler the
    real transports mount, driven here over the SDK memory pair. The gate
    denies under the fail_node policy, the tool result reports the failed
    run, and ngen-weave export-run emits JSON carrying the permission_denied
    record with the configured policy.
    """
    from ngen_weave.registry import reset as registry_reset
    from ngen_weave_cli.main import app
    from ngen_weave_server.local import LocalRunService
    from tests.fakes import FakeProvider

    try:
        _, flow = _denial_workflow()
        monkeypatch.chdir(tmp_path)  # export-run reads .ngen-weave/runs under cwd
        provider = FakeProvider(replies=[_OPENER_REPLY, _TOOL_CALL_REPLY])
        store = RunStore(Path(".ngen-weave") / "runs")
        engine = Engine(provider, store, checkpointer="memory")
        service = LocalRunService(engine, store, {DENIAL_KEY: flow})

        server = Server("g2-criteria")
        register_workflow_tools(server, {DENIAL_KEY: flow}, service)
        async with memory_session(server) as session:
            result = await session.call_tool(tool_name(DENIAL_KEY), {"question": "what?"})

        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "failed"
        assert payload["error"]["type"] == "DeniedToolError"
        assert "fail_node" in payload["error"]["message"]
        run_id = payload["run_id"]
        assert run_id

        export = runner.invoke(app, ["export-run", run_id])
        assert export.exit_code == 0, export.output
        document = json.loads(export.stdout_bytes.decode("utf-8"))
        denials = [r for r in document["records"] if r["kind"] == "permission_denied"]
        assert len(denials) == 1  # the gate emitted exactly one record before raising
        assert denials[0]["payload"]["policy"] == "fail_node"
        assert denials[0]["payload"]["tool"] == "forbidden"
        assert document["status"] == "failed"
    finally:
        registry_reset()


def _invoke_cli_in_thread(args: list[str]):
    """Run the Typer CLI on a helper thread so its asyncio.run succeeds.

    CLI verbs are sync entry points that create their own event loop; calling
    them from inside an async test needs a thread with no running loop.
    """
    import concurrent.futures

    from ngen_weave_cli.main import app

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: runner.invoke(app, args)).result()


async def test_criterion_5_budget_pause_then_resume_after_raise(
    example_project, tmp_path, monkeypatch
) -> None:
    """Criterion 5: exceeding the cost budget pauses; a raised cap completes.

    Launches through the stdio MCP main with --config pointing at a copied
    ngw.yaml carrying a tiny run.budget.cost_usd; the first model call alone
    crosses it, so the boundary hook pauses the run. Raising the limit in the
    copied config and resuming via ngen-weave resume finishes the workflow.
    """
    config_file = example_project / "ngw.yaml"
    configured = config_file.read_text().replace(
        "checkpointer: memory",
        # sqlite persists checkpoint state across the process boundary so the
        # CLI-side resume continues on the same checkpoint namespace.
        "checkpointer: sqlite\n  budget:\n    cost_usd: 0.05",
    )
    config_file.write_text(configured)

    diff = _review_diff()
    good_review = json.dumps({"review": "Looks good overall.", "diff": diff})
    _set_fake_replies(monkeypatch, tmp_path, [good_review])

    async with stdio_session(example_project, extra_args=["--config", str(config_file)]) as session:
        result = await session.call_tool(TOOL_NAME, {"diff": diff}, read_timeout_seconds=120)

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "paused"
    assert payload["run_id"]
    assert payload["waiting_on"]
    run_id = payload["run_id"]

    config_file.write_text(configured.replace("cost_usd: 0.05", "cost_usd: 10000"))

    approval = json.dumps({"reviewed_diff": diff, "verdict": "approve"})
    from tests.fakes import FakeProvider

    monkeypatch.setattr(
        "ngen_weave_cli.context.default_provider", lambda models_file: FakeProvider([approval])
    )
    # A budget-paused run carries no human slots; resume without a response
    # payload, driving a None seed on the same checkpoint namespace.
    resumed = _invoke_cli_in_thread(["resume", run_id])
    assert resumed.exit_code == 0, resumed.output
    assert "status completed" in resumed.output

    final = RunStore(Path(".ngen-weave") / "runs").load(run_id)
    assert final.status == "completed"
    assert final.output == {"reviewed_diff": diff, "verdict": "approve"}


def test_criterion_3_completed_run_export_matches_serializer(example_project, monkeypatch) -> None:
    """Criterion 3: a completed code_review run exports faithfully.

    Drives the example to completion through the CLI, exports with
    ngen-weave export-run, and checks structure, ordering, provenance
    counts, and byte equality against dump_run_json(store.load(id))
    computed in-test. The recorded strict-byte fixture lives at unit level
    in test_export.py; see RUN_JSON_KEYS for the split.
    """
    from ngen_weave_cli.context import NGEN_WEAVE_DIR
    from ngen_weave_cli.main import app
    from tests.fakes import FakeProvider

    diff = _review_diff()
    draft_reply = json.dumps({"review": "Looks good overall.", "diff": diff})
    finalize_reply = json.dumps({"reviewed_diff": diff, "verdict": "approve"})
    provider = FakeProvider(replies=[draft_reply, finalize_reply])
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)

    launched = runner.invoke(
        app,
        [
            "run",
            WORKFLOW,
            "-i",
            str(example_project / "request.json"),
            "-c",
            str(example_project / "ngw.yaml"),
        ],
    )
    assert launched.exit_code == 0, launched.output
    assert "status completed" in launched.output
    prefix = "run "
    run_id = next(
        line[len(prefix) :] for line in launched.output.splitlines() if line.startswith(prefix)
    )

    export = runner.invoke(app, ["export-run", run_id])
    assert export.exit_code == 0, export.output

    document = json.loads(export.stdout_bytes.decode("utf-8"))
    assert set(document) == RUN_JSON_KEYS

    records = document["records"]
    stamps = [r["ts"] for r in records]
    assert stamps == sorted(stamps)  # strictly ordered oldest-first, per the A2 contract

    by_node: dict[str, list] = {}
    for record in records:
        if record["kind"] == "node_activation":
            by_node.setdefault(record["node_path"], []).append(record)
    for path, activations in by_node.items():
        terminals = [a for a in activations if a["payload"]["status"] in _TERMINAL_ACTIVATIONS]
        statuses = [a["payload"]["status"] for a in activations]
        assert len(terminals) == 1, (path, statuses)

    model_paths = sorted(r["node_path"] for r in records if r["kind"] == "model_call")
    assert model_paths == [
        f"{WORKFLOW}.code_review.workflows.Draft",
        f"{WORKFLOW}.code_review.workflows.Finalize",
    ]  # one model_call per actual occurrence, nothing else
    assert not [r for r in records if r["kind"] == "observer_firing"]

    computed = dump_run_json(RunStore(NGEN_WEAVE_DIR / "runs").load(run_id))
    assert export.stdout_bytes == computed
