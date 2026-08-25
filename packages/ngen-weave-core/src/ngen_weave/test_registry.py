"""Tests for the in-process workflow registry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ngen_weave import registry
from ngen_weave.errors import ConfigError
from ngen_weave.workflow import Workflow, workflow_class_path


class In(BaseModel):
    text: str


class Out(BaseModel):
    result: str


class Leaf(Workflow):
    input_type = In
    output_type = Out

    def run(self, input, ctx):
        return Out(result=input.text)


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


def test_registration_and_lookup():
    registry.register(Leaf, source="test")
    assert registry.get(workflow_class_path(Leaf)) is Leaf


def test_existing_class_does_not_self_register():
    with pytest.raises(ConfigError, match="unknown workflow"):
        registry.get(workflow_class_path(Leaf))


def test_unknown_path_error_message():
    with pytest.raises(ConfigError, match="unknown workflow: no.such.Workflow"):
        registry.get("no.such.Workflow")


def test_duplicate_registration_names_both_sources():
    registry.register(Leaf, source="project manifest")
    with pytest.raises(ConfigError, match="project manifest.*entry points"):
        registry.register(Leaf, source="entry points")


def test_all_returns_snapshot():
    registry.register(Leaf, source="a")
    snapshot = registry.all()
    snapshot.clear()
    assert registry.get(workflow_class_path(Leaf)) is Leaf
    assert registry.all() == {workflow_class_path(Leaf): Leaf}


def test_reset_clears_everything():
    registry.register(Leaf, source="a")
    registry.reset()
    with pytest.raises(ConfigError, match="unknown workflow"):
        registry.get(workflow_class_path(Leaf))
