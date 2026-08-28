"""Loop realignment (Branch D, Step 7): single-parent retry loops and the
looping-graph join prohibition.

Covers the compile-time split in _check_fanin: acyclic graphs allow
equal-depth multi-parent fan-in; looping graphs allow single-parent shapes
only, so a control can conditionally re-enter a worker (retry semantics — the
re-fired node reads its static parent's last written output), and any
multi-parent join under a back edge is a ConfigError.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider

from ngen_weave import registry
from ngen_weave.config import Budget, RunSettings
from ngen_weave.engine import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.registry import register
from ngen_weave.workflow import (
    END,
    START,
    Control,
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


class JoinIn(BaseModel):
    items: list[Piece]


# "pass" is a keyword, so the model is built via type() (matches test_engine).
GateOut = type("GateOut", (BaseModel,), {"__annotations__": {"pass": bool}})


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}") -> type[Worker]:
    cls = type(name, (Worker,), {"prompt": prompt, "input_type": in_t, "output_type": out_t})
    register(cls, "test")
    return cls


def make_engine(replies: list[str], tmp_path: Path, *, budget: Budget | None = None) -> Engine:
    settings = RunSettings(checkpointer="memory", budget=budget)
    return Engine(
        FakeProvider(replies),
        RunStore(tmp_path / "runs"),
        checkpointer="memory",
        settings=settings,
    )


def activations_on(rf, node_cls: type[Workflow]) -> int:
    """Successful node_activation records for one node class."""
    leaf_path = workflow_class_path(node_cls)
    return len(
        [
            r
            for r in rf.records
            if r.kind == "node_activation"
            and r.payload.get("status") == "ok"
            and r.node_path.endswith(leaf_path)
        ]
    )


def build_retry_loop(a, b, t, *, always_retry: bool = False):
    """START→A→B→G control; G conditionally re-enters B twice, then T→END.

    Returns (loop, gate): the composite class and the control class, so
    budget-pause assertions can name the control's boundary.
    """
    calls: list[int] = []

    def decide(self, input):
        calls.append(1)
        return not always_retry and len(calls) >= 3

    gate = type(
        "Gate",
        (Control,),
        {"input_type": Piece, "output_type": GateOut, "decide": decide},
    )
    register(gate, "test")

    def build(self, g):
        g.add_node(a)
        g.add_node(b)
        g.add_node(gate)
        g.add_node(t)
        g.add_edge(START, a)
        g.add_edge(a, b)
        g.add_edge(b, gate)
        gate_path = workflow_class_path(gate)
        g.add_conditional_edges(
            gate,
            lambda s: "done" if s[gate_path]["pass"] else "retry",
            {"retry": b, "done": t},
        )
        g.add_edge(t, END)

    loop = type(
        "RetryLoop", (Workflow,), {"input_type": Root, "output_type": Final, "build": build}
    )
    register(loop, "test")
    return loop, gate


REPLIES = [
    '{"text":"a"}',
    '{"text":"b1"}',
    '{"text":"b2"}',
    '{"text":"b3"}',
    '{"text":"done"}',
]


async def test_midchain_retry_loop_completes(tmp_path):
    """Control retry: G re-enters B twice via conditional edges, then completes."""
    a = make_worker("A", Root, Piece)
    b = make_worker("B", Piece, Piece)
    t = make_worker("T", GateOut, Final, prompt="done on {pass}")
    loop, _gate = build_retry_loop(a, b, t)
    engine = make_engine(REPLIES, tmp_path)

    result = await engine.run(loop, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="done")
    rf = engine.store.load(result.run_id)
    assert activations_on(rf, b) == 3  # initial pass plus two conditional re-entries
    assert activations_on(rf, a) == 1
    assert activations_on(rf, t) == 1


def test_diamond_fan_in_compile_error(tmp_path):
    """Unequal-depth diamond fan-in stays a compile error naming target and depths."""
    a = make_worker("A", Root, Piece)
    b = make_worker("B", Piece, Piece)
    c = make_worker("C", Piece, Piece)
    d = make_worker("D", Piece, Piece)

    class SlotsIn(BaseModel):
        first: Piece
        second: Piece

    synth = make_worker("Synth", SlotsIn, Final, prompt="merge {first} {second}")

    def build(self, g):
        g.add_node(a)
        g.add_node(b)
        g.add_node(c)
        g.add_node(d)
        g.add_node(synth)
        g.add_edge(START, a)
        g.add_edge(a, b)
        g.add_edge(a, c)
        g.add_edge(c, d)
        g.add_edge(b, synth, into="first")
        g.add_edge(d, synth, into="second")
        g.add_edge(synth, END)

    make_engine(REPLIES, tmp_path)

    # Acyclic, no back edges: the equal-depth rule fires, naming the target
    # and each parent's depth (B's short side vs D's long side of the diamond).
    with pytest.raises(
        ConfigError,
        match=r"Synth has parents at different depths.*\.B at depth 1, .*\.D at depth 2",
    ):
        type("Diamond", (Workflow,), {"input_type": Root, "output_type": Final, "build": build})


async def test_equal_depth_fan_in_still_completes(tmp_path):
    """Acyclic equal-depth multi-parent fan-in (the diamond) still completes."""
    a = make_worker("A", Root, Piece)
    b = make_worker("B", Piece, Piece)
    c = make_worker("C", Piece, Piece)
    join = make_worker("Join", JoinIn, Final, prompt="merging {items}")

    def build(self, g):
        g.add_node(a)
        g.add_node(b)
        g.add_node(c)
        g.add_node(join)
        g.add_edge(START, a)
        g.add_edge(a, b)
        g.add_edge(a, c)
        g.add_edge(b, join)
        g.add_edge(c, join)
        g.add_edge(join, END)

    diamond = type(
        "EqualDepth", (Workflow,), {"input_type": Root, "output_type": Final, "build": build}
    )
    register(diamond, "test")
    engine = make_engine(REPLIES, tmp_path)

    result = await engine.run(diamond, Root(text="hi"))

    assert result.status == "completed"
    assert result.output == Final(text="b3")
    rf = engine.store.load(result.run_id)
    assert activations_on(rf, join) == 1


def test_multi_parent_join_in_loop_compile_error(tmp_path):
    """Any back edge plus a multi-parent static target is a compile error."""
    a = make_worker("A", Root, Piece)
    b = make_worker("B", Piece, Piece)
    c = make_worker("C", Piece, Piece)
    join = make_worker("Join", JoinIn, Final, prompt="merging {items}")

    def build(self, g):
        g.add_node(a)
        g.add_node(b)
        g.add_node(c)
        g.add_node(join)
        g.add_edge(START, a)
        g.add_edge(a, b)
        g.add_edge(a, c)
        g.add_edge(b, join)
        g.add_edge(c, join)
        # Equal-depth join (B and C both at depth 1) would compile acyclic,
        # but the conditional re-entry from the join makes the graph loop.
        g.add_conditional_edges(join, lambda s: "retry", {"retry": b})
        g.add_edge(join, END)

    make_engine(REPLIES, tmp_path)

    with pytest.raises(
        ConfigError,
        match=(
            r"join target .*\.Join has multiple static parents but the "
            r"graph loops \(.*\.Join -> .*\.B is a back edge\)"
        ),
    ):
        type("LoopJoin", (Workflow,), {"input_type": Root, "output_type": Final, "build": build})


async def test_capped_loop_pauses(tmp_path):
    """A control that always retries pauses at Budget(steps=N), never spins."""
    a = make_worker("A", Root, Piece)
    b = make_worker("B", Piece, Piece)
    t = make_worker("T", GateOut, Final, prompt="done on {pass}")
    loop, _gate = build_retry_loop(a, b, t, always_retry=True)
    engine = make_engine(REPLIES, tmp_path, budget=Budget(steps=2))

    result = await engine.run(loop, Root(text="hi"))

    assert result.status == "paused"
    assert result.output is None
    assert result.waiting is not None
    assert result.waiting["reason"] == "budget_exhausted"
    # The cap names the crossing activation: A then B commit (steps 1, 2), so
    # the pause lands on B before the control's re-entry can fire again.
    assert result.waiting["node_path"].endswith(workflow_class_path(b))
    rf = engine.store.load(result.run_id)
    breaches = [r for r in rf.records if r.kind == "budget_exhausted"]
    assert len(breaches) == 1
    assert breaches[0].payload == {"dimension": "steps", "limit": 2, "observed": 2}
    assert activations_on(rf, b) == 1  # exactly one pass, no spin past the cap
    assert activations_on(rf, t) == 0  # the loop never reached its exit
