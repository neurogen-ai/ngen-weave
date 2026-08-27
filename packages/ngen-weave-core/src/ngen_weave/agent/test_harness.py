"""ToolRegistry and ToolSpec behavior: registration, validation, dispatch.

Also carries the engine-side E3 acceptance scenario: a deny-listed tool
attempted mid-run is blocked, recorded by the gate once, and routed per the
denied_policy -- fail_node fails the run as an ordinary DataError while
return_to_review parks it waiting_human and completes after a corrected
resume.
"""

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider

from ngen_weave.agent.errors import UnknownToolError
from ngen_weave.agent.node import AgentNode
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolRegistry, ToolSpec
from ngen_weave.engine import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, DataError
from ngen_weave.registry import register, reset
from ngen_weave.workflow import END, START, Worker, Workflow, workflow_class_path


def make_tool(result: dict | None = None) -> ToolSpec:
    """Build a minimal valid spec whose fn echoes the result when called."""
    return ToolSpec(
        name="lookup",
        description="echoes a fixed result",
        parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        fn=_async_return(result if result is not None else {"ok": True}),
    )


def _async_return(value: dict):
    """Return an async callable that yields value."""

    async def _fn(args: dict) -> dict:
        return value

    return _fn


async def test_register_and_specs_round_trip():
    registry = ToolRegistry()
    tool = make_tool()
    registry.register(tool)
    assert registry.specs() == (tool,)


async def test_duplicate_registration_raises_config_error():
    registry = ToolRegistry()
    registry.register(make_tool())
    with pytest.raises(ConfigError, match="duplicate tool registration"):
        registry.register(make_tool())


@pytest.mark.parametrize(
    "name",
    ["", "Lookup", "9lives", "-lead", "has space", "dot.name"],
)
def test_malformed_name_rejected(name: str):
    registry = ToolRegistry()
    with pytest.raises(ConfigError, match="invalid tool name"):
        registry.register(ToolSpec(name=name, description="", parameters_schema={}, fn=None))


def test_invalid_schema_rejected_at_registration():
    registry = ToolRegistry()
    with pytest.raises(ConfigError, match="invalid parameters_schema"):
        registry.register(
            ToolSpec(name="x", description="", parameters_schema={"type": 7}, fn=None)
        )


async def test_call_executes_fn_with_validated_args():
    seen: list[dict] = []

    async def fn(args: dict) -> dict:
        seen.append(args)
        return {"found": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="add",
            description="",
            parameters_schema={
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            fn=fn,
        )
    )
    result = await registry.call("add", {"a": 2})
    assert result == {"found": True}
    assert seen == [{"a": 2}]


async def test_call_rejects_schema_violations_with_data_error():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="lookup",
            description="",
            parameters_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
                "additionalProperties": False,
            },
            fn=None,
        )
    )
    for bad in ({"q": 1}, {}, ["not-an-object"]):
        with pytest.raises(DataError, match="invalid arguments"):
            await registry.call("lookup", bad)


async def test_unknown_tool_raises_unknown_tool_error():
    registry = ToolRegistry()
    registry.register(make_tool())
    with pytest.raises(UnknownToolError, match="unknown tool 'missing'"):
        await registry.call("missing", {})


# --- E3: engine-side denial routing (criterion 4 end to end) -----------------


class Question(BaseModel):
    question: str


class Answer(BaseModel):
    answer: str


class Note(BaseModel):
    note: str


_TOOL_CALL_REPLY = '{"tool_call": {"name": "forbidden", "args": {}}}'
_OUTPUT_REPLY = '{"output": {"note": "done"}}'
_OPENER_REPLY = '{"answer": "first"}'


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated workflow classes reuse short names; isolate the global registry."""
    reset()
    yield
    reset()


def _opener(name: str):
    """A worker that opens the run so the denial lands mid-run, not at START."""
    cls = type(
        name,
        (Worker,),
        {"prompt": "carry {question}", "input_type": Question, "output_type": Answer},
    )
    register(cls, "test")
    return cls


def _agent_node(name: str, permissions: PermissionSet):
    """An AgentNode registering 'forbidden' but whose gate may disallow it."""

    async def _fn(args: dict) -> dict:
        return {"found": True}

    spec = ToolSpec(
        name="forbidden",
        description="blocked unless allowed",
        parameters_schema={"type": "object", "properties": {}},
        fn=_fn,
    )
    cls = type(
        name,
        (AgentNode,),
        {
            "input_type": Answer,
            "output_type": Note,
            "permissions": permissions,
            "tools": (spec,),
        },
    )
    register(cls, "test")
    return cls


def _flow(name: str, opener, agent) -> type[Workflow]:
    """START -> opener -> agent -> END so the denial happens after one commit."""

    def build(self, g):
        g.add_node(opener)
        g.add_node(agent)
        g.add_edge(START, opener)
        g.add_edge(opener, agent)
        g.add_edge(agent, END)

    wf = type(name, (Workflow,), {"input_type": Question, "output_type": Note, "build": build})
    register(wf, "test")
    return wf


def _engine(replies: list[str], tmp_path):
    provider = FakeProvider(replies)
    engine = Engine(provider, RunStore(tmp_path / "runs"), checkpointer="memory")
    return engine, provider


async def test_fail_node_policy_fails_run_as_ordinary_data_error(tmp_path):
    opener = _opener("E3FailOpener")
    agent = _agent_node("E3FailAgent", PermissionSet(allowed_tools=()))
    flow = _flow("E3FailFlow", opener, agent)
    engine, _ = _engine([_OPENER_REPLY, _TOOL_CALL_REPLY], tmp_path)

    result = await engine.run(flow, Question(question="q"))

    assert result.status == "failed"
    rf = engine.store.load(result.run_id)
    assert rf.error is not None and rf.error["type"] == "DeniedToolError"
    assert "fail_node" in rf.error["message"]
    agent_path = f"{workflow_class_path(flow)}.{workflow_class_path(agent)}"
    records = engine.store.load(result.run_id).records
    denied = [r for r in records if r.kind == "permission_denied"]
    assert len(denied) == 1  # the gate emitted exactly one record before raising
    assert denied[0].node_path == agent_path
    assert denied[0].payload == {
        "tool": "forbidden",
        "node_path": agent_path,
        "policy": "fail_node",
    }
    # The opener committed first; the agent's activation was marked invalid.
    opener_path = f"{workflow_class_path(flow)}.{workflow_class_path(opener)}"
    statuses = [
        (r.node_path, r.payload.get("status")) for r in records if r.kind == "node_activation"
    ]
    assert statuses == [
        (opener_path, "ok"),
        (agent_path, "invalid"),
    ]


async def test_return_to_review_pauses_then_completes_after_corrected_resume(tmp_path):
    opener = _opener("E3ReviewOpener")
    agent = _agent_node(
        "E3ReviewAgent",
        PermissionSet(allowed_tools=(), denied_policy="return_to_review"),
    )
    flow = _flow("E3ReviewFlow", opener, agent)
    engine, _ = _engine([_OPENER_REPLY, _TOOL_CALL_REPLY, _OUTPUT_REPLY], tmp_path)

    waiting = await engine.run(flow, Question(question="q"))

    agent_path = f"{workflow_class_path(flow)}.{workflow_class_path(agent)}"
    assert waiting.status == "waiting_human"
    assert waiting.waiting == {"node_path": agent_path, "reason": "returned_to_review"}
    records = engine.store.load(waiting.run_id).records
    denied = [r for r in records if r.kind == "permission_denied"]
    assert len(denied) == 1 and denied[0].payload["policy"] == "return_to_review"
    parked = next(r for r in records if r.payload.get("status") == "waiting_human")
    assert parked.node_path == agent_path and parked.payload["reason"] == "returned_to_review"

    # Corrected resume: widen the allowed surface on the class, then continue.
    agent.permissions = PermissionSet(
        allowed_tools=("forbidden",), denied_policy="return_to_review"
    )
    result = await engine.resume(waiting.run_id)

    assert result.status == "completed"
    assert result.output == Note(note="done")
    final = engine.store.load(waiting.run_id).records
    assert len([r for r in final if r.kind == "permission_denied"]) == 1  # still one only
    ok_records = [
        r
        for r in reversed(final)
        if r.kind == "node_activation"
        and r.payload.get("status") == "ok"
        and r.node_path == agent_path
    ]
    assert len(ok_records) == 1  # replay after resume produced one clean activation
