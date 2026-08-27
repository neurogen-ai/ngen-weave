"""Content-addressed artifact storage under .ngen-weave/projects/."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactMeta:
    """Identity of one artifact write.

    Attributes:
        run_id: Run that produced the value.
        node_path: Producing activation's accumulated node path.
        name: The output_type field persisted.
        input_hashes: Input field name -> sha256 of its canonical JSON dump,
            linking the artifact to the exact input that produced it.
    """

    run_id: str
    node_path: str
    name: str
    input_hashes: dict[str, str]


@dataclass(frozen=True)
class ArtifactRecord:
    """A stored blob: its hash, disk path, and producing meta."""

    sha256: str
    path: str  # .ngen-weave/projects/<project>/<sha256>
    meta: ArtifactMeta


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_value(value: Any) -> str:
    """Return the sha256 of a value's canonical JSON serialization."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return _digest(encoded)


class ArtifactStore:
    """Put bytes content-addressed and link sidecar metadata.

    Blobs live at <projects_dir>/<project>/<sha256>; identical bytes never
    rewrite (idempotent), so concurrent activations of the same value converge
    on one file. Sidecars may be overwritten by a later producer of identical
    content; the blob itself stays untouched.
    """

    def __init__(self, projects_dir: Path, project: str) -> None:
        self.projects_dir = Path(projects_dir)
        self.project = project
        self.directory = self.projects_dir / project
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, meta: ArtifactMeta) -> ArtifactRecord:
        """Store data under its content hash and return the record."""
        digest = _digest(data)
        path = self.directory / digest
        if not path.exists():
            tmp = self.directory / f"{digest}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return ArtifactRecord(sha256=digest, path=str(path), meta=meta)

    def link_meta(self, record: ArtifactRecord) -> None:
        """Write the sidecar JSON next to the blob with meta + provenance link."""
        payload = {
            **dataclasses.asdict(record.meta),
            "sha256": record.sha256,
            "path": record.path,
        }
        sidecar = Path(record.path + ".json")
        tmp = sidecar.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, sidecar)
