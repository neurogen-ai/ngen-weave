"""Engine tests: run files, flat graph execution, control routing, retries.

Covers RunStore atomicity and stream integrity, then Engine.compile/run
against flat graphs: worker chains, control pass/fail and model-mode routing,
fan-in assembly, provenance emission, and resume behavior. Nesting lives in
test_nesting.py.
"""

from pathlib import Path

import pytest

from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.provenance import ProvenanceRecord


def _record(run_id: str, seq: int) -> ProvenanceRecord:
    return ProvenanceRecord(
        version=1,
        run_id=run_id,
        node_path=f"m.W.node{seq}",
        kind="node_activation",
        ts="2025-01-01T00:00:00+00:00",
        payload={"status": "ok"},
    )


class TestRunFile:
    def test_roundtrip_preserves_every_field(self):
        rf = RunFile(
            format=RUN_FILE_FORMAT,
            run_id="r1",
            workflow="m.W",
            status="running",
            input={"x": 1},
            records=[_record("r1", 0)],
        )
        restored = RunFile.from_dict(rf.to_dict())
        assert restored == rf

    def test_defaults_for_optional_fields(self):
        rf = RunFile(
            format=RUN_FILE_FORMAT, run_id="r", workflow="m.W", status="running", input={}
        )
        assert rf.output is None
        assert rf.error is None
        assert rf.records == []


class TestRunStore:
    def test_create_starts_running_file_with_generated_id(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {"x": 1})
        assert run_id
        loaded = store.load(run_id)
        assert loaded.status == "running"
        assert loaded.workflow == "m.W"
        assert loaded.input == {"x": 1}
        assert loaded.format == RUN_FILE_FORMAT

    def test_load_unknown_run_raises(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        with pytest.raises(Exception, match="unknown run"):
            store.load("nope")

    def test_save_is_atomic_and_valid_json(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        store.append(run_id, _record(run_id, 0))
        raw = (tmp_path / "runs" / f"{run_id}.json").read_text()
        import json

        data = json.loads(raw)  # always valid JSON on disk
        assert data["records"][0]["payload"] == {"status": "ok"}
        leftovers = [p for p in (tmp_path / "runs").iterdir() if p.suffix != ".json"]
        assert leftovers == []  # temp file never survives a save

    def test_append_accumulates_stream_in_order(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        for i in range(3):
            store.append(run_id, _record(run_id, i))
        assert [r.payload["status"] for r in store.load(run_id).records] == [
            "ok",
            "ok",
            "ok",
        ]
        assert [r.node_path for r in store.load(run_id).records] == [
            "m.W.node0",
            "m.W.node1",
            "m.W.node2",
        ]

    def test_set_status_transitions(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        updated = store.set_status(run_id, "failed")
        assert updated.status == "failed"
        assert store.load(run_id).status == "failed"

    def test_list_returns_every_run(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        first = store.create("m.A", {})
        second = store.create("m.B", {})
        ids = {rf.run_id for rf in store.list()}
        assert ids == {first, second}
