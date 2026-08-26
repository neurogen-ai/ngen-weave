"""CLI command tests driven through Typer's runner against tmp directories."""

from __future__ import annotations

import json
import sys
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
        artifacts = ("echoed",)

        async def run(self, input, ctx):
            return EchoOut(echoed=input.text)
    """
)

REVIEW_SOURCE = textwrap.dedent(
    """
    from enum import Enum

    from pydantic import BaseModel

    from ngen_weave.workflow import END, START, Human, Workflow


    class ReviewIn(BaseModel):
        text: str


    class Verdict(str, Enum):
        approve = "approve"
        reject = "reject"


    class ReviewState(BaseModel):
        verdict: Verdict
        notes: str


    class Approve(Human):
        description = "test review"
        human_description = "approve or reject"
        input_type = ReviewIn
        output_type = ReviewState
        state_type = ReviewState


    class ReviewFlow(Workflow):
        description = "test review flow"
        human_description = "runs one review"
        input_type = ReviewIn
        output_type = ReviewState

        def build(self, g):
            g.add_node(Approve)
            g.add_edge(START, Approve)
            g.add_edge(Approve, END)
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
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)
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


def test_run_persists_declared_artifact_under_project(workflow_module, fake_provider, tmp_path):
    import hashlib
    import json as jsonlib

    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    result = runner.invoke(
        app,
        ["run", "cli_fixture_workflows.Echo", "-i", input_file, "-c", config, "--project", "demo"],
    )
    assert result.exit_code == 0, result.output
    projects = tmp_path / ".ngen-weave" / "projects" / "demo"
    blobs = [
        p
        for p in projects.iterdir()
        if not p.name.endswith(".json") and not p.name.endswith(".tmp")
    ]
    assert len(blobs) == 1
    expected_hash = hashlib.sha256(jsonlib.dumps("hi", sort_keys=True).encode()).hexdigest()
    assert blobs[0].name == expected_hash
    assert blobs[0].read_text() == '"hi"'
    sidecar = jsonlib.loads((projects / f"{expected_hash}.json").read_text())
    assert sidecar["name"] == "echoed"
    assert sidecar["sha256"] == expected_hash
    assert sidecar["run_id"]
    assert sidecar["node_path"] == "cli_fixture_workflows.Echo"
    assert set(sidecar["input_hashes"]) == {"text"}

    run_id = sidecar["run_id"]
    run_file = RunStore(tmp_path / ".ngen-weave" / "runs").load(run_id)
    writes = [r for r in run_file.records if r.kind == "artifact_write"]
    # The root workflow and its single leaf both declare artifacts, so both
    # scopes emit one artifact_write each.
    assert len(writes) == 2
    assert {w.node_path for w in writes} == {
        "cli_fixture_workflows.Echo",
        "cli_fixture_workflows.Echo.cli_fixture_workflows.Echo",
    }
    assert all(w.payload["artifact_sha256"] == expected_hash for w in writes)


def test_run_without_project_drops_artifacts(workflow_module, fake_provider, tmp_path):
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", INPUT_JSON)
    result = runner.invoke(
        app, ["run", "cli_fixture_workflows.Echo", "-i", input_file, "-c", config]
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".ngen-weave" / "projects").exists()


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
    (tmp_path / "ngen-weave.json").write_text(json.dumps({"modules": [FIXTURE_MODULE]}))
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


def _seed_waiting_review(tmp_path) -> str:
    """Seed a waiting_human run parked on the fixture review's Approve node."""
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    run_id = store.create("cli_fixture_workflows.ReviewFlow", {"text": "hi"})
    run_file = store.load(run_id)
    run_file.records.append(
        ProvenanceRecord(
            version=1,
            run_id=run_id,
            # mirrors the engine: parent class path joined with the human's own class path
            node_path="cli_fixture_workflows.ReviewFlow.cli_fixture_workflows.Approve",
            kind="node_activation",
            ts="2025-01-01T00:00:00Z",
            payload={"status": "waiting_human", "artifact": "review.yaml"},
        )
    )
    run_file.status = "waiting_human"
    store.save(run_file)
    return run_id


def _install_review_module(tmp_path, monkeypatch):
    (tmp_path / f"{FIXTURE_MODULE}.py").write_text(FIXTURE_SOURCE + "\n" + REVIEW_SOURCE)
    (tmp_path / "ngen-weave.json").write_text(json.dumps({"modules": [FIXTURE_MODULE]}))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # earlier tests may have imported the Echo-only version of the module
    monkeypatch.delitem(sys.modules, FIXTURE_MODULE, raising=False)


def test_resume_invalid_enum_payload_aborts_with_field_report(
    tmp_path, monkeypatch, workflow_module
):
    _install_review_module(tmp_path, monkeypatch)
    run_id = _seed_waiting_review(tmp_path)
    response = _write(tmp_path, "response.json", '{"verdict": "bogus", "notes": "x"}')

    result = runner.invoke(app, ["resume", run_id, "-p", response])

    assert result.exit_code == 1, result.output
    assert "verdict:" in result.stderr
    assert "Fix the fields above and resubmit." not in result.stderr
    store = RunStore(tmp_path / ".ngen-weave" / "runs")
    retries = [
        r
        for r in store.load(run_id).records
        if r.kind == "node_activation" and r.payload.get("status") == "retry"
    ]
    assert retries == []  # abort happens once; no retry records land


def test_resume_missing_required_field_aborts_with_field_name(
    tmp_path, monkeypatch, workflow_module
):
    _install_review_module(tmp_path, monkeypatch)
    run_id = _seed_waiting_review(tmp_path)
    response = _write(tmp_path, "response.json", '{"notes": "no verdict given"}')

    result = runner.invoke(app, ["resume", run_id, "-p", response])

    assert result.exit_code == 1, result.output
    assert "verdict" in result.stderr


def test_run_invalid_input_json_hard_fails_with_field_report(
    tmp_path, monkeypatch, workflow_module
):
    _install_review_module(tmp_path, monkeypatch)
    config = _write(tmp_path, "ngw.yaml", CONFIG_YAML)
    input_file = _write(tmp_path, "input.json", "{}")

    result = runner.invoke(app, ["run", "x", "-i", input_file, "-c", config])

    assert result.exit_code == 1, result.output
    assert "text:" in result.stderr  # field line from the shared formatter
    assert "does not match" in result.stderr
    assert "Fix the fields above and resubmit." in result.stderr
