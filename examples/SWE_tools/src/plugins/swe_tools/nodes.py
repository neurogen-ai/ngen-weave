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
- CarryAgent drives AgentNode's tool loop, then merges the final answer onto
  the cache. ScoutAgent, DevAgent, and ReviewerWorker run that loop as a pi
  RPC session via PiRpcAgentExecutor (pi's own tools, gate bypassed); the
  gated file-system toolset in tools.py is the harness-mode fallback.

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

import json
import os
import re
from typing import ClassVar

from ngen_weave.agent.node import AgentNode
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.pi_rpc import PiRpcAgentExecutor
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

# One activation = one headless pi subprocess driving pi's own agent loop
# (bash/read/grep tools, retries, compaction) instead of the gated
# text-only loop over CompletionProvider. SAFETY (see ngen_weave.agent.pi_rpc):
# pi bypasses the PermissionGate entirely, so these activations are trusted
# with pi's full tool set — the example targets a scratch checkout via
# SWE_TOOLS_REPO_ROOT, which is also where the subprocess runs. The factory
# form resolves that env var lazily at activation time, matching tools.py.


def _pi_rpc_executor(provider=None):
    return PiRpcAgentExecutor(
        provider,
        binary="pi",
        cwd=os.environ.get("SWE_TOOLS_REPO_ROOT"),
    )

# --- JSON prompt helper --------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SENTINEL = "\x00"
_UNSENTINEL = re.compile(_SENTINEL + r"(\w+)" + _SENTINEL)


def _json_prompt(spec: dict) -> str:
    """Serialize a JSON prompt spec into a render_prompt template.

    The spec IS the prompt: a JSON object with a role, task, response_type,
    response_format, rules, and input fields. Serializing with json.dumps
    keeps the spec canonical JSON for the model; braces are then doubled so
    str.format_map (inside render_prompt) treats them as literals, while
    {placeholder} fields on the carried cache are kept single-braced and
    interpolated at run time. Interpolated values may contain raw newlines
    or quotes; models read past that, and the spec half stays parseable.
    """

    text = _PLACEHOLDER.sub(
        lambda m: f"{_SENTINEL}{m.group(1)}{_SENTINEL}",
        json.dumps(spec, indent=2),
    )
    text = text.replace("{", "{{").replace("}", "}}")
    return _UNSENTINEL.sub(lambda m: "{" + m.group(1) + "}", text)


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

    `agent_task` is a JSON task spec (role, task, response_type, rules,
    required output fields) injected as an extra cache field before the run,
    so it rides inside the serialized user turn on top of the carried
    context. carry_forward still merges onto the ORIGINAL input, so the
    injected spec never travels downstream.
    """

    _defer_validation = True

    input_type = Cache
    output_type = Cache
    parsed_type: ClassVar[type[BaseModel]]
    agent_task: ClassVar[dict]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_defer_validation"):
            return
        parsed = getattr(cls, "parsed_type", None)
        if not (isinstance(parsed, type) and issubclass(parsed, BaseModel)):
            raise ConfigError(
                f"{workflow_class_path(cls)}: CarryAgent requires a parsed_type ClassVar"
            )
        task = getattr(cls, "agent_task", None)
        if not (isinstance(task, dict) and task):
            raise ConfigError(
                f"{workflow_class_path(cls)}: CarryAgent requires a non-empty "
                "agent_task dict ClassVar"
            )

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        enriched = input.model_copy(update={"agent_task": self.agent_task})
        produced = await super().run(enriched, ctx)
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
        "codebase search can answer without guessing. It is specific enough "
        "when it names concrete symbols, files, paths, or a checkable "
        "deliverable, or states them derivably. Vague ambition ('make it "
        "better', 'look at the code') is not specific.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Instruction:\n{instruction}\n"
    )


class DevSpecGate(CarryGate):
    """Dev entry gate: are the implementation instructions concrete enough?"""

    description = "Gate on dev instructions naming abstractions and target files."
    prompt = (
        "You are the dev gate. Decide whether the instructions below are clear "
        "enough to implement without weighing trade-offs: they should name the "
        "abstractions to build, the files that should contain them, and what "
        "is out of scope, or state these derivably. The instructions are a "
        "boundary, not a line-by-line edit plan; judgement-free execution is "
        "the bar. 'Improve the module' fails.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Instruction:\n{instruction}\n\n"
        "Dev instructions:\n{dev_instructions}\n"
    )


class ReviewValidityGate(CarryGate):
    """Reviewer entry gate: programmatic check for a step and a diff."""

    description = "Gate on the review input containing a step and a diff."

    def decide(self, input: BaseModel) -> bool:
        return bool(input.step.strip() and input.diff.strip())


class PlanGate(CarryGate):
    """Plan gate: is this plan sufficient to implement the instruction?"""

    description = "Gate on the plan achieving the instruction with no gaps."
    prompt = (
        "You are the plan gate. Ask the questions that decide the "
        "instruction's completion, then judge: is this plan sufficient to "
        "implement them? It passes only when it achieves the instruction, "
        "leaves no knowledge gaps, and every step is concrete and points the "
        "implementer in a direction: a deliverable, the approach or files or "
        "symbols to use, and how to verify it. A step that says 'explore', "
        "'investigate', or defers to a scouting pass fails. A step that "
        "needs a judgement call the plan has not pre-made is not discrete.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Instruction:\n{instruction}\n\n"
        "Plan:\n{plan}\n"
    )


class StepGate(CarryGate):
    """ImplementPlanStep entry gate: one plan part, specific enough to implement."""

    description = "Gate on exactly one plan part, specific enough to implement."
    prompt = (
        "You are the implementation gate. Ask: are my instructions specific "
        "enough to implement, and was exactly one part of the agent's overall "
        "plan fed into this workflow? The instructions below pass only when "
        "they are a single plan part that states a clear deliverable and "
        "names concrete files or abstractions where determinable. A bundle "
        "of several plan parts, or the whole plan, is not one part and fails. "
        "Instructions that ask the implementer to weigh trade-offs are also "
        "not specific enough.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Instructions:\n{step}\n"
    )


class PlanDetailGate(CarryGate):
    """Gate on plan detail: concrete abstractions/files, honest unknowns."""

    description = "Gate on the plan naming abstractions/files and honest unknowns."
    prompt = (
        "You are the plan detail gate. Decide whether the implementation plan "
        "below BOTH names concrete abstractions and their target files AND is "
        "honest about unknowns: it must explicitly list open questions "
        "wherever information is missing rather than papering over them. A "
        "plan that speculates instead of listing what it does not know fails. "
        "The plan is a boundary, not an edit plan: function bodies and "
        "per-line edit instructions are not required.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Plan:\n{plan}\n"
    )


class ScoutCheckGate(CarryGate):
    """Gate on the plan->scout loop: plan plus scout evidence suffices."""

    description = "Gate on the plan citing enough scout evidence to implement."
    prompt = (
        "You are the scout check gate. Decide whether the plan->scout loop "
        "may exit: only when the implementation plan, read together with the "
        "scout summary, can be implemented well, step by step, to achieve "
        "the meta instruction below. Every part of the plan must cite "
        "concrete evidence from the scout summary: exact file paths, "
        "symbols, or line ranges that tell the implementer what to change "
        "and how. If any part still rests on 'likely' or would send the "
        "implementer scouting, fail.\n\n"
        "Reply with exactly one word: the bare boolean true or false. Do not "
        "wrap it in JSON, quotes, or any other text; the verdict is read as a "
        "boolean by downstream logic.\n\n"
        "Meta instruction:\n{instruction}\n\n"
        "Plan:\n{plan}\n\n"
        "Scout summary:\n{file_summary}\n"
    )


# --- workers and agents --------------------------------------------------------


class Clarify(CarryWorker):
    """Scout fixer: rewrite the instruction into a specific, searchable objective."""

    description = "Rewrite a vague instruction into a specific objective."
    parsed_type = ClarifiedInstruction
    prompt = _json_prompt(
        {
            "role": "scout spec fixer",
            "task": (
                "The instruction failed the spec gate: it does not state a "
                "clear, targeted-search question. Rewrite it into one specific "
                "objective. Name concrete symbols, files, paths, or a "
                "checkable deliverable; keep every original constraint. Do not "
                "invent facts the instruction does not give: if information is "
                "missing, name it in clarifications instead of filling it in."
            ),
            "response_type": "json",
            "response_format": {
                "instruction": "the rewritten objective",
                "clarifications": (
                    "what you changed, and any information still missing"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                "A rewrite that drops a constraint or invents a fact is a failure.",
            ],
            "input": {
                "instruction": "{instruction}",
                "clarifications_so_far": "{clarifications}",
            },
        }
    )


class DevClarify(CarryWorker):
    """Dev fixer: produce concrete implementation instructions."""

    description = "Produce concrete abstractions and target files for the dev."
    parsed_type = DevInstructionsReply
    prompt = _json_prompt(
        {
            "role": "dev instruction fixer",
            "task": (
                "The dev instructions failed the dev gate: they are too vague "
                "to implement without weighing trade-offs. Rewrite them so "
                "they name the abstractions to build, the files that should "
                "contain them, and what is out of scope. The instructions are "
                "a boundary, not an edit plan: no function bodies, no "
                "per-line steps."
            ),
            "response_type": "json",
            "response_format": {
                "dev_instructions": (
                    "abstractions, their target files, and exclusions"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "Ground every abstraction in what already exists; cite "
                    "files or symbols where determinable."
                ),
            ],
            "input": {
                "instruction": "{instruction}",
                "current_dev_instructions": "{dev_instructions}",
            },
        }
    )


class PlanDraft(CarryWorker):
    """PlanSWETask planner: concrete, directive, single-window steps."""

    description = "Draft a plan of discrete, concrete, directive steps."
    parsed_type = PlanReply
    prompt = _json_prompt(
        {
            "role": "planner",
            "task": (
                "Draft a plan for the objective below. Each step stands "
                "alone, states a concrete deliverable and how to verify it, "
                "and fits one context window. Every step points the "
                "implementer in a direction: name the approach, the files or "
                "symbols to touch, and what done looks like, so no step ends "
                "with 'go find out'. The plan achieves the instruction, not a "
                "loose reading of it, and leaves no knowledge gaps: every "
                "ambiguity is resolved by a decision in the plan, never "
                "deferred to a scouting pass. Consult the clarifications if "
                "present."
            ),
            "response_type": "json",
            "response_format": {
                "plan": "the numbered plan",
                "dev_instructions": (
                    "optional implementation notes for the dev agent"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "The plan is a boundary, not an edit plan: no function "
                    "bodies, no per-file edit instructions."
                ),
                (
                    "No open questions and no explore steps: if information "
                    "is missing, make the call from what the instruction and "
                    "clarifications give."
                ),
            ],
            "input": {
                "objective": "{instruction}",
                "clarifications": "{clarifications}",
            },
        }
    )


class PlanRework(CarryWorker):
    """Plan fixer: grill the agent who called the planner about the gaps.

    Its output does not loop back into the planner: it ends the run, so the
    agent who invoked PlanSWETask reads the questions, provides more detail,
    does more scouting where it lacks knowledge, and re-invokes with its
    answers as clarifications.
    """

    description = "Grill the caller about where the plan leaves gaps."
    parsed_type = ClarificationsReply
    prompt = _json_prompt(
        {
            "role": "plan rework grill",
            "task": (
                "The plan gate rejected the plan below: it does not achieve "
                "the instruction, or it leaves knowledge gaps. Your reply "
                "goes straight back to the agent who called this planner. "
                "Poke them about where the gaps are: one pointed question per "
                "gap, numbered, each with your recommended answer. Ask only "
                "questions relevant to completing the instruction, and "
                "address the question that decides everything: is this plan "
                "sufficient to implement it? Tell them to provide more detail "
                "and to do more scouting first if they do not know the "
                "answers."
            ),
            "response_type": "json",
            "response_format": {
                "clarifications": (
                    "numbered questions to the caller, each with a "
                    "recommended answer, plus the scouting they owe"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "Name each gap concretely: which step, what is missing, "
                    "what you need to know."
                ),
                (
                    "Do not accept vague answers; a gap the caller could "
                    "close by scouting their own repo is theirs to scout, "
                    "not yours to guess."
                ),
            ],
            "input": {
                "objective": "{instruction}",
                "rejected_plan": "{plan}",
                "answers_already_given": "{clarifications}",
            },
        }
    )


class StepClarify(CarryWorker):
    """ImplementPlanStep entry fixer: sharpen the plan step itself."""

    description = "Rewrite a vague plan step into a concrete deliverable."
    parsed_type = StepClarified
    prompt = _json_prompt(
        {
            "role": "step spec fixer",
            "task": (
                "The plan step failed the implementation gate: it is not "
                "specific enough. Rewrite it into one concrete step with a "
                "clear deliverable and, where determinable, the files or "
                "abstractions it touches. Keep every original constraint; do "
                "not invent facts."
            ),
            "response_type": "json",
            "response_format": {
                "step": "the rewritten step",
                "clarifications": (
                    "what you changed, and any information still missing"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                "A rewrite that drops a constraint is a failure.",
                (
                    "If the instructions bundle several plan parts, reduce "
                    "them to the single part this workflow should implement "
                    "and list the remaining parts in clarifications so they "
                    "can be fed back separately."
                ),
            ],
            "input": {
                "plan_step": "{step}",
                "clarifications_so_far": "{clarifications}",
            },
        }
    )


class StepPlan(CarryWorker):
    """ImplementPlanStep planner: abstractions, target files, honest open questions.

    Runs BEFORE the scout, so it drafts purely from the plan step and must
    explicitly list what the scout must go find out.
    """

    description = "Plan one step: abstractions, target files, open questions."
    parsed_type = PlanReply
    prompt = _json_prompt(
        {
            "role": "step planner",
            "task": (
                "Write the implementation plan for the plan step below: the "
                "abstractions to build, the files that should contain them, "
                "and the open questions the scout must answer. This plan runs "
                "BEFORE the scout, so it is drafted from the step alone: be "
                "honest about unknowns and list explicitly everything you do "
                "not know yet instead of guessing. Consult the clarifications "
                "if present."
            ),
            "response_type": "json",
            "response_format": {
                "plan": (
                    "the implementation plan, with an explicit open-questions "
                    "section"
                ),
                "dev_instructions": "what the dev agent should implement",
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "Every unverifiable claim goes in open questions, not "
                    "stated as fact."
                ),
                (
                    "The plan is a boundary, not an edit plan: contracts and "
                    "file targets, no function bodies."
                ),
            ],
            "input": {
                "plan_step": "{step}",
                "clarifications": "{clarifications}",
            },
        }
    )


class PlanRefine(CarryWorker):
    """Plan loop fixer: sharpen detail/honesty after a detail-gate failure."""

    description = "Restate what the planner must fix after a detail-gate failure."
    parsed_type = ClarificationsReply
    prompt = _json_prompt(
        {
            "role": "plan refine fixer",
            "task": (
                "The plan detail gate rejected the implementation plan below. "
                "The gate passes a plan only when it names concrete "
                "abstractions and their target files AND explicitly lists open "
                "questions wherever information is missing. Restate precisely "
                "what the planner must fix, naming the sections that fail the "
                "bar."
            ),
            "response_type": "json",
            "response_format": {
                "clarifications": "adjustments for the planner",
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "Name each gap concretely: which abstraction or file is "
                    "missing, which unknown is papered over."
                ),
            ],
            "input": {
                "plan_step": "{step}",
                "rejected_plan": "{plan}",
                "prior_clarifications": "{clarifications}",
            },
        }
    )


class ScoutClarify(CarryWorker):
    """Scout loop fixer: name the plan parts that lack scout evidence."""

    description = "Restate which plan parts lack scout evidence for the planner."
    parsed_type = ClarificationsReply
    prompt = _json_prompt(
        {
            "role": "scout check fixer",
            "task": (
                "The scout check gate kept the plan->scout loop open: the "
                "implementation plan below does not yet cite enough concrete "
                "evidence from the scout summary to be implemented well, step "
                "by step, toward the meta instruction. Restate which parts of "
                "the plan lack evidence and how the planner should rescope or "
                "re-target the scouting. Evidence means an exact file path, "
                "symbol, or line range."
            ),
            "response_type": "json",
            "response_format": {
                "clarifications": "adjustments for the planner",
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                "List only the parts lacking evidence; do not relitigate the rest.",
            ],
            "input": {
                "meta_instruction": "{instruction}",
                "plan": "{plan}",
                "scout_summary": "{file_summary}",
                "prior_clarifications": "{clarifications}",
            },
        }
    )


class ReviewerWorker(CarryAgent):
    """Reviews the diff against the instruction item; BLOCKING/MAJOR/MINOR.

    Runs as a pi RPC session, so the reviewer inspects the actual repo with
    read/grep instead of judging the diff summary alone.
    """

    description = "Verify the plan step was implemented in the diff."
    parsed_type = ReviewReportReply
    permissions = PermissionSet(allowed_tools=("read_file", "grep"))
    executor = _pi_rpc_executor
    agent_task = {
        "role": "reviewer",
        "task": (
            "Verify that the instruction item (step) below has been "
            "implemented in the diff. The carried diff is a summary: use "
            "read/grep to inspect the actual repo files and confirm each "
            "claim before reporting it. Report only what the code justifies: "
            "no invented issues, no severity inflation. Cite exact file "
            "paths and line ranges as evidence for every finding."
        ),
        "response_type": "json",
        "rules": [
            (
                "Inspection only: do not modify, create, or delete files "
                "during review."
            ),
            "Report only issues caused or made reachable by this diff.",
            (
                "If everything looks good, say so plainly: "
                "'No issues found.'"
            ),
            (
                "When done, set the review_report field in your final output "
                "object; a report missing it is a failed run."
            ),
        ],
        "review_report_format": (
            "First line: 'Verdict: BLOCKING', 'Verdict: MAJOR', "
            "'Verdict: MINOR', or 'Verdict: OK'. Then one entry per "
            "finding: severity, location, evidence, and the smallest "
            "fix. Severities: BLOCKING means the item is not "
            "implemented or the diff breaks existing behavior; MAJOR "
            "means partially implemented; MINOR means implemented "
            "with nits. If nothing qualifies, the report is exactly: "
            "No issues found."
        ),
    }


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
    prompt = _json_prompt(
        {
            "role": "oracle",
            "task": (
                "The workflow below failed its gate {fail_count} times. "
                "Diagnose why: name the gate criterion that keeps going unmet "
                "and the part of the context that keeps missing it. Then "
                "recommend the smallest set of adjustments to the plan, the "
                "instructions, or the tool approach that should follow. Do not "
                "grow scope: a scope problem is resolved by cutting or "
                "re-scouting, not by retrying harder."
            ),
            "response_type": "json",
            "response_format": {
                "oracle_notes": "diagnosis first, then recommended adjustments",
                "instruction": "the adjusted objective, or the original if fine",
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "Do not recommend the same approach that already failed; "
                    "if retries cannot fix it, say so."
                ),
            ],
            "input": {
                "objective": "{instruction}",
                "scout_summary": "{file_summary}",
                "plan": "{plan}",
                "clarifications": "{clarifications}",
            },
        }
    )


class ScoutAgent(CarryAgent):
    """Finds files and snippets answering the instruction; runs as a pi RPC session."""

    description = "Search the repo for files and snippets answering the objective."
    parsed_type = FileSummary
    # pi supplies its own tools (read/grep/find/bash); the node's PermissionSet
    # only documents intent and protects a harness swap-back (pi bypasses it).
    permissions = PermissionSet(allowed_tools=("list_dir", "read_file", "bash"))
    executor = _pi_rpc_executor
    agent_task = {
        "role": "scout",
        "task": (
            "Search the repo for the files and snippets that answer the "
            "objective in this context. Move fast, but do not guess: prefer "
            "targeted search (grep with specific symbols or paths) and "
            "selective reading over broad sweeps. Start from paths and "
            "symbols the context already names; reserve unscoped grep for "
            "exhaustive verification."
        ),
        "response_type": "json",
        "rules": [
            "Cite exact file paths and line ranges; never cite from memory.",
            (
                "Return the minimum context another agent needs to act: "
                "relevant entry points, key types, data flow, files likely "
                "needing changes, constraints and open questions."
            ),
            (
                "Inspection only: do not modify, create, or delete files "
                "while scouting."
            ),
            (
                "When done, set the file_summary field in your final output "
                "object; a summary missing it is a failed run."
            ),
        ],
        "file_summary_sections": [
            "Files: path (lines N-M) and why each matters",
            "Key code: the types, functions, and snippets that matter",
            "How it connects: the data flow in a few sentences",
            "Start here: the first file another agent should open, and why",
            "Open questions: anything the objective asked that you could not verify",
        ],
    }


class DevAgent(CarryAgent):
    """Writes the code per the dev instructions; runs as a pi RPC session."""

    description = "Implement the dev instructions against the repo files."
    parsed_type = DevChangesReply
    permissions = PermissionSet(allowed_tools=("read_file", "write_file"))
    executor = _pi_rpc_executor
    agent_task = {
        "role": "dev",
        "task": (
            "Implement the dev_instructions in this context against the repo "
            "files. Read the carried context first, validate it against the "
            "actual code, then make the smallest correct change. Follow the "
            "patterns already in the codebase; the instructions are the "
            "contract, but the code is the source of truth for what exists."
        ),
        "response_type": "json",
        "rules": [
            (
                "Do not add speculative scaffolding, TODOs, placeholders, or "
                "scope the instructions did not ask for."
            ),
            (
                "If implementation reveals a decision the instructions do not "
                "cover, take the narrowest safe reading and record it in the "
                "diff summary; do not silently redesign."
            ),
            (
                "You can run shell commands and checks: verify your change "
                "(imports, tests, whatever exists) before reporting, and "
                "reread the files you changed."
            ),
            (
                "When done, set the diff field in your final output object; a "
                "report with no diff and no explicit 'no edits made' is a "
                "failed run."
            ),
        ],
        "diff_sections": [
            "What changed: file by file, one line each",
            "Why it matches the instructions",
            "Validation you ran and what it showed",
            "Risks and anything you had to decide yourself",
        ],
    }


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
