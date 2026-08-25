"""End-to-end provenance-addressability test for success criterion 4.

Runs both examples (code_review and doc_draft) through the Typer CLI with
FakeProvider so the gates pass and each run completes, then loads the run
file from .ngen-weave/runs/<run-id>/ and asserts that every executed node is
addressable by (run_id, node_path), carries the frozen six-field RunMetadata,
and that no unknown record kind ever appears.
"""

from __future__ import annotations

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

EXAMPLES_ROOT = Path(__file__).resolve().parents[2] / "examples"

METADATA_FIELDS = {
    "iterations",
    "tokens_in_context",
    "tokens_total",
    "cost_usd",
    "elapsed_ms",
    "last_output_valid",
}
KNOWN_KINDS = {"node_activation", "model_call", "artifact_write"}


def _invoke(root: Path, workflow: str) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            workflow,
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


def _assert_provenance_invariants(runs_dir: Path) -> None:
    store = RunStore(runs_dir)
    run_files = store.list()
    assert len(run_files) == 1
    records = run_files[0].records

    # No record kinds outside the frozen v0.1 set.
    kinds = {r.kind for r in records}
    assert kinds <= KNOWN_KINDS

    # Every ok activation: unique non-empty node_path, six-field metadata.
    seen_paths: set[str] = set()
    activation_paths: set[str] = set()
    for record in records:
        if record.kind == "node_activation" and record.payload.get("status") == "ok":
            assert record.run_id == run_files[0].run_id
            assert record.node_path
            assert record.node_path not in seen_paths
            seen_paths.add(record.node_path)
            activation_paths.add(record.node_path)
            metadata = record.payload.get("metadata")
            assert metadata is not None, record.node_path
            assert set(metadata.keys()) == METADATA_FIELDS
            assert isinstance(metadata["iterations"], int) and metadata["iterations"] >= 1
            assert isinstance(metadata["cost_usd"], (int, float))
            assert isinstance(metadata["elapsed_ms"], int) and metadata["elapsed_ms"] >= 0
            assert metadata["last_output_valid"] in (True, None)

    # Every model_call sits at a path that also has an ok node_activation.
    for record in records:
        if record.kind == "model_call":
            assert record.node_path, record.payload
            assert record.node_path in activation_paths, record.node_path


@pytest.fixture()
def example_env(tmp_path, monkeypatch):
    """Copy an example's fixtures into tmp_path and make its src importable."""

    def _install(name: str) -> Path:
        example_dir = EXAMPLES_ROOT / name
        for f in ("ngw.yaml", "request.json", "models.json", "ngen-weave.json"):
            shutil.copy(example_dir / f, tmp_path / f)
        monkeypatch.syspath_prepend(str(example_dir / "src"))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    yield _install
    registry_reset()
    reset_merged_registry()


def _fake_provider(monkeypatch, replies: list[str]) -> None:
    from tests.fakes import FakeProvider

    provider = FakeProvider(replies=replies)
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)


def test_code_review_provenance_addressable_by_node_path(example_env, monkeypatch):
    root = example_env("code_review")
    diff = json.loads((root / "request.json").read_text())["diff"]
    _fake_provider(
        monkeypatch,
        [
            json.dumps({"review": "Looks good overall.", "diff": diff}),
            json.dumps({"reviewed_diff": diff, "verdict": "approve"}),
        ],
    )
    _invoke(root, "code_review.workflows.CodeReview")
    _assert_provenance_invariants(root / ".ngen-weave" / "runs")


def test_doc_draft_provenance_addressable_by_node_path(example_env, monkeypatch):
    root = example_env("doc_draft")
    topic = json.loads((root / "request.json").read_text())["topic"]
    _fake_provider(
        monkeypatch,
        [
            json.dumps({"document": f"A short doc about {topic}.", "topic": topic}),
            "true",
            json.dumps({"document": f"The final doc about {topic}."}),
        ],
    )
    _invoke(root, "doc_draft.workflows.DocDraft")
    _assert_provenance_invariants(root / ".ngen-weave" / "runs")
