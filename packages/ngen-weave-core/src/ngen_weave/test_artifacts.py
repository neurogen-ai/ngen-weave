"""Artifact store tests: content addressing, sidecars, provenance linking.

Covers ArtifactStore.put idempotence and address stability, canonical value
hashing, sidecar contents, and the engine integration: a successful
activation whose workflow declares artifacts stores each named field and
emits an artifact_write record linking the blob to its producing activation,
whether the declaring workflow is a wired child or the run root.
"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider

from ngen_weave import registry
from ngen_weave.artifacts import ArtifactMeta, ArtifactStore, hash_value
from ngen_weave.engine import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.workflow import END, START, Worker, Workflow, workflow_class_path


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated test classes reuse short names; isolate the global registry."""
    registry.reset()
    yield
    registry.reset()


def _meta(**overrides) -> ArtifactMeta:
    defaults = {
        "run_id": "r1",
        "node_path": "m.W.Node",
        "name": "report",
        "input_hashes": {"diff": "abc123"},
    }
    return ArtifactMeta(**(defaults | overrides))


class TestHashValue:
    def test_canonical_regardless_of_key_order(self):
        assert hash_value({"a": 1, "b": [2, 3]}) == hash_value({"b": [2, 3], "a": 1})

    def test_different_values_hash_differently(self):
        assert hash_value({"a": 1}) != hash_value({"a": 2})

    def test_matches_sha256_of_canonical_json(self):
        value = {"x": "y"}
        expected = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert hash_value(value) == expected


class TestArtifactStore:
    def test_put_stores_bytes_under_content_address(self, tmp_path: Path):
        store = ArtifactStore(tmp_path / "projects", "demo")
        record = store.put(b"payload", _meta())
        assert Path(record.path).read_bytes() == b"payload"
        assert record.path == str(tmp_path / "projects" / "demo" / record.sha256)

    def test_put_is_idempotent_for_identical_bytes(self, tmp_path: Path):
        store = ArtifactStore(tmp_path / "projects", "demo")
        first = store.put(b"same", _meta())
        second = store.put(b"same", _meta(name="other"))
        assert first.path == second.path
        assert Path(first.path).read_bytes() == b"same"

    def test_different_bytes_land_at_different_addresses(self, tmp_path: Path):
        store = ArtifactStore(tmp_path / "projects", "demo")
        one = store.put(b"one", _meta())
        two = store.put(b"two", _meta())
        assert one.path != two.path

    def test_link_meta_writes_sidecar_beside_blob(self, tmp_path: Path):
        store = ArtifactStore(tmp_path / "projects", "demo")
        meta = _meta(input_hashes={"diff": "deadbeef"})
        record = store.put(b"value", meta)
        store.link_meta(record)
        sidecar = json.loads(Path(record.path + ".json").read_text())
        assert sidecar == {
            "run_id": "r1",
            "node_path": "m.W.Node",
            "name": "report",
            "input_hashes": {"diff": "deadbeef"},
            "sha256": record.sha256,
            "path": record.path,
        }

    def test_projects_are_isolated_by_name(self, tmp_path: Path):
        alpha = ArtifactStore(tmp_path / "projects", "alpha")
        beta = ArtifactStore(tmp_path / "projects", "beta")
        record = alpha.put(b"x", _meta())
        assert Path(record.path).parent == alpha.directory
        assert beta.directory != alpha.directory


# --- engine integration -------------------------------------------------------


def _worker(name: str, in_t, out_t, reply: str):
    async def run(self, input, ctx):
        return out_t(text=reply)

    cls = type(
        name,
        (Worker,),
        {"prompt": "produce", "input_type": in_t, "output_type": out_t, "run": run},
    )
    registry.register(cls, "test")
    return cls


def _chain(worker_cls, in_t, out_t, root_artifacts=()):
    def build(self, g):
        g.add_node(worker_cls)
        g.add_edge(START, worker_cls)
        g.add_edge(worker_cls, END)

    chain = type(
        "Chain",
        (Workflow,),
        {
            "input_type": in_t,
            "output_type": out_t,
            "artifacts": root_artifacts,
            "build": build,
        },
    )
    return chain, worker_cls


async def _run(chain, tmp_path, artifacts_store=None, replies=None):
    kwargs = {"artifacts": artifacts_store} if artifacts_store is not None else {}
    engine = Engine(
        FakeProvider(replies or ['{"text":"ok"}']),
        RunStore(tmp_path / "runs"),
        checkpointer="memory",
        **kwargs,
    )
    result = await engine.run(chain, chain.input_type(text="hello"))
    return engine, result


def _writes(engine, run_id):
    return [r for r in engine.store.load(run_id).records if r.kind == "artifact_write"]


async def test_declared_artifacts_persist_and_emit_provenance(tmp_path: Path):
    In = type("In", (BaseModel,), {"__annotations__": {"text": str}})
    Out = type("Out", (BaseModel,), {"__annotations__": {"text": str}})
    worker = _worker("W", In, Out, '{"text":"ok"}')
    worker.artifacts = ("text",)
    chain, _ = _chain(worker, In, Out)

    store = ArtifactStore(tmp_path / "projects", "demo")
    engine, result = await _run(chain, tmp_path, store)

    assert result.status == "completed"
    records = _writes(engine, result.run_id)
    assert len(records) == 1
    node_path = f"{workflow_class_path(chain)}.{workflow_class_path(worker)}"
    assert records[0].node_path == node_path

    payload = records[0].payload
    assert set(payload) == {"artifact_sha256", "name", "input_hashes"}
    assert payload["name"] == "text"
    assert payload["input_hashes"] == {"text": hash_value("hello")}

    blob = Path(store.directory / payload["artifact_sha256"])
    assert json.loads(blob.read_text()) == "ok"
    sidecar = json.loads(Path(str(blob) + ".json").read_text())
    assert sidecar["run_id"] == result.run_id
    assert sidecar["node_path"] == node_path
    assert sidecar["name"] == "text"
    assert sidecar["sha256"] == payload["artifact_sha256"]


async def test_root_scope_artifacts_persist_on_completion(tmp_path: Path):
    In = type("In", (BaseModel,), {"__annotations__": {"text": str}})
    Out = type("Out", (BaseModel,), {"__annotations__": {"text": str}})
    chain, _ = _chain(_worker("W", In, Out, '{"text":"ok"}'), In, Out, root_artifacts=("text",))
    registry.register(chain, "test")

    store = ArtifactStore(tmp_path / "projects", "demo")
    engine, result = await _run(chain, tmp_path, store)

    assert result.status == "completed"
    records = _writes(engine, result.run_id)
    assert len(records) == 1
    assert records[0].node_path == workflow_class_path(chain)
    assert records[0].payload["input_hashes"] == {"text": hash_value("hello")}
    blob = Path(store.directory / records[0].payload["artifact_sha256"])
    assert json.loads(blob.read_text()) == "ok"


async def test_undeclared_fields_do_not_persist(tmp_path: Path):
    In = type("In", (BaseModel,), {"__annotations__": {"text": str}})
    Out = type(
        "Out",
        (BaseModel,),
        {"__annotations__": {"text": str, "extra": int}, "extra": 0},
    )
    worker = _worker("W", In, Out, "7")
    worker.artifacts = ("extra",)
    chain, _ = _chain(worker, In, Out)
    registry.register(chain, "test")

    store = ArtifactStore(tmp_path / "projects", "demo")
    engine, result = await _run(chain, tmp_path, store, replies=['{"text":"ok","extra":7}'])

    assert result.status == "completed"
    writes = [r.payload["name"] for r in _writes(engine, result.run_id)]
    assert writes == ["extra"]
    blobs = [p for p in store.directory.iterdir() if not p.name.endswith(".json")]
    assert len(blobs) == 1
    assert json.loads(blobs[0].read_text()) == 7


async def test_no_artifact_store_means_no_persistence(tmp_path: Path):
    In = type("In", (BaseModel,), {"__annotations__": {"text": str}})
    Out = type("Out", (BaseModel,), {"__annotations__": {"text": str}})
    worker = _worker("W", In, Out, '{"text":"ok"}')
    worker.artifacts = ("text",)
    chain, _ = _chain(worker, In, Out)
    registry.register(chain, "test")

    engine, result = await _run(chain, tmp_path)

    assert result.status == "completed"
    assert _writes(engine, result.run_id) == []
