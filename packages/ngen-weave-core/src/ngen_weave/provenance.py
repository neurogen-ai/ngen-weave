"""Provenance records and run metadata."""

from dataclasses import dataclass, field
from typing import Literal

PROVENANCE_VERSION = 1

# v0.2 widens Kind with "budget_exhausted", "permission_denied" (consumed by
# Branch E), and "observer_firing".
#
# Payload contracts (defined here and nowhere else):
#   budget_exhausted: {"dimension": "cost_usd" | "steps", "limit": float | int,
#                      "observed": float | int}
#       Emitted once by the engine's activation-boundary hook when a run
#       reaches its configured budget cap; node_path is the activation that
#       crossed the limit.
#   permission_denied: {"tool": str, "node_path": str, "policy": str}
#       Emitted by the agent PermissionGate before raising its denial error;
#       policy is the PermissionSet denied_policy literal.
#   tool_call: {"tool": str, "node_path": str, "cost_usd": float}
#       Emitted by the agent PermissionGate after each executed tool call
#       (denials emit permission_denied instead, never tool_call); cost_usd
#       is the tool-reported spend (0.0 when the result carries none).
#   observer_firing: {"predicate": str, "field": ..., "op": ..., "value": ...,
#                     "observed": float | int, "action": str}
#       Emitted when a declared Observer's predicate fires against an
#       activation's RunMetadata at its boundary (or against the root scope
#       at run completion); predicate is pred.describe(), action the
#       Observer action literal ("pause" in v0.2).
Kind = Literal[
    "node_activation",
    "model_call",
    "artifact_write",
    "budget_exhausted",
    "permission_denied",
    "tool_call",
    "observer_firing",
]


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
