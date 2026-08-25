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
