"""Observer predicates, descriptions, validation, and boundary wiring."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider

import ngen_weave.engine.runner as ngen_runner  # noqa: F401
from ngen_weave import registry
from ngen_weave.config import Budget, RunSettings
from ngen_weave.engine import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.observers import Observer, ObserverPredicate, eq, ge, gt, le, lt
from ngen_weave.provenance import RunMetadata
from ngen_weave.workflow import END, START, Worker, workflow_class_path
from ngen_weave.workflow import Workflow as _W


class In(BaseModel):
    text: str


class Out(BaseModel):
    result: str


def meta(**overrides: float | int) -> RunMetadata:
    """RunMetadata with nominal six-field values; last_output_valid is never compared."""
    values: dict = {
        "iterations": 3,
        "tokens_in_context": 100,
        "tokens_total": 150,
        "cost_usd": 0.5,
        "elapsed_ms": 2000,
        "last_output_valid": True,
    }
    values.update(overrides)
    return RunMetadata(**values)


# --- evaluation truth table per op -------------------------------------------


def test_gt_evaluates_against_metadata():
    p = gt("cost_usd", 0.25)
    assert p.evaluate(meta()) is True
    assert p.evaluate(meta(cost_usd=0.25)) is False
    assert p.evaluate(meta(cost_usd=0.24)) is False


def test_lt_evaluates_against_metadata():
    p = lt("iterations", 5)
    assert p.evaluate(meta()) is True
    assert p.evaluate(meta(iterations=5)) is False
    assert p.evaluate(meta(iterations=6)) is False


def test_ge_evaluates_against_metadata():
    p = ge("tokens_total", 150)
    assert p.evaluate(meta()) is True
    assert p.evaluate(meta(tokens_total=151)) is True
    assert p.evaluate(meta(tokens_total=149)) is False


def test_le_evaluates_against_metadata():
    p = le("elapsed_ms", 2000)
    assert p.evaluate(meta()) is True
    assert p.evaluate(meta(elapsed_ms=1999)) is True
    assert p.evaluate(meta(elapsed_ms=2001)) is False


def test_eq_evaluates_against_metadata():
    p = eq("tokens_in_context", 100)
    assert p.evaluate(meta()) is True
    assert p.evaluate(meta(tokens_in_context=101)) is False


def test_predicate_is_frozen():
    p = gt("cost_usd", 1)
    with pytest.raises(AttributeError):  # dataclass frozen instance rejection
        p.value = 2  # type: ignore[misc]


# --- describe(): mechanically rendered exact strings --------------------------


def test_describe_exact_strings():
    cases = [
        (gt("cost_usd", 0.50), "cost_usd > 0.5"),
        (lt("iterations", 10), "iterations < 10"),
        (ge("tokens_total", 1000), "tokens_total >= 1000"),
        (le("elapsed_ms", 30000), "elapsed_ms <= 30000"),
        (eq("tokens_in_context", 256), "tokens_in_context == 256"),
    ]
    for pred, expected in cases:
        assert pred.describe() == expected


def test_describe_matches_evaluate_terms():
    """The description uses the same field/op/value the evaluator compares."""
    pred = ge("cost_usd", 0.75)
    observed = pred.describe()
    f, sym, val = observed.split(" ", maxsplit=2)
    assert getattr(meta(cost_usd=float(val)), f) == pytest.approx(0.75)
    assert sym == ">="


# --- import-time validation via validate_structure ----------------------------


def make_worker(name: str, **attrs):
    """Dynamically defined validated Worker subclass; attrs override class attributes."""

    def run(self, input, ctx):
        return Out(result="x")

    body = {
        "__module__": __name__,
        "__qualname__": name,
        "input_type": In,
        "output_type": Out,
        "prompt": "do {text}",
        "run": run,
    }
    body.update(attrs)
    return type(name, (Worker,), body)


GOOD_OBSERVATIONS = (Observer(gt("cost_usd", 1.0)),)


def test_valid_observations_pass_validation():
    cls = make_worker("ObsGoodWorker", observations=GOOD_OBSERVATIONS)
    assert len(cls.observations) == 1


def test_unknown_field_names_the_class_and_lists_expected_fields():
    pred = ObserverPredicate(field="last_output_valid", op="gt", value=1)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="WobblingWorker.*unknown predicate field"):
        make_worker("WobblingWorker", observations=(Observer(pred),))


def test_unknown_op_names_the_class_and_lists_expected_ops():
    pred = ObserverPredicate(field="cost_usd", op="neq", value=1)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="StrangedWorker.*unknown predicate op"):
        make_worker("StrangedWorker", observations=(Observer(pred),))


def test_non_numeric_value_rejected_naming_class():
    pred = ObserverPredicate(field="cost_usd", op="gt", value="high")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="ValuedWorker.*predicate value must be int or float"):
        make_worker("ValuedWorker", observations=(Observer(pred),))


def test_bool_value_rejected_naming_class():
    pred = ObserverPredicate(field="cost_usd", op="gt", value=True)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="BoolvaluedWorker.*predicate value must be int or float"):
        make_worker("BoolvaluedWorker", observations=(Observer(pred),))


def test_non_observer_entry_rejected_naming_class():
    with pytest.raises(ConfigError, match="ShapedWorker.*must be an Observer"):
        make_worker("ShapedWorker", observations=(gt("cost_usd", 1.0),))


def test_non_predicate_rejected_naming_class():
    with pytest.raises(ConfigError, match="PredlessWorker.*must be an ObserverPredicate"):
        make_worker(
            "PredlessWorker",
            observations=(Observer(predicate=None),),  # type: ignore[arg-type]
        )


def test_unknown_action_rejected_naming_class():
    obs = Observer(gt("cost_usd", 1.0), action="stop")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="StoppedWorker.*unknown action"):
        make_worker("StoppedWorker", observations=(obs,))


# --- engine wiring: evaluation at the activation boundary ---------------------

class Text(BaseModel):
    text: str


class Mid(BaseModel):
    text: str


class Tail(BaseModel):
    text: str


ENG_REPLIES = ['{"text":"one"}', '{"text":"two"}', '{"text":"three"}']


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated engine classes reuse short names; isolate the global registry."""
    registry.reset()
    yield
    registry.reset()


def bound_worker(name: str, in_t, out_t, **attrs):
    """Registered Worker subclass; attrs carry declared observations."""
    cls = type(
        name,
        (Worker,),
        {"prompt": "echo {text}", "input_type": in_t, "output_type": out_t, **attrs},
    )
    registry.register(cls, "test")
    return cls


def bound_chain(name: str, children, in_t, out_t, *, attrs=None):
    """Registered linear composite whose nodes are `children` in order."""

    def build(self, g):
        for c in children:
            g.add_node(c)
        g.add_edge(START, children[0])
        for a, b in zip(children, children[1:], strict=False):
            g.add_edge(a, b)
        g.add_edge(children[-1], END)

    cls = type(
        name,
        (_W,),
        {"input_type": in_t, "output_type": out_t, "build": build, **(attrs or {})},
    )
    registry.register(cls, "test")
    return cls


def bound_engine(tmp_path, provider=None, *, settings=None):
    """Engine over a tmp store, wired exactly like the budget suites."""
    return Engine(
        provider or FakeProvider(),
        RunStore(tmp_path / "runs"),
        checkpointer="sqlite",
        db_path=tmp_path / "cp.db",
        settings=settings,
    )


def kind_records(rf, kind: str):
    """All provenance records of one kind, oldest first."""
    return [r for r in rf.records if r.kind == kind]


def model_calls_on(rf, leaf) -> int:
    """Count model_call records attributed to one leaf's activation path."""
    leaf_path = workflow_class_path(leaf)
    return len(
        [
            r
            for r in rf.records
            if r.kind == "model_call" and r.node_path.endswith(leaf_path)
        ]
    )


async def test_composite_cost_crossing_threshold_pauses_with_one_record(tmp_path):
    cw1 = bound_worker("ObsCw1", Text, Mid)
    cw2 = bound_worker("ObsCw2", Mid, Mid)
    cw3 = bound_worker("ObsCw3", Mid, Tail)
    inner = bound_chain(
        "ObsInnerComp",
        [cw1, cw2],
        Text,
        Mid,
        attrs={"observations": (Observer(gt("cost_usd", 0.2)),)},
    )
    outer = bound_chain("ObsOuterComp", [inner, cw3], Text, Tail)
    engine = bound_engine(tmp_path, FakeProvider(ENG_REPLIES))

    result = await engine.run(outer, Text(text="hi"))

    assert result.status == "paused"
    assert result.output is None
    assert result.waiting == {
        "node_path": f"{workflow_class_path(outer)}.{workflow_class_path(inner)}",
        "reason": "observer_firing",
        "observer": "cost_usd > 0.2",
    }
    assert len(engine.provider.calls) == 2  # subtree ran; cw3 never started
    firings = kind_records(engine.store.load(result.run_id), "observer_firing")
    assert len(firings) == 1
    payload = firings[0].payload
    assert payload["predicate"] == "cost_usd > 0.2"
    assert payload["field"] == "cost_usd"
    assert payload["op"] == "gt"
    assert payload["value"] == pytest.approx(0.2)
    assert payload["observed"] == pytest.approx(0.229)  # subtree aggregate
    assert payload["action"] == "pause"


async def test_leaf_observer_fires_on_own_activation_metadata(tmp_path):
    low = bound_worker("OwnActWorker", Text, Mid, observations=(Observer(gt("cost_usd", 0.1)),))
    tail = bound_worker("OwnActTail", Mid, Tail)
    chain = bound_chain("OwnActChain", [low, tail], Text, Tail)
    engine = bound_engine(tmp_path, FakeProvider(ENG_REPLIES))

    result = await engine.run(chain, Text(text="hi"))

    assert result.status == "paused"
    assert result.waiting == {
        "node_path": f"{workflow_class_path(chain)}.{workflow_class_path(low)}",
        "reason": "observer_firing",
        "observer": "cost_usd > 0.1",
    }
    assert len(engine.provider.calls) == 1  # only the observed leaf ran
    firings = kind_records(engine.store.load(result.run_id), "observer_firing")
    assert len(firings) == 1
    assert firings[0].node_path.endswith(workflow_class_path(low))
    assert firings[0].payload["observed"] == pytest.approx(0.114)


async def test_depth_two_leaf_observer_pauses_and_resume_skips_committed_nodes(tmp_path):
    deep = bound_worker("DeepObsLeaf", Text, Mid, observations=(Observer(gt("cost_usd", 0.1)),))
    deeper = bound_worker("DeepPlainLeaf", Mid, Mid)
    tail3 = bound_worker("DeepObsTail", Mid, Tail)
    inner = bound_chain("DeepObsInner", [deep, deeper], Text, Mid)
    outer = bound_chain("DeepObsOuter", [inner, tail3], Text, Tail)
    engine = bound_engine(tmp_path, FakeProvider(ENG_REPLIES))

    paused = await engine.run(outer, Text(text="hi"))

    assert paused.status == "paused"
    assert paused.waiting["reason"] == "observer_firing"
    assert paused.waiting["observer"] == "cost_usd > 0.1"
    assert paused.waiting["node_path"].endswith(workflow_class_path(deep))
    assert len(engine.provider.calls) == 2

    resumed = bound_engine(tmp_path, FakeProvider(ENG_REPLIES[2:]))
    result = await resumed.resume(paused.run_id)

    assert result.status == "completed"
    assert result.output == Tail(text="three")
    rf = resumed.store.load(result.run_id)
    assert model_calls_on(rf, deep) == 1  # committed subtree never re-executed
    assert model_calls_on(rf, deeper) == 1
    assert model_calls_on(rf, tail3) == 1
    assert len(kind_records(rf, "observer_firing")) == 1
    assert rf.status == "completed"


async def test_resume_after_flat_observer_pause_completes(tmp_path):
    first = bound_worker("FlatObsWorker", Text, Mid, observations=(Observer(gt("cost_usd", 0.1)),))
    second = bound_worker("FlatObsTail", Mid, Tail)
    chain = bound_chain("FlatObsChain", [first, second], Text, Tail)
    engine = bound_engine(tmp_path, FakeProvider(ENG_REPLIES))
    paused = await engine.run(chain, Text(text="hi"))
    assert paused.status == "paused"

    resumed = bound_engine(tmp_path, FakeProvider(ENG_REPLIES[1:]))
    result = await resumed.resume(paused.run_id)

    assert result.status == "completed"
    assert result.output == Tail(text="two")
    rf = resumed.store.load(result.run_id)
    assert model_calls_on(rf, first) == 1 and model_calls_on(rf, second) == 1
    assert len(kind_records(rf, "observer_firing")) == 1


async def test_budget_breach_suppresses_observer_at_same_boundary(tmp_path):
    greedy = bound_worker(
        "ShortCircuitWorker", Text, Mid, observations=(Observer(gt("cost_usd", 0.05)),)
    )
    follower = bound_worker("ShortCircuitTail", Mid, Tail)
    chain = bound_chain("ShortCircuitChain", [greedy, follower], Text, Tail)
    engine = bound_engine(
        tmp_path,
        FakeProvider(ENG_REPLIES),
        settings=RunSettings(checkpointer="sqlite", budget=Budget(steps=1)),
    )

    result = await engine.run(chain, Text(text="hi"))

    assert result.status == "paused"
    assert result.waiting["reason"] == "budget_exhausted"
    rf = engine.store.load(result.run_id)
    breaches = kind_records(rf, "budget_exhausted")
    assert len(breaches) == 1
    assert breaches[0].payload == {"dimension": "steps", "limit": 1, "observed": 1}
    assert kind_records(rf, "observer_firing") == []  # order pinned from C1


async def test_root_observer_is_informational_at_completion(tmp_path):
    runner_leaf = bound_worker("RootInfoWorker", Text, Tail)
    root = bound_chain(
        "RootInfoChain",
        [runner_leaf],
        Text,
        Tail,
        attrs={"observations": (Observer(le("iterations", 5)),)},
    )
    engine = bound_engine(tmp_path, FakeProvider(ENG_REPLIES))

    result = await engine.run(root, Text(text="go"))

    assert result.status == "completed"
    assert result.output == Tail(text="one")
    firings = kind_records(engine.store.load(result.run_id), "observer_firing")
    assert len(firings) == 1
    assert firings[0].node_path == workflow_class_path(root)
    assert firings[0].payload["field"] == "iterations"
    assert firings[0].payload["op"] == "le"
    assert firings[0].payload["observed"] == 1
    assert firings[0].payload["action"] == "pause"
