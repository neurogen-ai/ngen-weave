"""Tests for project manifest loading and manifest-driven discovery."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from ngen_weave import registry
from ngen_weave.errors import ConfigError
from ngen_weave.manifest import (
    MANIFEST_NAME,
    ProjectManifest,
    discover_from_manifest,
    load_project_manifest,
)

_SEQ = itertools.count()


WORKER_BODY = """\
from pydantic import BaseModel
from ngen_weave.workflow import Worker


class In(BaseModel):
    text: str


class Out(BaseModel):
    result: str


class Leaf(Worker):
    input_type = In
    output_type = Out
    prompt = "summarize {text}"
"""


def _module_name() -> str:
    return f"wfman_{next(_SEQ)}"


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


def _write_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    (tmp_path / f"{name}.py").write_text(WORKER_BODY)
    monkeypatch.syspath_prepend(str(tmp_path))


def test_missing_file_yields_empty_manifest(tmp_path):
    assert load_project_manifest(tmp_path) == ProjectManifest(modules=())


def test_manifest_lists_modules_from_root(tmp_path):
    name = _module_name()
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"modules": [name]}))
    assert load_project_manifest(tmp_path).modules == (name,)


def test_manifest_without_modules_key_is_empty(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"other": 1}))
    assert load_project_manifest(tmp_path).modules == ()


def test_malformed_json_raises_config_error_naming_file(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("{not json")
    with pytest.raises(ConfigError, match=str(tmp_path / MANIFEST_NAME)):
        load_project_manifest(tmp_path)


def test_non_object_document_raises_config_error(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text("[1, 2]")
    with pytest.raises(ConfigError, match=MANIFEST_NAME):
        load_project_manifest(tmp_path)


def test_non_list_modules_raises_config_error(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"modules": "a.b"}))
    with pytest.raises(ConfigError, match="list of module paths"):
        load_project_manifest(tmp_path)


def test_non_string_entries_raise_config_error(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"modules": ["ok", 7]}))
    with pytest.raises(ConfigError, match="list of module paths"):
        load_project_manifest(tmp_path)


def test_discover_from_manifest_registers_module_workflows(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name)
    manifest = ProjectManifest(modules=(name,))
    found = discover_from_manifest(manifest)
    assert set(found) == {f"{name}.Leaf"}
    assert list(registry.all()) == [f"{name}.Leaf"]


def test_discover_from_manifest_source_label_names_manifest(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name)
    discover_from_manifest(ProjectManifest(modules=(name,)))
    with pytest.raises(ConfigError, match=f"project manifest.*{name}"):
        discover_from_manifest(ProjectManifest(modules=(name,)))


def test_discover_from_manifest_broken_module_raises_in_strict_mode(tmp_path, monkeypatch):
    name = _module_name()
    (tmp_path / f"{name}.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ConfigError, match="boom"):
        discover_from_manifest(ProjectManifest(modules=(name,)))


def test_discover_from_manifest_broken_module_skipped_when_tolerant(tmp_path, monkeypatch):
    broken, good = _module_name(), _module_name()
    (tmp_path / f"{broken}.py").write_text("raise RuntimeError('boom')\n")
    _write_module(tmp_path, monkeypatch, good)
    monkeypatch.syspath_prepend(str(tmp_path))
    manifest = ProjectManifest(modules=(broken, good))
    assert discover_from_manifest(manifest, strict=False).keys() == {f"{good}.Leaf"}


def test_empty_manifest_discovers_nothing():
    assert discover_from_manifest(ProjectManifest(modules=())) == {}
