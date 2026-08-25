"""ngen-weave CLI entry point."""

import asyncio
import json
from importlib.metadata import version
from pathlib import Path

import typer
from ngen_weave.config import load_config
from ngen_weave.discovery import discover
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, NgWeaveError
from ngen_weave.workflow import Workflow

from .context import NGEN_WEAVE_DIR, _build_engine, merged_registry


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("ngen-weave"))
        raise typer.Exit()


app = typer.Typer(
    name="ngen-weave",
    help="Durable human-in-the-loop AI workflows on LangGraph.",
    no_args_is_help=True,
)


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Print the ngen-weave version and exit.",
    ),
) -> None:
    """Durable human-in-the-loop AI workflows on LangGraph."""


def _fail(exc: Exception) -> None:
    """Print an error message on stderr and exit with status 1."""
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1)


def _read_json(path: Path) -> dict:
    """Read a JSON object file; parse failures are ConfigError."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


def _resolve_run_target(
    workflow_name: str, config_path: Path | None
) -> tuple[type[Workflow], dict[str, str], object | None]:
    """Resolve the workflow class, model bindings, and optional config.

    A -c config wins over the positional workflow argument; without one the
    named class path must resolve through the merged discovery map.
    """
    registry_map = merged_registry()
    config = load_config(config_path, registry_map) if config_path is not None else None
    if config is not None:
        return config.workflow, config.models, config
    cls = registry_map.get(workflow_name)
    if cls is None:
        raise ConfigError(
            f"unknown workflow: {workflow_name}; pass -c or list the module in ngen-weave.json"
        )
    return cls, {}, None


@app.command()
def validate(
    path: Path = typer.Argument(help="A YAML/JSON run config or a Python module path."),
) -> None:
    """Validate a run config or a workflow module without running anything."""
    try:
        if path.suffix.lower() in {".yaml", ".yml", ".json"}:
            config = load_config(path, merged_registry())
            config.workflow.input_type.model_json_schema()  # schemas must resolve
            typer.echo(f"ok: {config.workflow.__name__} ({path})")
        else:
            discover([str(path)], source="validate")
            typer.echo(f"ok: module {path}")
    except NgWeaveError as exc:
        _fail(exc)


@app.command("run")
def run_command(
    workflow: str = typer.Argument(help="Fully-qualified workflow class path."),
    input_file: Path | None = typer.Option(None, "-i", "--input", help="JSON input file."),
    config_path: Path | None = typer.Option(None, "-c", "--config", help="YAML/JSON run config."),
    project: str | None = typer.Option(None, "--project", help="Project name for artifacts."),
) -> None:
    """Run a workflow and print the run id plus final status."""
    try:
        wf, models, config = _resolve_run_target(workflow, config_path)
        if input_file is None:
            _fail(ConfigError("no input given; pass -i input.json"))
        raw = _read_json(input_file)
        model = wf.input_type.model_validate(raw)
        app_ctx = _build_engine(config, project=project)
        result = asyncio.run(app_ctx.engine.run(wf, model, models=models))
    except NgWeaveError as exc:
        _fail(exc)
    typer.echo(f"run {result.run_id}")
    typer.echo(f"status {result.status}")
    if result.status != "completed":
        raise typer.Exit(1)


@app.command()
def resume(
    run_id: str = typer.Argument(help="Run id to continue."),
    response_file: Path | None = typer.Option(
        None, "-p", "--response", help="JSON human-review response."
    ),
    project: str | None = typer.Option(None, "--project", help="Project name for artifacts."),
) -> None:
    """Continue a run from its checkpoint; exit 0 only on a terminal status."""
    try:
        payload = _read_json(response_file) if response_file is not None else None
        merged_registry()  # the run's workflow must be discoverable to resume it
        app_ctx = _build_engine(None, project=project)
        result = asyncio.run(app_ctx.engine.resume(run_id, payload))
    except NgWeaveError as exc:
        _fail(exc)
    typer.echo(f"status {result.status}")
    if result.status not in {"completed", "failed"}:
        raise typer.Exit(1)


@app.command()
def status(run_id: str = typer.Argument(help="Run id to inspect.")) -> None:
    """Print a run's workflow, status, waiting-on node, and cost so far."""
    store = RunStore(NGEN_WEAVE_DIR / "runs")
    try:
        run_file = store.load(run_id)
    except NgWeaveError as exc:
        _fail(exc)
    typer.echo(f"workflow {run_file.workflow}")
    typer.echo(f"status {run_file.status}")
    waiting_on = next(
        (
            record.node_path
            for record in reversed(run_file.records)
            if record.kind == "node_activation" and record.payload.get("status") == "waiting_human"
        ),
        None,
    )
    if waiting_on is not None:
        typer.echo(f"waiting-on {waiting_on}")
    cost = sum(
        record.payload.get("cost_usd", 0.0)
        for record in run_file.records
        if record.kind == "model_call"
    )
    typer.echo(f"cost_usd {cost:.6f}")
