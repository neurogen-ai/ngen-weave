<<<<<<< HEAD
"""Single-writer persistence for run files."""
=======
"""Single-writer persistence for run files.

RunStore owns the .ngen-weave/runs/ directory: every read and write of run
state goes through it, so later backends (SQLite at v0.2, Postgres at v0.6)
swap in behind one class. Files are written atomically per transition and are
always valid JSON, always sufficient to re-run.

Classes:
    RunStore: Create, load, save, append to, and list run files.
"""
>>>>>>> feat/artifacts

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile, RunStatus
from ngen_weave.errors import ConfigError
from ngen_weave.provenance import ProvenanceRecord


class RunStore:
    """Create, load, save, append to, and list run files.

    The store is the sole writer of run state; nothing else touches the runs
    directory.
    """

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def create(self, workflow_name: str, input_dump: dict) -> str:
        """Start a new run file and return its generated run id."""
        run_id = str(uuid.uuid4())
        run_file = RunFile(
            format=RUN_FILE_FORMAT,
            run_id=run_id,
            workflow=workflow_name,
            status="running",
            input=input_dump,
        )
        self.save(run_file)
        return run_id

    def load(self, run_id: str) -> RunFile:
        """Return the run file for run_id.

        Raises:
            ConfigError: Unknown run id.
        """
        path = self._path(run_id)
        if not path.is_file():
            raise ConfigError(f"unknown run: {run_id}")
        return RunFile.from_dict(json.loads(path.read_text()))

    def save(self, run_file: RunFile) -> None:
        """Persist run_file atomically (temp file + os.replace)."""
        path = self._path(run_file.run_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run_file.to_dict(), indent=2))
        os.replace(tmp, path)

    def append(self, run_id: str, record: ProvenanceRecord) -> None:
        """Append one provenance record to the run's stream (load-modify-save)."""
        run_file = self.load(run_id)
        run_file.records.append(record)
        self.save(run_file)

    def set_status(self, run_id: str, status: RunStatus) -> RunFile:
        """Transition a run's status and return the updated file."""
        run_file = self.load(run_id)
        run_file.status = status
        self.save(run_file)
        return run_file

    def list(self) -> list[RunFile]:
        """Return every stored run file, ordered by run id."""
        return [self.load(p.stem) for p in sorted(self.runs_dir.glob("*.json"))]
<<<<<<< HEAD
=======

    # --- review artifacts -----------------------------------------------------

    def save_review_artifact(self, run_id: str, node_name: str, yaml_text: str) -> Path:
        """Write one review artifact under runs/<run-id>/artifacts/ and return
        its path. Overwrites are idempotent replays of the same waiting node."""
        directory = self.runs_dir / run_id / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{node_name}.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml_text)
        os.replace(tmp, path)
        return path

    def read_review_artifact(self, path: Path) -> dict:
        """Return a review artifact's parsed YAML mapping.

        Raises:
            ConfigError: The artifact file is missing or malformed.
        """
        import yaml

        if not path.is_file():
            raise ConfigError(f"review artifact missing: {path}")
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or "response" not in data:
            raise ConfigError(f"review artifact {path} lacks a response section")
        return data
>>>>>>> feat/artifacts
