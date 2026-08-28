"""Backend-neutral RunService contract: every backend must satisfy these assertions."""

from __future__ import annotations

import json

import pytest
from ngen_weave.engine.state import RunFile
from ngen_weave.service import RunFilters, UnknownRunError
from ngen_weave.test_local_service import (
    Final,
    Piece,
    Review,
    Root,
    make_chain,
    make_human,
    make_review_flow,
    make_worker,
)
from ngen_weave.workflow import workflow_class_path

pytestmark = [pytest.mark.conformance]

CHAIN_REPLIES = ['{"text":"one"}', '{"text":"two"}']
HUMAN_REPLIES = ['{"text":"done"}']


def _waiting_flow(name: str):
    """START -> human -> finishing worker on approve."""
    human = make_human(f"ReviewH{name}", Root, Review)
    fin = make_worker(f"FinH{name}", Review, Final, prompt="ok {verdict}")
    return make_review_flow(human, {"approve": fin}, name=f"FlowCon{name}")


def _record_keys(run_file: RunFile) -> list[str]:
    """Canonical JSON per record for duplicate detection."""
    return [
        json.dumps(
            {
                "version": record.version,
                "kind": record.kind,
                "node_path": record.node_path,
                "ts": record.ts,
                "payload": record.payload,
            },
            sort_keys=True,
        )
        for record in run_file.records
    ]


async def test_launch_to_completed_returns_output_matching_output_type(service):
    """A linear chain ends completed with output validating as output_type."""
    w1 = make_worker("W1c", Root, Piece)
    w2 = make_worker("W2c", Piece, Final)
    chain = make_chain([w1, w2], Root, Final, name="ChainConA")
    svc = service(CHAIN_REPLIES, {workflow_class_path(chain): chain})

    handle = await svc.launch(workflow_class_path(chain), Root(text="hi"))

    assert handle.status == "completed"
    run_file = await svc.status(handle.run_id)
    assert isinstance(run_file, RunFile)
    assert run_file.status == "completed"
    parsed = chain.output_type.model_validate(run_file.output)
    assert isinstance(parsed, Final)


async def test_launching_unregistered_class_path_errors_consistently(service):
    """An unknown workflow key raises UnknownRunError on every backend."""
    w = make_worker("WUnk", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainConB")
    svc = service(['{"text":"x"}'], {workflow_class_path(chain): chain})

    with pytest.raises(UnknownRunError):
        await svc.launch("fixtures.Missing", Root(text="hi"))


async def test_human_interrupt_yields_waiting_on_human_then_resume_completes(service):
    """Summaries flag waiting_on_human at the interrupt; JSON resume completes."""
    flow = _waiting_flow("C")
    path = workflow_class_path(flow)
    svc = service(HUMAN_REPLIES, {path: flow})

    handle = await svc.launch(path, Root(text="hi"))
    assert handle.status == "waiting_human"
    summaries = await svc.list_runs(RunFilters(status="waiting_human"))
    assert [s.waiting_on_human for s in summaries] == [True]
    assert [s.workflow for s in summaries] == [path]

    result = await svc.resume(handle.run_id, payload={"verdict": "approve"})
    assert result.status == "completed"
    assert (await svc.status(handle.run_id)).output == {"text": "done"}


async def test_status_returns_parseable_runfile_with_ordered_unique_records(service):
    """status() hands back a RunFile whose records are oldest-first, no duplicates."""
    flow = _waiting_flow("D")
    path = workflow_class_path(flow)
    svc = service(HUMAN_REPLIES, {path: flow})

    handle = await svc.launch(path, Root(text="hi"))
    result = await svc.resume(handle.run_id, payload={"verdict": "approve"})

    assert result.status == "completed"
    run_file = await svc.status(handle.run_id)
    timestamps = [record.ts for record in run_file.records]
    assert timestamps == sorted(timestamps)  # oldest first
    keys = _record_keys(run_file)
    assert len(keys) == len(set(keys))  # no duplicates


async def test_list_runs_filters_by_workflow_and_status(service):
    """Workflow and status filters select exactly their matching runs."""
    done_worker = make_worker("WDf", Root, Final)
    done = make_chain([done_worker], Root, Final, name="ChainConE")
    waiting_flow = _waiting_flow("F")
    done_path = workflow_class_path(done)
    waiting_path = workflow_class_path(waiting_flow)
    # One reply serves both: the chain call; the flow makes no model calls while waiting.
    svc = service(['{"text":"one"}'], {done_path: done, waiting_path: waiting_flow})

    completed = await svc.launch(done_path, Root(text="a"))
    paused = await svc.launch(waiting_path, Root(text="b"))

    everything = await svc.list_runs()
    assert sorted(s.run_id for s in everything) == sorted([completed.run_id, paused.run_id])

    by_workflow = await svc.list_runs(RunFilters(workflow=done_path))
    assert [s.run_id for s in by_workflow] == [completed.run_id]

    by_status = await svc.list_runs(RunFilters(status="waiting_human"))
    assert [s.run_id for s in by_status] == [paused.run_id]

    empty = await svc.list_runs(RunFilters(status="cancelled"))
    assert empty == []


async def test_attach_note_round_trips(service):
    """Notes attach through any backend and read back in order via status()."""
    worker = make_worker("WNc", Root, Final)
    chain = make_chain([worker], Root, Final, name="ChainConG")
    svc = service(['{"text":"one"}'], {workflow_class_path(chain): chain})

    handle = await svc.launch(workflow_class_path(chain), Root(text="a"))

    await svc.attach_note(handle.run_id, "reviewed by ops")
    await svc.attach_note(handle.run_id, "follow-up pending")

    run_file = await svc.status(handle.run_id)
    assert run_file.notes == ["reviewed by ops", "follow-up pending"]


async def test_cancel_before_completion_ends_cancelled_and_double_cancel_is_idempotent(service):
    """Cancelling a waiting run terminal-writes cancelled; cancelling again is a no-op."""
    flow = _waiting_flow("I")
    path = workflow_class_path(flow)
    svc = service(HUMAN_REPLIES, {path: flow})

    handle = await svc.launch(path, Root(text="hi"))
    assert handle.status == "waiting_human"

    await svc.cancel(handle.run_id)
    run_file = await svc.status(handle.run_id)
    assert run_file.status == "cancelled"
    assert run_file.output is None
    assert run_file.error is None

    await svc.cancel(handle.run_id)  # double cancel: no exception
    assert (await svc.status(handle.run_id)).status == "cancelled"


async def test_list_reports_accurate_cost(service):
    """list_runs cost_usd equals the summed model_call record costs exactly."""
    flow = _waiting_flow("K")
    path = workflow_class_path(flow)
    svc = service(HUMAN_REPLIES, {path: flow})

    handle = await svc.launch(path, Root(text="hi"))
    result = await svc.resume(handle.run_id, payload={"verdict": "approve"})

    assert result.status == "completed"
    run_file = await svc.status(handle.run_id)
    expected = sum(
        float(record.payload.get("cost_usd") or 0.0)
        for record in run_file.records
        if record.kind == "model_call"
    )
    summaries = await svc.list_runs()
    cost_by_run = {summary.run_id: summary.cost_usd for summary in summaries}
    assert cost_by_run[handle.run_id] == pytest.approx(expected, abs=1e-9)


async def test_unknown_run_error_from_every_method_for_bogus_id(service):
    """Every run-id-taking method raises UnknownRunError for an unknown id."""
    worker = make_worker("WUb", Root, Final)
    chain = make_chain([worker], Root, Final, name="ChainConJ")
    svc = service(['{"text":"x"}'], {workflow_class_path(chain): chain})
    bogus = "bogus-run-id"

    with pytest.raises(UnknownRunError):
        await svc.resume(bogus)
    with pytest.raises(UnknownRunError):
        await svc.status(bogus)
    with pytest.raises(UnknownRunError):
        await svc.cancel(bogus)
    with pytest.raises(UnknownRunError):
        await svc.attach_note(bogus, "hi")
