"""Canonical run-JSON serialization: one writer, shared by every exporter."""

from __future__ import annotations

import dataclasses
import json

from ngen_weave.engine.state import RunFile

# v0.1 key set plus started_at and notes (emitted by RunFile's field set).
# Pre-1.0 the format may reshape without a compat promise.
_DUMP_KEYS = (
    "format",
    "run_id",
    "workflow",
    "status",
    "input",
    "output",
    "error",
    "attempts",
    "submissions",
    "started_at",
    "notes",
    "records",
)


def dump_run_json(run: RunFile) -> bytes:
    """Serialize run to canonical JSON: the v0.1 keys plus started_at, notes.

    The only run-JSON serializer in the codebase; CLI, HTTP export, and any
    future consumer emit these same bytes. Key order and record payloads are
    normalized via sort_keys so identical runs produce identical bytes.
    """
    dump = dataclasses.asdict(run)
    assert tuple(dump) == _DUMP_KEYS or set(dump) == set(_DUMP_KEYS)
    return json.dumps(dump, sort_keys=True, indent=2).encode("utf-8")


def load_run_json(data: bytes) -> RunFile:
    """Parse canonical run JSON back into a RunFile.

    Inverse of dump_run_json; keys with defaults (output, error, attempts,
    submissions, started_at, notes) may be absent, as in v0.1 flat files.
    Missing format defaults to RUN_FILE_FORMAT.
    """
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("run JSON must be an object")
    return RunFile.from_dict(parsed)
