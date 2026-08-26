"""Canonical run-JSON export bytes and their round trip through load_run_json."""

from __future__ import annotations

import json

from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile
from ngen_weave.export import dump_run_json, load_run_json
from ngen_weave.provenance import ProvenanceRecord

RUN_ID = "fixed-run-id"


def _fixture_run() -> RunFile:
    return RunFile(
        format=RUN_FILE_FORMAT,
        run_id=RUN_ID,
        workflow="m.W",
        status="completed",
        input={"text": "hi"},
        output={"echoed": "hi"},
        error=None,
        attempts=2,
        submissions={"m.W.Gate": {"verdict": "approve", "notes": "fine"}},
        started_at="2025-03-01T12:00:00+00:00",
        notes=["watched by ops"],
        records=[
            ProvenanceRecord(
                version=1,
                run_id=RUN_ID,
                node_path="m.W.draft",
                kind="model_call",
                ts="2025-03-01T12:00:01+00:00",
                payload={"variant": "sonnet", "tokens_total": 150, "cost_usd": 0.15},
            ),
            ProvenanceRecord(
                version=1,
                run_id=RUN_ID,
                node_path="m.W.Gate",
                kind="node_activation",
                ts="2025-03-01T12:00:02+00:00",
                payload={"status": "waiting_human"},
            ),
        ],
    )


# Recorded fixture: exactly the v0.1 key set plus started_at and notes.
# Pre-1.0 this byte format may reshape; no backwards-compat promise.
RECORDED_FIXTURE = """\
{
  "attempts": 2,
  "error": null,
  "format": 1,
  "input": {
    "text": "hi"
  },
  "notes": [
    "watched by ops"
  ],
  "output": {
    "echoed": "hi"
  },
  "records": [
    {
      "kind": "model_call",
      "node_path": "m.W.draft",
      "payload": {
        "cost_usd": 0.15,
        "tokens_total": 150,
        "variant": "sonnet"
      },
      "run_id": "fixed-run-id",
      "ts": "2025-03-01T12:00:01+00:00",
      "version": 1
    },
    {
      "kind": "node_activation",
      "node_path": "m.W.Gate",
      "payload": {
        "status": "waiting_human"
      },
      "run_id": "fixed-run-id",
      "ts": "2025-03-01T12:00:02+00:00",
      "version": 1
    }
  ],
  "run_id": "fixed-run-id",
  "started_at": "2025-03-01T12:00:00+00:00",
  "status": "completed",
  "submissions": {
    "m.W.Gate": {
      "notes": "fine",
      "verdict": "approve"
    }
  },
  "workflow": "m.W"
}"""


def test_dump_emits_recorded_fixture_bytes():
    assert dump_run_json(_fixture_run()) == RECORDED_FIXTURE.encode("utf-8")


def test_exported_keys_are_v01_plus_started_at_and_notes():
    parsed = json.loads(dump_run_json(_fixture_run()))
    assert set(parsed) == {
        "format",
        "run_id",
        "workflow",
        "status",
        "input",
        "output",
        "error",
        "attempts",
        "submissions",
        "records",
        "started_at",
        "notes",
    }


def test_dump_load_round_trip():
    run = _fixture_run()
    assert load_run_json(dump_run_json(run)) == run


def test_load_tolerates_missing_defaulted_keys():
    run = _fixture_run()
    dump = json.loads(dump_run_json(run))
    for absent in ("output", "error", "attempts", "submissions", "started_at", "notes"):
        dump.pop(absent)
    loaded = load_run_json(json.dumps(dump).encode())
    assert loaded.output is None
    assert loaded.error is None
    assert loaded.attempts == 0
    assert loaded.submissions == {}
    assert loaded.started_at == ""
    assert loaded.notes == []
    assert loaded.records == run.records


def test_dump_is_deterministic_regardless_of_payload_key_order():
    parsed = json.loads(dump_run_json(_fixture_run()))
    shuffled = dict(parsed)
    # Rebuild every nested payload with reversed key insertion order.
    shuffled["input"] = dict(reversed(list(parsed["input"].items())))
    shuffled["submissions"] = {
        node: dict(reversed(list(payload.items())))
        for node, payload in parsed["submissions"].items()
    }
    shuffled["records"] = [dict(reversed(list(rec.items()))) for rec in parsed["records"]]
    # Key insertion order must not leak into the canonical bytes.
    assert dump_run_json(RunFile.from_dict(shuffled)) == dump_run_json(_fixture_run())


def test_load_rejects_non_object_json():
    import pytest

    with pytest.raises(ValueError):
        load_run_json(b"[1, 2]")
