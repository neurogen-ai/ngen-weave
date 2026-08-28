"""Registry-visible workflows for the SWE_tools plugin.

Only the classes DEFINED in this module are registered by discovery (see
ngen-weave.json's module list): PlanSWETask and ImplementPlanStep. Every
other leaf and mid-level composite lives in plugins.swe_tools.nodes, which
no discovery channel lists.

PlanSWETask:

    START -> Scout -> PlanDraft -> PlanGate --pass--> END
                                 --fail(revise)--> PlanRework --cond--> PlanDraft
                                 --fail(fail_count >= ORACLE_AFTER)--> Oracle -> END

    The revise loop re-enters through dispatch-only nodes, so the gate's
    feedback and the fail counter survive each iteration (retry loops into
    nodes with static parents would re-fire on stale outputs instead).

ImplementPlanStep:

    START -> StepGate --pass--> StepPlan -> PlanDetailGate --pass--> StepScout
                      --fail--> StepClarify --cond--> StepPlan
                                        --fail--> PlanRefine --cond--> StepPlan
                      (detail gate)          StepScout -> ScoutCheckGate
                      --pass--> DevAgent -> ReviewerWorker -> END
                      --fail--> ScoutClarify --cond--> StepPlan
                      --fail(fail_count >= ORACLE_AFTER)--> Oracle -> END
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
    Scout,
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
    if verdict["pass"]:
        return "pass"
    return "oracle" if verdict["fail_count"] >= ORACLE_AFTER else "revise"


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
        "Plan a SWE task: scout, draft a discrete single-context-window plan, "
        "gate it, loop plan rework, escalate to the oracle after repeated failures."
    )
    human_description = (
        "Scouts the instruction, drafts a plan of discrete steps with evidence, "
        "and loops plan rework until the gate passes or the oracle is consulted."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        scout = Scout()
        plan_draft = PlanDraft()
        plan_gate = PlanGate()
        plan_rework = PlanRework()
        oracle = Oracle()
        for node in (scout, plan_draft, plan_gate, plan_rework, oracle):
            g.add_node(node)
        g.add_edge(START, scout)
        g.add_conditional_edges(scout, _go_router, {"go": plan_draft})
        g.add_edge(plan_draft, plan_gate)
        g.add_conditional_edges(
            plan_gate,
            _plan_router,
            {"pass": END, "revise": plan_rework, "oracle": oracle},
        )
        g.add_conditional_edges(plan_rework, _go_router, {"go": plan_draft})
        g.add_edge(oracle, END)


class ImplementPlanStep(Workflow):
    """Implement one plan step: gate, plan detail, scout answers, dev, review."""

    description = (
        "Implement a plan step: gate the step, plan detail with honest unknowns, "
        "scout answers, write the code, and review the diff against the step."
    )
    human_description = (
        "Checks the plan step is specific, plans abstractions and files honestly, "
        "scouts the plan's open questions, writes the code, and returns a "
        "BLOCKING/MAJOR/MINOR review of the diff against the step."
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
