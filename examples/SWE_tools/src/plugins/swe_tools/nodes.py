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

Gates are programmatic wherever the verdict is checkable without a model:
StepGate validates the fed step against the cached plan structure with a
Python predicate (zero LLM calls, the reason lands in gate_notes), PlanGate
checks the plan's structure programmatically before asking the model the one
question code cannot answer (per-step context-window sizing), and
ScoutCheckGate bounds the scout loop at SCOUT_ROUNDS_MAX rounds, after which
it passes with the open questions attached for the dev agent to resolve at
write-time. StepRecorder persists per-step progress into the cache's
step_status map after review, so the caller's loop over steps is mechanical.
"""

from __future__ import annotations

import json
import os
import re
from functools import partial
from typing import Any, ClassVar

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
#
# Each activation narrows pi's own tool surface via the -t allowlist:
# scouting and review are read-only work (bash/ls/grep/find), the dev worker
# additionally edits files (edit/write). There is no dedicated read tool in
# these sessions — the agents read through bash (cat/sed) instead.

_INSPECT_TOOLS = ("bash", "ls", "grep", "find")
_DEV_TOOLS = ("bash", "ls", "grep", "edit", "write")

# Max failed rounds of the StepPlan -> ScoutAgent -> ScoutCheckGate loop.
# Past this the scout gate passes with open questions attached for the dev
# agent to resolve at write-time; certainty at plan time is not worth more
# scout rounds.
SCOUT_ROUNDS_MAX = 2


def _pi_rpc_executor(provider=None, *, tools: tuple[str, ...]):
    return PiRpcAgentExecutor(
        provider,
        binary="pi",
        cwd=os.environ.get("SWE_TOOLS_REPO_ROOT"),
        extra_args=("-t", ",".join(tools)),
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


# --- plan-structure helpers ----------------------------------------------------

# First line of a review report: "Verdict: BLOCKING|MAJOR|MINOR|OK".
_VERDICT_RE = re.compile(r"Verdict:\s*(BLOCKING|MAJOR|MINOR|OK)", re.IGNORECASE)

# File paths in a dev diff summary; best effort, for the files_touched record.
_FILE_RE = re.compile(
    r"(?:[\w.\-]+/)*[\w.\-]+\.(?:py|js|ts|tsx|java|go|rs|c|h|cpp|hpp"
    r"|md|json|ya?ml|toml|txt|sh)\b"
)


def _plan_steps(value: Any) -> list[dict]:
    """Coerce the cache's `steps` field into a list of step dicts."""

    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, dict)]


def _has_dependency_cycle(ids: list[str], steps: list[dict]) -> bool:
    """Kahn's algorithm over depends_on; True when the graph has a cycle."""

    deps_of = {step_id: set() for step_id in ids}
    indegree = {step_id: 0 for step_id in ids}
    for step, step_id in zip(steps, ids):
        for dep in step.get("depends_on") or []:
            if dep in deps_of and dep not in deps_of[step_id]:
                deps_of[step_id].add(dep)
                indegree[step_id] += 1
    queue = [step_id for step_id, degree in indegree.items() if degree == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for step_id, deps in deps_of.items():
            if node in deps:
                deps.discard(node)
                indegree[step_id] -= 1
                if indegree[step_id] == 0:
                    queue.append(step_id)
    return seen != len(ids)


def _plan_structure_notes(steps: list[dict]) -> str:
    """Programmatic structural check of a plan step list; '' means it passes.

    Enforces: at least 2 steps (a one-giant-step plan fails), unique non-empty
    ids, a summary and a checkable completion_check on every step, and
    depends_on references that resolve into a DAG.
    """

    if len(steps) < 2:
        return (
            f"plan must have at least 2 steps; it has {len(steps)}. "
            "A one-giant-step plan fails."
        )
    notes: list[str] = []
    ids = [str(s.get("id", "")).strip() for s in steps]
    if any(not step_id for step_id in ids):
        notes.append("every step needs a non-empty id")
    if len(set(ids)) != len(ids):
        notes.append("step ids must be unique")
    if any(not str(s.get("summary", "")).strip() for s in steps):
        notes.append("every step needs a summary")
    if any(not str(s.get("completion_check", "")).strip() for s in steps):
        notes.append(
            "every step needs a completion_check a reviewer can grade later"
        )
    known = {step_id for step_id in ids if step_id}
    if any(
        dep not in known
        for s in steps
        for dep in (s.get("depends_on") or [])
    ):
        notes.append("depends_on entries must reference existing step ids")
    if not notes and _has_dependency_cycle(ids, steps):
        notes.append("depends_on must form a DAG; it has a cycle")
    return "; ".join(notes)


def _extract_open_questions(text: str) -> str:
    """Best-effort pull of the 'Open questions' section from a scout summary."""

    marker = "open questions"
    idx = text.lower().find(marker)
    if idx == -1:
        return ""
    return text[idx : idx + 600].strip()


# --- reply schemas: strict per-node produced fields --------------------------


class ClarifiedInstruction(BaseModel):
    """Scout fixer reply: rewritten instruction plus what was clarified."""

    instruction: str
    clarifications: str = ""


class FileSummary(BaseModel):
    """Scout agent reply: which files and snippets answer the question."""

    file_summary: str


class DevInstructionsReply(BaseModel):
    """Dev fixer reply: concrete implementation instructions."""

    dev_instructions: str


class PlanStep(BaseModel):
    """One plan step: what it delivers, what it touches, what proves it done."""

    id: str
    summary: str
    files_expected: list[str] = []
    depends_on: list[str] = []
    completion_check: str


class PlanStepsReply(BaseModel):
    """Plan worker reply: the plan as a structured step list, not one blob.

    The gate and the entry gate of ImplementPlanStep check this structure
    programmatically, so the shape is load-bearing: id, summary, files,
    dependencies, and a checkable completion criterion per step.
    """

    steps: list[PlanStep]
    dev_instructions: str = ""


class PlanReply(BaseModel):
    """Step-plan worker reply: the step's implementation plan, plus notes.

    Distinct from PlanStepsReply: this is the single-step implementation
    plan drafted inside ImplementPlanStep, consumed as prose by the detail
    and scout-check gates.
    """

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


BooleanVerdict = type(
    "BooleanVerdict",
    (BaseModel,),
    {
        "__annotations__": {"pass": bool},
        "__module__": __name__,
        "__qualname__": "BooleanVerdict",
        "__doc__": "Model-judged gate reply: the bare verdict as JSON.",
    },
)


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
        if cls.__dict__.get("run") is not None:
            # Model-mode gate that customizes run() (e.g. JsonGate); its
            # subclasses still require a prompt because their verdicts render
            # it, but a class shipping its own run() owns its prompt use.
            return
        if getattr(cls, "prompt", None) is None:
            raise ConfigError(
                f"{workflow_class_path(cls)}: model-mode CarryGate requires a prompt "
                "ClassVar or a decide() override"
            )

    def decide(self, input: BaseModel) -> bool:
        """Programmatic predicate; when not overridden the prompt is model-judged."""
        raise NotImplementedError

    async def _model_verdict(
        self, input: BaseModel, ctx: RunContext, extra: dict | None = None
    ) -> bool:
        """Render the prompt, call the model, parse the JSON verdict.

        Used by model-judged gates whose prompt is a JSON spec (via
        _json_prompt): the model answers {"pass": true|false} instead of a
        bare boolean word, which nudges JSON-shaped replies everywhere else.
        `extra` adds view-only prompt fields (e.g. a pre-rendered steps list)
        that do not live on the cache.
        """

        dump = input.model_dump()
        dump.update(extra or {})
        text = render_prompt(self.prompt or "", dump, ctx.node_path)
        completion = await ctx.provider.complete([{"role": "user", "content": text}])
        ctx.emit(
            "model_call",
            {
                "variant": None,
                "tokens_total": completion.tokens_total,
                "cost_usd": completion.cost_usd,
            },
        )
        produced = parse_output(BooleanVerdict, completion.text, ctx.node_path)
        return getattr(produced, "pass")

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


class JsonGate(CarryGate):
    """Model-judged gate whose prompt is a JSON spec (via _json_prompt).

    The model answers {"pass": true|false} in one JSON object; the verdict is
    parsed strictly. JSON-shaped prompts nudge JSON-shaped replies everywhere,
    so no gate asks for a bare boolean word.
    """

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        verdict = await self._model_verdict(input, ctx)
        produced = GateVerdict(
            **{
                "pass": verdict,
                "fail_count": input.fail_count + (0 if verdict else 1),
            }
        )
        return carry_forward(input, produced)


class SpecGate(JsonGate):
    """Scout entry gate: is the instruction a clear, targeted-search question?"""

    description = "Gate on the instruction being specific enough to search."
    prompt = _json_prompt(
        {
            "role": "scout spec gate",
            "task": (
                "Decide whether the instruction below states a clear, "
                "specific question or objective that a targeted codebase "
                "search can answer without guessing. It is specific enough "
                "when it names concrete symbols, files, paths, or a "
                "checkable deliverable, or states them derivably. Vague "
                "ambition ('make it better', 'look at the code') is not "
                "specific."
            ),
            "response_type": "json",
            "response_format": {"pass": "true or false"},
            "rules": ["Reply with exactly one JSON object and nothing else."],
            "input": {"instruction": "{instruction}"},
        }
    )


class DevSpecGate(JsonGate):
    """Dev entry gate: are the implementation instructions concrete enough?"""

    description = "Gate on dev instructions naming abstractions and target files."
    prompt = _json_prompt(
        {
            "role": "dev gate",
            "task": (
                "Decide whether the instructions below are clear enough to "
                "implement without weighing trade-offs: they should name the "
                "abstractions to build, the files that should contain them, "
                "and what is out of scope, or state these derivably. The "
                "instructions are a boundary, not a line-by-line edit plan; "
                "judgement-free execution is the bar. 'Improve the module' "
                "fails."
            ),
            "response_type": "json",
            "response_format": {"pass": "true or false"},
            "rules": ["Reply with exactly one JSON object and nothing else."],
            "input": {
                "instruction": "{instruction}",
                "dev_instructions": "{dev_instructions}",
            },
        }
    )


class ReviewValidityGate(CarryGate):
    """Reviewer entry gate: programmatic check for a step and a diff."""

    description = "Gate on the review input containing a step and a diff."

    def decide(self, input: BaseModel) -> bool:
        return bool(input.step.strip() and input.diff.strip())


class PlanGate(CarryGate):
    """Plan gate: multi-step structure, then model-judged per-step sizing.

    The structural criteria (>= 2 steps, valid DAG, checkable
    completion_check per step) are checked programmatically and fail in
    milliseconds with the reason in gate_notes. When the structure holds, the
    model judges the one criterion code cannot: whether every step is
    single-context-window sized and concrete enough to implement. The model
    is given a JSON prompt spec and replies with a JSON verdict.
    """

    description = "Gate on the plan being a multi-step, single-window-sized plan."
    prompt = _json_prompt(
        {
            "role": "plan gate",
            "task": (
                "Decide whether the step list below passes. It passes only "
                "when: (1) the steps together achieve the instruction with "
                "no knowledge gaps; (2) every step is single-context-window "
                "sized, meaning its deliverable, the files it touches, and "
                "its verification fit one working session without "
                "re-scouting the whole repo; (3) each step is concrete and "
                "points the implementer in a direction: a deliverable, the "
                "approach or files or symbols to use, and how to verify it. "
                "A step that says 'explore', 'investigate', or defers to a "
                "scouting pass fails. A step that needs a judgement call "
                "the plan has not pre-made is not discrete."
            ),
            "response_type": "json",
            "response_format": {"pass": "true or false"},
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "The step list's structure (step count, DAG, completion "
                    "checks) was already verified programmatically; judge "
                    "only concreteness and per-step sizing."
                ),
            ],
            "input": {
                "objective": "{instruction}",
                "steps": "{steps_json}",
            },
        }
    )

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        steps_json = json.dumps(input.steps or [], indent=2)
        notes = _plan_structure_notes(_plan_steps(input.steps))
        if notes:
            # Structural failure: no model call, fail in milliseconds.
            produced = GateVerdict(**{"pass": False, "fail_count": input.fail_count + 1})
            return carry_forward(input, produced).model_copy(
                update={"gate_notes": notes, "steps_json": steps_json}
            )
        verdict = await self._model_verdict(input, ctx, extra={"steps_json": steps_json})
        produced = GateVerdict(
            **{
                "pass": verdict,
                "fail_count": input.fail_count + (0 if verdict else 1),
            }
        )
        out = carry_forward(input, produced).model_copy(
            update={"steps_json": steps_json}
        )
        if not verdict:
            out = out.model_copy(
                update={
                    "gate_notes": (
                        "gate model judged at least one step not "
                        "single-context-window sized or not concrete enough"
                    )
                }
            )
        return out


class StepGate(CarryGate):
    """ImplementPlanStep entry gate: a programmatic predicate, not a model.

    decide() is a Python predicate over the cached plan (zero LLM calls, fails
    in milliseconds): the input must reference exactly one step_id from the
    cached plan, that step's dependencies must be marked done in step_status,
    and the step must not already be implemented. On fail the reason lands in
    gate_notes so the caller can fix its input instead of burning an inner
    clarify round-trip; the workflow routes a failed gate straight to END.
    """

    description = "Gate on exactly one cached plan step, dependencies done."

    def _evaluate(self, input: BaseModel) -> tuple[bool, str, str]:
        """Return (verdict, reason, step_id)."""

        steps = _plan_steps(input.steps)
        if not steps:
            return (
                False,
                "no plan in the cache; run PlanSWETask first and feed one of "
                "its steps",
                "",
            )
        matches = [
            step
            for step in steps
            if str(step.get("id", "")).strip()
            and re.search(
                rf"\b{re.escape(str(step.get('id', '')).strip())}\b", input.step
            )
        ]
        if len(matches) != 1:
            ids = ", ".join(str(s.get("id", "")) for s in steps)
            return (
                False,
                f"input must reference exactly one step id from the plan "
                f"({ids}); it references {len(matches)}",
                "",
            )
        step = matches[0]
        step_id = str(step.get("id", "")).strip()
        status = input.step_status if isinstance(input.step_status, dict) else {}
        record = status.get(step_id) or {}
        if record.get("status") == "done":
            return False, f"step {step_id} is already implemented", step_id
        undone = [
            dep
            for dep in (step.get("depends_on") or [])
            if (status.get(dep) or {}).get("status") != "done"
        ]
        if undone:
            return (
                False,
                f"step {step_id} depends on "
                f"{', '.join(str(d) for d in undone)}, not marked done yet",
                step_id,
            )
        return True, "", step_id

    def decide(self, input: BaseModel) -> bool:
        return self._evaluate(input)[0]

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        verdict, reason, step_id = self._evaluate(input)
        produced = GateVerdict(
            **{
                "pass": verdict,
                "fail_count": input.fail_count + (0 if verdict else 1),
            }
        )
        out = carry_forward(input, produced)
        update = {"gate_notes": reason} if reason else {}
        if verdict:
            update["step_id"] = step_id
        return out.model_copy(update=update) if update else out


class PlanDetailGate(JsonGate):
    """Gate on plan detail: concrete abstractions/files, honest unknowns."""

    description = "Gate on the plan naming abstractions/files and honest unknowns."
    prompt = _json_prompt(
        {
            "role": "plan detail gate",
            "task": (
                "Decide whether the implementation plan below BOTH names "
                "concrete abstractions and their target files AND is honest "
                "about unknowns: it must explicitly list open questions "
                "wherever information is missing rather than papering over "
                "them. A plan that speculates instead of listing what it "
                "does not know fails. The plan is a boundary, not an edit "
                "plan: function bodies and per-line edit instructions are "
                "not required."
            ),
            "response_type": "json",
            "response_format": {"pass": "true or false"},
            "rules": ["Reply with exactly one JSON object and nothing else."],
            "input": {"plan": "{plan}"},
        }
    )


class ScoutCheckGate(CarryGate):
    """Gate on the plan->scout loop: safe to start, not complete certainty.

    The old exit criterion ("certain enough to implement well, step by step")
    pushed the inner model to demand exhaustive verbatim evidence, so the loop
    burned scout rounds. This gate passes when the plan is SAFE TO START: it
    names concrete files/symbols for this step alone and lists its residual
    unknowns explicitly. The dev agent verifies those unknowns against the
    code at write-time, where verification is cheap. After SCOUT_ROUNDS_MAX
    failed rounds the gate passes anyway with the scout's open questions
    attached in gate_notes, so the loop always exits.
    """

    description = "Gate on the plan being safe to start implementing."
    prompt = _json_prompt(
        {
            "role": "scout check gate",
            "task": (
                "Decide whether the plan->scout loop may exit: only when the "
                "implementation plan is SAFE TO START, not complete. It is "
                "safe to start when the plan names concrete files and "
                "symbols for THIS step alone and every residual unknown is "
                "explicitly listed rather than papered over; the implementer "
                "verifies listed unknowns against the code before editing. "
                "Demanding verbatim evidence for the whole design makes the "
                "loop never exit; fail only when the plan would send the "
                "implementer scouting instead of editing."
            ),
            "response_type": "json",
            "response_format": {"pass": "true or false"},
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                (
                    "An explicit open-questions section is a sign of an "
                    "honest plan, not a failure; papering over unknowns is."
                ),
            ],
            "input": {
                "meta_instruction": "{instruction}",
                "plan": "{plan}",
                "scout_summary": "{file_summary}",
            },
        }
    )

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        verdict = await self._model_verdict(input, ctx)
        if verdict:
            produced = GateVerdict(**{"pass": True, "fail_count": input.fail_count})
            return carry_forward(input, produced)
        rounds = input.scout_fail_count + 1
        if rounds < SCOUT_ROUNDS_MAX:
            produced = GateVerdict(
                **{"pass": False, "fail_count": input.fail_count + 1}
            )
            return carry_forward(input, produced).model_copy(
                update={"scout_fail_count": rounds}
            )
        # Two scout rounds max, then pass with open questions attached.
        open_questions = _extract_open_questions(input.file_summary)
        produced = GateVerdict(**{"pass": True, "fail_count": input.fail_count + 1})
        return carry_forward(input, produced).model_copy(
            update={
                "scout_fail_count": rounds,
                "gate_notes": (
                    f"forced pass after {rounds} scout rounds; the dev agent "
                    "must verify these open questions against the code "
                    f"before editing: {open_questions or 'see the scout summary'}"
                ),
            }
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
    """PlanSWETask planner: emits a structured step list, not one blob.

    The step list is load-bearing: PlanGate checks its structure
    programmatically and ImplementPlanStep's StepGate matches the fed input
    against it, so every step needs id, summary, files_expected, depends_on,
    and a checkable completion_check.
    """

    description = "Draft a plan as a structured list of gated steps."
    parsed_type = PlanStepsReply
    prompt = _json_prompt(
        {
            "role": "planner",
            "task": (
                "Draft a plan for the objective below as a list of steps. "
                "Each step has: an id ('step-1', 'step-2', ...), a one-line "
                "summary, the files it is expected to touch, the ids of the "
                "steps it depends on, and a completion_check: a checkable "
                "criterion a reviewer can grade later. Each step stands "
                "alone and fits one context window. Every step points the "
                "implementer in a direction: name the approach, the files "
                "or symbols to touch, and what done looks like, so no step "
                "ends with 'go find out'. The steps together achieve the "
                "instruction, not a loose reading of it, and leave no "
                "knowledge gaps: every ambiguity is resolved by a decision "
                "in the plan, never deferred to a scouting pass. Consult "
                "the clarifications if present."
            ),
            "response_type": "json",
            "response_format": {
                "steps": (
                    "list of step objects: {id, summary, files_expected, "
                    "depends_on, completion_check}"
                ),
                "dev_instructions": (
                    "optional implementation notes for the dev agent"
                ),
            },
            "rules": [
                "Reply with exactly one JSON object and nothing else.",
                "At least 2 steps; depends_on must form a DAG.",
                (
                    "The plan is a boundary, not an edit plan: no function "
                    "bodies, no per-file edit instructions."
                ),
                (
                    "No open questions and no explore steps: if information "
                    "is missing, make the call from what the instruction "
                    "and clarifications give."
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
                "The plan gate rejected the step list below: it does not "
                "achieve the instruction, it leaves knowledge gaps, or it "
                "failed the gate's structure check (the reason, if any, is "
                "in gate_notes). Your reply goes straight back to the agent "
                "who called this planner. Poke them about where the gaps "
                "are: one pointed question per gap, numbered, each with "
                "your recommended answer. Ask only questions relevant to "
                "completing the instruction, and address the question that "
                "decides everything: is this plan sufficient to implement "
                "it? Tell them to provide more detail and to do more "
                "scouting first if they do not know the answers."
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
                "rejected_steps": "{steps_json}",
                "gate_notes": "{gate_notes}",
                "answers_already_given": "{clarifications}",
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
    grep/find instead of judging the diff summary alone.
    """

    description = "Verify the plan step was implemented in the diff."
    parsed_type = ReviewReportReply
    permissions = PermissionSet(allowed_tools=("read_file", "grep"))
    executor = partial(_pi_rpc_executor, tools=_INSPECT_TOOLS)
    agent_task = {
        "role": "reviewer",
        "task": (
            "Verify that the instruction item (step) below has been "
            "implemented in the diff. The carried diff is a summary: use "
            "grep/find and bash to inspect the actual repo files and confirm "
            "each claim before reporting it. Report only what the code "
            "justifies: no invented issues, no severity inflation. Cite "
            "exact file paths and line ranges as evidence for every finding."
        ),
        "response_type": "json",
        "rules": [
            (
                "Inspection only: your tools are bash, ls, grep, and find — "
                "do not modify, create, or delete files during review."
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


class ScoutAgent(CarryAgent):
    """Finds files and snippets answering the instruction; runs as a pi RPC session."""

    description = "Search the repo for files and snippets answering the objective."
    parsed_type = FileSummary
    # pi supplies its own tools; the node's PermissionSet
    # only documents intent and protects a harness swap-back (pi bypasses it).
    permissions = PermissionSet(allowed_tools=("list_dir", "read_file", "bash"))
    executor = partial(_pi_rpc_executor, tools=_INSPECT_TOOLS)
    agent_task = {
        "role": "scout",
        "task": (
            "Search the repo for the files and snippets that answer the "
            "objective in this context. Move fast, but do not guess: prefer "
            "targeted search (grep with specific symbols or paths) and "
            "selective reading (bash cat/sed for exact ranges) over broad "
            "sweeps. Start from paths and symbols the context already "
            "names; reserve unscoped grep for exhaustive verification."
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
                "Inspection only: your tools are bash, ls, grep, and find — "
                "do not modify, create, or delete files while scouting."
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
    """Writes the code per the dev instructions; runs as a pi RPC session.

    Residual unknowns left over from scouting are resolved HERE, at
    write-time, not re-scouted: the agent has the read-only inspect tools to
    verify each open question against the actual code before it edits.
    Prior steps' review reports reach it through the cache's step_status
    records, not through the repo history it would otherwise have to dig up.
    """

    description = "Implement the dev instructions against the repo files."
    parsed_type = DevChangesReply
    permissions = PermissionSet(allowed_tools=("read_file", "write_file"))
    executor = partial(_pi_rpc_executor, tools=_DEV_TOOLS)
    agent_task = {
        "role": "dev",
        "task": (
            "Implement the dev_instructions in this context against the repo "
            "files. Read the carried context first, validate it against the "
            "actual code, then make the smallest correct change. Follow the "
            "patterns already in the codebase; the instructions are the "
            "contract, but the code is the source of truth for what exists. "
            "If the context carries unresolved open questions from scouting "
            "(gate_notes and the scout summary's open questions section), "
            "verify each one against the actual code before you edit: you "
            "resolve them at write-time, not by re-scouting broadly. If "
            "step_status records prior steps' reviewer verdicts, read them "
            "first so this step composes with what earlier steps committed."
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
                "Your tools are bash, ls, grep, edit, and write — there is no "
                "dedicated read tool: read files through bash (cat/sed) "
                "before editing. Run checks that exist (imports, tests) and "
                "reread the files you changed before reporting."
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


class StepRecorder(Workflow):
    """Marks the reviewed step in the cache's step_status map.

    Programmatic post-review node, zero LLM calls. It reads the step id the
    StepGate matched, the reviewer's report, and the dev's diff summary, then
    persists {status, reviewer_verdict, files_touched} for that step id. The
    records ride the cache across the caller's sequential ImplementPlanStep
    invocations, which makes the caller's loop over steps mechanical ('next
    incomplete step') and leaves partial-progress telemetry behind if a trial
    dies at the timeout.
    """

    _defer_validation = True

    input_type = Cache
    output_type = Cache

    async def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        match = _VERDICT_RE.search(input.review_report)
        if match:
            verdict = match.group(1).upper()
        elif "no issues found" in input.review_report.lower():
            verdict = "OK"
        else:
            verdict = ""
        status = {
            "BLOCKING": "blocked",
            "MAJOR": "partial",
            "MINOR": "done",
            "OK": "done",
        }.get(verdict, "unknown")
        files = sorted(set(_FILE_RE.findall(input.diff)))[:25]
        records = dict(input.step_status) if isinstance(input.step_status, dict) else {}
        if input.step_id:
            records[input.step_id] = {
                "status": status,
                "reviewer_verdict": verdict,
                "files_touched": files,
            }
        return input.model_copy(update={"step_status": records})


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
