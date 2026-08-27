"""RunService protocol plus run-summary types.

This module defines the seam between callers of ngen-weave (server, CLI,
future UIs) and a running system: handle shapes, listing filters, and the
service contract. Later steps implement RunService, never amend it.

Classes:
    RunHandle: Lightweight identity-plus-status pair returned by launch/resume.
    RunSummary: One-row projection of a run for listings.
    RunFilters: Optional equality filters applied to summaries when listing.
    RunService: Protocol every implementation (local, remote) must satisfy.

Functions:
    summaries: Project loaded run files onto listing summaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ngen_weave.engine.state import RunFile, RunStatus


class UnknownRunError(KeyError):
    """A run id does not exist.

    Subclasses :class:`KeyError` so dict-keying contexts and ``except``
    clauses both treat unknown ids uniformly; the argument is the plain
    error message (``"unknown run: <run_id>"``), not a quoted key.
    """


@dataclass(frozen=True)
class RunHandle:
    """Lightweight outcome of launching or resuming a run.

    Attributes:
        run_id: Identifier of the launched or resumed run.
        status: Lifecycle status at the time the call returned.
    """

    run_id: str
    status: RunStatus


@dataclass(frozen=True)
class RunSummary:
    """One-row projection of a run for listings.

    Attributes:
        run_id: Identifier of the run.
        workflow: Fully-qualified class path of the run's root workflow.
        status: Current lifecycle status.
        started_at: UTC ISO-8601 timestamp; the first record's ts when the
            header carries no creation timestamp (legacy imports).
        cost_usd: Summed cost across model_call records.
        waiting_on_human: True while status == "waiting_human".
    """

    run_id: str
    workflow: str
    status: RunStatus
    started_at: str
    cost_usd: float
    waiting_on_human: bool


@dataclass(frozen=True)
class RunFilters:
    """Optional equality filters for ``list_runs``.

    Each set field must equal the summary's value; unset fields match all.
    """

    workflow: str | None = None
    status: RunStatus | None = None

    def matches(self, summary: RunSummary) -> bool:
        """Return True when `summary` satisfies every set filter field."""
        return self.workflow in (None, summary.workflow) and self.status in (None, summary.status)


class RunService(Protocol):
    """The one contract callers use against a running ngen-weave system."""

    async def launch(self, workflow: str, input: BaseModel) -> RunHandle: ...

    async def resume(self, run_id: str, payload: dict | None = None) -> RunHandle: ...

    async def status(self, run_id: str) -> RunFile: ...

    async def cancel(self, run_id: str) -> None: ...

    async def list_runs(self, filters: RunFilters | None = None) -> list[RunSummary]: ...

    async def attach_note(self, run_id: str, note: str) -> None: ...


def summaries(files: Iterable[RunFile]) -> list[RunSummary]:
    """Project loaded run files onto listing summaries, input order preserved.

    Pure projection over already-loaded files: cost and started_at are read
    from full record streams, so listing is O(records). That cost is accepted
    until profiling complains; no header-only summary ships in v0.2.
    """
    return [_summary(file) for file in files]


def _summary(file: RunFile) -> RunSummary:
    started_at = file.started_at or next((record.ts for record in file.records if record.ts), "")
    return RunSummary(
        run_id=file.run_id,
        workflow=file.workflow,
        status=file.status,
        started_at=started_at,
        cost_usd=sum(
            float(record.payload.get("cost_usd") or 0.0)
            for record in file.records
            if record.kind == "model_call"
        ),
        waiting_on_human=file.status == "waiting_human",
    )
