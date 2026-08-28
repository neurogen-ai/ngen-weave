"""Registry-visible workflows for the SWE_tools plugin.

Only the classes DEFINED in this module are registered by discovery (see
ngen-weave.json's module list): PlanSWETask and ImplementPlanStep. Every
other leaf and mid-level composite lives in plugins.swe_tools.nodes, which
no discovery channel lists.

PlanSWETask:

    START -> PlanDraft -> PlanGate --pass--> END
                        --fail--> PlanRework -> END

    PlanRework does not loop back into the planner: it grills the agent who
    invoked the workflow about where the plan leaves gaps, with pointed
    questions and the scouting it owes. The caller answers on the next
    invocation by feeding those answers in as clarifications.

ImplementPlanStep:

    START -> StepGate --pass--> StepPlan -> PlanDetailGate --pass--> StepScout
                      --fail--> StepClarify --cond--> StepPlan
                                        --fail--> PlanRefine --cond--> StepPlan
                      (detail gate)          StepScout -> ScoutCheckGate
                      --pass--> DevAgent -> ReviewerWorker -> END
                      --fail--> ScoutClarify --cond--> StepPlan
                      --fail(fail_count >= ORACLE_AFTER)--> Oracle -> END

    The entry gate only passes a single part of the overall plan, specific
    enough to implement. The plan->scout loop (StepPlan -> PlanDetailGate ->
    StepScout -> ScoutCheckGate -> ScoutClarify -> StepPlan) exits only once
    the scout check gate is sure the plan cites enough evidence from the
    scout that it can be implemented well, step by step, to achieve the
    meta-plan instruction given to this workflow.
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
    Oracle,
    PlanDetailGate,
    PlanDraft,
    PlanGate,
    PlanRefine,
    PlanRework,
    ReviewerWorker,
    ScoutAgent,
    ScoutCheckGate,
    ScoutClarify,
    StepClarify,
    StepGate,
    StepPlan,
)

ORACLE_AFTER = 3


def _go_router(state: dict) -> str:
    """Constant router: dispatch the sender's output to its single target."""
    return "go"


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
    if verdict["pass"]:
        return "dev"
    return "oracle" if verdict["fail_count"] >= ORACLE_AFTER else "replan"


class PlanSWETask(Workflow):
    """Turn an instruction into a gated plan of single-window steps."""

    description = (
        "Plan a SWE task: draft a discrete single-context-window plan that "
        "achieves the instruction with no knowledge gaps, gate it for "
        "sufficiency, and on failure grill the caller about the gaps."
    )
    human_description = (
        "Drafts a plan of concrete, directive steps that achieve the "
        "instruction, gates it on being sufficient to implement, and on "
        "failure returns pointed questions back to the calling agent, "
        "including the scouting the caller still owes."
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
    """Implement one plan step: gate, plan detail, scout answers, dev, review."""

    description = (
        "Implement a plan step: gate that exactly one specific plan part was "
        "fed in, plan detail with honest unknowns, and loop plan and scout "
        "until the plan cites enough evidence to implement step by step "
        "toward the meta-plan instruction, then write and review."
    )
    human_description = (
        "Checks exactly one specific plan step was fed in, drafts a plan with "
        "honest unknowns, and scouts until the check gate is satisfied the "
        "plan can be implemented well, step by step, to achieve the meta-plan "
        "instruction. Then writes the code and returns a BLOCKING/MAJOR/MINOR "
        "review of the diff against the step."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        step_gate = StepGate()
        step_clarify = StepClarify()
        step_plan = StepPlan()
        detail_gate = PlanDetailGate()
        plan_refine = PlanRefine()
        step_scout = ScoutAgent()
        scout_check = ScoutCheckGate()
        scout_clarify = ScoutClarify()
        dev = DevAgent()
        reviewer = ReviewerWorker()
        oracle = Oracle()
        for node in (
            step_gate,
            step_clarify,
            step_plan,
            detail_gate,
            plan_refine,
            step_scout,
            scout_check,
            scout_clarify,
            dev,
            reviewer,
            oracle,
        ):
            g.add_node(node)

        g.add_edge(START, step_gate)
        g.add_conditional_edges(step_gate, _step_router, {"pass": step_plan, "fail": step_clarify})
        g.add_conditional_edges(step_clarify, _go_router, {"go": step_plan})
        g.add_edge(step_plan, detail_gate)
        g.add_conditional_edges(
            detail_gate, _detail_router, {"pass": step_scout, "fail": plan_refine}
        )
        g.add_conditional_edges(plan_refine, _go_router, {"go": step_plan})
        g.add_edge(step_scout, scout_check)
        g.add_conditional_edges(
            scout_check,
            _scout_check_router,
            {"dev": dev, "replan": scout_clarify, "oracle": oracle},
        )
        g.add_conditional_edges(scout_clarify, _go_router, {"go": step_plan})
        g.add_edge(dev, reviewer)
        g.add_edge(reviewer, END)
        g.add_edge(oracle, END)
