"""AgentNode leaf: a model loop whose tool calls run only through the gate."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from ngen_weave.agent.gate import PermissionGate
from ngen_weave.agent.harness import HarnessAgentExecutor
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolRegistry, ToolSpec
from ngen_weave.workflow import RunContext, Workflow


class AgentNode(Workflow):
    """Leaf node that drives a CompletionProvider through gated tool use.

    Class attributes carry the whole declaration:

      permissions: Required PermissionSet for this activation; the engine-side
        validation pass rejects concrete subclasses that omit it.
      tools: ToolSpecs registered into a fresh per-activation ToolRegistry;
        empty by default. The registry is reachable only through the gate.
      executor: Optional factory hatch overriding the default executor; called
        as executor(provider) and must return an object exposing async
        execute(prompt, output_type, permissions, gate, ctx) -> BaseModel,
        matching HarnessAgentExecutor's surface. Tests inject fakes here; no
        separate mock executor ships as product.

    Subclasses declare input_type/output_type like any leaf; output_type both
    travels over outgoing edges and validates the model's final {"output":
    {...}} answer inside the harness (see HarnessAgentExecutor for the wire
    contract). Provenance: every provider turn emits one model_call record, so
    per-activation metadata accumulates real token/cost totals across turns.
    """

    # Intermediate base: no input_type/output_type/permissions of its own, so it
    # opts into Workflow's deferred-validation convention. Concrete subclasses
    # (which never declare _defer_validation in their own __dict__) are fully
    # validated at class-creation time, including the required permissions.
    _defer_validation = True

    permissions: ClassVar[PermissionSet]
    tools: ClassVar[Sequence[ToolSpec]] = ()
    executor: ClassVar[Callable[..., Any] | None] = None  # override hatch; default below

    def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        """Build this activation's gate over its tools and execute the loop.

        Args:
            input: Validated input model; serialized to JSON as the user turn.
            ctx: RunContext supplied by the engine; its provider backs the
                default executor.

        Returns:
            The validated output of the executor's tool-use loop.

        Raises:
            ConfigError: If a tools registration or permission wiring is
                malformed (surfaces from ToolRegistry.register at call time).
            AgentReplyError / DeniedToolError / ReturnToReviewError:
                Per the harness and gate semantics documented there.
        """
        registry = ToolRegistry()
        for spec in self.tools:
            registry.register(spec)
        gate = PermissionGate(registry, self.permissions, ctx)
        factory = type(self).executor if type(self).executor is not None else HarnessAgentExecutor
        executor = factory(ctx.provider)
        return executor.execute(
            json.dumps(input.model_dump(), ensure_ascii=False),
            type(self).output_type,
            self.permissions,
            gate,
            ctx,
        )
