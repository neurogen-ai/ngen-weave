"""Canonical code-review example: draft, gate, human review, finalize.

Shape: START -> draft -> gate --pass--> finalize / --fail--> human_review;
human approve --> finalize, reject --> draft (re-drafts with the same diff);
finalize -> END. The gate routes programmatically so end-to-end runs stay
deterministic without model calls for routing; the draft and finalize workers
still exercise the model path.
"""

from __future__ import annotations

from typing import Literal

from ngen_weave.workflow import (
    END,
    START,
    Control,
    GraphBuilder,
    Human,
    Worker,
    Workflow,
    workflow_class_path,
)
from pydantic import BaseModel


class ReviewRequest(BaseModel):
    """Run input: the unified diff under review."""

    diff: str


class ReviewedDiff(BaseModel):
    """Run output: the finalized reviewed diff plus the routing verdict."""

    reviewed_diff: str
    verdict: str


class DraftReview(BaseModel):
    """Draft output: the drafted review plus the diff carried forward."""

    review: str
    diff: str


# `pass` is a keyword, so the control output schema is built dynamically.
# Both fields need defaults because the engine constructs this model with
# the verdict alone.
GateVerdict = type(
    "GateVerdict",
    (BaseModel,),
    {
        "__annotations__": {"review": str, "diff": str, "pass": bool},
        "review": "",
        "diff": "",
        "__module__": __name__,
        "__qualname__": "GateVerdict",
        "__doc__": "Gate output: context carried forward plus the boolean pass.",
    },
)


class HumanDecision(BaseModel):
    """Review artifact slots the human edits; verdict routes out of the node."""

    verdict: Literal["approve", "reject"]
    notes: str = ""


class ReviewOutcome(BaseModel):
    """Human output: the decision plus context carried toward finalize/draft.

    Every field defaults so the gate's pass branch (no verdict yet) also fits
    this model on its way to finalize.
    """

    verdict: str = ""
    notes: str = ""
    review: str = ""
    diff: str = ""


def _gate_router(state: dict) -> str:
    """Read the gate's validated boolean and map it to a branch label."""
    verdict = state[workflow_class_path(Gate)]["pass"]
    return "pass" if verdict else "fail"


def _verdict_router(state: dict) -> str:
    """Read the human's verdict literal and map it to a branch label."""
    return state[workflow_class_path(HumanReview)]["verdict"]


class Draft(Worker):
    """Drafts a review of the diff; the model call exercises the provider."""

    description = "Draft a code review of the submitted diff."
    input_type = ReviewRequest
    output_type = DraftReview
    prompt = (
        "You are reviewing a code change. Write a concise review.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"review": "<your review>", "diff": <the diff text you were given>}}\n\n'
        "Diff:\n{diff}\n"
    )

    # The engine renders the prompt and parses the reply into output_type;
    # the reply carries only the review, so the diff is echoed locally.


class Gate(Control):
    """Programmatic gate: passes whenever the draft produced a non-empty review."""

    description = "Gate on the draft review being non-empty."
    input_type = DraftReview
    output_type = GateVerdict

    def decide(self, input: DraftReview) -> bool:
        return bool(input.review.strip())


class HumanReview(Human):
    """Interrupts for human approval; rejects loop back to the drafter."""

    description = "Approve or reject the drafted review."
    human_description = "Review the draft and approve it or send it back."
    input_type = GateVerdict
    output_type = ReviewOutcome
    state_type = HumanDecision
    verdict_field = "verdict"
    # The gate's validated output arrives whole on this node's single parent
    # edge, so dotted paths start at its fields directly.
    prefill = {"notes": "review"}

    def transform(self, context: BaseModel, state: BaseModel) -> ReviewOutcome:
        return ReviewOutcome(
            verdict=state.verdict,
            notes=state.notes,
            review=context.review,
            diff=context.diff,
        )


class Finalize(Worker):
    """Produces the final reviewed diff, persisted as a content-addressed artifact."""

    description = "Finalize the reviewed diff artifact."
    input_type = ReviewOutcome
    output_type = ReviewedDiff
    artifacts = ("reviewed_diff",)
    prompt = (
        "Apply the review notes to produce the final reviewed diff.\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"reviewed_diff": "<the reviewed diff text>", "verdict": "<approve or reject>"}}\n'
        "\nVerdict: {verdict}\nReview notes: {review}\n\nDiff:\n{diff}\n"
    )


class CodeReview(Workflow):
    """Composite wiring the canonical code-review graph."""

    description = "Code review: draft, gate, human approval, finalize."
    human_description = (
        "Drafts a review of your diff, gates it, waits for human approval, "
        "then finalizes the reviewed diff as an artifact."
    )
    input_type = ReviewRequest
    output_type = ReviewedDiff

    def build(self, g: GraphBuilder) -> None:
        draft = Draft()
        gate = Gate()
        human_review = HumanReview()
        finalize = Finalize()

        g.add_node(draft)
        g.add_node(gate)
        g.add_node(human_review)
        g.add_node(finalize)

        g.add_edge(START, draft)
        g.add_edge(draft, gate)
        g.add_conditional_edges(gate, _gate_router, {"pass": finalize, "fail": human_review})
        g.add_conditional_edges(
            human_review, _verdict_router, {"approve": finalize, "reject": draft}
        )
        g.add_edge(finalize, END)
