"""LocalRunService and HTTP-translation-layer tests against a fake engine.

Drives the service both directly (launch/resume/unknown paths/list filters/
notes) and through httpx.AsyncClient over the FastAPI app: routes translate
to service calls only; error mapping, filtering, and canonical export bytes.
"""

from __future__ import annotations

from typing import Literal

import httpx
import pytest
from ngen_weave import registry
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.export import dump_run_json
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

from ngen_weave_server.app import create_app
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


def make_app(tmp_path, replies, discovery_map):
    """Build the serving app over tmp dirs with an injected fake provider."""
    return create_app(
        runs_dir=tmp_path / "runs",
        runs_db_path=tmp_path / "runs.db",
        db_path=tmp_path / "checkpoints.db",
        models_file=tmp_path / "models.json",
        provider=FakeProvider(replies),
        discovery_map=discovery_map,
    )


def make_client(app) -> httpx.AsyncClient:
    """Async client speaking to the ASGI app in-process."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_http_launch_to_completion_and_notes(tmp_path):
    w1 = make_worker("W1h", Root, Piece)
    w2 = make_worker("W2h", Piece, Final)
    chain = make_chain([w1, w2], Root, Final, name="ChainHttp")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, ['{"text":"one"}', '{"text":"two"}'], {path: chain})

    async with make_client(app) as client:
        response = await client.post(
            "/runs", json={"workflow": path, "input": {"text": "hi"}}
        )
        assert response.status_code == 200
        handle = response.json()
        assert handle["status"] == "completed"

        run_id = handle["run_id"]
        response = await client.get(f"/runs/{run_id}")
        assert response.status_code == 200
        run_file = response.json()
        assert run_file["workflow"] == path
        assert run_file["status"] == "completed"
        assert run_file["output"] == {"text": "two"}

        response = await client.post(f"/runs/{run_id}/notes", json={"note": "ok"})
        assert response.status_code == 204
        response = await client.get(f"/runs/{run_id}")
        assert response.json()["notes"] == ["ok"]


async def test_http_resume_with_json_payload_completes(tmp_path):
    human = make_human("ReviewHttp", Root, Review)
    fin = make_worker("FinHttp", Review, Final, prompt="ok {verdict}")
    flow = make_review_flow(human, {"approve": fin}, name="FlowHttp")
    path = workflow_class_path(flow)
    app = make_app(tmp_path, ['{"text":"done"}'], {path: flow})

    async with make_client(app) as client:
        response = await client.post(
            "/runs", json={"workflow": path, "input": {"text": "hi"}}
        )
        run_id = response.json()["run_id"]
        assert response.json()["status"] == "waiting_human"

        response = await client.post(
            f"/runs/{run_id}/resume", json={"payload": {"verdict": "approve"}}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"


async def test_http_unknown_run_is_404_with_error_envelope(tmp_path):
    w = make_worker("WUnk", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainUnk")
    app = make_app(
        tmp_path, [], {workflow_class_path(chain): chain}
    )

    async with make_client(app) as client:
        for method, url, json_body in [
            ("get", "/runs/bogus", None),
            ("post", "/runs/bogus/resume", {"payload": None}),
            ("post", "/runs/bogus/cancel", None),
            ("post", "/runs/bogus/notes", {"note": "x"}),
            ("get", "/runs/bogus/export", None),
        ]:
            response = await client.request(method, url, json=json_body)
            assert response.status_code == 404, url
            error = response.json()["error"]
            assert error["type"] == "UnknownRunError"


async def test_http_bad_input_is_400_with_field_message(tmp_path):
    w = make_worker("WBad", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainBad")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, [], {path: chain})

    async with make_client(app) as client:
        response = await client.post("/runs", json={"workflow": path, "input": {}})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["type"] == "DataError"
        assert "text" in error["message"]

        response = await client.post("/runs", json={"input": {}})
        assert response.status_code == 400


async def test_http_list_filters_through_query_params(tmp_path):
    wd = make_worker("WDh", Root, Final)
    done = make_chain([wd], Root, Final, name="ChainHttpD")
    human = make_human("ReviewHt", Root, Review)
    fin = make_worker("FinHt", Review, Final, prompt="ok {verdict}")
    waiting = make_review_flow(human, {"approve": fin}, name="FlowHttpL")
    done_path = workflow_class_path(done)
    waiting_path = workflow_class_path(waiting)
    app = make_app(
        tmp_path, ['{"text":"one"}'], {done_path: done, waiting_path: waiting}
    )

    async with make_client(app) as client:
        completed = (
            await client.post("/runs", json={"workflow": done_path, "input": {"text": "a"}})
        ).json()["run_id"]
        paused = (
            await client.post("/runs", json={"workflow": waiting_path, "input": {"text": "b"}})
        ).json()["run_id"]

        response = await client.get(f"/runs?workflow={done_path}")
        assert [s["run_id"] for s in response.json()] == [completed]

        response = await client.get("/runs?status=waiting_human")
        assert [s["run_id"] for s in response.json()] == [paused]

        response = await client.get("/runs")
        assert len(response.json()) == 2


async def test_http_export_bytes_equal_dump_run_json(tmp_path):
    w1 = make_worker("W1e", Root, Piece)
    w2 = make_worker("W2e", Piece, Final)
    chain = make_chain([w1, w2], Root, Final, name="ChainExp")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, ['{"text":"one"}', '{"text":"two"}'], {path: chain})

    async with make_client(app) as client:
        response = await client.post(
            "/runs", json={"workflow": path, "input": {"text": "hi"}}
        )
        run_id = response.json()["run_id"]

        response = await client.get(f"/runs/{run_id}/export")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        expected = dump_run_json(app.state.run_service.store.load(run_id))
        assert response.content == expected
