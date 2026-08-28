"""MCP tool registration and dispatch against the SDK's in-memory session pair."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Literal

import pytest
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.state import RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.registry import register as registry_register
from ngen_weave.registry import reset as registry_reset
from ngen_weave.workflow import END, START, Human, Worker, Workflow
from pydantic import BaseModel

from ngen_weave_mcp.constants import POLL_INTERVAL_S
from ngen_weave_mcp.tools import register_workflow_tools, tool_name


class Root(BaseModel):
    text: str


class Counted(BaseModel):
    count: int


class Final(BaseModel):
    text: str


class Review(BaseModel):
    verdict: Literal["approve", "reject"]
    notes: str = ""


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}"):
    """One Worker subclass registered globally, like the conformance fixtures."""
    cls = type(
        name,
        (Worker,),
        {
            "description": f"{name} description.",
            "input_type": in_t,
            "output_type": out_t,
            "prompt": prompt,
        },
    )
    registry_register(cls, "test")
    return cls


def make_human(name: str):
    """One Human leaf with Review-shaped artifact slots."""
    cls = type(
        name,
        (Human,),
        {"input_type": Root, "output_type": Review, "state_type": Review},
    )
    registry_register(cls, "test")
    return cls


def make_pause_flow(name: str, human) -> type[Workflow]:
    """Single-Human root workflow (START -> human -> END) that parks at once."""

    def build(self, g):
        g.add_node(human)
        g.add_edge(START, human)
        g.add_edge(human, END)

    return type(
        name,
        (Workflow,),
        {
            "description": f"{name} description.",
            "input_type": Root,
            "output_type": Review,
            "build": build,
        },
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated test classes register globally; isolate each test."""
    registry_reset()
    yield
    registry_reset()


@asynccontextmanager
async def client_session(server):
    """Drive `server` over the SDK memory pair; yield an initialized client."""
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


def make_service(tmp_path, replies, discovery_map):
    """LocalRunService over a FakeProvider engine plus its store, for row checks."""
    from ngen_weave.local_service import LocalRunService
    from tests.fakes import FakeProvider

    provider = FakeProvider(replies)
    store = RunStore(tmp_path / "runs")
    engine = Engine(provider, store, checkpointer="memory")
    return LocalRunService(engine, store, dict(discovery_map)), store


ECHO_KEY = "fixtures.mcp_echo.EchoTool"
PAUSE_KEY = "fixtures.mcp_pause.PauseFlow"
COUNT_KEY = "fixtures.mcp_count.CountTool"


def build_fixtures(tmp_path, replies=None):
    """Echo + count + pause workflows wired into one service; plus its store."""
    echo = make_worker("EchoTool", Root, Final)
    count = make_worker("CountTool", Counted, Final, prompt="count {count}")
    pause_flow = make_pause_flow("PauseFlow", make_human("PauseHuman"))
    discovery_map = {ECHO_KEY: echo, PAUSE_KEY: pause_flow, COUNT_KEY: count}
    service, store = make_service(tmp_path, replies or ["ok"], discovery_map)
    return echo, pause_flow, count, service, store


async def list_tools(service, workflows):
    """List tools through the full in-memory MCP round trip."""
    server = Server("test-mcp")
    register_workflow_tools(server, workflows, service)
    async with client_session(server) as session:
        return await session.list_tools()


async def call_tool(service, workflows, name, arguments, **registration):
    """Call one tool through the full in-memory MCP round trip."""
    server = Server("test-mcp")
    register_workflow_tools(server, workflows, service, **registration)
    async with client_session(server) as session:
        return await session.call_tool(name, arguments)


def test_tool_name_sanitizes_dots_only():
    assert tool_name("examples.code_review.workflows.CodeReview") == (
        "examples-code_review-workflows-CodeReview"
    )
    assert tool_name("a.b_c.D") == "a-b_c-D"


async def test_list_tools_reports_one_entry_per_workflow(tmp_path):
    echo, pause_flow, _, service, _ = build_fixtures(tmp_path)

    listing = await list_tools(service, {ECHO_KEY: echo, PAUSE_KEY: pause_flow})

    assert [(t.name, t.description) for t in listing.tools] == [
        ("fixtures-mcp_echo-EchoTool", f"{echo.__name__} description."),
        ("fixtures-mcp_pause-PauseFlow", f"{pause_flow.__name__} description."),
    ]
    schema = next(t.input_schema for t in listing.tools if t.name.endswith("EchoTool"))
    assert schema == Root.model_json_schema()


async def test_completing_call_returns_validated_output(tmp_path):
    echo, _, _, service, _ = build_fixtures(tmp_path, replies=[json.dumps({"text": "from-model"})])

    result = await call_tool(service, {ECHO_KEY: echo}, tool_name(ECHO_KEY), {"text": "hello"})

    assert result.is_error is False
    assert result.structured_content == {"text": "from-model"}
    assert json.loads(result.content[0].text) == {"text": "from-model"}


async def test_parked_call_returns_run_id_and_waiting_node(tmp_path):
    _, pause_flow, _, service, _ = build_fixtures(tmp_path)

    result = await call_tool(
        service, {PAUSE_KEY: pause_flow}, tool_name(PAUSE_KEY), {"text": "please review"}
    )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "waiting_human"
    assert payload["run_id"]
    assert payload["waiting_on"]  # the parked node's path


async def test_invalid_arguments_produce_error_and_no_run_row(tmp_path):
    _, _, count, service, store = build_fixtures(tmp_path)

    result = await call_tool(
        service, {COUNT_KEY: count}, tool_name(COUNT_KEY), {"count": "not-a-number"}
    )

    assert result.is_error is True
    assert "ValidationError" in result.content[0].text
    assert store.list() == []


async def test_unknown_tool_names_an_error(tmp_path):
    echo, _, _, service, _ = build_fixtures(tmp_path)

    result = await call_tool(service, {ECHO_KEY: echo}, "fixtures-mcp-nothing", {})

    assert result.is_error is True


async def test_timeout_returns_error_naming_resumable_run(monkeypatch, tmp_path):
    """The timeout arm fires for runs stuck in 'running'; error carries run id."""

    launched = []

    class StuckService:
        async def launch(self, workflow, input):
            from ngen_weave.service import RunHandle

            launched.append(workflow)
            return RunHandle(run_id="run-stuck", status="running")

        async def status(self, run_id) -> RunFile:
            return RunFile(format=1, run_id=run_id, workflow="x", status="running", input={})

    monkeypatch.setattr("ngen_weave_mcp.constants.POLL_INTERVAL_S", 0.001)
    echo, _, _, _, _ = build_fixtures(tmp_path)
    result = await call_tool(
        StuckService(),
        {ECHO_KEY: echo},
        tool_name(ECHO_KEY),
        {"text": "hello"},
        tool_timeout_s=0.02,
    )

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["type"] == "TimeoutError"
    assert payload["run_id"] == "run-stuck"
    assert "resumable" in payload["error"]["message"]
    assert launched == [ECHO_KEY]


def test_missing_description_is_a_config_error():
    bare = type(
        "BareDescriptionWorker",
        (Worker,),
        {"input_type": Root, "output_type": Final, "prompt": "p"},
    )
    with pytest.raises(ConfigError, match="no description"):
        register_workflow_tools(Server("x"), {"x.Bare": bare}, None)


def test_sanitized_collision_names_both_paths():
    first = make_worker("CollisionOne", Root, Final)
    second = make_worker("CollisionTwo", Root, Final)
    # Dots -> hyphens only: these two distinct paths sanitize identically.
    with pytest.raises(ConfigError, match=r"one\.two\.Twin.*one-two\.Twin"):
        register_workflow_tools(Server("x"), {"one.two.Twin": first, "one-two.Twin": second}, None)


def test_poll_interval_is_engine_specified_quarter_second():
    assert POLL_INTERVAL_S == 0.25
