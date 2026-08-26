"""LocalRunService tests: the six RunService methods against a fake engine.

Drives the service directly over tmp-dir stores with a FakeProvider engine:
launch-to-completion, human-interrupt resume, unknown-id paths, list filters,
note round trips, and terminal-cancel idempotence.
"""

from __future__ import annotations

from typing import Literal

import pytest
from ngen_weave import registry
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.service import RunFilters, UnknownRunError
from ngen_weave.workflow import (
    END,
    START,
    Human,
    Worker,
    Workflow,
    workflow_class_path,
)
from pydantic import BaseModel
from tests.fakes import FakeProvider

from ngen_weave_server.local import LocalRunService


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
    notes: str = ""


def make_worker(name: str, in_t, out_t, prompt: str = "echo {text}"):
    cls = type(
        name,
        (Worker,),
        {"prompt": prompt, "input_type": in_t, "output_type": out_t},
    )
    registry.register(cls, "test")
    return cls


def make_chain(children, in_t, out_t, name: str = "Chain"):
    def build(self, g):
        for child in children:
            g.add_node(child)
        g.add_edge(START, children[0])
        for left, right in zip(children, children[1:], strict=False):
            g.add_edge(left, right)
        g.add_edge(children[-1], END)

    chain = type(name, (Workflow,), {"input_type": in_t, "output_type": out_t, "build": build})
    registry.register(chain, "test")
    return chain


def make_human(name: str, in_t, out_t):
    body = {"input_type": in_t, "output_type": out_t, "state_type": out_t}
    cls = type(name, (Human,), body)
    registry.register(cls, "test")
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

    flow = type(name, (Workflow,), {"input_type": in_t, "output_type": out_t, "build": build})
    registry.register(flow, "test")
    return flow


def make_service(tmp_path, replies) -> tuple[LocalRunService, FakeProvider]:
    provider = FakeProvider(replies)
    store = RunStore(tmp_path / "runs")
    engine = Engine(provider, store, checkpointer="memory")
    return LocalRunService(engine, store, {}), provider


async def test_launch_linear_chain_completes(tmp_path):
    w1 = make_worker("W1s", Root, Piece)
    w2 = make_worker("W2s", Piece, Final)
    chain = make_chain([w1, w2], Root, Final, name="LinearChainA")

    service, _ = make_service(
        tmp_path,
        ['{"text":"one"}', '{"text":"two"}'],
    )
    service.discovery_map.update({"fixtures.LinearChainA": chain})

    handle = await service.launch("fixtures.LinearChainA", Root(text="hi"))

    assert handle.status == "completed"
    run_file = await service.status(handle.run_id)
    assert run_file.workflow == workflow_class_path(chain)
    assert run_file.status == "completed"
    # W1 consumes reply "one", W2 the final reply "two": that is the output.
    assert run_file.output == {"text": "two"}
    assert run_file.notes == []


async def test_resume_after_human_interrupt_completes(tmp_path):
    human = make_human("ReviewSrv", Root, Review)
    fin = make_worker("FinSrv", Review, Final, prompt="ok {verdict}")
    flow = make_review_flow(human, {"approve": fin}, name="FlowSrv")

    service, _ = make_service(tmp_path, ['{"text":"done"}'])
    service.discovery_map.update({"fixtures.FlowSrv": flow})

    handle = await service.launch("fixtures.FlowSrv", Root(text="hi"))
    assert handle.status == "waiting_human"
    listings = await service.list_runs(RunFilters(status="waiting_human"))
    assert [s.waiting_on_human for s in listings] == [True]

    result = await service.resume(handle.run_id, payload={"verdict": "approve"})
    assert result.status == "completed"


async def test_unknown_ids_raise_everywhere(tmp_path):
    w = make_worker("WU", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainU")
    service, _ = make_service(tmp_path, ['{"text":"x"}'])
    service.discovery_map.update({"fixtures.ChainU": chain})

    with pytest.raises(UnknownRunError):
        await service.launch("fixtures.Missing", Root(text="hi"))
    with pytest.raises(UnknownRunError):
        await service.resume("nope")
    with pytest.raises(UnknownRunError):
        await service.status("nope")
    with pytest.raises(UnknownRunError):
        await service.cancel("nope")
    with pytest.raises(UnknownRunError):
        await service.attach_note("nope", "hi")


async def test_list_runs_filters_by_workflow_and_status(tmp_path):
    wd = make_worker("WD", Root, Final)
    done = make_chain([wd], Root, Final, name="ChainD")
    human = make_human("ReviewL", Root, Review)
    fin = make_worker("FinL", Review, Final, prompt="ok {verdict}")
    waiting = make_review_flow(human, {"approve": fin}, name="FlowL")

    service, _ = make_service(tmp_path, ['{"text":"one"}'])
    service.discovery_map.update(
        {"fixtures.ChainD": done, "fixtures.FlowL": waiting}
    )
    completed_handle = await service.launch("fixtures.ChainD", Root(text="a"))
    waiting_handle = await service.launch("fixtures.FlowL", Root(text="b"))

    everything = await service.list_runs()
    assert len(everything) == 2

    by_workflow = await service.list_runs(RunFilters(workflow=workflow_class_path(done)))
    assert [s.run_id for s in by_workflow] == [completed_handle.run_id]

    by_status = await service.list_runs(RunFilters(status="waiting_human"))
    assert [s.run_id for s in by_status] == [waiting_handle.run_id]

    empty = await service.list_runs(RunFilters(status="cancelled"))
    assert empty == []


async def test_attach_note_round_trips(tmp_path):
    wn = make_worker("WN", Root, Final)
    chain = make_chain([wn], Root, Final, name="ChainN")
    service, _ = make_service(tmp_path, ['{"text":"one"}'])
    service.discovery_map.update({"fixtures.ChainN": chain})
    handle = await service.launch("fixtures.ChainN", Root(text="a"))

    await service.attach_note(handle.run_id, "reviewed by ops")
    await service.attach_note(handle.run_id, "follow-up pending")

    run_file = await service.status(handle.run_id)
    assert run_file.notes == ["reviewed by ops", "follow-up pending"]


async def test_cancel_after_completion_is_noop(tmp_path):
    wc = make_worker("WC", Root, Final)
    chain = make_chain([wc], Root, Final, name="ChainC")
    service, _ = make_service(tmp_path, ['{"text":"one"}'])
    service.discovery_map.update({"fixtures.ChainC": chain})
    handle = await service.launch("fixtures.ChainC", Root(text="a"))
    assert handle.status == "completed"

    await service.cancel(handle.run_id)

    run_file = await service.status(handle.run_id)
    assert run_file.status == "completed"
