"""RunService protocol types: summary projection, filters, unknown-run error."""

from __future__ import annotations

from pathlib import Path

import pytest

from ngen_weave.engine.state import RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.provenance import ProvenanceRecord
from ngen_weave.service import RunFilters, RunSummary, UnknownRunError, summaries


def _record(run_id: str, seq: int, kind: str, payload: dict) -> ProvenanceRecord:
    return ProvenanceRecord(
        version=1,
        run_id=run_id,
        node_path=f"m.W.node{seq}",
        kind=kind,  # type: ignore[arg-type]
        ts=f"2025-01-01T00:00:{seq:02d}+00:00",
        payload=payload,
    )


def _file(
    run_id: str,
    status: str = "running",
    started_at: str = "2025-01-01T00:00:00+00:00",
    records: list[ProvenanceRecord] | None = None,
) -> RunFile:
    return RunFile(
        format=1,
        run_id=run_id,
        workflow="m.Workflow",
        status=status,  # type: ignore[arg-type]
        input={},
        output=None,
        error=None,
        attempts=0,
        submissions={},
        started_at=started_at,
        notes=[],
        records=records if records is not None else [],
    )


class TestSummariesProjection:
    def test_cost_sums_model_call_records_only(self):
        records = [
            _record("r1", 1, "node_activation", {"status": "running"}),
            _record("r1", 2, "model_call", {"variant": "s", "cost_usd": 0.25}),
            _record("r1", 3, "model_call", {"variant": "s", "cost_usd": 0.10}),
            _record("r1", 4, "artifact_write", {"cost_usd": 999}),  # non-model_call cost ignored
            _record("r1", 5, "model_call", {"variant": "s"}),  # missing cost counts as 0
        ]
        summary = summaries([_file("r1", records=records)])[0]
        assert isinstance(summary, RunSummary)
        assert summary.cost_usd == pytest.approx(0.35)

    def test_projection_fields(self):
        file = _file("r1", status="completed")
        [summary] = summaries([file])
        assert (
            summary.run_id == "r1"
            and summary.workflow == "m.Workflow"
            and summary.status == "completed"
            and summary.started_at == "2025-01-01T00:00:00+00:00"
            and summary.cost_usd == 0.0
            and summary.waiting_on_human is False
        )

    def test_waiting_on_human_flag_follows_status(self):
        waiting = summaries([_file("w", status="waiting_human")])[0]
        running = summaries([_file("r", status="running")])[0]
        assert waiting.waiting_on_human is True
        assert running.waiting_on_human is False

    def test_started_at_falls_back_to_first_record_ts_for_legacy_imports(self):
        file = _file("legacy", started_at="", records=[_record("legacy", 7, "model_call", {})])
        [summary] = summaries([file])
        assert summary.started_at == "2025-01-01T00:00:07+00:00"

    def test_empty_input_yields_empty_list(self):
        assert summaries([]) == []


class TestRunFilters:
    def _summaries(self) -> list[RunSummary]:
        files = [
            _file("a", status="waiting_human"),
            _file("b", status="completed"),
            _file("c", status="failed"),
        ]
        return summaries(files)

    def test_no_filters_match_everything(self):
        all_summaries = self._summaries()
        for summary in all_summaries:
            assert RunFilters().matches(summary)
            assert RunFilters(workflow=None, status=None).matches(summary)

    def test_workflow_filter_selects_matching_runs(self):
        matches = [
            s.run_id for s in self._summaries() if RunFilters(workflow="m.Workflow").matches(s)
        ]
        mismatches = [
            s.run_id for s in self._summaries() if RunFilters(workflow="other.Workflow").matches(s)
        ]
        assert matches == ["a", "b", "c"]
        assert mismatches == []

    def test_status_filter_selects_matching_status(self):
        by_filters = {
            status: [
                s.run_id
                for s in self._summaries()
                if RunFilters(status=status).matches(s)  # type: ignore[arg-type]
            ]
            for status in ("waiting_human", "completed", "cancelled")
        }
        assert by_filters["waiting_human"] == ["a"]
        assert by_filters["completed"] == ["b"]
        assert by_filters["cancelled"] == []

    def test_combined_filters_must_both_match(self):
        assert RunFilters(workflow="m.Workflow", status="completed").matches(self._summaries()[1])
        assert not RunFilters(workflow="m.Workflow", status="cancelled").matches(
            self._summaries()[0]
        )


class TestStoreLoadUnknownRun:
    def test_load_bogus_id_raises_unknown_run_error(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        store.create("m.W", {})
        with pytest.raises(UnknownRunError, match="unknown run"):
            store.load("no-such-run")

    def test_unknown_run_error_is_a_key_error(self):
        assert issubclass(UnknownRunError, KeyError)
