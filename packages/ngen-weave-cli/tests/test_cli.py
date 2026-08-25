"""CLI command tests driven through Typer's runner against tmp directories."""

from __future__ import annotations

import json
import textwrap

import pytest
from ngen_weave.engine.store import RunStore
from ngen_weave.provenance import ProvenanceRecord, join_path
from ngen_weave.registry import reset as registry_reset
from ngen_weave_cli.context import reset_merged_registry
from ngen_weave_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

FIXTURE_MODULE = "cli_fixture_workflows"

FIXTURE_SOURCE = textwrap.dedent(
    """
    from pydantic import BaseModel

    from ngen_weave.workflow import Workflow


    class EchoIn(BaseModel):
        text: str


    class EchoOut(BaseModel):
        echoed: str


    class Echo(Workflow):
        description = "test echo workflow"
        human_description = "echoes its input"
        input_type = EchoIn
        output_type = EchoOut

        async def run(self, input, ctx):
            return EchoOut(echoed=input.text)
    """
)

CONFIG_YAML = textwrap.dedent(
    """
    workflow: cli_fixture_workflows.Echo
    params: {}
    run:
      checkpointer: memory
    """
)

INPUT_JSON = '{"text": "hi"}'


@pytest.fixture()
def workflow_module(tmp_path, monkeypatch):
    """Install the fixture workflow module and anchor the CLI at tmp_path."""
    (tmp_path / f"{FIXTURE_MODULE}.py").write_text(FIXTURE_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield FIXTURE_MODULE
    registry_reset()
    reset_merged_registry()


@pytest.fixture()
def fake_provider(monkeypatch):
    """Route every engine construction to a FakeProvider."""
    from tests.fakes import FakeProvider

    provider = FakeProvider()
    monkeypatch.setattr(
        "ngen_weave_cli.context.default_provider", lambda models_file: provider
    )
    return provider


def _write(tmp_path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def test_validate_config_ok(workflow_module, tmp_path):
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    result = runner.invoke(app, ["validate", config])
    assert result.exit_code == 0
    assert "ok" in result.output


def test_validate_config_unknown_workflow(tmp_path):
    config = _write(tmp_path, "ngw.yaml", "workflow: does.not.exist.Nope\n")
    result = runner.invoke(app, ["validate", config])
    assert result.exit_code == 1
    assert "unknown workflow" in result.stderr


def test_validate_module_ok(workflow_module):
    result = runner.invoke(app, ["validate", FIXTURE_MODULE])
    assert result.exit_code == 0
    assert "ok" in result.output


def test_validate_module_broken_definition(workflow_module, tmp_path):
    broken_source = (
        "from pydantic import BaseModel\n"
        "from ngen_weave.workflow import Workflow\n"
        "class I(BaseModel):\n    x: int = 0\n"
        "class O(BaseModel):\n    y: int = 0\n"
        "class Bad(Workflow):\n"
        "    description = 'bad'\n"
        "    input_type = I\n    output_type = O\n"
    )
    (tmp_path / "cli_broken_module.py").write_text(broken_source)
    result = runner.invoke(app, ["validate", "cli_broken_module"])
    assert result.exit_code == 1
    assert "error" in result.stderr


def test_run_completes_and_writes_run_file(workflow_module, fake_provider, tmp_path):
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    result = runner.invoke(
        app, ["run", "cli_fixture_workflows.Echo", "-i", input_file, "-c", config]
    )
    assert result.exit_code == 0, result.output
    assert "status completed" in result.output
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    runs = store.list()
    assert len(runs) == 1
    assert runs[0].output == {"echoed": "hi"}


def test_run_requires_input(workflow_module, fake_provider, tmp_path):
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    result = runner.invoke(app, ["run", "cli_fixture_workflows.Echo", "-c", config])
    assert result.exit_code == 1
    assert "-i" in result.stderr


def test_run_unknown_workflow_fails(workflow_module, fake_provider, tmp_path):
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    result = runner.invoke(app, ["run", "no.such.Module.Nope", "-i", input_file])
    assert result.exit_code == 1
    assert "unknown workflow" in result.stderr


def test_run_config_overrides_positional(workflow_module, fake_provider, tmp_path):
    """-c wins when both the positional name and a config are given."""
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    result = runner.invoke(app, ["run", "stale.OldName", "-i", input_file, "-c", config])
    assert result.exit_code == 0, result.output
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    assert store.list()[0].workflow == "cli_fixture_workflows.Echo"


def test_resume_completed_run_is_noop(workflow_module, fake_provider, tmp_path):
    (tmp_path / "ngen-weave.json").write_text(
        json.dumps({"modules": [FIXTURE_MODULE]})
    )
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    run_result = runner.invoke(app, ["run", "x", "-i", input_file, "-c", config])
    run_id = run_result.output.splitlines()[0].split()[-1]
    result = runner.invoke(app, ["resume", run_id])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert "completed" in result.output


def test_resume_unknown_run_fails(workflow_module, tmp_path):
    result = runner.invoke(app, ["resume", "no-such-run"])
    assert result.exit_code == 1
    assert "unknown run" in result.stderr


def _seed_run(tmp_path, status: str = "running") -> str:
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    run_id = store.create("cli_fixture_workflows.Echo", {"text": "hi"})
    run_file = store.load(run_id)
    run_file.records.append(
        ProvenanceRecord(
            version=1,
            run_id=run_id,
            node_path=join_path("cli_fixture_workflows.Echo"),
            kind="model_call",
            ts="2025-01-01T00:00:00Z",
            payload={"variant": "sonnet", "tokens_total": 150, "cost_usd": 0.15},
        )
    )
    run_file.status = status
    store.save(run_file)
    return run_id


def test_status_prints_cost(workflow_module, tmp_path):
    run_id = _seed_run(tmp_path)
    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "cli_fixture_workflows.Echo" in result.output
    assert "cost_usd 0.150000" in result.output
    assert "waiting-on" not in result.output


def test_status_prints_waiting_on(workflow_module, tmp_path):
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    run_id = _seed_run(tmp_path, status="waiting_human")
    run_file = store.load(run_id)
    run_file.records.append(
        ProvenanceRecord(
            version=1,
            run_id=run_id,
            node_path=join_path("cli_fixture_workflows.Echo", "Review"),
            kind="node_activation",
            ts="2025-01-01T00:00:01Z",
            payload={"status": "waiting_human"},
        )
    )
    store.save(run_file)
    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "waiting-on cli_fixture_workflows.Echo.Review" in result.output


def test_status_unknown_run_fails(workflow_module, tmp_path):
    result = runner.invoke(app, ["status", "no-such-run"])
    assert result.exit_code == 1
    assert "unknown run" in result.stderr


def test_json_input_must_be_object(workflow_module, fake_provider, tmp_path):
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", "[1, 2]")
    result = runner.invoke(app, ["run", "x", "-i", input_file, "-c", config])
    assert result.exit_code == 1
    assert "object" in result.stderr
