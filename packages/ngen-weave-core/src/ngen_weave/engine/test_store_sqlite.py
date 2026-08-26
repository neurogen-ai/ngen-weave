"""RunStore over SQLite: round trip, commit boundaries, totals, legacy import."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.provenance import ProvenanceRecord


def _record(run_id: str, seq: int, kind: str, payload: dict) -> ProvenanceRecord:
    return ProvenanceRecord(
        version=1,
        run_id=run_id,
        node_path=f"m.W.node{seq}",
        kind=kind,  # type: ignore[arg-type]
        ts=f"2025-01-01T00:00:{seq:02d}+00:00",
        payload=payload,
    )


def _db_path(runs_dir: Path) -> Path:
    return runs_dir.with_name("runs.db")


class TestRoundTrip:
    def test_create_append_load_round_trip(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {"x": 1})
        record = _record(run_id, 1, "model_call", {"variant": "s", "cost_usd": 0.25})
        store.append(run_id, record)
        loaded = store.load(run_id)

        assert loaded.format == RUN_FILE_FORMAT
        assert loaded.status == "running"
        assert loaded.input == {"x": 1}
        assert loaded.started_at  # stamped at creation
        assert loaded.records == [replace(record, version=1)]
        assert loaded.notes == []

    def test_list_returns_headers_only(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        first = store.create("m.A", {})
        second = store.create("m.B", {"y": 2})
        store.append(first, _record(first, 1, "node_activation", {"status": "ok"}))
        store.save(replace(store.load(first), output={"done": True}, status="completed"))

        runs = store.list()
        assert len(runs) == 2
        by_id = {run.run_id: run for run in runs}
        assert set(by_id) == {first, second}
        # Header-only: records list is empty and untouched; headers are real.
        assert all(run.records == [] for run in runs)
        assert by_id[first].output == {"done": True}
        assert by_id[first].status == "completed"
        assert by_id[second].input == {"y": 2}

    def test_set_status_transitions(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        updated = store.set_status(run_id, "failed")
        assert updated.status == "failed"
        assert updated.records == []
        assert store.load(run_id).status == "failed"

    def test_load_unknown_run_raises(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        with pytest.raises(ConfigError, match="unknown run"):
            store.load("nope")


class TestTotalsAndCommittedVisibility:
    def test_mid_run_kill_yields_exactly_committed_records_ordered(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        appended = [
            _record(run_id, 1, "node_activation", {"status": "ok"}),
            _record(run_id, 2, "model_call", {"variant": "v", "cost_usd": 0.25}),
            _record(run_id, 3, "artifact_write", {"artifact_sha256": "abc"}),
        ]
        for record in appended[:2]:
            store.append(run_id, record)

        # An independent connection sees exactly what committed so far —
        # the crash-consistent window a killed process leaves behind.
        raw = sqlite3.connect(_db_path(tmp_path / "runs"))
        rows = raw.execute(
            "SELECT seq, node_path, kind, payload_json FROM records "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        raw.close()
        observed = [
            (seq, node_path, kind, payload_json) for seq, node_path, kind, payload_json in rows
        ]
        assert observed == [
            (
                1,
                appended[0].node_path,
                "node_activation",
                json.dumps({"status": "ok"}, sort_keys=True),
            ),
            (
                2,
                appended[1].node_path,
                "model_call",
                json.dumps({"variant": "v", "cost_usd": 0.25}, sort_keys=True),
            ),
        ]

        # A fresh store (post-kill reader) sees the same ordered stream,
        # then the third append lands as seq 3 without renumbering anything.
        store.append(run_id, appended[2])
        reloaded = RunStore(tmp_path / "runs").load(run_id)
        assert [r.node_path for r in reloaded.records] == [record.node_path for record in appended]
        assert reloaded.records == [replace(record, version=1) for record in appended]

    def test_append_updates_cost_and_activations(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        store.append(run_id, _record(run_id, 1, "node_activation", {"status": "invalid"}))
        store.append(run_id, _record(run_id, 2, "model_call", {"variant": "v", "cost_usd": 0.10}))
        store.append(run_id, _record(run_id, 3, "model_call", {"variant": "w", "cost_usd": 0.15}))
        store.append(run_id, _record(run_id, 4, "artifact_write", {"artifact_sha256": "x"}))

        conn = sqlite3.connect(_db_path(tmp_path / "runs"))
        cost_usd, activations = conn.execute(
            "SELECT cost_usd, activations FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        conn.close()
        assert cost_usd == pytest.approx(0.25)
        # Exactly one node_activation record was appended; each increments
        # activations once (plan A2 totals rule).
        assert activations == 1


def _legacy_flat_file(tmp_path: Path) -> tuple[str, dict]:
    """Write one v0.1-layout flat file; return its id and on-disk dict."""
    run_id = "legacy-run-id"
    run_file = RunFile(
        format=RUN_FILE_FORMAT,
        run_id=run_id,
        workflow="m.Legacy",
        status="completed",
        input={"text": "hi"},
        output={"echoed": "hi"},
        error=None,
        attempts=1,
        submissions={"m.Legacy.Gate": {"verdict": "approve"}},
        records=[
            _record(run_id, 1, "model_call", {"variant": "s", "cost_usd": 0.05}),
            _record(run_id, 2, "node_activation", {"status": "ok"}),
        ],
    )
    dump = run_file.to_dict()
    dump.pop("started_at")  # not part of the v0.1 layout
    dump.pop("notes")
    legacy_dir = tmp_path / "runs"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / f"{run_id}.json").write_text(json.dumps(dump, indent=2))
    return run_id, dump


class TestLegacyImport:
    def test_imports_once_then_noop(self, tmp_path: Path):
        run_id, dump = _legacy_flat_file(tmp_path)

        store = RunStore(tmp_path / "runs")
        loaded = store.load(run_id)
        assert loaded.workflow == "m.Legacy"
        assert loaded.status == "completed"
        assert loaded.attempts == 1
        assert loaded.submissions == dump["submissions"]
        assert [r.node_path for r in loaded.records] == ["m.W.node1", "m.W.node2"]
        assert [r.kind for r in loaded.records] == ["model_call", "node_activation"]
        assert loaded.output == {"echoed": "hi"}
        # Legacy files lack started_at; falls back to the first record's ts.
        assert loaded.started_at == loaded.records[0].ts

        store.close()
        again = RunStore(tmp_path / "runs")
        runs = again.list()
        assert [r.run_id for r in runs] == [run_id]
        reloaded = again.load(run_id)
        assert reloaded.records == loaded.records
        # Totals survived the import; second init did not double-count them.
        conn = sqlite3.connect(_db_path(tmp_path / "runs"))
        cost_usd, activations = conn.execute(
            "SELECT cost_usd, activations FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        conn.close()
        assert activations == 1
        assert reloaded.status == "completed"

    def test_import_skips_ids_already_in_database(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        existing = store.create("m.Fresh", {"x": 9})
        store.close()
        # Overwrite nothing: an unrelated legacy id coexists with db-native ones.
        run_id, _dump = _legacy_flat_file(tmp_path)
        combined = RunStore(tmp_path / "runs")
        ids = {run.run_id for run in combined.list()}
        assert ids == {existing, run_id}


class TestMalformedLegacy:
    def test_malformed_legacy_file_fails_loudly(self, tmp_path: Path):
        legacy_dir = tmp_path / "runs"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "bad.json").write_text("{not json")
        with pytest.raises(ConfigError, match="cannot import legacy run file"):
            RunStore(tmp_path / "runs")
