"""End-to-end test of the doc-draft example driven through the Typer CLI.

Runs the composite workflow from examples/doc_draft via `run ... --project`,
with the example's src dir made importable and its fixture files copied into
tmp_path. The quality gate runs in model mode, so the canned reply "true"
makes it pass: the graph routes draft -> quality_gate --pass--> finalize and
completes without any human interrupt. The run file's model_call provenance
must show the haiku variant on the quality gate (bound via ngw.yaml's models
section) and the default variant on draft and finalize.
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

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "doc_draft"
EXAMPLE_SRC = EXAMPLE_DIR / "src"

WORKFLOW = "doc_draft.workflows.DocDraft"


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
    """Route every engine construction to a FakeProvider with canned replies.

    Workers parse their whole reply into the output schema, so Draft's and
    Finalize's replies are full JSON objects; the model-mode QualityGate gets
    the bare token "true", which parse_boolean reads as pass=True.
    """
    from tests.fakes import FakeProvider

    topic = json.loads((EXAMPLE_DIR / "request.json").read_text())["topic"]
    draft_reply = json.dumps({"document": f"A short doc about {topic}.", "topic": topic})
    finalize_reply = json.dumps({"document": f"The final doc about {topic}."})
    provider = FakeProvider(replies=[draft_reply, "true", finalize_reply])
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)
    return provider


def test_doc_draft_run_completes_with_variant_routed_gate(example_project, fake_provider):
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
    sidecar = json.loads((projects / f"{blob.name}.json").read_text())
    assert sidecar["name"] == "document"
    assert sidecar["sha256"] == blob.name
    assert sidecar["node_path"].endswith("Finalize")

    # Provenance: the model-mode gate makes a real model_call record, and each
    # call carries the variant resolved from ngw.yaml's models section — the
    # gate is bound to haiku by class path, the workers use the default.
    run_file = RunStore(root / ".ngen-weave" / "runs").load(sidecar["run_id"])
    model_calls = [r for r in run_file.records if r.kind == "model_call"]
    by_node = {r.node_path.rsplit(".", 1)[-1]: r.payload["variant"] for r in model_calls}
    assert set(by_node) == {"Draft", "QualityGate", "Finalize"}
    assert by_node["QualityGate"] == "haiku"
    assert by_node["Draft"] == "default"
    assert by_node["Finalize"] == "default"

    # The provider saw exactly the same variant split, in call order.
    assert [variant for _, variant in fake_provider.calls] == ["default", "haiku", "default"]
    # The gate actually ran in model mode: its prompt went out rendered with
    # the drafted document, and its reply was the boolean token.
    gate_messages, _ = fake_provider.calls[1]
    assert "quality gate" in gate_messages[0]["content"]
