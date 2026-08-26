"""Engine tests: run files, flat graph execution, control routing, retries.

Covers RunStore atomicity and stream integrity, then Engine.compile/run
against flat graphs: worker chains, control pass/fail and model-mode routing,
fan-in assembly, provenance emission, and resume behavior. Nesting lives in
test_nesting.py.
"""

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field
from tests.fakes import FakeProvider

import ngen_weave.engine.runner as ngen_runner
from ngen_weave import registry
from ngen_weave.engine import Engine
from ngen_weave.engine.runner import _INPUT_KEY, _LAST_KEY  # noqa: F401
from ngen_weave.engine.state import RUN_FILE_FORMAT, RunFile
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, DataError, InfraError
from ngen_weave.models.provider import Completion
from ngen_weave.provenance import ProvenanceRecord
from ngen_weave.registry import register
from ngen_weave.workflow import (
    END,
    START,
    Control,
    Human,
    Worker,
    Workflow,
    workflow_class_path,
)


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
        rf = RunFile(format=RUN_FILE_FORMAT, run_id="r", workflow="m.W", status="running", input={})
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


def make_chain(children, in_t, out_t, name: str = "Chain"):
    def build(self, g):
        for c in children:
            g.add_node(c)
        g.add_edge(START, children[0])
        for a, b in zip(children, children[1:], strict=False):
            g.add_edge(a, b)
        g.add_edge(children[-1], END)

    chain = type(name, (Workflow,), {"input_type": in_t, "output_type": out_t, "build": build})
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
    engine, provider = make_engine(
        ['{"text":"one"}', '{"text":"two"}', '{"text":"three"}'], tmp_path
    )

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


async def test_invalid_worker_output_retries_then_fails(tmp_path, monkeypatch):
    import ngen_weave.engine.runner as runner_module

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(runner_module, "_sleep", fake_sleep)
    w1 = make_worker("W1", Root, Piece)
    chain = make_chain([w1], Root, Piece)
    engine, _ = make_engine(["total garbage"], tmp_path)

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "failed"
    assert result.output is None
    rf = engine.store.load(result.run_id)
    assert rf.error is not None and rf.error["type"] == "AgentReplyError"
    statuses = [r.payload.get("status") for r in rf.records if r.kind == "node_activation"]
    assert statuses.count("retry") == engine.max_retries
    assert "invalid" not in statuses  # AgentReplyError bypasses the invalid branch


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

    chain = type(
        "BadBranches", (Workflow,), {"input_type": Piece, "output_type": Final, "build": build}
    )
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

    chain = type(
        "Collected", (Workflow,), {"input_type": Root, "output_type": Final, "build": build}
    )
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

    chain = type(
        "Dispatched", (Workflow,), {"input_type": Piece, "output_type": Final, "build": build}
    )
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
    engine2 = Engine(
        FakeProvider(['{"text":"ignored"}']),
        fresh_store,
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
    )
    again = await engine2.resume(result.run_id)
    assert again.status == "completed"
    assert again.output == Final(text="persisted")


async def test_resume_with_payload_on_non_waiting_run_raises(tmp_path):
    chain = make_chain([make_worker("W1", Root, Final)], Root, Final)
    engine, _ = make_engine(['{"text":"out"}'], tmp_path)
    result = await engine.run(chain, Root(text="hi"))
    with pytest.raises(ConfigError, match="not waiting for human input"):
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


# --- human nodes: artifacts, interrupts, resume ------------------------------


class Review(BaseModel):
    verdict: Literal["approve", "reject"]
    notes: str = ""


class StrictReview(BaseModel):
    verdict: Literal["approve", "reject"]
    notes: str  # required-without-default


def make_human(name, in_t, out_t, state_t=None, prefill=None, extra=None):
    body = {
        "input_type": in_t,
        "output_type": out_t,
        "state_type": state_t or out_t,
        "prefill": prefill or {},
    }
    body.update(extra or {})
    cls = type(name, (Human,), body)
    register(cls, "test")
    return cls


def make_review_flow(h, branches, in_t=Root, out_t=Final, name="Flow"):
    """START -> h -> conditional edges on the submitted verdict."""
    h_path = workflow_class_path(h)

    def router(state):
        return state[h_path]["verdict"]

    def build(self, g):
        g.add_node(h)
        for target in branches.values():
            if target is not END:
                g.add_node(target)
                g.add_edge(target, END)
        g.add_edge(START, h)
        mapping = {label: target for label, target in branches.items()}
        g.add_conditional_edges(h, router, mapping)

    wf = type(name, (Workflow,), {"input_type": in_t, "output_type": out_t, "build": build})
    register(wf, "test")
    return wf


def test_parse_output_strips_markdown_code_fence():
    """Real models habitually fence JSON; the parser tolerates one wrapper."""
    from pydantic import BaseModel

    from ngen_weave.engine.runner import parse_output

    class Out(BaseModel):
        text: str

    fenced = '```json\n{"text": "hi"}\n```'
    parsed = parse_output(Out, fenced, "p")
    assert parsed.text == "hi"
    # Unfenced JSON and bare values keep working.
    assert parse_output(Out, '{"text": "hi"}', "p").text == "hi"


async def test_human_run_writes_artifact_and_waits(tmp_path):
    import yaml

    h = make_human("ReviewA", Root, Review)
    fin = make_worker("FinA", Review, Final, prompt="ok {verdict}")
    rej = make_worker("RejA", Review, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    engine, _ = make_engine(['{"text":"done"}'], tmp_path)

    result = await engine.run(flow, Root(text="hi"))

    assert result.status == "waiting_human"
    assert result.output is None
    assert result.waiting["node_path"] == f"{workflow_class_path(flow)}.{workflow_class_path(h)}"
    artifact = Path(result.waiting["artifact"])
    data = yaml.safe_load(artifact.read_text())
    assert set(data) == {"context", "response"}
    assert data["context"] == {"text": "hi"}
    assert data["response"] == {"verdict": None, "notes": None}
    rf = engine.store.load(result.run_id)
    assert any(
        r.kind == "node_activation" and r.payload.get("status") == "waiting_human"
        for r in rf.records
    )


async def test_prefill_seeds_response_slots(tmp_path):
    import yaml

    h = make_human("ReviewP", Root, Review, prefill={"notes": "text"})
    fin = make_worker("FinP", Review, Final, prompt="ok {verdict}")
    rej = make_worker("RejP", Review, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    engine, _ = make_engine(['{"text":"done"}'], tmp_path)

    result = await engine.run(flow, Root(text="seeded"))

    data = yaml.safe_load(Path(result.waiting["artifact"]).read_text())
    assert data["response"]["notes"] == "seeded"


async def test_resume_with_payload_routes_on_verdict_and_completes(tmp_path):
    h = make_human("ReviewR", Root, Review)
    approve = make_worker("Approve", Review, Final, prompt="ok {verdict}")
    reject = make_worker("Reject", Review, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": approve, "reject": reject})
    engine, _ = make_engine(['{"text":"yes"}', '{"text":"no"}'], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))

    result = await engine.resume(waiting.run_id, payload={"verdict": "approve", "notes": ""})

    assert result.status == "completed"
    assert result.output == Final(text="yes")
    rf = engine.store.load(waiting.run_id)
    writes = [r for r in rf.records if r.kind == "artifact_write"]
    assert len(writes) == 1
    assert set(writes[0].payload) == {"artifact", "artifact_sha256"}
    assert rf.submissions[f"{workflow_class_path(flow)}.{workflow_class_path(h)}"] == {
        "verdict": "approve",
        "notes": "",
    }
    # The rejected branch exists but never ran.
    paths = [r.node_path for r in rf.records if r.payload.get("status") == "ok"]
    assert workflow_class_path(reject) not in " ".join(paths)


async def test_resume_reject_branch_routes_to_other_worker(tmp_path):
    h = make_human("ReviewX", Root, Review)
    approve = make_worker("ApproveX", Review, Final, prompt="ok {verdict}")
    reject = make_worker("RejectX", Review, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": approve, "reject": reject})
    engine, _ = make_engine(['{"text":"no"}'], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))

    result = await engine.resume(waiting.run_id, payload={"verdict": "reject", "notes": ""})

    assert result.status == "completed"
    assert result.output == Final(text="no")


async def test_incomplete_submission_keeps_run_waiting(tmp_path):
    h = make_human("ReviewS", Root, StrictReview, state_t=StrictReview)
    fin = make_worker("FinS", StrictReview, Final, prompt="ok {verdict}")
    rej = make_worker(f"Rej{h.__name__}", h.output_type, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    engine, _ = make_engine(['{"text":"done"}'], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))

    with pytest.raises(DataError, match="notes"):
        await engine.resume(waiting.run_id, payload={"verdict": "approve"})

    assert engine.store.load(waiting.run_id).status == "waiting_human"


async def test_resume_without_payload_reads_local_artifact(tmp_path):
    import yaml

    h = make_human("ReviewL", Root, Review)
    fin = make_worker("FinL", Review, Final, prompt="ok {verdict}")
    rej = make_worker(f"Rej{h.__name__}", h.output_type, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    engine, _ = make_engine(['{"text":"local"}'], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))

    artifact = Path(waiting.waiting["artifact"])
    data = yaml.safe_load(artifact.read_text())
    data["response"] = {"verdict": "approve", "notes": "filled locally"}
    artifact.write_text(yaml.safe_dump(data, sort_keys=False))

    result = await engine.resume(waiting.run_id)

    assert result.status == "completed"
    assert result.output == Final(text="local")


async def test_crash_before_submit_resumes_from_fresh_engine_sqlite(tmp_path):
    h = make_human("ReviewC", Root, Review)
    fin = make_worker("FinC", Review, Final, prompt="ok {verdict}")
    rej = make_worker(f"Rej{h.__name__}", h.output_type, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    runs_dir = tmp_path / "runs"
    db_path = tmp_path / "cp.db"
    engine, _ = make_engine(['{"text":"after-crash"}'], tmp_path)
    engine.checkpointer = "sqlite"
    engine.db_path = db_path
    waiting = await engine.run(flow, Root(text="go"))
    assert waiting.status == "waiting_human"

    fresh_provider = FakeProvider(['{"text":"after-crash"}'])
    fresh = Engine(fresh_provider, RunStore(runs_dir), checkpointer="sqlite", db_path=db_path)
    result = await fresh.resume(waiting.run_id, payload={"verdict": "approve", "notes": ""})

    assert result.status == "completed"
    assert result.output == Final(text="after-crash")


async def test_transform_override_shapes_the_output(tmp_path):
    h = make_human(
        "ReviewT",
        Root,
        Final,
        state_t=Review,
        extra={"transform": lambda self, context, state: Final(text=state.notes)},
    )

    def build(self, g):
        g.add_node(h)
        g.add_edge(START, h)
        g.add_edge(h, END)

    flow = type("FlowT", (Workflow,), {"input_type": Root, "output_type": Final, "build": build})
    register(flow, "test")
    engine, _ = make_engine([], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))

    result = await engine.resume(waiting.run_id, payload={"verdict": "approve", "notes": "shaped"})

    assert result.status == "completed"
    assert result.output == Final(text="shaped")


async def test_resume_completed_run_is_still_a_noop(tmp_path):
    h = make_human("ReviewN", Root, Review)
    fin = make_worker("FinN", Review, Final, prompt="ok {verdict}")
    rej = make_worker(f"Rej{h.__name__}", h.output_type, Final, prompt="no {verdict}")
    flow = make_review_flow(h, {"approve": fin, "reject": rej})
    engine, _ = make_engine(['{"text":"done"}'], tmp_path)
    waiting = await engine.run(flow, Root(text="go"))
    done = await engine.resume(waiting.run_id, payload={"verdict": "approve", "notes": ""})

    again = await engine.resume(done.run_id)

    assert again.status == "completed"
    assert again.output == done.output


class _ParentOut(BaseModel):
    text: str
    sort_key: int


def _build_marker_fanin(collector_attrs: dict):
    """Three marker-emitting workers fanning into one list-collecting child."""

    w1 = make_worker("MW1", Root, _ParentOut, prompt="review {text}")
    w2 = make_worker("MW2", Root, _ParentOut, prompt="review {text}")
    w3 = make_worker("MW3", Root, _ParentOut, prompt="review {text}")

    class Reviews(BaseModel):
        reviews: list[_ParentOut] = Field(min_length=3, max_length=3)

    collector_cls = type(
        "MarkerCollector",
        (Worker,),
        collector_attrs | {"input_type": Reviews, "output_type": Final},
    )
    register(collector_cls, "test")

    def build(self, g):
        g.add_node(w1)
        g.add_node(w2)
        g.add_node(w3)
        g.add_node(collector_cls)
        g.add_edge(START, w1)
        # Declaration order of the fan-in edges fixes assembly order:
        # w1, then w2, then w3.
        g.add_edge(w1, collector_cls)
        g.add_edge(w1, w2)
        g.add_edge(w2, collector_cls)
        g.add_edge(w2, w3)
        g.add_edge(w3, collector_cls)
        g.add_edge(collector_cls, END)

    chain = type(
        "MarkerFanin",
        (Workflow,),
        {"input_type": Root, "output_type": Final, "build": build},
    )
    register(chain, "test")
    return chain


_REPLIES = [
    '{"text":"MARKER-1","sort_key":3}',
    '{"text":"MARKER-2","sort_key":1}',
    '{"text":"MARKER-3","sort_key":2}',
    '{"text":"collected"}',
]


async def test_collected_fanin_preserves_every_marker_in_declaration_order(tmp_path):
    chain = _build_marker_fanin(
        {
            "prompt": "{reviews[0].text} | {reviews[1].text} | {reviews[2].text}",
        }
    )
    engine, provider = make_engine(_REPLIES, tmp_path)

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "completed"
    collector_prompt = provider.calls[3][0][0]["content"]
    # NO LOSS: every parent marker reaches the collector prompt exactly once.
    for i in (1, 2, 3):
        assert collector_prompt.count(f"MARKER-{i}") == 1
    # DEFAULT ORDER: add_edge declaration order (index positions ascending).
    positions = [collector_prompt.index(f"MARKER-{i}") for i in (1, 2, 3)]
    assert positions == sorted(positions)


async def test_collected_fanin_is_deterministic_across_fresh_engines(tmp_path):
    chain = _build_marker_fanin(
        {
            "prompt": "{reviews[0].text} | {reviews[1].text} | {reviews[2].text}",
        }
    )
    prompts = []
    for i in range(2):
        engine, provider = make_engine(_REPLIES, tmp_path / f"run{i}")
        result = await engine.run(chain, Root(text="hi"))
        assert result.status == "completed"
        prompts.append(provider.calls[3][0][0]["content"])

    assert prompts[0] == prompts[1]


@pytest.mark.xfail(strict=True, reason="collect_order lands in v0.1.3")
async def test_collect_order_sort_key_reorders_fanin(tmp_path):
    chain = _build_marker_fanin(
        {
            "collect_order": "sort_key",
            "prompt": "{reviews[0].text} | {reviews[1].text} | {reviews[2].text}",
        }
    )
    engine, provider = make_engine(_REPLIES, tmp_path)

    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "completed"
    collector_prompt = provider.calls[3][0][0]["content"]
    # sort_key values are MARKER-1:3, MARKER-2:1, MARKER-3:2, so sorted
    # order is MARKER-2 < MARKER-3 < MARKER-1.
    positions = [collector_prompt.index(f"MARKER-{i}") for i in (2, 3, 1)]
    assert positions == sorted(positions)


class TestCompileCacheKey:
    """Compile caching must key on outer scopes, not just class and bindings."""

    async def test_same_class_different_outer_scopes_get_distinct_graphs(self, tmp_path):
        leaf = make_worker("CacheLeaf", Root, Piece)
        middle = make_chain([leaf], Root, Piece)
        outer_a = make_chain([middle], Root, Piece, name="CacheOuterA")
        outer_b = make_chain([middle], Root, Piece, name="CacheOuterB")
        # Same root class and the same bindings dict: the pre-fix key (root
        # path + models, without outer_scopes) collides on the second
        # lookup. Only outer_scopes differs between the two compilations.
        models = {workflow_class_path(outer_a): "slow"}
        engine, provider = make_engine(["{}"], tmp_path)
        graph_a = engine.compile(middle, models, outer_scopes=(outer_a,))
        graph_b = engine.compile(middle, models, outer_scopes=(outer_b,))
        assert graph_a is not graph_b
        assert graph_a.variants[workflow_class_path(leaf)] == "slow"
        assert graph_b.variants[workflow_class_path(leaf)] == "default"

    async def test_identical_compilation_returns_cached_object(self, tmp_path):
        leaf = make_worker("CacheLeaf2", Root, Piece)
        chain = make_chain([leaf], Root, Piece)
        engine, _ = make_engine(["{}"], tmp_path)
        models = {workflow_class_path(leaf): "fast"}
        first = engine.compile(chain, models)
        second = engine.compile(chain, models)
        assert first is second


class TestAgentReplyRetries:
    """Schema-invalid model replies retry, then fail with a friendly report.

    Pins the two validation regimes: agent replies get max_retries attempts,
    user payloads abort once (CLI side in test_cli.py), and ProviderError
    stays non-retryable inside the leaf loop.
    """

    @staticmethod
    def _patch_sleep(monkeypatch):
        import ngen_weave.engine.runner as runner_module

        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        monkeypatch.setattr(runner_module, "_sleep", fake_sleep)
        return delays

    async def test_prose_forever_fails_after_retries_with_field_report(self, tmp_path, monkeypatch):
        delays = self._patch_sleep(monkeypatch)
        w1 = make_worker("ProseW", Root, Piece)
        chain = make_chain([w1], Root, Piece)
        provider = FakeProvider(["just some prose, no json here"])
        engine = Engine(provider, RunStore(tmp_path / "runs"), checkpointer="memory")
        engine.max_retries = 3

        result = await engine.run(chain, Root(text="hi"))

        assert result.status == "failed"
        rf = engine.store.load(result.run_id)
        assert rf.error is not None and rf.error["type"] == "AgentReplyError"
        message = rf.error["message"]
        assert "Piece:" in message  # model name heads the field report
        assert "  - " in message  # field line from the formatter
        assert "last reply:" in message
        # one backoff per retry after the initial attempt
        assert len(delays) == engine.max_retries

    async def test_valid_reply_on_second_call_completes_with_two_iterations(
        self, tmp_path, monkeypatch
    ):
        self._patch_sleep(monkeypatch)
        w1 = make_worker("SecondTryW", Root, Piece)
        chain = make_chain([w1], Root, Piece)
        provider = FakeProvider(["nope", '{"text":"recovered"}'])
        engine = Engine(provider, RunStore(tmp_path / "runs"), checkpointer="memory")

        result = await engine.run(chain, Root(text="hi"))

        assert result.status == "completed"
        rf = engine.store.load(result.run_id)
        activation = next(
            r for r in rf.records if r.kind == "node_activation" and r.payload.get("status") == "ok"
        )
        assert activation.payload["metadata"]["iterations"] == 2

    async def test_unparseable_control_reply_retries_then_fails(self, tmp_path, monkeypatch):
        delays = self._patch_sleep(monkeypatch)
        chain, _gate = make_control_chain(prompt="is {text} acceptable?")
        provider = FakeProvider(["the committee has deliberated at length"])
        engine = Engine(provider, RunStore(tmp_path / "runs"), checkpointer="memory")
        engine.max_retries = 2

        result = await engine.run(chain, Piece(text="hi"))

        assert result.status == "failed"
        rf = engine.store.load(result.run_id)
        assert rf.error is not None and rf.error["type"] == "AgentReplyError"
        message = rf.error["message"]
        assert "not a parseable boolean verdict" in message
        assert "last reply:" in message
        assert len(delays) == engine.max_retries

    async def test_provider_error_is_not_retried(self, tmp_path, monkeypatch):
        from ngen_weave.errors import ProviderError

        delays = self._patch_sleep(monkeypatch)

        class BoomProvider(FakeProvider):
            async def complete(self, messages, *, variant=None):
                raise ProviderError("model call failed for variant 'default': boom")

        w1 = make_worker("BoomW", Root, Piece)
        chain = make_chain([w1], Root, Piece)
        engine = Engine(BoomProvider(), RunStore(tmp_path / "runs"), checkpointer="memory")

        result = await engine.run(chain, Root(text="hi"))

        assert result.status == "failed"
        assert len(delays) == 0
        rf = engine.store.load(result.run_id)
        assert rf.error is not None and rf.error["type"] == "ProviderError"
        retries = [
            r
            for r in rf.records
            if r.kind == "node_activation" and r.payload.get("status") == "retry"
        ]
        assert retries == []  # ProviderError never enters the backoff path
