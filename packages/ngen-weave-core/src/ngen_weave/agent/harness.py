"""Gated real-model tool-use loop driven through CompletionProvider."""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from ngen_weave.constants import MAX_TURNS, REPAIR_NUDGE, REPLY_EXCERPT_CHARS
from ngen_weave.engine.runner import _strip_code_fence
from ngen_weave.errors import AgentReplyError
from ngen_weave.schema_errors import format_validation_error


class HarnessAgentExecutor:
    """Drives CompletionProvider through a gated tool-use loop.

    Wire contract between model replies and actions: every reply must be (or be
    wrapped by) a markdown code fence around plain JSON carrying exactly one
    top-level key --

      {"tool_call": {"name": "<tool>", "args": {...}}}
          -> gate.call(name, args); the tool's result dict goes back to the
             model as a user message serialized as {"tool_result": ...}.
      {"output": {<fields of output_type>}}
          -> the final answer; validated with output_type.model_validate and
             returned to the caller.

    Anything else -- unparseable text, both keys, unknown keys, malformed tool
    call bodies, or an output dict failing output_type validation -- counts as
    an unparseable turn: the reply is echoed back into the conversation with a
    repair nudge so the model can correct itself, and the turn is spent. After
    MAX_TURNS (3) provider turns without a validated final answer, execute()
    raises AgentReplyError citing the last reply, so the engine's ordinary
    AgentReplyError retry policy governs what happens next.

    The system message states the target output_type JSON Schema and lists the
    gated tool specs; the user message carries the caller's prompt. Every
    provider turn emits one model_call provenance record via ctx.emit (variant
    None -- variant resolution belongs to compiled workflows), so budgets,
    observers, and run reports see agent turns like any other model traffic.

    Tool calls go through the gate only; the raw registry is invisible here.
    Gate denials are deliberately not caught: DeniedToolError fails the node as
    an ordinary DataError and ReturnToReviewError propagates for engine-side
    routing (E3). No mock executor ships as product -- tests inject fakes.
    """

    def __init__(self, provider) -> None:
        self._provider = provider

    async def execute(
        self,
        prompt: str,
        output_type: type[BaseModel],
        permissions,
        gate,
        ctx,
    ) -> BaseModel:
        """Run the loop and return the validated final answer.

        Args:
            prompt: User-turn content for this activation.
            output_type: Pydantic model the final answer must validate against;
                its schema is stated in the system message.
            permissions: The PermissionSet backing the gate, mirrored on the
                signature so executors can render ceilings without unwrapping.
            gate: PermissionGate over the node's tools; sole execution path,
                also the source of the tool surface listed to the model.
            ctx: This activation's RunContext; carries emit() and provider.

        Returns:
            The validated output_type instance built from the model's final
            {"output": {...}} reply.

        Raises:
            AgentReplyError: MAX_TURNS elapsed without a validated final answer.
            DeniedToolError / ReturnToReviewError / UnknownToolError:
                Propagated untouched from the gate or registry underneath it.
        """
        messages = [
            {"role": "system", "content": self._system_message(output_type, gate)},
            {"role": "user", "content": prompt},
        ]
        last_reply = ""
        for _turn in range(MAX_TURNS):
            completion = await self._provider.complete(messages, variant=None)
            ctx.emit(
                "model_call",
                {
                    "variant": None,
                    "tokens_total": completion.tokens_total,
                    "cost_usd": completion.cost_usd,
                },
            )
            last_reply = completion.text
            action = self._parse_action(completion.text)
            if action is None or action.get("invalid") is not None:
                detail = action["invalid"] if action else ""
                last_reply = f"{completion.text}\n{detail}".strip()
                messages.append({"role": "assistant", "content": completion.text})
                messages.append({"role": "user", "content": f"{detail}\n{REPAIR_NUDGE}".strip()})
                continue
            if action["tool_call"] is not None:
                result = await gate.call(action["tool_call"]["name"], action["tool_call"]["args"])
                messages.append({"role": "assistant", "content": completion.text})
                messages.append({"role": "user", "content": json.dumps({"tool_result": result})})
                continue
            try:
                return output_type.model_validate(action["output"])
            except ValidationError as exc:
                hint = format_validation_error(output_type, exc)
                last_reply = f"{completion.text}\n{hint}"
                messages.append({"role": "assistant", "content": completion.text})
                messages.append(
                    {"role": "user", "content": f"The output fields are wrong:\n{hint}"}
                )
        raise AgentReplyError(
            f"{ctx.node_path}: no validated final answer after {MAX_TURNS} turns\n"
            f"last reply: {last_reply[:REPLY_EXCERPT_CHARS]!r}"
        )

    def _system_message(self, output_type: type[BaseModel], gate) -> str:
        """Build the system message naming the output schema and the tool surface."""
        specs = [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters_schema": spec.parameters_schema,
            }
            for spec in gate.specs()
        ]
        return (
            "You are a workflow node inside ngen-weave. Target output schema "
            "(draft JSON Schema):\n"
            f"{json.dumps(output_type.model_json_schema(), indent=2)}\n"
            "Each turn reply with exactly one JSON object (optionally inside a code "
            'fence): {"tool_call": {"name": <name>, "args": <object>}} to invoke a '
            'tool once, or {"output": <object matching the target schema>} when done.\n'
            f"Available tools: {json.dumps(specs, indent=2)}"
        )

    @staticmethod
    def _parse_action(text: str) -> dict | None:
        """Parse one reply into an action envelope.

        Returns:
            {"tool_call": {...}, "output": None} for a well-formed tool-call
            envelope, {"tool_call": None, "output": {...}} for a final-answer
            envelope, {"invalid": <diagnosis>} for envelope-shaped replies whose
            body fails structural checks, or None when the reply fits no shape.
        """
        candidate = _strip_code_fence(text)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        keys = set(parsed)
        if keys == {"tool_call"}:
            call = parsed["tool_call"]
            if not isinstance(call, dict):
                return {"invalid": "tool_call must be an object"}
            name, args = call.get("name"), call.get("args")
            if not isinstance(name, str) or not isinstance(args, dict):
                return {"invalid": "tool_call needs a string name and object args"}
            return {"tool_call": {"name": name, "args": args}, "output": None}
        if keys == {"output"}:
            if isinstance(parsed["output"], dict):
                return {"tool_call": None, "output": parsed["output"]}
            return {"invalid": "output must be an object"}
        return {"invalid": f'unrecognized keys {sorted(keys)}; expected "tool_call" or "output"'}
