"""Per-activation permission declaration for agent tool use."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PermissionSet:
    """What one agent activation may do, enforced engine-side by the gate.

    Attributes:
        allowed_tools: Tool names the gate will let through; anything else is denied.
        denied_policy: What happens on denial: "fail_node" raises DeniedToolError
            (ordinary node failure), "return_to_review" raises ReturnToReviewError.
        max_calls: Per-activation ceiling on executed tool calls; None is unbounded.
        budget_usd: Per-activation spend ceiling over tool-reported cost; None is unbounded.
    """

    allowed_tools: tuple[str, ...]
    denied_policy: Literal["fail_node", "return_to_review"] = "fail_node"
    max_calls: int | None = None
    budget_usd: float | None = None
