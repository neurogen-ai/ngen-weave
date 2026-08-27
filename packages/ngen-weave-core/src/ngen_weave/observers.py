"""Structured observer predicates and pause-only actions declared on workflows."""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ngen_weave.errors import ConfigError

if TYPE_CHECKING:
    from ngen_weave.provenance import RunMetadata

# last_output_valid excluded: bool, not comparable.
PREDICATE_FIELDS = frozenset(
    {"iterations", "tokens_in_context", "tokens_total", "cost_usd", "elapsed_ms"}
)
PREDICATE_OPS = frozenset({"gt", "lt", "ge", "le", "eq"})
OBSERVER_ACTIONS = frozenset({"pause"})  # stop may join later; reroute stays post-1.0

_OP_SYMBOLS = {"gt": ">", "lt": "<", "ge": ">=", "le": "<=", "eq": "=="}
_OP_FNS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "lt": operator.lt,
    "ge": operator.ge,
    "le": operator.le,
    "eq": operator.eq,
}


@dataclass(frozen=True)
class ObserverPredicate:
    """Structured comparison over a scope's RunMetadata field.

    Constructed directly with field/op/value and carried verbatim
    on Workflow.observations; unknown fields and ops are rejected at import
    time by validate_structure, so evaluate() needs no body policing.

    Attributes:
        field: Name of the RunMetadata numeric field the predicate watches.
        op: Comparison operator applied between the observed and configured numbers.
        value: Threshold the comparison runs against.
    """

    field: Literal["iterations", "tokens_in_context", "tokens_total", "cost_usd", "elapsed_ms"]
    op: Literal["gt", "lt", "ge", "le", "eq"]
    value: float | int

    def evaluate(self, meta: RunMetadata) -> bool:
        """Apply the stored comparison against meta's named field."""
        compare = _OP_FNS.get(self.op)
        if compare is None:
            raise ConfigError(
                f"observer predicate has unknown op {self.op!r}; expected one of "
                f"{', '.join(sorted(PREDICATE_OPS))}"
            )
        return bool(compare(getattr(meta, self.field), self.value))

    def describe(self) -> str:
        """Render the comparison mechanically, e.g. "cost_usd > 0.5"; cannot drift from it."""
        return f"{self.field} {_OP_SYMBOLS[self.op]} {self.value}"


@dataclass(frozen=True)
class Observer:
    """One declared supervision rule: a predicate plus the action its fire takes.

    Attributes:
        predicate: The structured comparison evaluated at activation boundaries.
        action: What happens when the predicate fires; pause only in v0.2.
    """

    predicate: ObserverPredicate
    action: Literal["pause"] = "pause"
