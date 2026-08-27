"""FastAPI HTTP translation layer: routes translate to LocalRunService calls only."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from ngen_weave.config import RunSettings, load_config
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.state import RunStatus
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, DataError, NgWeaveError
from ngen_weave.export import dump_run_json
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.service import RunFilters, UnknownRunError
from ngen_weave.wiring import LazyProvider, default_provider, merged_registry
from ngen_weave.workflow import Workflow
from pydantic import BaseModel

from ngen_weave_server.local import LocalRunService


class LaunchBody(BaseModel):
    """Request body for ``POST /runs``."""

    workflow: str
    input: dict = {}


class ResumeBody(BaseModel):
    """Request body for ``POST /runs/{run_id}/resume``."""

    payload: dict | None = None


class NoteBody(BaseModel):
    """Request body for ``POST /runs/{run_id}/notes``."""

    note: str


def _error_response(status_code: int, exc: Exception) -> JSONResponse:
    """Render an NgWeave-shaped failure as a JSON error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": type(exc).__name__, "message": str(exc)}},
    )


async def unknown_run(request: Request, exc: UnknownRunError) -> JSONResponse:
    """Map missing ids onto 404."""
    return _error_response(404, exc)


async def bad_config(request: Request, exc: ConfigError) -> JSONResponse:
    """Map static configuration problems onto 400."""
    return _error_response(400, exc)


async def bad_data(request: Request, exc: DataError) -> JSONResponse:
    """Map input-validation failures onto 400 with the field-level report."""
    return _error_response(400, exc)


async def server_error(request: Request, exc: NgWeaveError) -> JSONResponse:
    """Map every other domain error onto 500 with the error envelope."""
    return _error_response(500, exc)


async def request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map malformed request bodies onto 400 with per-field lines."""
    lines = "\n".join(
        f"  - {'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
    )
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "type": "ValidationError",
                "message": f"request body:\n{lines}",
            }
        },
    )


def create_app(
    *,
    config_path: Path | None = None,
    runs_db_path: Path = Path(".ngen-weave/runs.db"),
    runs_dir: Path = Path(".ngen-weave/runs"),
    db_path: Path = Path(".ngen-weave/checkpoints.db"),
    models_file: Path = Path("models.json"),
    provider: CompletionProvider | None = None,
    discovery_map: dict[str, type[Workflow]] | None = None,
) -> FastAPI:
    """Build the serving app around a locally wired LocalRunService.

    Args:
        config_path: YAML/JSON run configuration; when given it loads first
            and derives the checkpoint database, models file, and retry
            knobs. Paths for the runs store stay explicit arguments.
        runs_db_path: SQLite database backing the RunStore.
        runs_dir: Directory holding run review artifacts and the store's runs
            database sibling; no legacy scanning.
        db_path: LangGraph checkpointer database file.
        models_file: models.json location used when no config_path is given.
        provider: CompletionProvider override (tests embed fakes); None defers
            to a LazyProvider over the configured models file.
        discovery_map: Workflow registry override; None uses the merged
            entry-point-plus-manifest discovery map.

    Returns:
        A FastAPI application exposing the six RunService methods as routes;
        the backing service is reachable as ``app.state.run_service``.
    """
    settings = RunSettings()
    if config_path is not None:
        config = load_config(config_path, merged_registry())
        settings = config.run
        models_file = config.models_file
    if provider is None:
        provider = LazyProvider(lambda: default_provider(models_file))

    store = RunStore(runs_dir, db_path=runs_db_path)
    engine = Engine(
        provider,
        store,
        checkpointer=settings.checkpointer,
        db_path=db_path,
        max_retries=settings.max_retries,
        retry_backoff_ms=settings.retry_backoff_ms,
        settings=settings,
    )
    registry = dict(discovery_map) if discovery_map is not None else merged_registry()
    service = LocalRunService(engine, store, registry)

    app = FastAPI()
    app.state.run_service = service
    app.add_exception_handler(UnknownRunError, unknown_run)
    app.add_exception_handler(ConfigError, bad_config)
    app.add_exception_handler(DataError, bad_data)
    app.add_exception_handler(NgWeaveError, server_error)
    app.add_exception_handler(RequestValidationError, request_validation)

    @app.post("/runs")
    async def create_run(body: LaunchBody) -> dict:
        """Launch a workflow by class path and return its RunHandle."""
        handle = await service.launch(body.workflow, body.input)
        return dataclasses.asdict(handle)

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, body: ResumeBody) -> dict:
        """Resume a paused or waiting run with the submitted payload."""
        handle = await service.resume(run_id, body.payload)
        return dataclasses.asdict(handle)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        """Return the full run file: metadata plus the record stream."""
        run_file = await service.status(run_id)
        return run_file.to_dict()

    @app.post("/runs/{run_id}/cancel", status_code=204)
    async def cancel_run(run_id: str) -> Response:
        """Request cancellation at the next activation boundary."""
        await service.cancel(run_id)
        return Response(status_code=204)

    @app.get("/runs")
    async def list_runs(workflow: str | None = None, status: RunStatus | None = None) -> list[dict]:
        """Summaries of stored runs, filtered by workflow and/or status."""
        found = await service.list_runs(RunFilters(workflow=workflow, status=status))
        return [dataclasses.asdict(summary) for summary in found]

    @app.post("/runs/{run_id}/notes", status_code=204)
    async def attach_note(run_id: str, body: NoteBody) -> Response:
        """Append a free-text note to the run's annotations."""
        await service.attach_note(run_id, body.note)
        return Response(status_code=204)

    @app.get("/runs/{run_id}/export")
    async def export_run(run_id: str) -> Response:
        """Serve the canonical run JSON produced by the single serializer."""
        data = dump_run_json(store.load(run_id))
        return Response(content=data, media_type="application/json")

    return app
