"""Local in-process implementation of the RunService protocol.

Wires the LangGraph-backed Engine and RunStore behind the six protocol
methods; HTTP translation and clients live in their own modules.
"""

from __future__ import annotations

from ngen_weave.engine.runner import Engine
from ngen_weave.engine.state import RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import DataError
from ngen_weave.schema_errors import format_validation_error
from ngen_weave.service import (
    RunFilters,
    RunHandle,
    RunSummary,
    UnknownRunError,
    summaries,
)
from ngen_weave.workflow import Workflow
from pydantic import BaseModel, ValidationError


class LocalRunService:
    """Reference RunService implementation over one in-process Engine.

    Args:
        engine: The engine that runs and resumes workflows.
        store: The run store backing status, listing, and notes.
        discovery_map: Merged workflow registry mapping fully-qualified class
            paths to workflow classes; `launch` resolves through it.
    """

    def __init__(
        self,
        engine: Engine,
        store: RunStore,
        discovery_map: dict[str, type[Workflow]],
    ) -> None:
        self.engine = engine
        self.store = store
        self.discovery_map = discovery_map

    async def launch(self, workflow: str, input: BaseModel) -> RunHandle:
        """Run the named workflow on validated input and return its handle.

        Raises:
            UnknownRunError: The class path is not in the discovery map.
            DataError: The input does not match the workflow's input_type.
        """
        wf = self.discovery_map.get(workflow)
        if wf is None:
            raise UnknownRunError(f"unknown workflow: {workflow}")
        raw = input.model_dump() if isinstance(input, BaseModel) else input
        try:
            model = wf.input_type.model_validate(raw)
        except ValidationError as exc:
            raise DataError(format_validation_error(wf.input_type, exc)) from exc
        result = await self.engine.run(wf, model)
        return RunHandle(run_id=result.run_id, status=result.status)

    async def resume(self, run_id: str, payload: dict | None = None) -> RunHandle:
        """Continue run_id from its checkpoint; unknown ids raise UnknownRunError."""
        result = await self.engine.resume(run_id, payload)
        return RunHandle(run_id=result.run_id, status=result.status)

    async def status(self, run_id: str) -> RunFile:
        """Return the full run file for run_id; unknown ids raise UnknownRunError."""
        return self.store.load(run_id)

    async def cancel(self, run_id: str) -> None:
        """Request cancellation at the next activation boundary.

        The engine owns the flag and the terminal write (see C1); cancelling
        an already-terminal run is a no-op, unknown ids raise UnknownRunError.
        """
        self.engine.cancel(run_id)

    async def list_runs(self, filters: RunFilters | None = None) -> list[RunSummary]:
        """Summaries of every stored run, filtered by the given fields.

        Summaries project over fully loaded run files per the B2 contract:
        header rows carry no records, so cost_usd would always read zero
        without loading each stream whole. The O(records) listing cost is
        documented and accepted until profiling complains.
        """
        found = summaries(
            self.store.load(header.run_id) for header in self.store.list()
        )
        if filters is not None:
            found = [summary for summary in found if filters.matches(summary)]
        return found

    async def attach_note(self, run_id: str, note: str) -> None:
        """Append note to the run's annotations through the store only."""
        run_file = self.store.load(run_id)
        run_file.notes.append(note)
        self.store.save(run_file)
