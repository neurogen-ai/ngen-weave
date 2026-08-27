"""Review parity: remote JSON resume equals the local YAML artifact resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import make_local_service
from ngen_weave.provenance import ProvenanceRecord
from ngen_weave.workflow import workflow_class_path
from ngen_weave_server.test_local_service import (
    Final,
    Review,
    Root,
    make_human,
    make_review_flow,
    make_worker,
)

RESPONSE = {"verdict": "approve", "notes": ""}
REPLIES = ['{"text":"done"}']

pytestmark = [pytest.mark.conformance]


def _human_flow(name: str):
    """START -> human -> finishing worker on approve."""
    human = make_human(f"ReviewP{name}", Root, Review)
    fin = make_worker(f"FinP{name}", Review, Final, prompt="ok {verdict}")
    return make_review_flow(human, {"approve": fin}, name=f"FlowConP{name}")


def _waiting_artifact(run_file) -> str:
    """Return the artifact path recorded at the most recent waiting activation."""
    for record in reversed(run_file.records):
        if record.kind == "node_activation" and record.payload.get("status") == "waiting_human":
            return record.payload["artifact"]
    raise AssertionError("run never reached waiting_human")


def _normalized(records: list[ProvenanceRecord]) -> list[tuple]:
    """Comparable stream: identity fields only, run-id-bound paths scrubbed.

    Timing is also scrubbed: elapsed_ms inside per-scope metadata is wall-clock,
    so it lives outside the parity guarantee alongside record timestamps.
    """
    rows = []
    for record in records:
        payload = dict(record.payload)
        if "artifact" in payload:
            payload["artifact"] = Path(payload["artifact"]).name
        if isinstance(payload.get("metadata"), dict):
            payload["metadata"] = {
                key: value for key, value in payload["metadata"].items() if key != "elapsed_ms"
            }
        rows.append((record.kind, record.node_path, json.dumps(payload, sort_keys=True)))
    return rows


async def test_json_resume_matches_yaml_artifact_resume(service, tmp_path):
    """Identical response dicts through both submission paths end identical."""
    flow = _human_flow("")
    path = workflow_class_path(flow)
    discovery = {path: flow}

    # JSON half: whichever backend the fixture parametrized over.
    json_svc = service(REPLIES, discovery)
    handle_a = await json_svc.launch(path, Root(text="hi"))
    assert handle_a.status == "waiting_human"
    result_a = await json_svc.resume(handle_a.run_id, payload=RESPONSE)

    # YAML artifact half: submissions live on disk, so a backend that reads
    # artifacts must drive it; LocalRunService owns those tmp dirs alone.
    local = make_local_service(tmp_path / "yaml-half", REPLIES, discovery)
    handle_b = await local.launch(path, Root(text="hi"))
    assert handle_b.status == "waiting_human"
    artifact = Path(_waiting_artifact(await local.status(handle_b.run_id)))
    document = yaml.safe_load(artifact.read_text())
    document["response"] = RESPONSE
    artifact.write_text(yaml.safe_dump(document))
    result_b = await local.resume(handle_b.run_id, payload=None)

    assert result_a.status == "completed"
    assert result_b.status == "completed"
    file_a = await json_svc.status(handle_a.run_id)
    file_b = await local.status(handle_b.run_id)
    assert file_a.output == file_b.output == {"text": "done"}
    assert _normalized(file_a.records) == _normalized(file_b.records)
