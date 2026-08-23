"""Tests for provenance records, run metadata, and path joining."""

from dataclasses import FrozenInstanceError

import pytest

from ngen_weave.provenance import (
    PROVENANCE_VERSION,
    ProvenanceRecord,
    RunMetadata,
    join_path,
)


@pytest.fixture
def record() -> ProvenanceRecord:
    return ProvenanceRecord(
        version=PROVENANCE_VERSION,
        run_id="run-1",
        node_path=join_path("examples.code_review.workflows.CodeReview", "draft"),
        kind="node_activation",
        ts="2025-01-01T00:00:00+00:00",
        payload={"status": "ok"},
    )


def test_record_construction(record: ProvenanceRecord):
    assert record.version == PROVENANCE_VERSION
    assert record.run_id == "run-1"
    assert record.kind == "node_activation"
    assert record.payload == {"status": "ok"}


def test_record_frozen(record: ProvenanceRecord):
    with pytest.raises(FrozenInstanceError):
        record.run_id = "run-2"  # type: ignore[misc]


def test_metadata_frozen():
    meta = RunMetadata(
        iterations=3,
        tokens_in_context=100,
        tokens_total=250,
        cost_usd=0.01,
        elapsed_ms=1500,
        last_output_valid=True,
    )
    assert meta.iterations == 3
    with pytest.raises(FrozenInstanceError):
        meta.cost_usd = 99.0  # type: ignore[misc]


def test_join_path_two_levels():
    assert join_path("a.b.C", "D") == "a.b.C.D"


def test_join_path_depth_two_nesting():
    root = "examples.code_review.workflows.CodeReview"
    assert join_path(root, "Inner", "Gate") == f"{root}.Inner.Gate"


def test_join_path_single_segment():
    assert join_path("X") == "X"


def test_payload_defaults_to_empty_dict():
    rec = ProvenanceRecord(
        version=1,
        run_id="r",
        node_path="p",
        kind="model_call",
        ts="2025-01-01T00:00:00+00:00",
    )
    assert rec.payload == {}
