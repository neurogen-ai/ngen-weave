"""Shared RunService conformance suite: one contract exercised by every backend."""

from __future__ import annotations

import httpx
import pytest
from ngen_weave import registry
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.service import RunService
from ngen_weave.workflow import Workflow
from ngen_weave_server.client import HttpRunService
from ngen_weave_server.local import LocalRunService
from ngen_weave_server.test_local_service import (
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
from tests.fakes import FakeProvider


@pytest.fixture(autouse=True)
def _clean_registry():
    """Generated test classes register globally; isolate each test."""
    registry.reset()
    yield
    registry.reset()


def make_local_service(
    tmp_path, replies, discovery_map: dict[str, type[Workflow]]
) -> LocalRunService:
    """LocalRunService over a FakeProvider engine writing into tmp dirs."""
    provider = FakeProvider(replies)
    store = RunStore(tmp_path / "runs")
    engine = Engine(provider, store, checkpointer="memory")
    return LocalRunService(engine, store, discovery_map)


def make_http_service(
    tmp_path, replies, discovery_map: dict[str, type[Workflow]]
) -> HttpRunService:
    """HttpRunService against the ASGI app wired to the same tmp dirs."""
    app = make_app(tmp_path, replies, discovery_map)
    return HttpRunService("http://test", transport=httpx.ASGITransport(app=app))


@pytest.fixture(params=["local", "http"])
def service(request, tmp_path):
    """RunService factories parametrized over backends on shared tmp wiring.

    Returns ``build(replies, discovery_map) -> RunService`` where the backend
    follows request.param: "local" yields LocalRunService, "http" an
    HttpRunService speaking to the ASGI app over identical tmp dirs.
    """

    def build(replies: list[str], discovery_map: dict[str, type[Workflow]]) -> RunService:
        factory = make_local_service if request.param == "local" else make_http_service
        return factory(tmp_path, replies, discovery_map)

    return build


__all__ = [
    "Final",
    "Piece",
    "Review",
    "Root",
    "make_chain",
    "make_human",
    "make_local_service",
    "make_review_flow",
    "make_worker",
]
