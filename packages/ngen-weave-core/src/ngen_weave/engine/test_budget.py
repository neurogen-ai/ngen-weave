"""Budget enforcement and cooperative cancellation (C1 semantics)."""

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider

import ngen_weave.engine.runner as ngen_runner  # noqa: F401
from ngen_weave import registry
from ngen_weave.config import Budget, RunSettings
from ngen_weave.engine import Engine
from ngen_weave.engine.state import RunResult
from ngen_weave.engine.store import RunStore
from ngen_weave.workflow import END, START, Worker, Workflow, workflow_class_path
from ngen_weave.workflow import Workflow as _W


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


REPLIES = ['{"text":"one"}', '{"text":"two"}', '{"text":"three"}']


def make_worker(name: str, in_t, out_t) -> type[Worker]:
    cls = type(name, (Worker,), {"prompt": "echo {text}", "input_type": in_t, "output_type": out_t})
    registry.register(cls, "test")
    return cls


def make_chain(children, in_t, out_t, name: str = "BudChain"):
    def build(self, g):
        for c in children:
            g.add_node(c)
        g.add_edge(START, children[0])
        for a, b in zip(children, children[1:], strict=False):
            g.add_edge(a, b)
        g.add_edge(children[-1], END)

    chain = type(name, (_W,), {"input_type": in_t, "output_type": out_t, "build": build})
    registry.register(chain, "test")
    return chain


def make_composite(children, in_t, out_t, name: str):
    return make_chain(children, in_t, out_t, name=name)


def make_engine(tmp_path, *, settings: RunSettings | None = None, provider=None):
    return Engine(
        provider or FakeProvider(REPLIES),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
        settings=settings,
    )


def make_settings(budget: Budget | None) -> RunSettings:
    return RunSettings(checkpointer="sqlite", budget=budget)


def kind_records(rf, kind: str):
    return [r for r in rf.records if r.kind == kind]


def model_calls_on(rf, leaf: type[Workflow]) -> int:
    leaf_path = workflow_class_path(leaf)
    return len(
        [r for r in rf.records if r.kind == "model_call" and r.node_path.endswith(leaf_path)]
    )


# --- construction contract ----------------------------------------------------


def test_run_result_waiting_defaults_and_carries_budget_pause() -> None:
    bare = RunResult("r1", "completed", None)
    assert bare.waiting is None
    waiting = RunResult("r1", "paused", None, {"node_path": "a.B", "reason": "budget_exhausted"})
    assert waiting.waiting == {"node_path": "a.B", "reason": "budget_exhausted"}


# --- cost breach --------------------------------------------------------------


async def test_cost_breach_pauses_mid_chain_with_one_record(tmp_path):
    w1 = make_worker("Cbw1", Root, Piece)
    w2 = make_worker("Cbw2", Piece, Piece)
    w3 = make_worker("Cbw3", Piece, Final)
    chain = make_chain([w1, w2, w3], Root, Final)
    provider = FakeProvider(REPLIES)
    # call costs: ~0.114 then ~0.115 -> cap crossed by the second activation
    engine = make_engine(tmp_path, settings=make_settings(Budget(cost_usd=0.2)), provider=provider)

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "paused"
    assert result.output is None
    assert result.waiting is not None
    assert result.waiting["reason"] == "budget_exhausted"
    assert result.waiting["node_path"] == f"{workflow_class_path(chain)}.{workflow_class_path(w2)}"
    assert len(provider.calls) == 2  # third worker never started
    rf = engine.store.load(result.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert len(breaches) == 1
    payload = breaches[0].payload
    assert payload["dimension"] == "cost_usd"
    assert payload["limit"] == pytest.approx(0.2)
    assert payload["observed"] == pytest.approx(0.229)  # 0.114 + 0.115


# --- steps breach -------------------------------------------------------------


async def test_steps_breach_pauses_with_one_record(tmp_path):
    w1 = make_worker("Sbw1", Root, Piece)
    w2 = make_worker("Sbw2", Piece, Piece)
    w3 = make_worker("Sbw3", Piece, Final)
    chain = make_chain([w1, w2, w3], Root, Final)
    engine = make_engine(tmp_path, settings=make_settings(Budget(steps=2)))

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "paused"
    assert result.waiting == {
        "node_path": f"{workflow_class_path(chain)}.{workflow_class_path(w2)}",
        "reason": "budget_exhausted",
    }
    rf = engine.store.load(result.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert len(breaches) == 1
    assert breaches[0].payload == {"dimension": "steps", "limit": 2, "observed": 2}


async def test_under_budget_run_never_emits_and_completes(tmp_path):
    chain = make_chain(
        [make_worker("Ubw1", Root, Piece), make_worker("Ubw2", Piece, Final)], Root, Final
    )
    engine = make_engine(tmp_path, settings=make_settings(Budget(cost_usd=100.0, steps=100)))

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "completed"
    rf = engine.store.load(result.run_id)
    assert kind_records(rf, "budget_exhausted") == []


# --- raised-cap resume --------------------------------------------------------


async def test_raised_cap_fresh_engine_resume_completes_without_reexecution(tmp_path):
    w1 = make_worker("Rbw1", Root, Piece)
    w2 = make_worker("Rbw2", Piece, Piece)
    w3 = make_worker("Rbw3", Piece, Final)
    chain = make_chain([w1, w2, w3], Root, Final)
    engine = make_engine(
        tmp_path,
        settings=make_settings(Budget(cost_usd=0.15)),
        provider=FakeProvider(REPLIES),
    )
    paused = await engine.run(chain, Root(text="hi"))
    assert paused.status == "paused"

    resumed = make_engine(
        tmp_path, settings=make_settings(Budget(cost_usd=5.0)), provider=FakeProvider(REPLIES[2:])
    )
    result = await resumed.resume(paused.run_id)

    assert result.status == "completed"
    assert result.output == Final(text="three")
    rf = resumed.store.load(result.run_id)
    assert len(kind_records(rf, "budget_exhausted")) == 1  # still exactly one
    assert model_calls_on(rf, w1) == 1  # completed nodes not re-executed
    assert model_calls_on(rf, w2) == 1
    assert model_calls_on(rf, w3) == 1


# --- cancellation -------------------------------------------------------------


def make_cancel_leaf(name: str, in_t, out_t, holder: dict) -> type[Workflow]:
    async def run(self, input, ctx):  # noqa: ANN001, ARG001
        holder["engine"].cancel(ctx.run_id)
        return out_t(text="leaf-done")

    cls = type(
        name,
        (Workflow,),
        {"input_type": in_t, "output_type": out_t, "run": run},
    )
    registry.register(cls, "test")
    return cls


async def test_cancel_between_transitions_ends_cancelled(tmp_path):
    holder: dict = {}
    cl = make_cancel_leaf("Xcl1", Root, Piece, holder)
    w2 = make_worker("Xcl2", Piece, Final)
    chain = make_chain([cl, w2], Root, Final)
    engine = make_engine(tmp_path, provider=FakeProvider(['{"text":"tail"}']))
    holder["engine"] = engine

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "cancelled"
    assert result.waiting is None
    rf = engine.store.load(result.run_id)
    assert rf.status == "cancelled"
    assert rf.error is None
    tails = [
        r
        for r in rf.records
        if r.node_path.endswith(workflow_class_path(w2)) and r.kind != "artifact_write"
    ]
    assert tails == []  # work remained; it was never executed
    # Only the cancelling leaf committed; nothing past its boundary.
    statuses = [
        r.payload.get("status")
        for r in rf.records
        if r.kind == "node_activation" and r.payload.get("status") == "ok"
    ]
    assert statuses == ["ok"]


async def test_double_cancel_idempotent_and_terminal_noop(tmp_path):
    holder: dict = {}
    cl = make_cancel_leaf("Dcl1", Root, Piece, holder)
    chain = make_chain([cl], Root, Piece)
    engine = make_engine(tmp_path)
    holder["engine"] = engine
    result = await engine.run(chain, Root(text="hi"))
    assert result.status == "cancelled"
    engine.cancel(result.run_id)  # double-cancel: no-op, no exception
    engine.cancel(result.run_id)
    assert engine.store.load(result.run_id).status == "cancelled"

    done_chain = make_chain([make_worker("Dcl2", Root, Final)], Root, Final, name="DoneCancelChain")
    finished = await make_engine(tmp_path).run(done_chain, Root(text="go"))
    assert finished.status == "completed"
    engine.cancel(finished.run_id)  # terminal runs are untouched
    assert engine.store.load(finished.run_id).status == "completed"


# --- nested granularity -------------------------------------------------------


async def test_depth_two_activation_breach_pauses_at_composite_boundary_and_resumes(tmp_path):
    w1 = make_worker("Ncw1", Root, Piece)
    w2 = make_worker("Ncw2", Piece, Piece)
    w3 = make_worker("Ncw3", Piece, Final)
    inner = make_composite([w1, w2], Root, Piece, name="InnerComp")

    def outer_build(self, g):
        g.add_node(inner)
        g.add_node(w3)
        g.add_edge(START, inner)
        g.add_edge(inner, w3)
        g.add_edge(w3, END)

    outer = type(
        "OuterComp",
        (_W,),
        {"input_type": Root, "output_type": Final, "build": outer_build},
    )
    registry.register(outer, "test")

    provider = FakeProvider(REPLIES)
    engine = make_engine(tmp_path, settings=make_settings(Budget(steps=2)), provider=provider)
    # Depth-2 activations (w1, w2 inside inner) surface only at the enclosing
    # composite's commit boundary, so the breach fires there -- approved
    # deviation documented at the driver's consumption loop.
    paused = await engine.run(outer, Root(text="hi"))

    assert paused.status == "paused"
    assert paused.waiting == {
        "node_path": f"{workflow_class_path(outer)}.{workflow_class_path(inner)}",
        "reason": "budget_exhausted",
    }
    assert len(provider.calls) == 2
    rf = engine.store.load(paused.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert len(breaches) == 1
    assert breaches[0].payload == {"dimension": "steps", "limit": 2, "observed": 3}

    # Only w3 is uncommitted behind the pause, so the resumed engine's fresh
    # provider supplies its reply first -- same slicing as the flat resume.
    resumed = make_engine(
        tmp_path,
        settings=make_settings(Budget(steps=99)),
        provider=FakeProvider(REPLIES[2:]),
    )
    result = await resumed.resume(paused.run_id)
    assert result.status == "completed"
    assert result.output == Final(text="three")
    rf = resumed.store.load(result.run_id)
    assert model_calls_on(rf, w1) == 1  # subtree work not replayed on resume
    assert model_calls_on(rf, w2) == 1
    assert len(kind_records(rf, "budget_exhausted")) == 1


# --- -1 uncapped sentinel -----------------------------------------------------


UNCAPPED_VARIANTS = [
    Budget(),  # no dimensions set
    Budget(steps=-1),
    Budget(cost_usd=-1),
    Budget(cost_usd=-1, steps=-1),
]


@pytest.mark.parametrize("budget", UNCAPPED_VARIANTS, ids=str)
async def test_uncapped_budgets_complete_without_budget_records(tmp_path, budget):
    """None and the -1 sentinel both mean unlimited; nothing may ever pause."""
    w1 = make_worker("Uncw1", Root, Piece)
    w2 = make_worker("Uncw2", Piece, Final)
    chain = make_chain([w1, w2], Root, Final)
    engine = make_engine(
        tmp_path,
        settings=make_settings(budget),
        provider=FakeProvider(REPLIES),
    )

    result = await engine.run(chain, Root(text="hi"))

    assert result.status == "completed"
    rf = engine.store.load(result.run_id)
    assert kind_records(rf, "budget_exhausted") == []


async def test_mixed_dims_cap_independently(tmp_path):
    """A -1 dim never breaches; the finite sibling still does, on its own dimension."""
    w1 = make_worker("Mixw1", Root, Piece)
    w2 = make_worker("Mixw2", Piece, Piece)
    w3 = make_worker("Mixw3", Piece, Final)
    chain = make_chain([w1, w2, w3], Root, Final)

    # cost capped at 0.2, steps unlimited: breach must report cost_usd.
    engine_cost = make_engine(
        tmp_path / "cost",
        settings=make_settings(Budget(cost_usd=0.2, steps=-1)),
        provider=FakeProvider(REPLIES),
    )
    result = await engine_cost.run(chain, Root(text="hi"))
    assert result.status == "paused"
    rf = engine_cost.store.load(result.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert [b.payload["dimension"] for b in breaches] == ["cost_usd"]

    # steps capped at 2, cost unlimited: breach must report steps.
    engine_steps = make_engine(
        tmp_path / "steps",
        settings=make_settings(Budget(steps=2, cost_usd=-1)),
        provider=FakeProvider(REPLIES),
    )
    result = await engine_steps.run(chain, Root(text="hi"))
    assert result.status == "paused"
    rf = engine_steps.store.load(result.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert [b.payload["dimension"] for b in breaches] == ["steps"]


async def test_uncapped_budget_on_nested_workflow_completes(tmp_path):
    """Nested composites under an uncapped budget run to completion cleanly."""
    w1 = make_worker("Nuncw1", Root, Piece)
    w2 = make_worker("Nuncw2", Piece, Piece)
    w3 = make_worker("Nuncw3", Piece, Final)
    inner = make_composite([w1, w2], Root, Piece, name="NuncInner")
    outer = make_chain([inner, w3], Root, Final, name="NuncOuter")
    engine = make_engine(
        tmp_path,
        settings=make_settings(Budget(cost_usd=-1, steps=-1)),
        provider=FakeProvider(REPLIES),
    )

    result = await engine.run(outer, Root(text="hi"))

    assert result.status == "completed"
    rf = engine.store.load(result.run_id)
    assert kind_records(rf, "budget_exhausted") == []
