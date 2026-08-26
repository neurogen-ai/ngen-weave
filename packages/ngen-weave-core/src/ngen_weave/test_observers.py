"""Tests for observer predicates, descriptions, and import-time validation."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ngen_weave.errors import ConfigError
from ngen_weave.observers import Observer, ObserverPredicate, eq, ge, gt, le, lt
from ngen_weave.provenance import RunMetadata
from ngen_weave.workflow import Worker


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
