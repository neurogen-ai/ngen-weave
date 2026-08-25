"""Nesting tests: recursive composition, cost attribution, deep resume.

Covers success criterion 3: a two-level nested workflow attributes per-scope
RunMetadata correctly (each composite's record sums its own records plus its
descendants'), node paths accumulate one class-path segment per level, and a
crashed run resumes from a fresh Engine against the same SQLite database.
Variant bindings resolve through the full enclosing-scope chain.
"""

from pathlib import Path  # noqa: F401  (tmp_path fixtures)
from typing import Literal

import pytest
from pydantic import BaseModel, Field
from tests.fakes import FakeProvider

import ngen_weave.engine.runner as ngen_runner
from ngen_weave import registry
from ngen_weave.engine import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, InfraError
from ngen_weave.models.provider import Completion
from ngen_weave.registry import register
from ngen_weave.workflow import (
    END,
    START,
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


class Root(BaseModel):
    text: str


class Piece(BaseModel):
    text: str


class Final(BaseModel):
    text: str


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}"):
    cls = type(name, (Worker,), {"prompt": prompt, "input_type": in_t, "output_type": out_t})
    register(cls, "test")
    return cls


def make_chain(name: str, children, in_t, out_t):
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


def make_engine(replies: list[str], tmp_path, **kw) -> Engine:
    return Engine(FakeProvider(replies), RunStore(tmp_path / "runs"), checkpointer="memory", **kw)


def activations(rf, path_prefix: str | None = None):
    return [
        r
        for r in rf.records
        if r.kind == "node_activation"
        and r.payload.get("status") == "ok"
        and (path_prefix is None or r.node_path == path_prefix)
    ]


async def test_two_level_nesting_attributes_cost_per_scope(tmp_path):
    inner_w1 = make_worker("IW1", Root, Piece)
    inner_w2 = make_worker("IW2", Piece, Piece)
    inner = make_chain("Inner", [inner_w1, inner_w2], Root, Piece)
    outer_w = make_worker("OW", Piece, Final)
    outer = make_chain("Outer", [inner, outer_w], Root, Final)

    engine = make_engine(['{"text":"i1"}', '{"text":"i2"}', '{"text":"o1"}'], tmp_path)
    result = await engine.run(outer, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="o1")
    rf = engine.store.load(result.run_id)

    root_path = workflow_class_path(outer)
    inner_path = workflow_class_path(inner)
    w1_path = f"{root_path}.{inner_path}.{workflow_class_path(inner_w1)}"
    assert any(r.node_path == w1_path for r in rf.records), rf.records[0].node_path

    # Inner scope metadata sums only the inner workers; outer sums everything.
    inner_meta = activations(rf, f"{root_path}.{inner_path}")[0].payload["metadata"]
    outer_meta = activations(rf, root_path)[0].payload["metadata"]
    w2_path = f"{root_path}.{inner_path}.{workflow_class_path(inner_w2)}"
    outer_w_path = f"{root_path}.{workflow_class_path(outer_w)}"
    inner_total = sum(
        r.payload["metadata"]["tokens_total"]
        for r in activations(rf)
        if r.node_path in {w1_path, w2_path}
    )
    outer_w_meta = activations(rf, outer_w_path)[0].payload["metadata"]
    assert set(inner_meta) == {
        "iterations",
        "tokens_in_context",
        "tokens_total",
        "cost_usd",
        "elapsed_ms",
        "last_output_valid",
    }
    assert inner_meta["tokens_total"] == inner_total
    assert outer_meta["tokens_total"] == inner_total + outer_w_meta["tokens_total"]
    assert outer_meta["cost_usd"] == pytest.approx(
        inner_meta["cost_usd"] + outer_w_meta["cost_usd"]
    )
    assert inner_meta["last_output_valid"] is True


async def test_nested_composites_fan_in_collected(tmp_path):
    source = make_worker("Src", Root, Piece)
    branch_a_inner = make_chain("BranchA", [make_worker("AW", Piece, Piece)], Piece, Piece)
    branch_b_inner = make_chain("BranchB", [make_worker("BW", Root, Piece)], Root, Piece)

    class Reviews(BaseModel):
        reviews: list[Piece] = Field(min_length=2, max_length=2)

    reducer = make_worker("Reduce", Reviews, Final, prompt="reducing {reviews}")

    def build(self, g):
        g.add_node(source)
        g.add_node(branch_a_inner)
        g.add_node(branch_b_inner)
        g.add_node(reducer)
        g.add_edge(START, source)
        g.add_edge(source, branch_a_inner)
        g.add_edge(source, branch_b_inner)
        g.add_edge(branch_a_inner, reducer)
        g.add_edge(branch_b_inner, reducer)
        g.add_edge(reducer, END)

    outer = type(
        "CollectedNest", (Workflow,), {"input_type": Root, "output_type": Final, "build": build}
    )
    register(outer, "test")

    engine = make_engine(
        ['{"text":"s"}', '{"text":"a"}', '{"text":"b"}', '{"text":"both"}'], tmp_path
    )
    result = await engine.run(outer, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="both")
    rf = engine.store.load(result.run_id)
    # Depth-2 activations exist under both branch composites.
    a_path = workflow_class_path(branch_a_inner)
    b_path = workflow_class_path(branch_b_inner)
    depth2 = {r.node_path for r in rf.records if a_path in r.node_path or b_path in r.node_path}
    assert len(depth2) >= 2
    reduce_meta = [
        r.payload["metadata"]
        for r in activations(rf)
        if workflow_class_path(reducer) in r.node_path
    ]
    assert len(reduce_meta) == 1


async def test_variant_bindings_resolve_through_enclosing_scopes(tmp_path):
    leaf_in = make_worker("LeafIn", Root, Final)
    leaf_out = make_worker("LeafOut", Final, Final)
    inner = make_chain("Inner", [leaf_in], Root, Final)
    outer = make_chain("Outer", [inner, leaf_out], Root, Final)

    models = {
        workflow_class_path(outer): "outer_v",
        workflow_class_path(inner): "inner_v",
    }
    engine = make_engine(['{"text":"in"}', '{"text":"out"}'], tmp_path)
    compiled = engine.compile(outer, models)

    assert compiled.variants[workflow_class_path(leaf_out)] == "outer_v"
    # The inner graph's variant table lives on the child CompiledGraph.
    child_compiled = next(
        c for k, c in engine._compiled.items() if k[0] == workflow_class_path(inner)
    )
    assert child_compiled.variants[workflow_class_path(leaf_in)] == "inner_v"

    result = await engine.run(outer, Root(text="hi"), models=models)
    assert result.status == "completed"


async def test_cyclic_composite_wiring_fails_at_compile(tmp_path):
    def build_a(self, g):
        g.add_node(B)
        g.add_edge(START, B)
        g.add_edge(B, END)

    def build_b(self, g):
        g.add_node(A)
        g.add_edge(START, A)
        g.add_edge(A, END)

    A = type(
        "CycleA",
        (Workflow,),
        {
            "input_type": Root,
            "output_type": Final,
            "build": build_a,
            # Each side names the other before both exist; validation of the
            # pair itself is not the point here, compile-time cycle detection is.
            "_defer_validation": True,
        },
    )
    B = type(
        "CycleB",
        (Workflow,),
        {"input_type": Root, "output_type": Final, "build": build_b, "_defer_validation": True},
    )
    register(A, "test")
    register(B, "test")

    engine = make_engine(["{}"], tmp_path)
    with pytest.raises(ConfigError, match="[Cc]yclic"):
        engine.compile(A)


async def test_crash_mid_run_resumes_from_fresh_engine(tmp_path, monkeypatch):
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ngen_runner, "_sleep", fake_sleep)

    class Flaky(FakeProvider):
        def __init__(self, replies: list[str], fail_times: int) -> None:
            super().__init__(replies)
            self.fail_times = fail_times

        async def complete(self, messages: list[dict], *, variant: str | None = None) -> Completion:
            if len(self.calls) < self.fail_times:
                self.calls.append((messages, variant))
                raise InfraError("transport down")
            return await super().complete(messages, variant=variant)

    inner_w1 = make_worker("SW1", Root, Piece)
    inner_w2 = make_worker("SW2", Piece, Final)
    inner = make_chain("SurvivorInner", [inner_w1, inner_w2], Root, Final)
    outer = make_chain("SurvivorOuter", [inner], Root, Final)

    broken = Engine(
        Flaky(["{}", "{}"], fail_times=99),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
        max_retries=1,
        retry_backoff_ms=1,
    )
    failed = await broken.run(outer, Root(text="hi"))
    assert failed.status == "failed"

    # Fresh process state: new Engine, same sqlite db and run files.
    healed = Engine(
        FakeProvider(['{"text":"i1"}', '{"text":"done"}']),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
    )
    resumed = await healed.resume(failed.run_id)

    assert resumed.status == "completed"
    assert resumed.output == Final(text="done")
    rf = healed.store.load(resumed.run_id)
    ok_records = activations(rf)
    root_path = workflow_class_path(outer)
    assert any(r.node_path == root_path for r in ok_records)
    depth2 = f"{root_path}.{workflow_class_path(inner)}.{workflow_class_path(inner_w2)}"
    assert any(r.node_path == depth2 for r in ok_records)


# --- depth-2 interrupt: success criterion 3's second half --------------------


class Review(BaseModel):
    verdict: Literal["approve", "reject"]
    notes: str = ""


def _make_review_inner():
    h = type(
        "InnerReview",
        (Human,),
        {"input_type": Piece, "output_type": Review, "state_type": Review},
    )
    register(h, "test")
    fin = make_worker("InnerFinish", Review, Final, prompt="ok {verdict}")
    rej = make_worker("InnerReject", Review, Final, prompt="no {verdict}")
    hp = workflow_class_path(h)

    def build(self, g):
        g.add_node(h)
        g.add_node(fin)
        g.add_node(rej)
        g.add_edge(fin, END)
        g.add_edge(rej, END)
        g.add_edge(START, h)
        g.add_conditional_edges(h, lambda s: s[hp]["verdict"], {"approve": fin, "reject": rej})

    inner = type(
        "ReviewInner",
        (Workflow,),
        {"input_type": Piece, "output_type": Final, "build": build},
    )
    register(inner, "test")
    return inner, h, fin


async def test_interrupt_at_depth_two_resumes(tmp_path):
    inner, h, fin = _make_review_inner()
    outer = make_chain("ReviewOuter", [inner], Piece, Final)
    engine = make_engine(['{"text":"deep-done"}'], tmp_path)

    waiting = await engine.run(outer, Piece(text="go"))

    assert waiting.status == "waiting_human"
    h_path = f"{workflow_class_path(outer)}.{workflow_class_path(inner)}.{workflow_class_path(h)}"
    assert waiting.waiting["node_path"] == h_path

    result = await engine.resume(waiting.run_id, payload={"verdict": "approve", "notes": ""})

    assert result.status == "completed"
    assert result.output == Final(text="deep-done")
    rf = engine.store.load(waiting.run_id)
    ok_paths = [
        r.node_path
        for r in rf.records
        if r.kind == "node_activation" and r.payload.get("status") == "ok"
    ]
    # Per-scope metadata at every level: human node, inner composite, root.
    for expected in (h_path, f"{workflow_class_path(outer)}.{workflow_class_path(inner)}"):
        assert expected in ok_paths
    writes = [r for r in rf.records if r.kind == "artifact_write"]
    assert len(writes) == 1


async def test_interrupt_at_depth_two_resumes_sqlite(tmp_path):
    """Depth-2 interrupt resume on the durable sqlite checkpointer.

    Mirrors test_interrupt_at_depth_two_resumes on sqlite: nested graphs get
    thread ids derived from the run id and their checkpoint namespace, so the
    child's checkpoints cannot interleave with the root's in one chain and a
    durable resume replays against the right graph state.
    """
    inner, h, _fin = _make_review_inner()
    outer = make_chain("ReviewOuterLite", [inner], Piece, Final)
    engine = Engine(
        FakeProvider(['{"text":"deep-done"}']),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
    )

    waiting = await engine.run(outer, Piece(text="go"))
    assert waiting.status == "waiting_human"

    # Fresh-engine resume against the same sqlite database, as the CLI does.
    healed = Engine(
        FakeProvider(['{"text":"deep-done"}']),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
    )
    result = await healed.resume(waiting.run_id, payload={"verdict": "approve", "notes": ""})

    assert result.status == "completed"
    assert result.output == Final(text="deep-done")
