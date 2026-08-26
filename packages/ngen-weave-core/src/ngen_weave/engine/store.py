"""Single-writer persistence for run state in one SQLite database.

RunStore owns run persistence: every read and write of run state goes
through it, so later backends (Postgres at v0.6) swap in behind one class.
Records append per transition and header fields update separately, so no
write ever rewrites the whole record stream. Review artifacts stay files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile, RunStatus
from ngen_weave.errors import ConfigError
from ngen_weave.export import load_run_json
from ngen_weave.provenance import PROVENANCE_VERSION, ProvenanceRecord

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  workflow TEXT, status TEXT,
  input_json TEXT, output_json TEXT, error_json TEXT,
  attempts INTEGER, submissions_json TEXT,
  started_at TEXT, notes_json TEXT NOT NULL DEFAULT '[]',
  cost_usd REAL NOT NULL DEFAULT 0, activations INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS records (
  run_id TEXT REFERENCES runs(run_id), seq INTEGER NOT NULL,
  ts TEXT, node_path TEXT, kind TEXT, payload_json TEXT,
  PRIMARY KEY (run_id, seq));
"""

_RUN_COLUMNS = (
    "run_id, workflow, status, input_json, output_json, error_json, "
    "attempts, submissions_json, started_at, notes_json"
)


class RunStore:
    """Create, load, save, append to, and list runs in one SQLite database.

    The store is the sole writer of run state; nothing else touches the runs
    database. The runs directory keeps its role as the home of review
    artifacts and of any legacy v0.1 flat run files (imported at init).
    """

    def __init__(self, runs_dir: Path, *, db_path: Path | None = None) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        # .ngen-weave/runs -> .ngen-weave/runs.db: sibling of the runs dir,
        # never shared with the checkpointer's database file.
        self.db_path = Path(db_path) if db_path is not None else self.runs_dir.with_name("runs.db")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._import_legacy()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # --- run state -----------------------------------------------------------

    def create(self, workflow_name: str, input_dump: dict) -> str:
        """Start a new run row and return its generated run id."""
        run_id = str(uuid.uuid4())
        self.save(
            RunFile(
                format=RUN_FILE_FORMAT,
                run_id=run_id,
                workflow=workflow_name,
                status="running",
                input=input_dump,
                started_at=datetime.now(UTC).isoformat(),
            )
        )
        return run_id

    def load(self, run_id: str) -> RunFile:
        """Return the run file for run_id.

        Raises:
            ConfigError: Unknown run id.
        """
        row = self._fetch_row(run_id)
        if row is None:
            raise ConfigError(f"unknown run: {run_id}")
        records = [
            ProvenanceRecord(
                version=PROVENANCE_VERSION,
                run_id=row["run_id"],
                node_path=rec["node_path"],
                kind=rec["kind"],  # type: ignore[arg-type]
                ts=rec["ts"],
                payload=json.loads(rec["payload_json"]),
            )
            for rec in self._conn.execute(
                "SELECT node_path, kind, ts, payload_json FROM records "
                "WHERE run_id = ? ORDER BY seq",
                (row["run_id"],),
            )
        ]
        return self._header_file(row, records)

    def save(self, run_file: RunFile) -> None:
        """Persist a run file atomically: header fields plus its record stream.

        Callers pass a freshly loaded RunFile whose records are current, so
        saving replaces the stored stream with exactly what the caller holds;
        totals columns are recomputed from those records. Hot-path appends go
        through ``append`` instead, one INSERT without rewriting the stream.
        """
        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO runs ({_RUN_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  workflow = excluded.workflow,
                  status = excluded.status,
                  input_json = excluded.input_json,
                  output_json = excluded.output_json,
                  error_json = excluded.error_json,
                  attempts = excluded.attempts,
                  submissions_json = excluded.submissions_json,
                  started_at = excluded.started_at,
                  notes_json = excluded.notes_json
                """,
                (
                    run_file.run_id,
                    run_file.workflow,
                    run_file.status,
                    json.dumps(run_file.input, sort_keys=True),
                    _json_or_none(run_file.output),
                    _json_or_none(run_file.error),
                    run_file.attempts,
                    json.dumps(run_file.submissions, sort_keys=True),
                    run_file.started_at,
                    json.dumps(run_file.notes),
                ),
            )
            self._replace_records(run_id=run_file.run_id, records=run_file.records)

    def _replace_records(self, run_id: str, records: list[ProvenanceRecord]) -> None:
        """Swap the stored record stream for `records` and derive the totals.

        Must be called inside an open transaction. model_call payloads
        contribute their cost_usd to runs.cost_usd; node_activation payloads
        count into runs.activations.
        """
        self._conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
        self._conn.executemany(
            "INSERT INTO records (run_id, seq, ts, node_path, kind, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    seq,
                    record.ts,
                    record.node_path,
                    record.kind,
                    json.dumps(record.payload, sort_keys=True),
                )
                for seq, record in enumerate(records, start=1)
            ],
        )
        cost = sum(
            float(record.payload.get("cost_usd") or 0.0)
            for record in records
            if record.kind == "model_call"
        )
        activations = sum(1 for record in records if record.kind == "node_activation")
        self._conn.execute(
            "UPDATE runs SET cost_usd = ?, activations = ? WHERE run_id = ?",
            (cost, activations, run_id),
        )

    def append(self, run_id: str, record: ProvenanceRecord) -> None:
        """Commit one record plus its totals update in a single transaction.

        model_call payloads add their cost_usd to runs.cost_usd;
        node_activation payloads increment activations. No load-modify-save
        ever touches the record stream.

        Raises:
            ConfigError: Unknown run id.
        """
        with self._conn:
            if self._conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise ConfigError(f"unknown run: {run_id}")
            self._conn.execute(
                """
                INSERT INTO records (run_id, seq, ts, node_path, kind, payload_json)
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?
                FROM records WHERE run_id = ?
                """,
                (
                    run_id,
                    record.ts,
                    record.node_path,
                    record.kind,
                    json.dumps(record.payload, sort_keys=True),
                    run_id,
                ),
            )
            if record.kind == "model_call":
                cost = float(record.payload.get("cost_usd") or 0.0)
                if cost:
                    self._conn.execute(
                        "UPDATE runs SET cost_usd = cost_usd + ? WHERE run_id = ?",
                        (cost, run_id),
                    )
            elif record.kind == "node_activation":
                self._conn.execute(
                    "UPDATE runs SET activations = activations + 1 WHERE run_id = ?",
                    (run_id,),
                )

    def set_status(self, run_id: str, status: RunStatus) -> RunFile:
        """Transition a run's status and return the updated file.

        Raises:
            ConfigError: Unknown run id.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id)
            )
        if cursor.rowcount != 1:
            raise ConfigError(f"unknown run: {run_id}")
        return self.load(run_id)

    def list(self) -> list[RunFile]:
        """Return header-only snapshots of every run, ordered by run id.

        Reads headers from the runs table without touching records, so each
        returned RunFile carries an empty records list.
        """
        rows = self._conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY run_id").fetchall()
        return [self._header_file(row, []) for row in rows]

    # --- review artifacts (file-based by design) -------------------------------

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

    # --- internals -------------------------------------------------------------

    def _fetch_row(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def _header_file(self, row: sqlite3.Row, records: list[ProvenanceRecord]) -> RunFile:
        """Assemble the versioned-envelope RunFile for one runs-table row."""
        return RunFile(
            format=RUN_FILE_FORMAT,
            run_id=row["run_id"],
            workflow=row["workflow"],
            status=row["status"],  # type: ignore[arg-type]
            input=json.loads(row["input_json"]) if row["input_json"] is not None else {},
            output=json.loads(row["output_json"]) if row["output_json"] is not None else None,
            error=json.loads(row["error_json"]) if row["error_json"] is not None else None,
            attempts=row["attempts"] or 0,
            submissions=json.loads(row["submissions_json"]) if row["submissions_json"] else {},
            started_at=row["started_at"] or "",
            notes=json.loads(row["notes_json"]),
            records=records,
        )

    def _import_legacy(self) -> None:
        """Import every legacy v0.1 flat run file whose id is absent, idempotently.

        save() persists headers, records, and derived totals together, so an
        imported run carries the cost/activation totals its records imply;
        second init skips already-present ids and cannot double-count them.
        """
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                run = load_run_json(path.read_bytes())
            except Exception as exc:
                raise ConfigError(f"{path}: cannot import legacy run file: {exc}") from exc
            if self._fetch_row(run.run_id) is not None:
                continue
            # Legacy files carry no started_at; fall back to the first record's ts.
            first_ts = next((r.ts for r in run.records if r.ts), "")
            imported = replace(run, started_at=run.started_at or first_ts)
            self.save(imported)


def _json_or_none(value: dict | list | None) -> str | None:
    return json.dumps(value, sort_keys=True) if value is not None else None
