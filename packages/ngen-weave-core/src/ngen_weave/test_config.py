"""Tests for run configuration loading."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from ngen_weave.config import ResolvedConfig, RunSettings, load_config
from ngen_weave.errors import ConfigError
from ngen_weave.registry import all as registry_all
from ngen_weave.workflow import Workflow

MODELS_JSON = {
    "defaultVariant": "sonnet",
    "variants": {"sonnet": {"model": "test/sonnet"}, "haiku": {"model": "test/haiku"}},
}


class ProbeIn(BaseModel):
    text: str


class ProbeOut(BaseModel):
    result: str


class Probe(Workflow):
    """Minimal concrete leaf satisfying import-time validation."""

    input_type = ProbeIn
    output_type = ProbeOut

    def run(self, input, ctx):  # noqa: ARG002
        return ProbeOut(result="x")


@pytest.fixture()
def env(tmp_path: Path) -> Path:
    (tmp_path / "models.json").write_text(json.dumps(MODELS_JSON))
    return tmp_path


def write_config(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_yaml_config_loads_with_defaults(env: Path) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\n",
    )
    cfg = load_config(cfg_path, registry_all())
    assert isinstance(cfg, ResolvedConfig)
    assert cfg.workflow is Probe
    assert cfg.params == {}
    assert cfg.models == {}
    assert cfg.run == RunSettings()


def test_json_matches_yaml(env: Path) -> None:
    body = {
        "workflow": f"{Probe.__module__}.Probe",
        "params": {"threshold": 0.5},
        "models": {f"{Probe.__module__}.Probe": "haiku"},
        "run": {"checkpointer": "memory", "max_retries": 2},
    }
    yaml_path = write_config(env, "a.yaml", json.dumps(body))
    json_path = write_config(env, "b.json", json.dumps(body))
    assert load_config(yaml_path, registry_all()) == load_config(json_path, registry_all())


def test_models_file_resolves_relative_to_config_dir(tmp_path: Path) -> None:
    sub = tmp_path / "configs"
    sub.mkdir()
    (tmp_path / "models.json").write_text(json.dumps(MODELS_JSON))
    cfg_path = write_config(
        sub,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\n"
        f"models_file: ../models.json\n"
        f"models:\n  {Probe.__module__}.Probe: haiku\n",
    )
    cfg = load_config(cfg_path, registry_all())
    assert cfg.models_file == tmp_path / "models.json"
    assert cfg.models == {f"{Probe.__module__}.Probe": "haiku"}


def test_unknown_workflow_fails(env: Path) -> None:
    cfg_path = write_config(env, "ngw.yaml", "workflow: no.such.Workflow\n")
    with pytest.raises(ConfigError, match="unknown workflow"):
        load_config(cfg_path, registry_all())


def test_unknown_top_level_key_fails(env: Path) -> None:
    cfg_path = write_config(env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nmodelz: {{}}\n")
    with pytest.raises(ConfigError, match="unknown keys.*modelz"):
        load_config(cfg_path, registry_all())


def test_unknown_run_key_fails(env: Path) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nrun:\n  checkpointers: sqlite\n",
    )
    with pytest.raises(ConfigError, match="unknown run keys"):
        load_config(cfg_path, registry_all())


def test_bad_checkpointer_value_fails(env: Path) -> None:
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nrun:\n  checkpointer: redis\n"
    )
    with pytest.raises(ConfigError, match="checkpointer"):
        load_config(cfg_path, registry_all())


def test_binding_to_unknown_variant_fails(env: Path) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nmodels:\n  {Probe.__module__}.Probe: turbo\n",
    )
    with pytest.raises(ConfigError, match="turbo"):
        load_config(cfg_path, registry_all())


def test_binding_to_unknown_class_fails(env: Path) -> None:
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nmodels:\n  no.Such: haiku\n"
    )
    with pytest.raises(ConfigError, match="unknown workflow"):
        load_config(cfg_path, registry_all())


def test_bindings_without_models_file_fail(tmp_path: Path) -> None:
    cfg_path = write_config(
        tmp_path,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nmodels:\n  {Probe.__module__}.Probe: haiku\n",
    )
    with pytest.raises(ConfigError, match="models.json not found"):
        load_config(cfg_path, registry_all())


# --- run.budget parsing -------------------------------------------------------


def test_budget_cost_usd_parses(env: Path) -> None:
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n    cost_usd: 1.5\n"
    )
    cfg = load_config(cfg_path, registry_all())
    assert cfg.run.budget is not None
    assert cfg.run.budget.cost_usd == 1.5
    assert cfg.run.budget.steps is None


def test_budget_steps_parses_as_int(env: Path) -> None:
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n    steps: 10\n"
    )
    cfg = load_config(cfg_path, registry_all())
    assert cfg.run.budget.steps == 10
    assert cfg.run.budget.cost_usd is None


def test_budget_both_fields_parse(env: Path) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n    cost_usd: 2\n    steps: 5\n",
    )
    cfg = load_config(cfg_path, registry_all())
    assert cfg.run.budget.cost_usd == 2.0
    assert cfg.run.budget.steps == 5


def test_budget_without_any_limit_fails(env: Path) -> None:
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nrun:\n  budget: {{}}\n"
    )
    with pytest.raises(ConfigError, match="at least one"):
        load_config(cfg_path, registry_all())


def test_null_budget_is_not_valid(env: Path) -> None:
    # run.budget present but empty means an empty mapping in YAML? No: bare
    # "budget:" parses as None, which is not a mapping and must be rejected
    # like any malformed config value.
    cfg_path = write_config(
        env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n"
    )
    with pytest.raises(ConfigError, match="run.budget must be a mapping"):
        load_config(cfg_path, registry_all())


def test_budget_unknown_key_fails(env: Path) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n    tokens: 99\n",
    )
    with pytest.raises(ConfigError, match="unknown run.budget keys.*tokens"):
        load_config(cfg_path, registry_all())


@pytest.mark.parametrize("field", ["cost_usd", "steps"])
@pytest.mark.parametrize("bad", [0, -3, True])
def test_budget_nonpositive_values_fail(env: Path, field: str, bad) -> None:
    cfg_path = write_config(
        env,
        "ngw.yaml",
        f"workflow: {Probe.__module__}.Probe\nrun:\n  budget:\n    {field}: {json.dumps(bad)}\n",
    )
    with pytest.raises(ConfigError, match=field):
        load_config(cfg_path, registry_all())


def test_no_budget_block_yields_none(env: Path) -> None:
    cfg_path = write_config(env, "ngw.yaml", f"workflow: {Probe.__module__}.Probe\n")
    cfg = load_config(cfg_path, registry_all())
    assert cfg.run.budget is None
