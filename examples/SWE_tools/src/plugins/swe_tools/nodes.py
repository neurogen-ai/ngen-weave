"""Hidden node layer for the SWE_tools plugin.

Everything here is deliberately NOT registry-visible: only
`plugins.swe_tools.workflows` is listed for discovery, and discovery
registers classes DEFINED in listed modules only (re-exports are skipped),
so only PlanSWETask and ImplementPlanStep appear in the registry.

Node kinds
----------

CarryWorker / CarryAgent / CarryGate subclass Workflow (and AgentNode)
rather than the engine-managed Worker / Control leaves:

- A Worker leaf is a prompt->parse activation whose reply REPLACES the input,
  dropping carried context; a CarryWorker overrides run() to merge the parsed
  reply onto the carried cache instead.
- A Control leaf's output is constructed from the verdict alone; a CarryGate
  re-emits the whole input cache plus `pass` and an incremented `fail_count`.
- CarryAgent drives AgentNode's gated tool loop, then merges the final answer
  onto the cache.

Loop shapes
-----------

The engine's 0.2.x loop semantics (docs/engine/execution.md) constrain the
wiring: a re-entered node reads its static parent's last written output,
while a dispatch-only target reads the sender's fresh output. Feedback
therefore only survives loops whose re-entry targets are dispatch-only:

    START -> gate --pass--> worker (dispatch-only) --static--> next gate
                  --fail--> fixer (dispatch-only, reads gate output)
                               --cond--> worker (re-entry, refined cache)

Every mid-chain worker here is dispatch-only for exactly that reason; each
composite's internal clarify loop runs one fixer round and then proceeds
(re-gating the clarified result happens only where a counter can survive the
loop, i.e. through dispatch-only re-entry).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from ngen_weave.agent.node import AgentNode
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolSpec
from ngen_weave.engine.runner import parse_boolean, parse_output, render_prompt
from ngen_weave.errors import AgentReplyError, ConfigError
from ngen_weave.schema_errors import format_validation_error
from ngen_weave.workflow import (
    END,
    START,
    GraphBuilder,
    RunContext,
    Workflow,
    workflow_class_path,
)
from pydantic import BaseModel, ValidationError

from .cache import Cache, GateVerdict, carry_forward
from .tools import BASH, LIST_DIR, READ_FILE, WRITE_FILE

# --- reply schemas: strict per-node produced fields --------------------------


class ClarifiedInstruction(BaseModel):
    """Scout fixer reply: rewritten instruction plus what was clarified."""

    instruction: str
    clarifications: str = ""


class StepClarified(BaseModel):
    """Step fixer reply: rewritten plan step plus what was clarified."""

    step: str
    clarifications: str = ""


class FileSummary(BaseModel):
    """Scout agent reply: which files and snippets answer the question."""

    file_summary: str


class DevInstructionsReply(BaseModel):
    """Dev fixer reply: concrete implementation instructions."""

    dev_instructions: str


class PlanReply(BaseModel):
    """Plan worker reply: the plan, plus implementation instructions if asked."""

    plan: str
    dev_instructions: str = ""


class ClarificationsReply(BaseModel):
    """Fixer reply that only adjusts the planning context."""

    clarifications: str


class DevChangesReply(BaseModel):
    """Dev agent reply: summary of the code changes it wrote."""

    diff: str


class ReviewReportReply(BaseModel):
    """Reviewer reply: BLOCKING/MAJOR/MINOR report on the diff."""

    review_report: str


class OracleAdviceReply(BaseModel):
    """Oracle reply: recommendations, plus an optionally adjusted objective."""

    oracle_notes: str
    instruction: str = ""


# --- carry node bases ---------------------------------------------------------


class CarryWorker(Workflow):
    """Model leaf: prompt -> provider call -> strict reply parse -> carry merge.

    The reply is parsed strictly against `parsed_type` (only the fields this
    node produces, required fields enforced) so a missing produced field fails
    loudly instead of silently defaulting, then merged onto the carried cache.
    """

    _defer_validation = True

    input_type = Cache
    output_type = Cache
    prompt: ClassVar[str]
    parsed_type: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_defer_validation"):
            return
        path = workflow_class_path(cls)
        if getattr(cls, "prompt", None) is None:
            raise ConfigError(f"{path}: CarryWorker requires a prompt ClassVar")
        parsed = getattr(cls, "parsed_type", None)
        if not (isinstance(parsed, type) and issubclass(parsed, BaseModel)):
            raise ConfigError(f"{path}: CarryWorker requires a parsed_type ClassVar")

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        text = render_prompt(self.prompt, input.model_dump(), ctx.node_path)
        completion = await ctx.provider.complete([{"role": "user", "content": text}])
        ctx.emit(
            "model_call",
            {
                "variant": None,
                "tokens_total": completion.tokens_total,
                "cost_usd": completion.cost_usd,
            },
        )
        produced = parse_output(self.parsed_type, completion.text, ctx.node_path)
        return carry_forward(input, produced)


class CarryGate(Workflow):
    """Control-style gate whose output carries the whole input cache forward.

    Override decide() for a programmatic verdict; otherwise `prompt` is
    rendered and the reply parsed as a boolean. The output is the carried
    cache plus `pass` and `fail_count` (incremented on fail), so dispatch-only
    re-entry reads the fresh refined context AND the surviving loop counter.
    """

    _defer_validation = True

    input_type = Cache
    output_type = Cache
    prompt: ClassVar[str | None] = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_defer_validation"):
            return
        if cls.decide is not CarryGate.decide:
            return
        if getattr(cls, "prompt", None) is None:
            raise ConfigError(
                f"{workflow_class_path(cls)}: model-mode CarryGate requires a prompt "
                "ClassVar or a decide() override"
            )

    def decide(self, input: BaseModel) -> bool:
        """Programmatic predicate; when not overridden the prompt is model-judged."""
        raise NotImplementedError

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        if type(self).decide is not CarryGate.decide:
            verdict = self.decide(input)
        else:
            text = render_prompt(self.prompt or "", input.model_dump(), ctx.node_path)
            completion = await ctx.provider.complete([{"role": "user", "content": text}])
            ctx.emit(
                "model_call",
                {
                    "variant": None,
                    "tokens_total": completion.tokens_total,
                    "cost_usd": completion.cost_usd,
                },
            )
            verdict = parse_boolean(completion.text, ctx.node_path)
        produced = GateVerdict(
            **{
                "pass": verdict,
                "fail_count": input.fail_count + (0 if verdict else 1),
            }
        )
        return carry_forward(input, produced)


class CarryAgent(AgentNode):
    """Gated tool-use agent whose final answer merges onto the carried cache.

    The whole input cache rides to the model as the user turn (AgentNode
    serializes the input dump), so the agent sees all prior context. The
    harness validates the final {"output": {...}} against output_type (Cache,
    all-optional), so run() re-validates the fields the agent actually set
    against `parsed_type` — a missing required produced field is a loud,
    retryable AgentReplyError instead of a silent default.
    """

    _defer_validation = True

    input_type = Cache
    output_type = Cache
    parsed_type: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_defer_validation"):
            return
        parsed = getattr(cls, "parsed_type", None)
        if not (isinstance(parsed, type) and issubclass(parsed, BaseModel)):
            raise ConfigError(
                f"{workflow_class_path(cls)}: CarryAgent requires a parsed_type ClassVar"
            )

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        produced = await super().run(input, ctx)
        echoed = {key: getattr(produced, key) for key in produced.model_fields_set}
        try:
            strict = self.parsed_type.model_validate(echoed)
        except ValidationError as exc:
            raise AgentReplyError(
                f"{ctx.node_path}: agent output does not match "
                f"{format_validation_error(self.parsed_type, exc)}"
            ) from None
        return carry_forward(input, strict)


# --- gates --------------------------------------------------------------------


class SpecGate(CarryGate):
    """Scout entry gate: is the instruction a clear, targeted-search question?"""

    description = "Gate on the instruction being specific enough to search."
    prompt = (
        "You are the scout's spec gate. Decide whether the instruction below "
        "states a clear, specific question or objective that a targeted "
        "codebase search can answer without guessing.\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Instruction:\n{instruction}\n"
    )


class DevSpecGate(CarryGate):
    """Dev entry gate: are the implementation instructions concrete enough?"""

    description = "Gate on dev instructions naming abstractions and target files."
    prompt = (
        "You are the dev gate. Decide whether the instructions below are clear "
        "enough to implement: they should name the abstractions to build and "
        "the files that should contain them, or state them derivably.\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Instruction:\n{instruction}\n\n"
        "Dev instructions:\n{dev_instructions}\n"
    )


class ReviewValidityGate(CarryGate):
    """Reviewer entry gate: programmatic check for a step and a diff."""

    description = "Gate on the review input containing a step and a diff."

    def decide(self, input: BaseModel) -> bool:
        return bool(input.step.strip() and input.diff.strip())


class PlanGate(CarryGate):
    """Plan gate: discrete steps, evidence-backed, single-context-window each."""

    description = "Gate on the plan consisting of discrete achievable steps."
    prompt = (
        "You are the plan gate. Decide whether the plan below consists of "
        "discrete steps, each reliably achievable within a single context "
        "window, supported by arguments or evidence (citations of files or "
        "snippets).\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Plan:\n{plan}\n"
    )


class StepGate(CarryGate):
    """ImplementPlanStep entry gate: is the plan step specific enough?"""

    description = "Gate on the plan step being concrete enough to implement."
    prompt = (
        "You are the implementation gate. Decide whether the plan step below "
        "is specific enough to implement: it should state a clear deliverable "
        "and name concrete files or abstractions where determinable.\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Plan step:\n{step}\n"
    )


class PlanDetailGate(CarryGate):
    """Gate on plan detail: concrete abstractions/files, honest unknowns."""

    description = "Gate on the plan naming abstractions/files and honest unknowns."
    prompt = (
        "You are the plan detail gate. Decide whether the implementation plan "
        "below BOTH names concrete abstractions and their target files AND is "
        "honest about unknowns (it must explicitly list open questions wherever "
        "information is missing rather than papering over them).\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Plan:\n{plan}\n"
    )


class ScoutCheckGate(CarryGate):
    """Gate on the scout having answered the plan's open questions."""

    description = "Gate on the scout summary answering the plan's questions."
    prompt = (
        "You are the scout check gate. Decide whether the scout summary below "
        "answers every open question the implementation plan raises.\n\n"
        "Reply with exactly one word: true or false.\n\n"
        "Plan:\n{plan}\n\n"
        "Scout summary:\n{file_summary}\n"
    )


# --- workers and agents --------------------------------------------------------


class Clarify(CarryWorker):
    """Scout fixer: rewrite the instruction into a specific, searchable objective."""

    description = "Rewrite a vague instruction into a specific objective."
    parsed_type = ClarifiedInstruction
    prompt = (
        "The spec gate judged the instruction below not specific enough for a "
        "targeted search. Rewrite it into a clear, specific objective, keeping "
        "every original constraint, and state what you clarified.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"instruction": "<rewritten objective>", "clarifications": "<what you clarified>"}}\n\n'
        "Instruction:\n{instruction}\n\n"
        "Clarifications so far:\n{clarifications}\n"
    )


class DevClarify(CarryWorker):
    """Dev fixer: produce concrete implementation instructions."""

    description = "Produce concrete abstractions and target files for the dev."
    parsed_type = DevInstructionsReply
    prompt = (
        "The dev gate judged the instructions below too vague to implement. "
        "Produce concrete development instructions: name the abstractions to "
        "build and the files that should contain them.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"dev_instructions": "<abstractions and files>"}}\n\n'
        "Instruction:\n{instruction}\n\n"
        "Current dev instructions:\n{dev_instructions}\n"
    )


class PlanDraft(CarryWorker):
    """PlanSWETask planner: discrete, evidence-backed, window-sized steps."""

    description = "Draft a plan of discrete, evidence-backed steps."
    parsed_type = PlanReply
    prompt = (
        "Draft a plan for the objective below. The plan must consist of "
        "discrete steps, each reliably achievable within a single context "
        "window, supported by arguments or evidence (cite files or snippets "
        "from the scout summary). Consult the clarifications if present.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"plan": "<the plan>", "dev_instructions": "<optional implementation notes>"}}\n\n'
        "Objective:\n{instruction}\n\n"
        "Scout summary:\n{file_summary}\n\n"
        "Clarifications:\n{clarifications}\n"
    )


class PlanRework(CarryWorker):
    """Plan loop fixer: restate the objective scope after a plan-gate failure."""

    description = "Adjust the planning context after a plan-gate failure."
    parsed_type = ClarificationsReply
    prompt = (
        "The plan gate rejected the plan below: it does not yet consist of "
        "discrete steps reliably achievable within a single context window. "
        "Restate the objective scope and the adjustments the planner should "
        "make (trim ambition, split steps, note what needs re-scouting).\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"clarifications": "<adjustments for the planner>"}}\n\n'
        "Objective:\n{instruction}\n\n"
        "Rejected plan:\n{plan}\n\n"
        "Prior clarifications:\n{clarifications}\n"
    )


class StepClarify(CarryWorker):
    """ImplementPlanStep entry fixer: sharpen the plan step itself."""

    description = "Rewrite a vague plan step into a concrete deliverable."
    parsed_type = StepClarified
    prompt = (
        "The implementation gate judged the plan step below not specific "
        "enough. Rewrite it into a concrete step with a clear deliverable, "
        "keeping every original constraint, and state what you clarified.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"step": "<rewritten step>", "clarifications": "<what you clarified>"}}\n\n'
        "Plan step:\n{step}\n\n"
        "Clarifications so far:\n{clarifications}\n"
    )


class StepPlan(CarryWorker):
    """ImplementPlanStep planner: abstractions, target files, honest open questions.

    Runs BEFORE the scout, so it drafts purely from the plan step and must
    explicitly list what the scout must go find out.
    """

    description = "Plan one step: abstractions, target files, open questions."
    parsed_type = PlanReply
    prompt = (
        "Write the implementation plan for the plan step below: the "
        "abstractions to build, the files that should contain them, and the "
        "open questions the scout must answer. Be honest about unknowns: list "
        "explicitly everything you do not know yet. Consult the clarifications "
        "if present.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"plan": "<the implementation plan>", '
        '"dev_instructions": "<what the dev agent should implement>"}}\n\n'
        "Plan step:\n{step}\n\n"
        "Clarifications:\n{clarifications}\n"
    )


class PlanRefine(CarryWorker):
    """Plan loop fixer: sharpen detail/honesty after a detail-gate failure."""

    description = "Restate what the planner must fix after a detail-gate failure."
    parsed_type = ClarificationsReply
    prompt = (
        "The plan detail gate rejected the implementation plan below: it must "
        "name concrete abstractions and target files, and be honest about "
        "unknowns. Restate precisely what the planner must fix.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"clarifications": "<adjustments for the planner>"}}\n\n'
        "Plan step:\n{step}\n\n"
        "Rejected plan:\n{plan}\n\n"
        "Prior clarifications:\n{clarifications}\n"
    )


class ScoutClarify(CarryWorker):
    """Scout loop fixer: restate the plan's unanswered questions."""

    description = "Restate unanswered plan questions for the planner."
    parsed_type = ClarificationsReply
    prompt = (
        "The scout check gate found the scout summary below does not answer "
        "every open question the implementation plan raises. Restate the "
        "unanswered questions and how the planner should scope around them.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"clarifications": "<adjustments for the planner>"}}\n\n'
        "Plan:\n{plan}\n\n"
        "Scout summary:\n{file_summary}\n\n"
        "Prior clarifications:\n{clarifications}\n"
    )


class ReviewerWorker(CarryWorker):
    """Reviews the diff against the instruction item; BLOCKING/MAJOR/MINOR."""

    description = "Verify the plan step was implemented in the diff."
    parsed_type = ReviewReportReply
    prompt = (
        "Verify that the instruction item below has been implemented in the "
        "diff. Report severity: BLOCKING (not implemented or breaks existing "
        "behavior), MAJOR (partially implemented), or MINOR (implemented with "
        "nits). Discuss what is missing.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"review_report": "<severity plus discussion>"}}\n\n'
        "Instruction item:\n{step}\n\n"
        "Diff:\n{diff}\n"
    )


class Oracle(CarryWorker):
    """Given the whole context after repeated gate failures, recommends next moves.

    The engine's 0.2.x observers are pause-only ("reroute stays post-1.0"), so
    an in-graph reroute on too many failures must be driven by a gate's
    fail_count and router instead; Oracle is that reroute target. To run it
    standalone, move this class into workflows.py (which registers it) and
    point ngw.yaml at it.
    """

    description = "Recommends plan/instruction/tool adjustments after repeated failures."
    parsed_type = OracleAdviceReply
    prompt = (
        "You are the oracle. The workflow below failed its gate "
        "{fail_count} times. Given the whole context, recommend adjustments "
        "to either the plan, the instructions, or the tool calls that should "
        "follow.\n\n"
        "Reply with exactly one JSON object and nothing else, no markdown fences:\n"
        '{{"oracle_notes": "<recommendations>", '
        '"instruction": "<adjusted objective, or the original if fine>"}}\n\n'
        "Objective:\n{instruction}\n\n"
        "Scout summary:\n{file_summary}\n\n"
        "Plan:\n{plan}\n\n"
        "Clarifications:\n{clarifications}\n"
    )


class ScoutAgent(CarryAgent):
    """Finds files and snippets answering the instruction; read tools + bash."""

    description = "Search the repo for files and snippets answering the objective."
    parsed_type = FileSummary
    tools: ClassVar[Sequence[ToolSpec]] = (LIST_DIR, READ_FILE, BASH)
    permissions = PermissionSet(allowed_tools=(LIST_DIR.name, READ_FILE.name, BASH.name))


class DevAgent(CarryAgent):
    """Writes the code per the dev instructions; reads and writes files."""

    description = "Implement the dev instructions against the repo files."
    parsed_type = DevChangesReply
    tools: ClassVar[Sequence[ToolSpec]] = (READ_FILE, WRITE_FILE)
    permissions = PermissionSet(allowed_tools=(READ_FILE.name, WRITE_FILE.name))


# --- mid-level composites (hidden, reusable) -----------------------------------


def _proceed(state: dict) -> str:
    """Constant router: dispatch the fixer's output to the pass target."""
    return "proceed"


def _scout_router(state: dict) -> str:
    gate = state[workflow_class_path(SpecGate)]
    return "pass" if gate["pass"] else "fail"


class Scout(Workflow):
    """Composite: spec gate -> (one clarify round if vague) -> scouted summary.

    The clarify loop runs one round: gate fail dispatches Clarify, which
    dispatches the scout agent with the refined instruction. The agent is
    dispatch-only so both entries read the sender's fresh cache.
    """

    description = "Scout: gate specificity, clarify, then find files and snippets."
    human_description = (
        "Checks that the instruction is specific enough for a targeted search, "
        "clarifies it if not, then lists and reads candidate files and produces "
        "a summary of which files and snippets answer the question."
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
        g.add_conditional_edges(spec_gate, _scout_router, {"pass": scout_agent, "fail": clarify})
        g.add_conditional_edges(clarify, _proceed, {"proceed": scout_agent})
        g.add_edge(scout_agent, END)


def _dev_router(state: dict) -> str:
    gate = state[workflow_class_path(DevSpecGate)]
    return "pass" if gate["pass"] else "fail"


class Dev(Workflow):
    """Composite: dev gate -> (one clarify round if vague) -> write the code."""

    description = "Dev: gate instruction clarity, then write the code."
    human_description = (
        "Checks that the implementation instructions are clear enough (the "
        "abstractions required and the files that should contain them), "
        "clarifies them if not, then writes the code with a gated agent."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        dev_gate = DevSpecGate()
        dev_clarify = DevClarify()
        dev_agent = DevAgent()
        for node in (dev_gate, dev_clarify, dev_agent):
            g.add_node(node)
        g.add_edge(START, dev_gate)
        g.add_conditional_edges(dev_gate, _dev_router, {"pass": dev_agent, "fail": dev_clarify})
        g.add_conditional_edges(dev_clarify, _proceed, {"proceed": dev_agent})
        g.add_edge(dev_agent, END)


def _validity_router(state: dict) -> str:
    gate = state[workflow_class_path(ReviewValidityGate)]
    return "pass" if gate["pass"] else "fail"


class Reviewer(Workflow):
    """Composite: validity gate -> verify the step against the diff.

    The validity gate is programmatic (step and diff must both be present);
    an invalid input completes the run with an empty report rather than
    sending garbage to the model.
    """

    description = "Reviewer: gate input validity, then verify the step against the diff."
    human_description = (
        "Given a single instruction item and a diff of the codebase, verifies "
        "the item has been implemented and returns a BLOCKING/MAJOR/MINOR "
        "report discussing what is missing."
    )
    input_type = Cache
    output_type = Cache

    def build(self, g: GraphBuilder) -> None:
        validity = ReviewValidityGate()
        reviewer = ReviewerWorker()
        for node in (validity, reviewer):
            g.add_node(node)
        g.add_edge(START, validity)
        g.add_conditional_edges(validity, _validity_router, {"pass": reviewer, "fail": END})
        g.add_edge(reviewer, END)
