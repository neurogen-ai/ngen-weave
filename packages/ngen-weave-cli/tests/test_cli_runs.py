"""Runs-management CLI verbs over a RunService: local and HTTP backends."""

from __future__ import annotations

import json
import textwrap

import httpx
import pytest
from ngen_weave.engine.store import RunStore
from ngen_weave.provenance import ProvenanceRecord, join_path
from ngen_weave.registry import reset as registry_reset
from ngen_weave_cli.context import reset_merged_registry
from ngen_weave_cli.main import app
from ngen_weave_server.app import create_app
from tests.fakes import FakeProvider
from typer.testing import CliRunner

runner = CliRunner()

FIXTURE_MODULE = "cli_runs_fixture_workflows"

FIXTURE_SOURCE = textwrap.dedent(
    """
    from pydantic import BaseModel

    from ngen_weave.workflow import Workflow


    class EchoIn(BaseModel):
        text: str


    class EchoOut(BaseModel):
        echoed: str


    class Echo(Workflow):
        description = "test echo workflow"
        human_description = "echoes its input"
        input_type = EchoIn
        output_type = EchoOut

        async def run(self, input, ctx):
            return EchoOut(echoed=input.text)
    """
)


@pytest.fixture()
def workflow_module(tmp_path, monkeypatch):
    """Install the fixture workflow module plus manifest, anchored at tmp_path."""
    (tmp_path / f"{FIXTURE_MODULE}.py").write_text(FIXTURE_SOURCE)
    (tmp_path / "ngen-weave.json").write_text(json.dumps({"modules": [FIXTURE_MODULE]}))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield FIXTURE_MODULE
    registry_reset()
    reset_merged_registry()


def _seed_run(tmp_path, workflow: str, status: str) -> str:
    """Insert one run row through the local store with one model_call record."""
    return _seed_through(_store(tmp_path), workflow, status)


def _store(tmp_path) -> RunStore:
    """A RunStore on the same database the CLI's local stack reads."""
    return RunStore(tmp_path / ".ngen-weave" / "runs")


# --- local backend -----------------------------------------------------------


def test_workflows_lists_paths_and_human_descriptions(workflow_module):
    result = runner.invoke(app, ["workflows"])

    assert result.exit_code == 0, result.stderr
    assert f"{FIXTURE_MODULE}.Echo\techoes its input" in result.output
    # Person-facing inventory only; the MCP-facing description must not appear.
    assert "test echo workflow" not in result.output


def test_runs_lists_seeded_runs_with_fields(workflow_module, tmp_path):
    done_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "completed")
    waiting_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "waiting_human")

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.stderr
    lines = {line.split()[0]: line for line in result.output.splitlines()}
    assert set(lines) == {done_id, waiting_id}
    assert f"{FIXTURE_MODULE}.Echo  completed" in lines[done_id]
    assert "waiting-human" not in lines[done_id]
    assert f"{FIXTURE_MODULE}.Echo  waiting_human" in lines[waiting_id]
    assert "waiting-human" in lines[waiting_id]
    assert "0.150000" in lines[done_id]
    # started_at prints the run header's creation timestamp verbatim.
    assert _store(tmp_path).load(done_id).started_at in lines[done_id]


def test_runs_filters_by_workflow_and_status(workflow_module, tmp_path):
    echo_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "completed")
    other_id = _seed_run(tmp_path, "other.Workflow", "running")

    by_workflow = runner.invoke(app, ["runs", "--workflow", f"{FIXTURE_MODULE}.Echo"])
    assert by_workflow.exit_code == 0, by_workflow.stderr
    ids = [line.split()[0] for line in by_workflow.output.splitlines()]
    assert ids == [echo_id]

    by_status = runner.invoke(app, ["runs", "--status", "running"])
    assert by_status.exit_code == 0, by_status.stderr
    assert [line.split()[0] for line in by_status.output.splitlines()] == [other_id]

    none = runner.invoke(app, ["runs", "--status", "cancelled"])
    assert none.exit_code == 0, none.stderr
    assert none.output.strip() == ""


def test_cancel_unknown_run_exits_1(workflow_module):
    result = runner.invoke(app, ["cancel", "no-such-run"])

    assert result.exit_code == 1
    assert "unknown run" in result.stderr


def test_cancel_prints_resulting_status(workflow_module, tmp_path):
    run_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "waiting_human")

    result = runner.invoke(app, ["cancel", run_id])

    assert result.exit_code == 0, result.stderr
    assert "status cancelled" in result.output
    assert _store(tmp_path).load(run_id).status == "cancelled"


def test_cancel_terminal_run_is_noop(workflow_module, tmp_path):
    run_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "completed")

    result = runner.invoke(app, ["cancel", run_id])

    assert result.exit_code == 0, result.stderr
    assert "status completed" in result.output


def test_note_appends_to_run_notes(workflow_module, tmp_path):
    run_id = _seed_run(tmp_path, f"{FIXTURE_MODULE}.Echo", "paused")

    result = runner.invoke(app, ["note", run_id, "reviewed by ops"])

    assert result.exit_code == 0, result.stderr
    assert _store(tmp_path).load(run_id).notes == ["reviewed by ops"]


def test_note_unknown_run_exits_1(workflow_module):
    result = runner.invoke(app, ["note", "no-such-run", "text"])

    assert result.exit_code == 1
    assert "unknown run" in result.stderr


# --- HTTP backend (--url against the D2 ASGI app) ----------------------------


def make_url_ctx(monkeypatch, tmp_path, replies: list[str] | None = None):
    """Build the serving app over tmp dirs and patch its transport into HttpRunService."""
    provider = FakeProvider(replies or [])
    srv_dir = tmp_path / "srv"
    http_app = create_app(
        runs_dir=srv_dir / "runs",
        runs_db_path=srv_dir / "runs.db",
        db_path=tmp_path / "checkpoints.db",
        models_file=tmp_path / "models.json",
        provider=provider,
        discovery_map={},
    )

    from ngen_weave_server import client as client_mod

    real_cls = client_mod.HttpRunService

    def factory(base_url: str) -> object:
        """HttpRunService speaking to the ASGI app in-process."""
        return real_cls(base_url, transport=httpx.ASGITransport(app=http_app))

    monkeypatch.setattr(client_mod, "HttpRunService", factory)
    return http_app


@pytest.fixture()
def http_env(workflow_module, monkeypatch, tmp_path):
    """Workflow module anchored at tmp_path plus a patched HTTP backend."""
    http_app = make_url_ctx(monkeypatch, tmp_path)
    return http_app


def test_http_runs_lists_seeded_rows(http_env, tmp_path):
    store = http_env.state.run_service.store
    run_id = _seed_through(store, f"{FIXTURE_MODULE}.Echo", "waiting_human")

    result = runner.invoke(app, ["runs", "--url", "http://test", "--status", "waiting_human"])

    assert result.exit_code == 0, result.stderr
    assert [line.split()[0] for line in result.output.splitlines()] == [run_id]
    assert "waiting-human" in result.output


def test_http_cancel_prints_resulting_status(http_env):
    store = http_env.state.run_service.store
    run_id = _seed_through(store, f"{FIXTURE_MODULE}.Echo", "waiting_human")

    result = runner.invoke(app, ["cancel", run_id, "--url", "http://test"])

    assert result.exit_code == 0, result.stderr
    assert "status cancelled" in result.output
    assert store.load(run_id).status == "cancelled"


def test_http_unknown_cancel_exits_1(http_env):
    result = runner.invoke(app, ["cancel", "no-such-run", "--url", "http://test"])

    assert result.exit_code == 1
    assert "unknown run" in result.stderr


def test_http_note_round_trips(http_env):
    store = http_env.state.run_service.store
    run_id = _seed_through(store, f"{FIXTURE_MODULE}.Echo", "paused")

    result = runner.invoke(app, ["note", run_id, "via url", "--url", "http://test"])

    assert result.exit_code == 0, result.stderr
    assert store.load(run_id).notes == ["via url"]


def _seed_through(store: RunStore, workflow: str, status: str) -> str:
    """Seed one run row with a model_call record through the given store."""
    run_id = store.create(workflow, {"text": "hi"})
    run_file = store.load(run_id)
    run_file.records.append(
        ProvenanceRecord(
            version=1,
            run_id=run_id,
            node_path=join_path(workflow),
            kind="model_call",
            ts="2025-01-01T00:00:00Z",
            payload={"variant": "sonnet", "tokens_total": 150, "cost_usd": 0.15},
        )
    )
    run_file.status = status
    store.save(run_file)
    return run_id
