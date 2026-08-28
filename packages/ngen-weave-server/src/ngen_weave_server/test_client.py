"""HttpRunService tests: the six RunService methods over the ASGI app.

Error shapes per status code: 404 -> UnknownRunError, 400 -> ConfigError, 500 -> NgWeaveError.
"""

from __future__ import annotations

import httpx
import pytest
from ngen_weave import registry
from ngen_weave.engine.state import RunFile
from ngen_weave.errors import ConfigError, InfraError, NgWeaveError
from ngen_weave.service import RunFilters, UnknownRunError
from ngen_weave.test_local_service import (
    Final,
    Piece,
    Review,
    Root,
    make_app,
    make_chain,
    make_human,
    make_review_flow,
    make_worker,
)
from ngen_weave.workflow import workflow_class_path
from pydantic import BaseModel

from ngen_weave_server.client import HttpRunService


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated test classes reuse short names; isolate the global registry."""
    registry.reset()
    yield
    registry.reset()


class BadInput(BaseModel):
    """Schema-invalid launch payload: no `text`, so the backend rejects it."""

    other: str = ""


def make_service(app) -> HttpRunService:
    """HttpRunService pointed at the ASGI app in-process."""
    return HttpRunService("http://test", transport=httpx.ASGITransport(app=app))


async def test_launch_status_and_notes_through_the_client(tmp_path):
    w1 = make_worker("W1cl", Root, Piece)
    w2 = make_worker("W2cl", Piece, Final)
    chain = make_chain([w1, w2], Root, Final, name="ChainClient")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, ['{"text":"one"}', '{"text":"two"}'], {path: chain})

    async with make_service(app) as service:
        handle = await service.launch(path, Root(text="hi"))
        assert handle.status == "completed"

        run_file = await service.status(handle.run_id)
        assert isinstance(run_file, RunFile)
        assert run_file.workflow == path
        assert run_file.output == {"text": "two"}
        assert run_file.notes == []

        await service.attach_note(handle.run_id, "ok")
        assert (await service.status(handle.run_id)).notes == ["ok"]


async def test_resume_after_human_through_the_client(tmp_path):
    human = make_human("ReviewCli", Root, Review)
    fin = make_worker("FinCli", Review, Final, prompt="ok {verdict}")
    flow = make_review_flow(human, {"approve": fin}, name="FlowClient")
    path = workflow_class_path(flow)
    app = make_app(tmp_path, ['{"text":"done"}'], {path: flow})

    async with make_service(app) as service:
        handle = await service.launch(path, Root(text="hi"))
        assert handle.status == "waiting_human"

        result = await service.resume(handle.run_id, payload={"verdict": "approve"})
        assert result.status == "completed"
        assert (await service.status(handle.run_id)).output == {"text": "done"}


async def test_unknown_ids_raise_unknown_run_error_everywhere(tmp_path):
    w = make_worker("WCliU", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainCliUnk")
    app = make_app(tmp_path, [], {workflow_class_path(chain): chain})

    async with make_service(app) as service:
        with pytest.raises(UnknownRunError):
            await service.launch("fixtures.Missing", Root(text="hi"))
        with pytest.raises(UnknownRunError):
            await service.resume("bogus")
        with pytest.raises(UnknownRunError):
            await service.status("bogus")
        with pytest.raises(UnknownRunError):
            await service.cancel("bogus")
        with pytest.raises(UnknownRunError):
            await service.attach_note("bogus", "hi")


async def test_bad_input_raises_config_error_with_field_message(tmp_path):
    w = make_worker("WCliBad", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainCliBad")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, [], {path: chain})

    async with make_service(app) as service:
        # Backend DataError renders as 400; the client reports ConfigError.
        with pytest.raises(ConfigError) as excinfo:
            await service.launch(path, BadInput())
        assert "text" in str(excinfo.value)


async def test_list_runs_filters_through_the_client(tmp_path):
    wd = make_worker("WDcl", Root, Final)
    done = make_chain([wd], Root, Final, name="ChainCliD")
    human = make_human("ReviewClL", Root, Review)
    fin = make_worker("FinClL", Review, Final, prompt="ok {verdict}")
    waiting = make_review_flow(human, {"approve": fin}, name="FlowCliL")
    done_path = workflow_class_path(done)
    waiting_path = workflow_class_path(waiting)
    app = make_app(tmp_path, ['{"text":"one"}'], {done_path: done, waiting_path: waiting})

    async with make_service(app) as service:
        completed = await service.launch(done_path, Root(text="a"))
        paused = await service.launch(waiting_path, Root(text="b"))

        everything = await service.list_runs()
        assert sorted(s.run_id for s in everything) == sorted([completed.run_id, paused.run_id])
        assert all(s.waiting_on_human for s in everything if s.run_id == paused.run_id)

        by_workflow = await service.list_runs(RunFilters(workflow=done_path))
        assert [s.run_id for s in by_workflow] == [completed.run_id]

        by_status = await service.list_runs(RunFilters(status="waiting_human"))
        assert [s.run_id for s in by_status] == [paused.run_id]

        assert await service.list_runs(RunFilters(status="cancelled")) == []


async def test_backend_500_maps_to_ngweave_error(tmp_path):
    w = make_worker("WCli500", Root, Final)
    chain = make_chain([w], Root, Final, name="ChainCli500")
    app = make_app(tmp_path, ['{"text":"one"}'], {workflow_class_path(chain): chain})

    async def failing(run_id):
        raise InfraError("downstream outage")

    async with make_service(app) as service:
        handle = await service.launch(workflow_class_path(chain), Root(text="a"))
        app.state.run_service.status = failing

        with pytest.raises(NgWeaveError, match="downstream outage"):
            await service.status(handle.run_id)


async def test_cancel_after_completion_is_noop_through_the_client(tmp_path):
    wc = make_worker("WCliC", Root, Final)
    chain = make_chain([wc], Root, Final, name="ChainCliCancel")
    path = workflow_class_path(chain)
    app = make_app(tmp_path, ['{"text":"one"}'], {path: chain})

    async with make_service(app) as service:
        handle = await service.launch(path, Root(text="a"))

        await service.cancel(handle.run_id)

        assert (await service.status(handle.run_id)).status == "completed"
