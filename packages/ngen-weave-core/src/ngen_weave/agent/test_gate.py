"""PermissionGate behavior: allow/deny routing, provenance emission, budget ceilings."""

import pytest

from ngen_weave.agent.errors import DeniedToolError, ReturnToReviewError
from ngen_weave.agent.gate import PermissionGate
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolRegistry, ToolSpec
from ngen_weave.errors import DataError, NgWeaveError
from ngen_weave.workflow import RunContext


def make_ctx(records: list) -> RunContext:
    """Build a RunContext that appends emitted (kind, payload) pairs to records."""

    def emit(kind: str, payload: dict) -> None:
        records.append((kind, payload))

    return RunContext(run_id="r1", node_path="demo.Root.worker", emit=emit, provider=None)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(args: dict) -> dict:
        return {"echo": args}

    registry.register(
        ToolSpec(name="echo", description="", parameters_schema={"type": "object"}, fn=echo)
    )
    return registry


def make_permitted_registry(*costs: float) -> ToolRegistry:
    """Registry whose "bill" tool reports each cost in its result dict."""
    registry = ToolRegistry()
    calls = {"n": 0}

    async def bill(args: dict) -> dict:
        cost = costs[calls["n"]]
        calls["n"] += 1
        return {"out": args.get("x"), "cost_usd": cost}

    registry.register(ToolSpec(name="bill", description="", parameters_schema={}, fn=bill))
    return registry


async def test_allowed_tool_delegates_to_inner():
    gate = PermissionGate(make_registry(), PermissionSet(allowed_tools=("echo",)), make_ctx([]))
    assert await gate.call("echo", {"a": 1}) == {"echo": {"a": 1}}


async def test_denied_tool_fails_node_and_emits_exactly_one_record():
    records: list = []
    gate = PermissionGate(
        make_registry(), PermissionSet(allowed_tools=("other",)), make_ctx(records)
    )
    with pytest.raises(DeniedToolError):
        await gate.call("echo", {})
    assert records == [
        (
            "permission_denied",
            {"tool": "echo", "node_path": "demo.Root.worker", "policy": "fail_node"},
        )
    ]


async def test_fail_node_error_is_data_error_subclass():
    gate = PermissionGate(make_registry(), PermissionSet(allowed_tools=()), make_ctx([]))
    with pytest.raises(DataError):
        await gate.call("echo", {})


async def test_return_to_review_raises_right_type_and_one_record():
    records: list = []
    gate = PermissionGate(
        make_registry(),
        PermissionSet(allowed_tools=(), denied_policy="return_to_review"),
        make_ctx(records),
    )
    with pytest.raises(ReturnToReviewError):
        await gate.call("echo", {})
    assert not isinstance(ReturnToReviewError("x"), DataError)
    assert isinstance(ReturnToReviewError("x"), NgWeaveError)
    assert [kind for kind, _payload in records] == ["permission_denied"]


async def test_max_calls_ceiling_denies_after_limit():
    records: list = []
    gate = PermissionGate(
        make_registry(),
        PermissionSet(allowed_tools=("echo",), max_calls=2),
        make_ctx(records),
    )
    await gate.call("echo", {"n": 1})
    await gate.call("echo", {"n": 2})
    with pytest.raises(DeniedToolError, match="denied"):
        await gate.call("echo", {"n": 3})
    kinds = [kind for kind, _payload in records]
    assert kinds == ["permission_denied"]
    assert records[0][1]["tool"] == "echo"


async def test_budget_usd_denies_once_spend_observed():
    records: list = []
    gate = PermissionGate(
        make_permitted_registry(0.5, 0.75),
        PermissionSet(allowed_tools=("bill",), budget_usd=1.0),
        make_ctx(records),
    )
    first = await gate.call("bill", {"x": 1})
    second = await gate.call("bill", {"x": 2})
    assert first == {"out": 1, "cost_usd": 0.5}
    assert second == {"out": 2, "cost_usd": 0.75}
    with pytest.raises(DeniedToolError):
        await gate.call("bill", {"x": 3})
    assert [kind for kind, _payload in records] == ["permission_denied"]


async def test_under_budget_calls_never_emit_or_raise():
    records: list = []
    gate = PermissionGate(
        make_permitted_registry(0.5),
        PermissionSet(allowed_tools=("bill",), budget_usd=10.0, max_calls=5),
        make_ctx(records),
    )
    assert await gate.call("bill", {}) == {"out": None, "cost_usd": 0.5}
    assert records == []
