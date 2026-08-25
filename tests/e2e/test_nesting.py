"""End-to-end test of two-level nesting around code_review via the Typer CLI.

Defines an OuterReview composite in a local module (written into tmp_path and
made importable, then listed in the copied ngen-weave.json manifest next to
code_review.workflows). The outer graph wires OuterDraft -> CodeReview ->
OuterFinalize, so the example's canonical chain runs at depth 2.

Two cases exercise success criterion 3's halves through the real CLI:
cost attribution (the outer composite scope's accumulated cost_usd equals the
sum of every model_call inside its subtree) and a resumable interrupt parked
by HumanReview inside CodeReview, resumed through `resume <id> -p response`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from ngen_weave.engine.store import RunStore
from ngen_weave.registry import reset as registry_reset
from ngen_weave_cli.context import reset_merged_registry
from ngen_weave_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "code_review"
EXAMPLE_SRC = EXAMPLE_DIR / "src"

ROOT_PATH = "outer_review.OuterReview"
CODE_REVIEW_PATH = "code_review.workflows.CodeReview"

OUTER_MODULE = '''"""Outer composite fixture: nests code_review's CodeReview at depth 2."""

from ngen_weave.workflow import END, START, GraphBuilder, Worker, Workflow
from pydantic import BaseModel

from code_review.workflows import CodeReview, ReviewRequest, ReviewedDiff


class TaskRequest(BaseModel):
    """Outer run input: a free-form task string."""

    task: str


class OuterResult(BaseModel):
    """Outer run output: the task plus the child chain's reviewed diff."""

    task: str
    reviewed_diff: str
    verdict: str


class OuterDraft(Worker):
    """Boundary worker producing ReviewRequest-shaped input for the child."""

    description = "Turn the task into a review request carrying the diff."
    input_type = TaskRequest
    output_type = ReviewRequest
    prompt = "Prepare a review request for task: {task}\\n"


class OuterFinalize(Worker):
    """Boundary worker consuming the child's ReviewedDiff; declares no artifacts."""

    description = "Wrap the reviewed diff with the originating task."
    input_type = ReviewedDiff
    output_type = OuterResult
    prompt = "Summarize verdict {verdict} for diff:\\n{reviewed_diff}\\n"


class OuterReview(Workflow):
    """Composite: outer draft, nested CodeReview, outer finalize."""

    description = "Nested code review under an outer wrapper."
    human_description = "Runs a code review nested one level deep."
    input_type = TaskRequest
    output_type = OuterResult

    def build(self, g: GraphBuilder) -> None:
        draft = OuterDraft()
        review = CodeReview()
        finalize = OuterFinalize()
        g.add_node(draft)
        g.add_node(review)
        g.add_node(finalize)
        g.add_edge(START, draft)
        g.add_edge(draft, review)
        g.add_edge(review, finalize)
        g.add_edge(finalize, END)
'''


def _write_project(root: Path, checkpointer: str) -> None:
    """Lay out the project files the CLI reads, in tmp_path."""
    (root / "outer_review.py").write_text(OUTER_MODULE)
    (root / "ngen-weave.json").write_text(
        json.dumps({"modules": ["outer_review", "code_review.workflows"]})
    )
    shutil.copy(EXAMPLE_DIR / "models.json", root / "models.json")
    (root / "ngw.yaml").write_text(
        f"workflow: {ROOT_PATH}\nparams: {{}}\nmodels: {{}}\nrun:\n  checkpointer: {checkpointer}\n"
    )


@pytest.fixture()
def outer_project(tmp_path, monkeypatch):
    """Make both workflow modules importable and chdir into the project."""
    _write_project(tmp_path, checkpointer="memory")
    (tmp_path / "input.json").write_text(json.dumps({"task": TASK}))
    monkeypatch.syspath_prepend(str(EXAMPLE_SRC))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    registry_reset()
    reset_merged_registry()


DIFF = json.loads((EXAMPLE_DIR / "request.json").read_text())["diff"]
TASK = "review ticket 7"


def _replies(*, empty_review: bool) -> list[str]:
    """One full-JSON reply per model call, in engine call order.

    Order: OuterDraft, inner Draft, inner Finalize, OuterFinalize. An empty
    inner review makes Gate.decide fail so HumanReview parks the run.
    """
    draft_review = "" if empty_review else "Looks good overall."
    return [
        json.dumps({"diff": DIFF}),
        json.dumps({"review": draft_review, "diff": DIFF}),
        json.dumps({"reviewed_diff": DIFF, "verdict": "approve"}),
        json.dumps({"task": TASK, "reviewed_diff": DIFF, "verdict": "approve"}),
    ]


def _fake_provider(monkeypatch, replies: list[str]) -> None:
    from tests.fakes import FakeProvider

    provider = FakeProvider(replies=replies)
    monkeypatch.setattr("ngen_weave_cli.context.default_provider", lambda models_file: provider)


def _scope_metadata(run_file, node_path: str) -> dict:
    records = [
        r
        for r in run_file.records
        if r.kind == "node_activation"
        and r.node_path == node_path
        and r.payload.get("status") == "ok"
    ]
    assert len(records) == 1, f"expected one ok activation on {node_path}"
    return records[0].payload["metadata"]


def _run_id(output: str) -> str:
    prefix = "run "
    return next(line[len(prefix) :] for line in output.splitlines() if line.startswith(prefix))


def test_nested_run_attributes_subtree_cost_to_outer_scope(outer_project, monkeypatch):
    """The outer scope's cost_usd equals its whole subtree's model_call costs."""
    root = outer_project
    _fake_provider(monkeypatch, _replies(empty_review=False))

    result = runner.invoke(
        app,
        [
            "run",
            ROOT_PATH,
            "-i",
            str(root / "input.json"),
            "-c",
            str(root / "ngw.yaml"),
            "--project",
            "demo",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status completed" in result.output

    run_file = RunStore(root / ".ngen-weave" / "runs").load(_run_id(result.output))

    # Depth-2 model calls exist under the nested CodeReview scope.
    model_calls = [r for r in run_file.records if r.kind == "model_call"]
    inner_calls = [
        r for r in model_calls if r.node_path.startswith(f"{ROOT_PATH}.{CODE_REVIEW_PATH}.")
    ]
    assert len(inner_calls) == 2, [r.node_path for r in model_calls]
    inner_call_paths = {r.node_path for r in inner_calls}
    outer_calls = [r for r in model_calls if r.node_path not in inner_call_paths]
    assert len(outer_calls) == 2, [r.node_path for r in model_calls]

    subtree_cost = sum(r.payload["cost_usd"] for r in model_calls)
    inner_cost = sum(r.payload["cost_usd"] for r in inner_calls)

    # The nested composite scope's metadata sums exactly its own subtree.
    inner_meta = _scope_metadata(run_file, f"{ROOT_PATH}.{CODE_REVIEW_PATH}")
    assert inner_meta["cost_usd"] == pytest.approx(inner_cost)
    assert inner_meta["tokens_total"] == sum(r.payload["tokens_total"] for r in inner_calls)

    # Parent-level attribution: the outer composite's accumulated cost_usd
    # includes all inner model_call costs (and nothing else remains).
    outer_meta = _scope_metadata(run_file, ROOT_PATH)
    assert outer_meta["cost_usd"] == pytest.approx(subtree_cost)
    assert outer_meta["tokens_total"] == sum(r.payload["tokens_total"] for r in model_calls)
    assert outer_meta["last_output_valid"] is True


def test_interrupt_at_depth_two_parks_and_resumes_through_cli(outer_project):
    """HumanReview inside the nested CodeReview parks the run; CLI resumes it."""
    from tests.fakes import FakeProvider

    root = outer_project
    _write_project(root, checkpointer="sqlite")  # checkpoint state survives on disk
    provider = FakeProvider(replies=_replies(empty_review=True))
    target = "ngen_weave_cli.context.default_provider"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(target, lambda models_file: provider)
        first = runner.invoke(
            app,
            [
                "run",
                ROOT_PATH,
                "-i",
                str(root / "input.json"),
                "-c",
                str(root / "ngw.yaml"),
                "--project",
                "demo",
            ],
        )
    finally:
        monkeypatch.undo()
    assert first.exit_code == 1, first.output
    assert "status waiting_human" in first.output
    run_id = _run_id(first.output)

    # Fresh process: reset caches and resume through the CLI.
    reset_merged_registry()
    registry_reset()
    response_file = root / "response.json"
    response_file.write_text(json.dumps({"verdict": "approve", "notes": "lgtm"}))
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(target, lambda models_file: provider)
        second = runner.invoke(
            app, ["resume", run_id, "-p", str(response_file), "--project", "demo"]
        )
    finally:
        monkeypatch.undo()
    assert second.exit_code == 0, second.output
    assert "status completed" in second.output

    run_file = RunStore(root / ".ngen-weave" / "runs").load(run_id)
    waiting = [
        r
        for r in run_file.records
        if r.kind == "node_activation" and r.payload.get("status") == "waiting_human"
    ]
    assert len(waiting) == 1
    # The interrupt happened at depth 2, inside the nested composite.
    # Leaf paths accumulate one FULL class-path segment per level.
    human_leaf = f"{ROOT_PATH}.{CODE_REVIEW_PATH}.code_review.workflows.HumanReview"
    assert waiting[0].node_path == human_leaf
    # After resume, per-scope ok metadata exists at every level.
    ok_paths = {
        r.node_path
        for r in run_file.records
        if r.kind == "node_activation" and r.payload.get("status") == "ok"
    }
    for expected in (
        human_leaf,
        f"{ROOT_PATH}.{CODE_REVIEW_PATH}.code_review.workflows.Finalize",
        f"{ROOT_PATH}.{CODE_REVIEW_PATH}",
        ROOT_PATH,
    ):
        assert expected in ok_paths
