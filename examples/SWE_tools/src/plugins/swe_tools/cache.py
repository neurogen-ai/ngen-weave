"""Blanket carry cache: one schema every node in this package reads and writes.

`Cache` is the "cache schema" carry mechanism. Every field defaults, so any
node's output validates as any node's input, and `extra="allow"` keeps
arbitrary ad-hoc fields alive across edges: they survive `model_dump()` and
are re-accepted by `Cache.model_validate()`.

`carry_forward` is the "fill automatically from input into output" half: a
node parses its model reply strictly into a small per-node reply schema
(`parsed_type`) carrying only the fields it produces, and `carry_forward`
re-emits the input cache with exactly those fields overwritten. Everything
else travels through unmodified, so context produced by earlier nodes
(instruction, scout summary, plan, ...) reaches later nodes intact.

`GateVerdict` is the strict reply schema gates construct: the boolean verdict
plus the loop failure counter. `pass` is a keyword, so both models are built
dynamically.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

_CacheSpec = {
    "__annotations__": {
        "instruction": str,
        "clarifications": str,
        "file_summary": str,
        "plan": str,
        "step": str,
        "dev_instructions": str,
        "diff": str,
        "review_report": str,
        # PlanTask output: the plan as a structured step list
        # (list of {id, summary, files_expected, depends_on, completion_check}).
        "steps": Any,
        # JSON rendering of the step list (set by PlanGate; a readable form
        # for PlanRework and for the caller that returns to the top agent).
        "steps_json": str,
        # Per-step progress, keyed by step id:
        # {id: {status, reviewer_verdict, files_touched}}. Persisted across the
        # top agent's sequential ImplementPlanStep invocations.
        "step_status": Any,
        # The step id the StepGate matched (set on pass; read by StepRecorder).
        "step_id": str,
        # Why a programmatic gate failed, or a forced pass's open questions.
        "gate_notes": str,
        # Scout-check round counter (distinct from fail_count, which is shared
        # by every gate in the run).
        "scout_fail_count": int,
        "fail_count": int,
        "pass": bool,  # `pass` is a keyword: Cache is built via type().
    },
    "instruction": "",
    "clarifications": "",
    "file_summary": "",
    "plan": "",
    "step": "",
    "dev_instructions": "",
    "diff": "",
    "review_report": "",
    "steps": "",
    "steps_json": "",
    "step_status": "",
    "step_id": "",
    "gate_notes": "",
    "scout_fail_count": 0,
    "fail_count": 0,
    "pass": False,
    "__module__": __name__,
    "__qualname__": "Cache",
    "__doc__": (
        "Carried context shared by every node in this package. All fields "
        "default; `pass` is the last gate verdict, `fail_count` its loop "
        "counter, `steps` the cached plan's structured step list, "
        "`step_status` the persisted per-step progress, and unknown keys are "
        "preserved (extra='allow') so arbitrary fields ride across edges."
    ),
    "model_config": ConfigDict(extra="allow"),
}

Cache = type("Cache", (BaseModel,), _CacheSpec)


def carry_forward(input: BaseModel, produced: BaseModel) -> Cache:
    """Re-emit `input`'s carried fields, overwritten by what `produced` set.

    Only keys actually present on the produced instance (exclude_unset) are
    taken; defaults the reply did not fill never clobber carried context.
    Extra keys on the input survive via Cache's extra='allow' config.
    """
    data = input.model_dump()
    for key, value in produced.model_dump(exclude_unset=True).items():
        data[key] = value
    return Cache.model_validate(data)


GateVerdict = type(
    "GateVerdict",
    (BaseModel,),
    {
        "__annotations__": {"pass": bool, "fail_count": int},
        "__module__": __name__,
        "__qualname__": "GateVerdict",
        "__doc__": "One gate activation's verdict: boolean `pass` and the carried failure count.",
    },
)
