"""PermissionGate: wraps a ToolRegistry and enforces a PermissionSet per activation."""

from ngen_weave.agent.errors import DeniedToolError, ReturnToReviewError
from ngen_weave.agent.permissions import PermissionSet
from ngen_weave.agent.tools import ToolRegistry
from ngen_weave.workflow import RunContext


class PermissionGate:
    """Wraps any ToolRegistry; enforces a PermissionSet.

    The agent loop never sees the raw registry. Denials follow one path for
    every reason (name not allowed, max_calls reached, budget_usd spent): the
    gate emits exactly one permission_denied provenance record with the C1
    payload {"tool", "node_path", "policy"}, then raises per the denied_policy:
    DeniedToolError under "fail_node" (ordinary DataError node failure) or
    ReturnToReviewError under "return_to_review".

    Every executed call emits one tool_call provenance record with the payload
    {"tool", "node_path", "cost_usd"} (cost_usd is the tool-reported spend,
    0.0 when the result carries none), so per-call activity is visible in the
    provenance stream alongside the per-turn model_call records.

    Usage accounting is per-activation and pre-call: ceilings are checked before
    executing the next call, so a spent budget blocks further calls rather than
    refunding. Tools report spend by including a numeric "cost_usd" key in their
    result dict; results without it contribute zero. max_calls counts executed
    calls only, not denials.
    """

    def __init__(self, inner: ToolRegistry, permissions: PermissionSet, ctx: RunContext) -> None:
        """Bind the wrapped registry, this activation's permission set, and its context."""
        self._inner = inner
        self._permissions = permissions
        self._ctx = ctx
        self._calls_used = 0
        self._spend_usd = 0.0

    async def call(self, name: str, args: dict) -> dict:
        """Run one gated tool call on behalf of the agent loop.

        Args:
            name: Tool identifier; must be in PermissionSet.allowed_tools.
            args: Raw tool arguments, validated downstream by the registry.

        Returns:
            The inner registry's result dict for an executed call.

        Raises:
            DeniedToolError: Under fail_node when the call is blocked.
            ReturnToReviewError: Under return_to_review when blocked.
        """
        perms = self._permissions
        over_calls = perms.max_calls is not None and self._calls_used >= perms.max_calls
        over_budget = perms.budget_usd is not None and self._spend_usd >= perms.budget_usd
        if name not in perms.allowed_tools or over_calls or over_budget:
            self._deny(name)
        result = await self._inner.call(name, args)
        cost = result.get("cost_usd")
        if isinstance(cost, int | float):
            self._spend_usd += float(cost)
        self._calls_used += 1
        self._ctx.emit(
            "tool_call",
            {
                "tool": name,
                "node_path": self._ctx.node_path,
                "cost_usd": float(cost) if isinstance(cost, int | float) else 0.0,
            },
        )
        return result

    def specs(self) -> tuple:
        """Expose the wrapped registry's ToolSpecs without exposing the registry.

        The agent loop's system prompt lists the tool surface from here; the
        underlying ToolRegistry stays invisible so calls can only run through
        call() and its permission checks.
        """
        return self._inner.specs()

    def _deny(self, name: str) -> None:
        """Emit the permission_denied record once, then raise per policy."""
        policy = self._permissions.denied_policy
        self._ctx.emit(
            "permission_denied",
            {"tool": name, "node_path": self._ctx.node_path, "policy": policy},
        )
        if policy == "return_to_review":
            raise ReturnToReviewError(f"tool {name!r} denied; routed back to review ({policy})")
        raise DeniedToolError(f"tool {name!r} denied under {policy}")
