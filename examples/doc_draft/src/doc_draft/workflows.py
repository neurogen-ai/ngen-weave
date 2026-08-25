"""Doc-draft example: draft, model-judged quality gate, human review, finalize.

Shape: START -> draft -> quality_gate --pass--> finalize / --fail-->
human_review; human approve --> finalize, reject --> draft; finalize -> END.
Unlike code_review's programmatic gate, the quality gate here runs in
model mode: it has no decide() override, so its rendered prompt goes to the
model and the reply is parsed as the routing boolean. The gate leaf is bound
to a cheaper model variant purely through ngw.yaml's models section.
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


class DraftRequest(BaseModel):
    """Run input: the topic to draft a document about."""

    topic: str


class FinalDoc(BaseModel):
    """Run output: the finalized document."""

    document: str


class DraftOutput(BaseModel):
    """Draft output: the drafted document plus the topic carried forward."""

    document: str
    topic: str


# `pass` is a keyword, so the control output schema is built dynamically.
# The extra field needs a default because the engine constructs this model
# with the verdict alone.
GateVerdict = type(
    "GateVerdict",
    (BaseModel,),
    {
        "__annotations__": {"document": str, "pass": bool},
        "document": "",
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
    document: str = ""


def _gate_router(state: dict) -> str:
    """Read the gate's validated boolean and map it to a branch label."""
    verdict = state[workflow_class_path(QualityGate)]["pass"]
    return "pass" if verdict else "fail"


def _verdict_router(state: dict) -> str:
    """Read the human's verdict literal and map it to a branch label."""
    return state[workflow_class_path(HumanReview)]["verdict"]


class Draft(Worker):
    """Drafts a short document on the topic; the model call exercises the provider."""

    description = "Draft a short document on the requested topic."
    input_type = DraftRequest
    output_type = DraftOutput
    prompt = "Write a concise, well-structured document about {topic}.\n"

    # The engine renders the prompt and parses the reply into output_type;
    # the reply carries only the document, so the topic is echoed locally.


class QualityGate(Control):
    """Model-mode gate: the rendered prompt asks the model to judge the draft.

    No decide() override, so the engine sends the prompt to the configured
    model variant and parses the reply as the boolean pass verdict.
    """

    description = "Gate on the drafted document meeting quality standards."
    input_type = DraftOutput
    output_type = GateVerdict
    prompt = (
        "You are a quality gate for documents. Judge whether the following "
        "document on '{topic}' is clear, accurate, and complete.\n\n"
        "{document}\n\nReply with exactly 'true' if it passes, else 'false'.\n"
    )


class HumanReview(Human):
    """Interrupts for human approval; rejects loop back to the drafter."""

    description = "Approve or reject the gated draft."
    human_description = "Review the draft and approve it or send it back."
    input_type = GateVerdict
    output_type = ReviewOutcome
    state_type = HumanDecision
    verdict_field = "verdict"
    # The gate's validated output arrives whole on this node's single parent
    # edge, so dotted paths start at its fields directly.
    prefill = {"notes": "document"}

    def transform(self, context: BaseModel, state: BaseModel) -> ReviewOutcome:
        return ReviewOutcome(
            verdict=state.verdict,
            notes=state.notes,
            document=context.document,
        )


class Finalize(Worker):
    """Produces the final document, persisted as a content-addressed artifact."""

    description = "Finalize the approved document artifact."
    input_type = ReviewOutcome
    output_type = FinalDoc
    artifacts = ("document",)
    prompt = (
        "Produce the final version of the document.\n"
        "\nVerdict: {verdict}\nReviewer notes: {notes}\n\nDocument:\n{document}\n"
    )


class DocDraft(Workflow):
    """Composite wiring the doc-draft graph."""

    description = "Doc draft: draft, model quality gate, human approval, finalize."
    human_description = (
        "Drafts a document on your topic, gates it with a model judge, waits "
        "for human approval, then finalizes the document as an artifact."
    )
    input_type = DraftRequest
    output_type = FinalDoc

    def build(self, g: GraphBuilder) -> None:
        draft = Draft()
        gate = QualityGate()
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
