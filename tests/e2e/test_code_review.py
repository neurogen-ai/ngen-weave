"""End-to-end test of the code-review example driven through the Typer CLI.

Runs the composite workflow from examples/code_review via `run ... --project`,
with the example's src dir made importable and its fixture files copied into
tmp_path. With FakeProvider's non-empty reply the programmatic gate passes, so
the graph routes draft -> gate --pass--> finalize and completes without any
human interrupt.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from ngen_weave.engine.store import RunStore
from ngen_weave.registry import reset as registry_reset
from ngen_weave_cli.context import reset_merged_registry
from ngen_weave_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "code_review"
EXAMPLE_SRC = EXAMPLE_DIR / "src"

WORKFLOW = "code_review.workflows.CodeReview"


@pytest.fixture()
def example_project(tmp_path, monkeypatch):
    """Copy the example fixtures into tmp_path, make its src importable."""
    for name in ("ngw.yaml", "request.json", "models.json", "ngen-weave.json"):
        shutil.copy(EXAMPLE_DIR / name, tmp_path / name)
    monkeypatch.syspath_prepend(str(EXAMPLE_SRC))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    registry_reset()
    reset_merged_registry()


@pytest.fixture()
def fake_provider(monkeypatch):
    """Route every engine construction to a FakeProvider with JSON replies.

    Workers parse their whole reply into the output schema, so each canned
    reply is a full JSON object: one for Draft, one for Finalize.
    """
    from tests.fakes import FakeProvider

    diff = json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]
    draft_reply = json.dumps({"review": "Looks good overall.", "diff": diff})
    finalize_reply = json.dumps({"reviewed_diff": diff, "verdict": "approve"})
    provider = FakeProvider(replies=[draft_reply, finalize_reply])
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)
    return provider


def test_code_review_run_completes_and_persists_artifact(example_project, fake_provider):
    root = example_project
    result = runner.invoke(
        app,
        [
            "run",
            WORKFLOW,
            "-i",
            str(root / "request.json"),
            "-c",
            str(root / "ngw.yaml"),
            "--project",
            "demo",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status completed" in result.output

    # Exactly one content-addressed blob (plus sidecar) under projects/demo.
    projects = root / ".ngen-weave" / "projects" / "demo"
    blobs = [p for p in projects.iterdir() if not p.name.endswith(".json")]
    assert len(blobs) == 1
    blob = blobs[0]
    expected_hash = hashlib.sha256(blob.read_bytes()).hexdigest()
    assert blob.name == expected_hash

    sidecar = json.loads((projects / f"{expected_hash}.json").read_text())
    assert sidecar["name"] == "reviewed_diff"
    assert sidecar["sha256"] == expected_hash
    assert sidecar["run_id"]
    assert sidecar["node_path"].endswith("Finalize")
    assert isinstance(sidecar["input_hashes"], dict) and sidecar["input_hashes"]

    # Provenance: one artifact_write from the declaring Finalize leaf, model
    # calls on both workers. (The composite root declares no artifacts, so no
    # root-scope artifact_write is emitted.)
    run_file = RunStore(root / ".ngen-weave" / "runs").load(sidecar["run_id"])
    writes = [r for r in run_file.records if r.kind == "artifact_write"]
    assert len(writes) == 1
    assert writes[0].node_path == f"{WORKFLOW}.code_review.workflows.Finalize", writes[0].node_path

    model_calls = [r for r in run_file.records if r.kind == "model_call"]
    call_paths = [r.node_path for r in model_calls]
    assert any(p.endswith("Draft") for p in call_paths)
    assert any(p.endswith("Finalize") for p in call_paths)


def test_code_review_kill_and_resume_at_human_review(example_project):
    """Kill-and-resume across CLI invocations at the human review node.

    The FakeProvider returns an empty draft review so Gate.decide fails and
    the graph routes to HumanReview, parking the run as waiting_human (the
    CLI exits 1). The copied config is switched to the sqlite checkpointer so
    checkpoint state survives in tmp_path between invocations; a fresh process
    is simulated by resetting the merged registry cache and rebuilding the
    engine through a second CLI invocation.
    """
    from tests.fakes import FakeProvider

    root = example_project
    diff = json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]
    draft_reply = json.dumps({"review": "", "diff": diff})  # empty review -> gate fails
    finalize_reply = json.dumps({"reviewed_diff": diff, "verdict": "approve"})
    provider = FakeProvider(replies=[draft_reply, finalize_reply])
    monkeypatch_target = "ngen_weave_cli.context.default_provider"

    # Force the interrupt path and make checkpoint state survive on disk.
    config_file = root / "ngw.yaml"
    config_file.write_text(
        config_file.read_text().replace("checkpointer: memory", "checkpointer: sqlite")
    )

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(monkeypatch_target, lambda models_file: provider)
        first = runner.invoke(
            app,
            [
                "run",
                WORKFLOW,
                "-i",
                str(root / "request.json"),
                "-c",
                str(config_file),
                "--project",
                "demo",
            ],
        )
        assert first.exit_code == 1, first.output
        assert "status waiting_human" in first.output
        prefix = "run "
        run_id = next(
            line[len(prefix) :] for line in first.output.splitlines() if line.startswith(prefix)
        )
        assert run_id
    finally:
        monkeypatch.undo()

    # Fresh process: reset caches and rebuild everything through the CLI again.
    reset_merged_registry()
    registry_reset()
    response_file = root / "response.json"
    response_file.write_text(json.dumps({"verdict": "approve", "notes": "lgtm"}))
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(monkeypatch_target, lambda models_file: provider)
        second = runner.invoke(
            app, ["resume", run_id, "-p", str(response_file), "--project", "demo"]
        )
        assert second.exit_code == 0, second.output
        assert "status completed" in second.output
    finally:
        monkeypatch.undo()

    projects = root / ".ngen-weave" / "projects" / "demo"
    blobs = [p for p in projects.iterdir() if not p.name.endswith(".json")]
    assert len(blobs) == 1
    blob = blobs[0]
    sidecar = json.loads((projects / f"{blob.name}.json").read_text())
    assert sidecar["name"] == "reviewed_diff"
    assert sidecar["run_id"] == run_id
    assert json.loads(blob.read_text()) == diff
