"""Registry-visible workflows for the SWE_tools plugin.

Only the classes DEFINED in this module are registered by discovery (see
ngen-weave.json's module list): PlanSWETask and ImplementPlanStep. Every
other leaf and mid-level composite lives in plugins.swe_tools.nodes, which
no discovery channel lists.

PlanSWETask:

    START -> PlanDraft -> PlanGate --pass--> END
                        --fail--> PlanRework -> END

    The planner emits a structured step list (id, summary, files_expected,
    depends_on, completion_check per step), not one blob. PlanGate checks the
    structure programmatically (>= 2 steps, valid DAG, checkable completion
    criterion per step) and asks the model only whether each step is
    single-context-window sized; a one-giant-step plan fails the gate. On
    failure PlanRework does not loop back into the planner: it grills the
    agent who invoked the workflow about where the plan leaves gaps, with
    pointed questions and the scouting it owes. The caller answers on the
    next invocation by feeding those answers in as clarifications. The run
    returns the step list only: ids and one-line summaries, no scout
    evidence, no code.

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


def _go_router(state: dict) -> str:
    """Constant router: dispatch the sender's output to its single target."""
    return "go"


class PlanSWETask(Workflow):
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
        "Returns the step list only: ids and one-line summaries."
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
