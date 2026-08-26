"""Workflow-as-tool registration over an MCP server; blocking run dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as types
from ngen_weave.errors import ConfigError
from ngen_weave.schema_errors import format_validation_error
from ngen_weave.service import RunService
from ngen_weave.workflow import Workflow
from pydantic import ValidationError

POLL_INTERVAL_S = 0.25  # engine-specified status poll cadence while blocking
DEFAULT_TOOL_TIMEOUT_S = 3600.0

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_PARKED_STATUSES = {"paused", "waiting_human"}


def tool_name(class_path: str) -> str:
    """Return the MCP tool name: the class path with dots sanitized to hyphens."""
    return class_path.replace(".", "-")


def register_workflow_tools(
    server: Any,
    workflows: dict[str, type[Workflow]],
    service: RunService,
    *,
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
) -> None:
    """Expose each workflow as one MCP tool on `server`.

    Builds the surface once at startup: names are sanitized class paths,
    descriptions come from Workflow.description (required non-empty here),
    and input schemas pass through workflow.input_type.model_json_schema()
    unchanged. Tool calls dispatch through `service`, polling until the run
    reaches a terminal or parked state, or the caller-supplied timeout.

    Raises:
        ConfigError: A workflow lacks a description, or two sanitized names
            collide.
    """
    registry: dict[str, tuple[str, type[Workflow]]] = {}
    for path, wf in workflows.items():
        if not wf.description:
            raise ConfigError(
                f"{path}: workflow has no description; one is required to expose "
                "it as an MCP tool"
            )
        name = tool_name(path)
        if name in registry:
            raise ConfigError(
                f"MCP tool name collision: {registry[name][0]} and {path} both "
                f"sanitize to '{name}'"
            )
        registry[name] = (path, wf)

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=wf.description,
                    inputSchema=wf.input_type.model_json_schema(),
                )
                for name, (_, wf) in sorted(registry.items())
            ]
        )

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        entry = registry.get(params.name)
        if entry is None:
            return _error_result({"error": f"unknown tool: {params.name}"})
        path, wf = entry
        arguments = params.arguments or {}
        try:
            model = wf.input_type.model_validate(arguments)
        except ValidationError as exc:
            return _error_result(
                {
                    "error": {
                        "type": "ValidationError",
                        "message": format_validation_error(wf.input_type, exc),
                    }
                }
            )
        handle = await service.launch(path, model)
        return await _await_run(handle.run_id, wf, service, tool_timeout_s)

    server.add_request_handler(
        "tools/list", types.PaginatedRequestParams, on_list_tools
    )
    server.add_request_handler("tools/call", types.CallToolRequestParams, on_call_tool)


async def _await_run(
    run_id: str, wf: type[Workflow], service: RunService, tool_timeout_s: float
) -> types.CallToolResult:
    """Poll a launched run to terminal/parked status or timeout.

    Blocking by design: humans resume parked runs out of band via the runs
    API or CLI, so this call returns instead of holding the connection open.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + tool_timeout_s
    while True:
        file = await service.status(run_id)
        if file.status == "completed":
            output = wf.output_type.model_validate(file.output)
            payload = output.model_dump()
            return types.CallToolResult(
                content=[_text(json.dumps(payload))],
                structuredContent=payload,
            )
        if file.status == "failed":
            return _error_result(
                {"run_id": run_id, "status": file.status, "error": file.error or {}}
            )
        if file.status in _TERMINAL_STATUSES | _PARKED_STATUSES:
            return _plain_result(
                {
                    "run_id": run_id,
                    "status": file.status,
                    "waiting_on": _parked_node(file),
                }
            )
        if loop.time() >= deadline:
            return _error_result(
                {
                    "run_id": run_id,
                    "status": file.status,
                    "error": {
                        "type": "TimeoutError",
                        "message": (
                            f"run did not reach a terminal or parked state within "
                            f"{tool_timeout_s}s; it stays resumable via the runs API "
                            "or CLI"
                        ),
                    },
                }
            )
        await asyncio.sleep(POLL_INTERVAL_S)


def _parked_node(file) -> str:
    """Return the node path a paused/waiting_human run is stopped at."""
    for record in reversed(file.records):
        if record.kind in {"budget_exhausted", "observer_firing"} and record.node_path:
            return record.node_path
        if (
            record.kind == "node_activation"
            and record.payload.get("status") == "waiting_human"
        ):
            return record.node_path
    return ""


def _text(value: str) -> types.TextContent:
    """Wrap one string as a text content block."""
    return types.TextContent(type="text", text=value)


def _plain_result(payload: dict[str, Any]) -> types.CallToolResult:
    """One normal result whose JSON payload describes the run's disposition."""
    return types.CallToolResult(content=[_text(json.dumps(payload))])


def _error_result(payload: dict[str, Any]) -> types.CallToolResult:
    """One MCP error result carrying the JSON payload as its message."""
    return types.CallToolResult(
        content=[_text(json.dumps(payload))],
        isError=True,
    )
