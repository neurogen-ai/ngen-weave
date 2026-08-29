"""Registry-visible workflows for the SWE_tools plugin.

Only the classes DEFINED in this module are registered by discovery (see
ngen-weave.json's module list): ScoutDir, PlanTask and ImplementPlanStep.
Every other leaf and mid-level composite lives in plugins.swe_tools.nodes,
which no discovery channel lists.

ScoutDir:

    START -> SpecGate --pass--> ScoutAgent -> END
                      --fail--> Clarify --cond--> ScoutAgent

    The caller's standalone read-only look at the repo: one question in, one
    bounded summary out (file paths, key symbols, data flow, open questions).
    It exists for the clarification phase of PlanTask, when PlanRework grills
    the caller about gaps and tells them to do the scouting they owe. The
    summary is summary-shaped on purpose: the scout returns paths, line
    ranges, and one-line notes, not verbatim code, so the caller's context
    stays small. Inside ImplementPlanStep, scouting happens where the code
    gets written (StepScout feeds the inner plan->scout loop), not here.

PlanTask:

    START -> PlanDraft -> PlanGate --pass--> END
                        --fail--> PlanRework -> END

    The planner emits a structured step list (id, summary, files_expected,
    depends_on, completion_check per step), not one blob. PlanGate checks the
    structure programmatically (>= 2 steps, valid DAG, checkable completion
    criterion per step) and asks the model only whether each step is
    single-context-window sized; a one-giant-step plan fails the gate. On
    failure PlanRework does not loop back into the planner: it grills the
    agent who invoked the workflow about where the plan leaves gaps, with
    pointed questions and the scouting it owes (ScoutDir is the tool for
    that). The caller answers on the next invocation by feeding those answers
    in as clarifications. The run returns the step list only: ids and
    one-line summaries, no scout evidence, no code. The list is designed to
    be fed back to ImplementPlanStep one step at a time.

ImplementPlanStep:

    START -> StepGate --pass--> StepPlan -> PlanDetailGate --pass--> StepScout
                      --fail--> END
                                        --fail--> PlanRefine --cond--> StepPlan
                      (detail gate)     StepScout -> ScoutCheckGate
                      --pass--> DevAgent -> ReviewerWorker -> StepRecorder -> END
                      --fail--> ScoutClarify --cond--> StepPlan

    The entry gate is a programmatic predicate over the cached plan (zero LLM
    calls): the input must reference exactly one step_id from the cached plan,
    that step's dependencies must be marked done in the cache's step_status
    map, and the step must not already be implemented. A failed gate ends the
    run with the reason in gate_notes instead of burning an inner clarify
    round-trip; the caller fixes its input and re-invokes.

    The plan->scout loop (StepPlan -> PlanDetailGate -> StepScout ->
    ScoutCheckGate -> ScoutClarify -> StepPlan) exits once the plan is safe to
    start: it names concrete files/symbols for this step alone and lists its
    residual unknowns explicitly. After SCOUT_ROUNDS_MAX failed rounds the
    scout gate passes with the open questions attached in gate_notes, and the
    DevAgent verifies those unknowns against the code at write-time, where
    verification is cheap. Prior steps' review reports reach the DevAgent
    through the cache's step_status records, not through the top agent's
    context.

    StepRecorder persists per-step {status, reviewer_verdict, files_touched}
    into the cache's step_status map, so the caller's loop over steps is
    mechanical ("next incomplete step") and partial-progress telemetry
    survives even if a trial dies at the timeout.
"""

from __future__ import annotations

from ngen_weave.workflow import (
    END,
    START,
    GraphBuilder,
    Workflow,
    workflow_class_path,
)

from .cache import Cache
from .nodes import (
    Clarify,
    DevAgent,
    PlanDetailGate,
    PlanDraft,
    PlanGate,
    PlanRefine,
    PlanRework,
    ReviewerWorker,
    ScoutAgent,
    ScoutCheckGate,
    ScoutClarify,
    SpecGate,
    StepGate,
    StepPlan,
    StepRecorder,
)


def _plan_router(state: dict) -> str:
    verdict = state[workflow_class_path(PlanGate)]
    return "pass" if verdict["pass"] else "fail"


def _step_router(state: dict) -> str:
    gate = state[workflow_class_path(StepGate)]
    return "pass" if gate["pass"] else "fail"


def _detail_router(state: dict) -> str:
    gate = state[workflow_class_path(PlanDetailGate)]
    return "pass" if gate["pass"] else "fail"


def _scout_check_router(state: dict) -> str:
    verdict = state[workflow_class_path(ScoutCheckGate)]
    # Forced passes after SCOUT_ROUNDS_MAX arrive here already marked pass;
    # the open questions ride in gate_notes for the DevAgent.
    return "dev" if verdict["pass"] else "replan"


def _scout_router(state: dict) -> str:
    gate = state[workflow_class_path(SpecGate)]
    return "pass" if gate["pass"] else "fail"


def _go_router(state: dict) -> str:
    """Constant router: dispatch the sender's output to its single target."""
    return "go"


def _proceed(state: dict) -> str:
    """Constant router: dispatch the fixer's output to its single target."""
    return "proceed"


class ScoutDir(Workflow):
    """Answer one question about the repo with a bounded read-only summary.

    Registry-visible so the calling agent can scout its own gaps during
    PlanTask's clarification phase, when PlanRework names the scouting it
    still owes. The scout agent is instructed to return a summary, not
    verbatim code, so the caller's context stays small; the heavy verbatim
    evidence lives inside ImplementPlanStep's own scout loop, where the code
    gets written.
    """

    description = (
        "Scout the repo: gate that the question is specific enough for a "
        "targeted search, clarify it if not, then search and return a "
        "bounded summary of the files and snippets that answer it."
    )
    human_description = (
        "Checks that the question is specific enough for a targeted search, "
        "clarifies it if not, then lists and reads candidate files and "
        "produces a summary of which files and snippets answer the question. "
        "Read-only: returns file paths, line ranges, and one-line notes, "
        "never verbatim code dumps."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        spec_gate = SpecGate()
        clarify = Clarify()
        scout_agent = ScoutAgent()
        for node in (spec_gate, clarify, scout_agent):
            g.add_node(node)
        g.add_edge(START, spec_gate)
        g.add_conditional_edges(
            spec_gate, _scout_router, {"pass": scout_agent, "fail": clarify}
        )
        g.add_conditional_edges(clarify, _proceed, {"proceed": scout_agent})
        g.add_edge(scout_agent, END)


class PlanTask(Workflow):
    """Turn an instruction into a gated, structured list of single-window steps."""

    description = (
        "Plan a SWE task: draft a structured step list (id, summary, files, "
        "dependencies, completion check per step) that achieves the "
        "instruction with no knowledge gaps, gate it for multi-step structure "
        "and per-step sizing, and on failure grill the caller about the gaps."
    )
    human_description = (
        "Drafts a plan of concrete, directive steps that achieve the "
        "instruction, gates it on being a multi-step plan sized one context "
        "window per step, and on failure returns pointed questions back to "
        "the calling agent, including the scouting the caller still owes. "
        "Returns the step list only: ids and one-line summaries, ready to be "
        "fed to ImplementPlanStep one step at a time."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        plan_draft = PlanDraft()
        plan_gate = PlanGate()
        plan_rework = PlanRework()
        for node in (plan_draft, plan_gate, plan_rework):
            g.add_node(node)
        g.add_edge(START, plan_draft)
        g.add_edge(plan_draft, plan_gate)
        g.add_conditional_edges(
            plan_gate,
            _plan_router,
            {"pass": END, "fail": plan_rework},
        )
        g.add_edge(plan_rework, END)


class ImplementPlanStep(Workflow):
    """Implement one plan step: programmatic gate, bounded scouting, dev, review."""

    description = (
        "Implement a plan step: gate programmatically that exactly one step "
        "id from the cached plan was fed in and its dependencies are done, "
        "plan detail with honest unknowns, loop plan and scout at most two "
        "rounds until the plan is safe to start, then write, review, and "
        "record the step's verdict in the cache."
    )
    human_description = (
        "Checks exactly one plan step was fed in (programmatic gate against "
        "the cached plan), drafts a plan with honest unknowns, and scouts "
        "until the plan is safe to start, at which point the dev agent "
        "resolves any remaining open questions against the code while "
        "writing. Returns a BLOCKING/MAJOR/MINOR review of the diff against "
        "the step and persists the step's status in the cache."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        step_gate = StepGate()
        step_plan = StepPlan()
        detail_gate = PlanDetailGate()
        plan_refine = PlanRefine()
        step_scout = ScoutAgent()
        scout_check = ScoutCheckGate()
        scout_clarify = ScoutClarify()
        dev = DevAgent()
        reviewer = ReviewerWorker()
        recorder = StepRecorder()
        for node in (
            step_gate,
            step_plan,
            detail_gate,
            plan_refine,
            step_scout,
            scout_check,
            scout_clarify,
            dev,
            reviewer,
            recorder,
        ):
            g.add_node(node)

        g.add_edge(START, step_gate)
        g.add_conditional_edges(
            step_gate, _step_router, {"pass": step_plan, "fail": END}
        )
        g.add_edge(step_plan, detail_gate)
        g.add_conditional_edges(
            detail_gate, _detail_router, {"pass": step_scout, "fail": plan_refine}
        )
        g.add_conditional_edges(plan_refine, _go_router, {"go": step_plan})
        g.add_edge(step_scout, scout_check)
        g.add_conditional_edges(
            scout_check,
            _scout_check_router,
            {"dev": dev, "replan": scout_clarify},
        )
        g.add_conditional_edges(scout_clarify, _go_router, {"go": step_plan})
        g.add_edge(dev, reviewer)
        g.add_edge(reviewer, recorder)
        g.add_edge(recorder, END)
