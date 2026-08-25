"""Provenance records and run metadata."""

from dataclasses import dataclass, field
from typing import Literal

PROVENANCE_VERSION = 1

# v0.2 widens Kind with "budget_exhausted" and "observer_firing"
Kind = Literal["node_activation", "model_call", "artifact_write"]


@dataclass(frozen=True)
class ProvenanceRecord:
    """One provenance event in its versioned envelope.

    Attributes:
        version: Envelope version, always PROVENANCE_VERSION.
        run_id: Identifier of the run that emitted this record.
        node_path: Dot path of class paths, root workflow first, e.g.
            "examples.code_review.workflows.CodeReview.draft".
        kind: Event type; payload contents are defined where the record is emitted.
        ts: UTC ISO-8601 timestamp.
        payload: Kind-typed event details.
    """

    version: int  # always PROVENANCE_VERSION
    run_id: str
    node_path: str
    kind: Kind
    ts: str  # UTC ISO-8601
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunMetadata:
    """Frozen six-field summary of a scope's activity, computed by the engine.

    The surface is frozen for v0.1. The engine emits it inside each composite's
    node_activation payload; v0.2 supervision reads exactly this. iterations
    counts node activations on the scope's path;
    token and cost fields sum model_call payloads; last_output_valid reflects
    the most recent activation's validation result.

    Attributes:
        iterations: Number of node activations so far in the scope.
        tokens_in_context: Summed input-token count across model calls.
        tokens_total: Summed total-token count across model calls.
        cost_usd: Summed cost in USD across model calls.
        elapsed_ms: Milliseconds since scope start.
        last_output_valid: Validation result of the latest activation; None before any output.
    """

    iterations: int
    tokens_in_context: int
    tokens_total: int
    cost_usd: float
    elapsed_ms: int
    last_output_valid: bool | None


def join_path(*segments: str) -> str:
    """Join workflow class-path segments into a node_path string.

    Args:
        *segments: Class paths or path segments in order from run root to leaf.

    Returns:
        The segments joined by ".", forming e.g.
        "...code_review.CodeReview.Inner.Gate" for depth-2 activations.
    """
    return ".".join(segments)
