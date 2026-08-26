"""Run file and result shapes.

RunFile is the one JSON document per run under .ngen-weave/runs/: metadata
plus the full event/provenance stream, always valid and sufficient to re-run.
RunResult is what Engine.run/resume return to callers.

Classes:
    RunFile: Complete persisted state of one run, serialized by RunStore.
    RunResult: Terminal or in-flight outcome of a run call.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from ngen_weave.provenance import ProvenanceRecord

RunStatus = Literal[
    "running", "waiting_human", "paused", "completed", "failed", "cancelled"
]

RUN_FILE_FORMAT = 1


@dataclass
class RunFile:
    """Complete persisted state of one run.

    Attributes:
        format: Run-file schema version, always RUN_FILE_FORMAT.
        run_id: Identifier of this run; also the checkpoint thread id.
        workflow: Fully-qualified class path of the run's root workflow.
        status: Current lifecycle status.
        input: The root workflow's input dump.
        output: Output dump once completed, else None.
        error: {"type", "message"} when failed, else None.
        attempts: How many times the engine has driven this run; each attempt
            gets its own checkpoint namespace so a failed run re-executes
            from the top instead of replaying a dead superstep.
        started_at: UTC ISO-8601 creation timestamp; empty for legacy imports.
        notes: Operator-attached free-text annotations.
        records: Full provenance stream, oldest first.
    """

    format: int  # always RUN_FILE_FORMAT
    run_id: str
    workflow: str
    status: RunStatus
    input: dict
    output: dict | None = None
    error: dict[str, str] | None = None  # {"type": str, "message": str}
    attempts: int = 0
    submissions: dict[str, dict] = field(default_factory=dict)
    started_at: str = ""  # UTC ISO-8601
    notes: list[str] = field(default_factory=list)
    records: list[ProvenanceRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the plain-dict shape written to disk."""
        return {
            "format": self.format,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": self.status,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "attempts": self.attempts,
            "submissions": self.submissions,
            "started_at": self.started_at,
            "notes": list(self.notes),
            "records": [dataclasses.asdict(record) for record in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunFile:
        """Rebuild a RunFile from its on-disk dict shape."""
        return cls(
            format=data["format"],
            run_id=data["run_id"],
            workflow=data["workflow"],
            status=data["status"],
            input=data["input"],
            output=data.get("output"),
            error=data.get("error"),
            attempts=data.get("attempts", 0),
            submissions=data.get("submissions", {}),
            started_at=data.get("started_at", ""),
            notes=list(data.get("notes", ())),
            records=[ProvenanceRecord(**record) for record in data.get("records", ())],
        )


@dataclass
class RunResult:
    """Terminal or in-flight outcome of an Engine.run/resume call.

    Attributes:
        run_id: Identifier of the run.
        status: Status at the time the call returned.
        output: Validated root output model when completed, else None.
        waiting: {"node_path", "artifact"} when waiting_human, and
            {"node_path", "reason": "budget_exhausted"} after a budget pause;
            None otherwise.
    """

    run_id: str
    status: RunStatus
    output: BaseModel | None
    waiting: dict | None = None
