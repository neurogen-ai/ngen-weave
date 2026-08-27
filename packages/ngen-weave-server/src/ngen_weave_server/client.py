"""RunService client speaking HTTP for remote backends.

Maps remote error envelopes onto protocol-shaped exceptions.
"""

from __future__ import annotations

from typing import Any

import httpx
from ngen_weave.engine.state import RunFile
from ngen_weave.errors import ConfigError, NgWeaveError
from ngen_weave.service import RunFilters, RunHandle, RunSummary, UnknownRunError
from pydantic import BaseModel


class HttpRunService:
    """Speaks the HTTP routes back into the six RunService methods."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        """Open an async client against base_url; transport overrides delivery."""
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport)

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> HttpRunService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request and map non-2xx replies onto protocol errors.

        The 404/400/500 mapping mirrors the app's exception handlers so callers
        see UnknownRunError / ConfigError / NgWeaveError regardless of backend.
        """
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            message = _error_message(response)
            if response.status_code == 404:
                raise UnknownRunError(message)
            if response.status_code == 400:
                raise ConfigError(message)
            raise NgWeaveError(message)
        return response

    async def launch(self, workflow: str, input: BaseModel) -> RunHandle:
        """Launch the named workflow by class path on the remote server."""
        response = await self._request(
            "POST", "/runs", json={"workflow": workflow, "input": input.model_dump()}
        )
        data = response.json()
        return RunHandle(run_id=data["run_id"], status=data["status"])

    async def resume(self, run_id: str, payload: dict | None = None) -> RunHandle:
        """Continue run_id on the remote server from its checkpoint."""
        response = await self._request("POST", f"/runs/{run_id}/resume", json={"payload": payload})
        data = response.json()
        return RunHandle(run_id=data["run_id"], status=data["status"])

    async def status(self, run_id: str) -> RunFile:
        """Return the full run file for run_id via GET /runs/{run_id}."""
        response = await self._request("GET", f"/runs/{run_id}")
        return RunFile.from_dict(response.json())

    async def cancel(self, run_id: str) -> None:
        """Request cancellation at the next activation boundary (204)."""
        await self._request("POST", f"/runs/{run_id}/cancel")

    async def list_runs(self, filters: RunFilters | None = None) -> list[RunSummary]:
        """Summaries of stored runs on the remote server, filtered by query params."""
        params = {}
        if filters is not None:
            if filters.workflow is not None:
                params["workflow"] = filters.workflow
            if filters.status is not None:
                params["status"] = filters.status
        response = await self._request("GET", "/runs", params=params)
        summaries = [
            RunSummary(
                run_id=item["run_id"],
                workflow=item["workflow"],
                status=item["status"],
                started_at=item["started_at"],
                cost_usd=item["cost_usd"],
                waiting_on_human=item["waiting_on_human"],
            )
            for item in response.json()
        ]
        return summaries

    async def attach_note(self, run_id: str, note: str) -> None:
        """Append a free-text note through POST /runs/{run_id}/notes (204)."""
        await self._request("POST", f"/runs/{run_id}/notes", json={"note": note})


def _error_message(response: httpx.Response) -> str:
    """Pull the envelope message out of an error reply when parseable."""
    try:
        return response.json()["error"]["message"]
    except Exception:
        return response.text
