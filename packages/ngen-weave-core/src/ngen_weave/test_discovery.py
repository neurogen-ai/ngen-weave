"""Tests for explicit-listing workflow discovery."""

from __future__ import annotations

import itertools
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

from ngen_weave import discovery, registry
from ngen_weave.errors import ConfigError

_SEQ = itertools.count()


def _module_name(prefix: str = "wfmod") -> str:
    return f"{prefix}_{next(_SEQ)}"


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


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


def _write_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    return path


def test_discovers_modules_from_plain_list(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, WORKER_BODY)
    found = discovery.discover([name])
    path = f"{name}.Leaf"
    assert set(found) == {path}
    assert registry.get(path).__module__ == name


def test_same_short_name_in_two_modules_coexists(tmp_path, monkeypatch):
    a, b = _module_name(), _module_name()
    _write_module(tmp_path, monkeypatch, a, WORKER_BODY)
    _write_module(tmp_path, monkeypatch, b, WORKER_BODY)
    found = discovery.discover([a, b])
    assert set(found) == {f"{a}.Leaf", f"{b}.Leaf"}
    assert found[f"{a}.Leaf"] is not found[f"{b}.Leaf"]


def test_module_order_is_preserved_in_registration(tmp_path, monkeypatch):
    a, b = _module_name(), _module_name()
    _write_module(tmp_path, monkeypatch, a, WORKER_BODY)
    _write_module(tmp_path, monkeypatch, b, WORKER_BODY)
    discovery.discover([a, b])
    assert list(registry.all()) == [f"{a}.Leaf", f"{b}.Leaf"]


def test_dotted_module_paths_register(tmp_path, monkeypatch):
    pkg = _module_name("wfmodpkg")
    (tmp_path / pkg).mkdir()
    (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / pkg / "workflows.py").write_text(WORKER_BODY)
    monkeypatch.syspath_prepend(str(tmp_path))
    found = discovery.discover([f"{pkg}.workflows"])
    assert set(found) == {f"{pkg}.workflows.Leaf"}


def test_reexported_classes_do_not_register(tmp_path, monkeypatch):
    a, b = _module_name(), _module_name()
    _write_module(tmp_path, monkeypatch, a, WORKER_BODY)
    _write_module(tmp_path, monkeypatch, b, f"from {a} import Leaf\n")
    found = discovery.discover([b])
    assert found == {}


def test_duplicate_across_sources_names_both(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, WORKER_BODY)
    discovery.discover([name], source="project manifest")
    with pytest.raises(ConfigError, match="project manifest.*entry points"):
        discovery.discover([name], source="entry points")


def test_broken_module_raises_in_strict_mode(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, "raise RuntimeError('boom')\n")
    with pytest.raises(ConfigError, match=name):
        discovery.discover([name])


def test_strict_mode_collects_all_import_failures(tmp_path, monkeypatch):
    first, second = _module_name(), _module_name()
    _write_module(tmp_path, monkeypatch, first, "raise RuntimeError('boom-one')\n")
    _write_module(tmp_path, monkeypatch, second, "raise RuntimeError('boom-two')\n")
    with pytest.raises(ConfigError, match="boom-one.*boom-two"):
        discovery.discover([first, second])


def test_broken_module_skipped_in_tolerant_mode(tmp_path, monkeypatch):
    broken, good = _module_name(), _module_name()
    _write_module(tmp_path, monkeypatch, broken, "raise RuntimeError('boom')\n")
    _write_module(tmp_path, monkeypatch, good, WORKER_BODY)
    found = discovery.discover([broken, good], strict=False)
    assert set(found) == {f"{good}.Leaf"}


def test_structural_error_surfaces_at_import(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(
        tmp_path,
        monkeypatch,
        name,
        "from pydantic import BaseModel\n"
        "from ngen_weave.workflow import Worker\n\n"
        "class In(BaseModel):\n"
        "    text: str\n\n"
        "class Out(BaseModel):\n"
        "    result: str\n\n"
        "class Leaf(Worker):\n"
        "    input_type = In\n"
        "    output_type = Out\n",
    )
    with pytest.raises(ConfigError, match="requires a prompt"):
        discovery.discover([name])


def test_entry_point_module_registers(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, WORKER_BODY)
    ep = EntryPoint(name="workflows", value=name, group=discovery.ENTRY_POINT_GROUP)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: (ep,))
    found = discovery.discover_entry_points()
    assert set(found) == {f"{name}.Leaf"}


def test_overlapping_entry_points_name_both_dists(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, WORKER_BODY)
    eps = (
        SimpleNamespace(name="a", value=name, dist=SimpleNamespace(name="dist-1")),
        SimpleNamespace(name="b", value=name, dist=SimpleNamespace(name="dist-2")),
    )
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: eps)
    with pytest.raises(ConfigError, match="dist-1.*dist-2"):
        discovery.discover_entry_points()


def test_empty_entry_point_group_finds_nothing(monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: ())
    assert discovery.discover_entry_points() == {}


def test_duplicate_within_one_call_raises(tmp_path, monkeypatch):
    name = _module_name()
    _write_module(tmp_path, monkeypatch, name, WORKER_BODY)
    with pytest.raises(ConfigError, match=name):
        discovery.discover([name, name])
