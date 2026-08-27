"""Differential test: _valid_tool_name must reproduce the retired [a-z][a-z0-9_-]* regex exactly."""

import re

import pytest

from ngen_weave.agent.tools import _valid_tool_name
from ngen_weave.errors import ConfigError

_OLD_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

_ACCEPT = [
    "a",
    "z",
    "hello_world",
    "check-status",
    "tool2",
    "a-b_c-d_e9",
    "x" * 200,
]

_REJECT = [
    "",
    "A",
    "Hello",
    "TOOL",
    "2tool",
    "_lead",
    "-lead",
    "has space",
    "tab\there",
    "new\nline",
    "carriage\rreturn",
    "trailing ",
    " leading",
    "café",
    "日本語",
    "٣",
    "tool.name",
    "tool/name",
    "tool+plus",
    "emoji🎉",
    "null\x00byte",
]


def test_accepted_names_match_old_regex():
    for name in _ACCEPT:
        assert _OLD_RE.fullmatch(name) is not None, name
        assert _valid_tool_name(name), name


def test_rejected_names_rejected_by_old_regex():
    for name in _REJECT:
        assert _OLD_RE.fullmatch(name) is None, repr(name)
        assert not _valid_tool_name(name), repr(name)


@pytest.mark.parametrize("name", _ACCEPT + _REJECT + ["ab1-_", "b_c", "c9d8e7f6", "zz----__99"])
def test_validator_agrees_with_old_regex_everywhere(name):
    assert _valid_tool_name(name) == (_OLD_RE.fullmatch(name) is not None)


def test_registry_rejects_bad_name_with_old_message():
    from ngen_weave.agent.tools import ToolRegistry, ToolSpec

    async def fn(args: dict) -> dict:
        return {}

    with pytest.raises(ConfigError, match=r"must match \[a-z\]\[a-z0-9_-\]*"):
        spec = ToolSpec(name="BadName", description="", parameters_schema={}, fn=fn)
        ToolRegistry().register(spec)
