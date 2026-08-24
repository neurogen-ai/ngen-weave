"""Engine tests: run files, flat graph execution, control routing, retries.

Covers RunStore atomicity and stream integrity, then Engine.compile/run
against flat graphs: worker chains, control pass/fail and model-mode routing,
fan-in assembly, provenance emission, and resume behavior. Nesting lives in
test_nesting.py.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from tests.fakes import FakeProvider

import ngen_weave.engine.runner as ngen_runner
from ngen_weave import registry
from ngen_weave.engine import Engine
from ngen_weave.engine.runner import _INPUT_KEY, _LAST_KEY  # noqa: F401
from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, InfraError
from ngen_weave.models.provider import Completion
from ngen_weave.provenance import ProvenanceRecord
from ngen_weave.registry import register
from ngen_weave.workflow import END, START, Control, Worker, Workflow, workflow_class_path


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated test classes reuse short names; isolate the global registry."""
    registry.reset()
    yield
    registry.reset()


def _record(run_id: str, seq: int) -> ProvenanceRecord:
    return ProvenanceRecord(
        version=1,
        run_id=run_id,
        node_path=f"m.W.node{seq}",
        kind="node_activation",
        ts="2025-01-01T00:00:00+00:00",
        payload={"status": "ok"},
    )


class TestRunFile:
    def test_roundtrip_preserves_every_field(self):
        rf = RunFile(
            format=RUN_FILE_FORMAT,
            run_id="r1",
            workflow="m.W",
            status="running",
            input={"x": 1},
            records=[_record("r1", 0)],
        )
        restored = RunFile.from_dict(rf.to_dict())
        assert restored == rf

    def test_defaults_for_optional_fields(self):
        rf = RunFile(
            format=RUN_FILE_FORMAT, run_id="r", workflow="m.W", status="running", input={}
        )
        assert rf.output is None
        assert rf.error is None
        assert rf.records == []


class TestRunStore:
    def test_create_starts_running_file_with_generated_id(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {"x": 1})
        assert run_id
        loaded = store.load(run_id)
        assert loaded.status == "running"
        assert loaded.workflow == "m.W"
        assert loaded.input == {"x": 1}
        assert loaded.format == RUN_FILE_FORMAT

    def test_load_unknown_run_raises(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        with pytest.raises(Exception, match="unknown run"):
            store.load("nope")

    def test_save_is_atomic_and_valid_json(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        store.append(run_id, _record(run_id, 0))
        raw = (tmp_path / "runs" / f"{run_id}.json").read_text()
        import json

        data = json.loads(raw)  # always valid JSON on disk
        assert data["records"][0]["payload"] == {"status": "ok"}
        leftovers = [p for p in (tmp_path / "runs").iterdir() if p.suffix != ".json"]
        assert leftovers == []  # temp file never survives a save

    def test_append_accumulates_stream_in_order(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        for i in range(3):
            store.append(run_id, _record(run_id, i))
        assert [r.payload["status"] for r in store.load(run_id).records] == [
            "ok",
            "ok",
            "ok",
        ]
        assert [r.node_path for r in store.load(run_id).records] == [
            "m.W.node0",
            "m.W.node1",
            "m.W.node2",
        ]

    def test_set_status_transitions(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        run_id = store.create("m.W", {})
        updated = store.set_status(run_id, "failed")
        assert updated.status == "failed"
        assert store.load(run_id).status == "failed"

    def test_list_returns_every_run(self, tmp_path: Path):
        store = RunStore(tmp_path / "runs")
        first = store.create("m.A", {})
        second = store.create("m.B", {})
        ids = {rf.run_id for rf in store.list()}
        assert ids == {first, second}


# --- runner tests ------------------------------------------------------------





class Root(BaseModel):
    text: str


class Piece(BaseModel):
    text: str


class Final(BaseModel):
    text: str


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}"):
    cls = type(
        name,
        (Worker,),
        {"prompt": prompt, "input_type": in_t, "output_type": out_t},
    )
    register(cls, "test")
    return cls


def make_chain(children, in_t, out_t):
    def build(self, g):
        for c in children:
            g.add_node(c)
        g.add_edge(START, children[0])
        for a, b in zip(children, children[1:], strict=False):
            g.add_edge(a, b)
        g.add_edge(children[-1], END)

    chain = type("Chain", (Workflow,), {"input_type": in_t, "output_type": out_t, "build": build})
    register(chain, "test")
    return chain



def make_engine(replies: list[str] | None, tmp_path):
    provider = FakeProvider(replies if replies is not None else ["{}"])
    engine = Engine(provider, RunStore(tmp_path / "runs"), checkpointer="memory")
    return engine, provider


async def test_linear_worker_chain_completes(tmp_path):
    w1 = make_worker("W1", Root, Piece)
    w2 = make_worker("W2", Piece, Piece)
    w3 = make_worker("W3", Piece, Final)
    chain = make_chain([w1, w2, w3], Root, Final)
    engine, provider = make_engine(['{"text":"one"}', '{"text":"two"}', '{"text":"three"}'],
        tmp_path)

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="three")
    assert [m for _, m in provider.calls] == ["default"] * 3

    rf = engine.store.load(result.run_id)
    kinds = [(r.kind, r.payload.get("status")) for r in rf.records]
    assert kinds.count(("node_activation", "ok")) == 4  # three nodes plus the root scope
    assert kinds.count(("model_call", None)) == 3
    assert rf.output == {"text": "three"}
    assert rf.error is None
    paths = [r.node_path for r in rf.records]
    root_path = workflow_class_path(chain)
    assert f"{root_path}.{workflow_class_path(w2)}" in paths


async def test_activation_metadata_sums_model_calls(tmp_path):
    w1 = make_worker("W1", Root, Piece)
    chain = make_chain([w1], Root, Piece)
    engine, _ = make_engine(['{"text":"abcd"}'], tmp_path)

    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "completed"
    rf = engine.store.load(result.run_id)
    activation = next(r for r in rf.records if r.kind == "node_activation")
    meta = activation.payload["metadata"]
    assert set(meta) == {
        "iterations",
        "tokens_in_context",
        "tokens_total",
        "cost_usd",
        "elapsed_ms",
        "last_output_valid",
    }
    assert meta["tokens_in_context"] == 100  # FakeProvider first call
    assert meta["tokens_total"] == 100 + len('{"text":"abcd"}')
    assert meta["last_output_valid"] is True


def make_control_chain(decide=None, prompt=None):
    GateOut = type("GateOut", (BaseModel,), {"__annotations__": {"pass": bool}})
    attrs: dict = {"input_type": Piece, "output_type": GateOut}
    if decide is not None:
        attrs["decide"] = decide
    else:
        attrs["prompt"] = prompt
    gate = type("Gate", (Control,), attrs)
    good = make_worker("Good", GateOut, Final, prompt="approved on {pass}")
    bad = make_worker("Bad", GateOut, Final, prompt="rejected on {pass}")

    def build(self, g):
        g.add_node(gate)
        g.add_node(good)
        g.add_node(bad)
        g.add_edge(START, gate)
        g.add_conditional_edges(
            gate,
            lambda s: "ok" if s[workflow_class_path(gate)]["pass"] else "flip",
            {"ok": good, "flip": bad},
        )
        g.add_edge(good, END)
        g.add_edge(bad, END)

    chain = type("Gated", (Workflow,), {"input_type": Piece, "output_type": Final, "build": build})
    register(gate, "test")
    register(chain, "test")
    return chain, gate


async def test_programmatic_control_routes_pass_and_fail(tmp_path):
    chain, gate = make_control_chain(decide=lambda self, i: i.text != "skip")

    engine, _ = make_engine(['{"text":"done-good"}', '{"text":"done-bad"}'], tmp_path)
    result = await engine.run(chain, Piece(text="go"))
    assert result.status == "completed"
    assert result.output == Final(text="done-good")

    result2 = await engine.run(chain, Piece(text="skip"))
    assert result2.status == "completed"
    assert result2.output == Final(text="done-bad")


async def test_model_mode_control_parses_boolean_reply(tmp_path):
    chain, gate = make_control_chain(prompt="is {text} acceptable?")
    engine, provider = make_engine(["false", '{"text":"done-bad"}'], tmp_path)

    result = await engine.run(chain, Piece(text="check me"))

    assert result.status == "completed"
    assert result.output == Final(text="done-bad")
    first_call_messages, first_variant = provider.calls[0]
    assert "is check me acceptable?" in first_call_messages[0]["content"]
    assert first_variant == "default"
    rf = engine.store.load(result.run_id)
    model_calls = [r for r in rf.records if r.kind == "model_call"]
    assert model_calls[0].payload["variant"] == "default"


async def test_invalid_worker_output_fails_without_retry(tmp_path):
    w1 = make_worker("W1", Root, Piece)
    chain = make_chain([w1], Root, Piece)
    engine, _ = make_engine(["total garbage"], tmp_path)

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "failed"
    assert result.output is None
    rf = engine.store.load(result.run_id)
    assert rf.error is not None and rf.error["type"] == "DataError"
    statuses = [r.payload.get("status") for r in rf.records if r.kind == "node_activation"]
    assert "retry" not in statuses
    assert "invalid" in statuses


async def test_unmapped_router_label_is_data_error(tmp_path):
    GateOut = type("GateOut", (BaseModel,), {"__annotations__": {"pass": bool}})
    gate = type(
        "Gate",
        (Control,),
        {
            "input_type": Piece,
            "output_type": GateOut,
            "decide": lambda self, i: True,
        },
    )
    fallback = make_worker("Fallback", GateOut, Final, prompt="echo {pass}")

    def build(self, g):
        g.add_node(gate)
        g.add_node(fallback)
        g.add_edge(START, gate)
        g.add_conditional_edges(gate, lambda s: "nowhere", {"known": fallback})
        g.add_edge(fallback, END)

    chain = type("BadBranches", (Workflow,), {"input_type": Piece, "output_type": Final,
        "build": build})
    register(chain, "test")
    engine, _ = make_engine(None, tmp_path)

    result = await engine.run(chain, Piece(text="x"))
    assert result.status == "failed"
    rf = engine.store.load(result.run_id)
    assert rf.error is not None and "absent from its branch map" in rf.error["message"]


async def test_resume_on_completed_run_is_noop(tmp_path):
    chain = make_chain([make_worker("W1", Root, Final)], Root, Final)
    engine, _ = make_engine(['{"text":"out"}'], tmp_path)
    result = await engine.run(chain, Root(text="hi"))
    before = len(engine.store.load(result.run_id).records)

    again = await engine.resume(result.run_id)

    assert again.status == "completed"
    assert again.output == Final(text="out")
    assert len(engine.store.load(result.run_id).records) == before


async def test_named_slot_fanin_assembles_inputs(tmp_path):
    a = make_worker("A", Root, Piece, prompt="a sees {text}")
    b = make_worker("B", Piece, Piece, prompt="b sees {text}")

    class SynthIn(BaseModel):
        first: Piece
        second: Piece

    synth = make_worker("Synth", SynthIn, Final, prompt="merged {first.text} with {second.text}")

    def build(self, g):
        g.add_node(a)
        g.add_node(b)
        g.add_node(synth)
        g.add_edge(START, a)
        g.add_edge(a, b)
        g.add_edge(a, synth, into="first")
        g.add_edge(b, synth, into="second")
        g.add_edge(synth, END)

    chain = type("Slotted", (Workflow,), {"input_type": Root, "output_type": Final, "build": build})
    register(chain, "test")
    engine, provider = make_engine(
        ['{"text":"p1"}', '{"text":"p2"}', '{"text":"merged-out"}'], tmp_path
    )

    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "completed"
    third_prompt = provider.calls[2][0][0]["content"]
    assert third_prompt == "merged p1 with p2"


async def test_collected_fanin_assembles_list_field(tmp_path):
    r1 = make_worker("R1", Root, Piece)
    r2 = make_worker("R2", Piece, Piece)
    r3 = make_worker("R3", Piece, Piece)

    class Reviews(BaseModel):
        reviews: list[Piece] = Field(min_length=2, max_length=5)

    reducer = make_worker("Reduce", Reviews, Final, prompt="reducing {reviews}")

    def build(self, g):
        g.add_node(r1)
        g.add_node(r2)
        g.add_node(r3)
        g.add_node(reducer)
        g.add_edge(START, r1)
        g.add_edge(r1, r2)
        g.add_edge(r1, r3)
        g.add_edge(r1, reducer)
        g.add_edge(r2, reducer)
        g.add_edge(r3, reducer)
        g.add_edge(reducer, END)

    chain = type("Collected", (Workflow,), {"input_type": Root, "output_type": Final,
        "build": build})
    register(chain, "test")
    engine, provider = make_engine(
        ['{"text":"a"}', '{"text":"b"}', '{"text":"c"}', '{"text":"final"}'], tmp_path
    )

    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "completed"
    last_prompt = provider.calls[3][0][0]["content"]
    assert "a" in last_prompt and "b" in last_prompt and "c" in last_prompt


async def test_dispatch_target_receives_sender_output(tmp_path):
    GateOut = type("GateOut", (BaseModel,), {"__annotations__": {"pass": bool}})
    gate = type(
        "Gate",
        (Control,),
        {"input_type": Piece, "output_type": GateOut, "decide": lambda self, i: True},
    )
    after = make_worker("After", GateOut, Final, prompt="got {pass}")

    def build(self, g):
        g.add_node(gate)
        g.add_node(after)
        g.add_edge(START, gate)
        g.add_conditional_edges(gate, lambda s: "only", {"only": after})
        g.add_edge(after, END)

    chain = type("Dispatched", (Workflow,), {"input_type": Piece, "output_type": Final,
        "build": build})
    register(chain, "test")
    engine, provider = make_engine(['{"text":"after-out"}'], tmp_path)

    result = await engine.run(chain, Piece(text="x"))
    assert result.status == "completed"
    assert provider.calls[0][0][0]["content"] == "got True"


async def test_sqlite_checkpointer_run_and_resume_completed(tmp_path):

    chain = make_chain([make_worker("W1", Root, Final)], Root, Final)
    engine, _ = make_engine(['{"text":"persisted"}'], tmp_path)
    engine.checkpointer = "sqlite"
    engine.db_path = tmp_path / "cp.db"

    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "completed"

    fresh_store = RunStore(tmp_path / "runs")
    engine2 = Engine(FakeProvider(['{"text":"ignored"}']), fresh_store, checkpointer="sqlite",
        db_path=tmp_path / "cp.db")
    again = await engine2.resume(result.run_id)
    assert again.status == "completed"
    assert again.output == Final(text="persisted")


async def test_resume_with_payload_before_human_support_raises(tmp_path):
    chain = make_chain([make_worker("W1", Root, Final)], Root, Final)
    engine, _ = make_engine(['{"text":"out"}'], tmp_path)
    result = await engine.run(chain, Root(text="hi"))
    with pytest.raises(ConfigError, match="later step"):
        await engine.resume(result.run_id, payload={"verdict": "approve"})


# --- retry policy ------------------------------------------------------------


class FlakyProvider(FakeProvider):
    """FakeProvider that raises InfraError for the first `fail_times` calls."""

    def __init__(self, replies: list[str], fail_times: int) -> None:
        super().__init__(replies)
        self.fail_times = fail_times

    async def complete(self, messages: list[dict], *, variant: str | None = None) -> Completion:
        if len(self.calls) < self.fail_times:
            self.calls.append((messages, variant))
            raise InfraError("transport down")
        return await super().complete(messages, variant=variant)


async def test_infra_failure_retries_with_backoff_then_fails(tmp_path, monkeypatch):
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(ngen_runner, "_sleep", fake_sleep)
    w1 = make_worker("W1", Root, Piece)
    chain = make_chain([w1], Root, Piece)
    provider = FlakyProvider(['{"text":"never"}'], fail_times=99)
    engine = Engine(
        provider,
        RunStore(tmp_path / "runs"),
        checkpointer="memory",
        max_retries=2,
        retry_backoff_ms=5,
    )

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "failed"
    assert provider.calls.__len__() == 3  # initial attempt + two retries
    assert delays == [0.005, 0.010]  # exponential from the base
    rf = engine.store.load(result.run_id)
    assert rf.error is not None and rf.error["type"] == "InfraError"
    retries = [
        r.payload["attempt"]
        for r in rf.records
        if r.kind == "node_activation" and r.payload.get("status") == "retry"
    ]
    assert retries == [1, 2]


async def test_transient_infra_failure_recovers_within_budget(tmp_path, monkeypatch):
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ngen_runner, "_sleep", fake_sleep)
    w1 = make_worker("W1", Root, Final)
    chain = make_chain([w1], Root, Final)
    provider = FlakyProvider(['{"text":"recovered"}'], fail_times=1)
    engine = Engine(
        provider,
        RunStore(tmp_path / "runs"),
        checkpointer="memory",
        max_retries=3,
        retry_backoff_ms=1,
    )

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="recovered")
    rf = engine.store.load(result.run_id)
    retries = [
        r for r in rf.records if r.kind == "node_activation" and r.payload.get("status") == "retry"
    ]
    assert len(retries) == 1
