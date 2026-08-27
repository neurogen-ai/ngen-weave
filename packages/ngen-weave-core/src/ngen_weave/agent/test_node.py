"""AgentNode declaration rules: import-time skip for the base, gating for leaves."""

import pytest
from pydantic import BaseModel

import ngen_weave.agent.node  # noqa: F401 -- must not raise at import
from ngen_weave.agent.node import AgentNode
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.errors import ConfigError


class _In(BaseModel):
    text: str


class _Out(BaseModel):
    text: str


def test_import_does_not_raise():
    """Re-importing the module is inert; AgentNode is a usable base class."""
    from ngen_weave.agent import node as node_module

    assert node_module.AgentNode.__mro__[1].__name__ == "Workflow"


def test_concrete_subclass_requires_permissions():
    with pytest.raises(ConfigError, match=r"ConcreteNoPerm.*permissions"):

        class ConcreteNoPerm(AgentNode):
            input_type = _In
            output_type = _Out


def test_error_names_the_class():
    """A non-PermissionSet is rejected too, naming the offending class."""
    with pytest.raises(ConfigError, match="BadPerms.*permissions"):

        class BadPerms(AgentNode):
            input_type = _In
            output_type = _Out
            permissions = ("deny", "everything")  # not a PermissionSet


def test_valid_concrete_subclass_is_fully_validated():
    class ConcreteAgentNode(AgentNode):
        input_type = _In
        output_type = _Out
        permissions = PermissionSet(allowed_tools=())

    from ngen_weave.workflow import validate_structure

    validate_structure(ConcreteAgentNode)
