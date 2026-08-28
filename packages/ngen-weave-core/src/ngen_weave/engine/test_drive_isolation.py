"""Concurrent drives on one Engine keep their outcome state separate.

Regression for the v0.2.0 review defect: Engine._drive used to reset three
Engine-level attributes (_waiting, _boundary_stop, _breach_emitted) at the
start of every drive, so overlapping drives erased each other's pause,
cancel, and waiting decisions. The state now lives in a per-drive
_DriveState object; these tests pin the isolation on the budget-pause path
and on the waiting_human path.
"""

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel
from tests.fakes import FakeProvider, PerRunBudgetEngine

import ngen_weave.engine.runner as ngen_runner  # noqa: F401
from ngen_weave import registry
from ngen_weave.config import Budget
from ngen_weave.engine.store import RunStore
from ngen_weave.workflow import (
    END,
    START,
    Human,
    Worker,
    workflow_class_path,
)
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


class Review(BaseModel):
    verdict: Literal["approve", "reject"]


REPLIES = ['{"text":"one"}', '{"text":"two"}', '{"text":"three"}']


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}") -> type[Worker]:
    cls = type(name, (Worker,), {"prompt": prompt, "input_type": in_t, "output_type": out_t})
    registry.register(cls, "test")
    return cls


def make_chain(children, in_t, out_t, name: str):
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


def make_human(name: str, in_t, out_t) -> type[Human]:
    cls = type(
        name,
        (Human,),
        {"input_type": in_t, "output_type": out_t, "state_type": out_t},
    )
    registry.register(cls, "test")
    return cls


def budget_records(rf):
    return [r for r in rf.records if r.kind == "budget_exhausted"]


def waiting_records(rf):
    return [
        r
        for r in rf.records
        if r.kind == "node_activation" and r.payload.get("status") == "waiting_human"
    ]


def make_engine(tmp_path, run_round: int) -> PerRunBudgetEngine:
    """One Engine, memory checkpointer: two drives share the instance, not a db."""
    return PerRunBudgetEngine(
        FakeProvider(REPLIES),
        RunStore(tmp_path / f"runs-{run_round}"),
        checkpointer="memory",
        db_path=tmp_path / f"cp-{run_round}.db",
    )


async def test_concurrent_drives_keep_outcomes_separate(tmp_path) -> None:
    """One Engine, two workflow configs differing in budget, launched together.

    Run A's config caps steps at 1: it must pause at its first boundary with
    exactly one budget_exhausted record. Run B's config has no budget: it
    must complete with a validated output. Drive B's bookkeeping (its reset
    in the pre-fix world) must not erase A's pause decision, and A's must
    not leak into B. The whole body runs twice in one session to catch
    ordering flakiness.
    """
    for run_round in range(2):
        # Distinct class names per round: the registry rejects duplicates.
        a1 = make_worker(f"IsoA1r{run_round}", Root, Piece)
        a2 = make_worker(f"IsoA2r{run_round}", Piece, Piece)
        a3 = make_worker(f"IsoA3r{run_round}", Piece, Final)
        chain_a = make_chain([a1, a2, a3], Root, Final, name=f"IsoChainAr{run_round}")

        b1 = make_worker(f"IsoB1r{run_round}", Root, Piece)
        b2 = make_worker(f"IsoB2r{run_round}", Piece, Piece)
        b3 = make_worker(f"IsoB3r{run_round}", Piece, Final)
        chain_b = make_chain([b1, b2, b3], Root, Final, name=f"IsoChainBr{run_round}")

        engine = make_engine(tmp_path, run_round)
        # Run A's config carries run.budget (steps=1); run B's carries none.
        engine.per_workflow_budgets[workflow_class_path(chain_a)] = Budget(steps=1)

        run_a, run_b = await asyncio.gather(
            engine.run(chain_a, Root(text="hi")),
            engine.run(chain_b, Root(text="hi")),
        )

        assert run_a.run_id != run_b.run_id

        # A pauses at its first boundary with the budget_exhausted contract.
        assert run_a.status == "paused"
        assert run_a.output is None
        assert run_a.waiting is not None
        assert run_a.waiting == {
            "node_path": f"{workflow_class_path(chain_a)}.{workflow_class_path(a1)}",
            "reason": "budget_exhausted",
        }
        rf_a = engine.store.load(run_a.run_id)
        assert len(budget_records(rf_a)) == 1
        assert budget_records(rf_a)[0].payload == {
            "dimension": "steps",
            "limit": 1,
            "observed": 1,
        }

        # B completes; its records carry no budget bookkeeping from A.
        assert run_b.status == "completed"
        assert run_b.waiting is None
        assert isinstance(run_b.output, Final)
        rf_b = engine.store.load(run_b.run_id)
        assert budget_records(rf_b) == []
        assert waiting_records(rf_b) == []


async def test_concurrent_drives_keep_waiting_separate(tmp_path) -> None:
    """One Engine, two drives: one parks waiting_human, the other completes.

    The waiting signal travels through the per-drive state (set by the
    node-side emitter closure), so run B's completion must not erase run
    A's waiting halt, and A's halt must not surface as B's outcome. The
    whole body runs twice in one session to catch ordering flakiness.
    """
    for run_round in range(2):
        h = make_human(f"IsoHr{run_round}", Root, Review)
        fin = make_worker(f"IsoFinr{run_round}", Review, Final, prompt="echo {verdict}")
        chain_a = make_chain([h, fin], Root, Final, name=f"IsoWaitChainAr{run_round}")

        b1 = make_worker(f"IsoWB1r{run_round}", Root, Piece)
        b2 = make_worker(f"IsoWB2r{run_round}", Piece, Final)
        chain_b = make_chain([b1, b2], Root, Final, name=f"IsoWaitChainBr{run_round}")

        engine = make_engine(tmp_path, run_round)

        run_a, run_b = await asyncio.gather(
            engine.run(chain_a, Root(text="hi")),
            engine.run(chain_b, Root(text="hi")),
        )

        assert run_a.run_id != run_b.run_id

        # A parks at its human leaf with the waiting contract intact.
        assert run_a.status == "waiting_human"
        assert run_a.output is None
        assert run_a.waiting is not None
        assert run_a.waiting["node_path"] == (
            f"{workflow_class_path(chain_a)}.{workflow_class_path(h)}"
        )
        assert run_a.waiting["artifact"] is not None
        rf_a = engine.store.load(run_a.run_id)
        assert len(waiting_records(rf_a)) == 1

        # B completes; A's waiting halt did not bleed into B's outcome.
        assert run_b.status == "completed"
        assert run_b.waiting is None
        assert isinstance(run_b.output, Final)
        rf_b = engine.store.load(run_b.run_id)
        assert waiting_records(rf_b) == []
