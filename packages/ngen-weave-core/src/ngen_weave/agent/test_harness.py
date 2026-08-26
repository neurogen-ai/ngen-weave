"""ToolRegistry and ToolSpec behavior: registration, validation, dispatch."""

import pytest

from ngen_weave.agent.errors import UnknownToolError
from ngen_weave.agent.tools import ToolRegistry, ToolSpec
from ngen_weave.errors import ConfigError, DataError


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
            parameters_schema={"type": "object", "properties": {"q": {"type": "string"}},
                               "required": ["q"], "additionalProperties": False},
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


