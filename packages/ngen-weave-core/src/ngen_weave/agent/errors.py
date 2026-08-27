"""Agent-specific error taxonomy: re-exports plus gate-local errors."""

# AgentReplyError is re-exported rather than redefined: it already exists as a
# retryable NgWeaveError (not DataError) -- "exhausted without a parseable reply"
# is a content failure the engine may recover by retrying, which DataError never does.

from ngen_weave.errors import AgentReplyError, DataError, NgWeaveError

__all__ = [
    "AgentReplyError",
    "DataError",
    "DeniedToolError",
    "NgWeaveError",
    "ReturnToReviewError",
    "UnknownToolError",
]


class UnknownToolError(DataError):
    """A tool invoked by name that no registry knows about.

    Arises at call time from runtime arguments (typically a model-chosen tool
    name), so it flows through the graph as an ordinary node failure like any
    other DataError; duplicate or malformed registration is instead a
    ConfigError raised once at setup.
    """


class DeniedToolError(DataError):
    """The permission gate blocked a tool call under the fail_node policy.

    The permission_denied record was already emitted by the gate before this
    was raised; it flows as an ordinary DataError node failure and never
    retries.
    """


class ReturnToReviewError(NgWeaveError):
    """The permission gate routed a blocked tool call back to human review.

    Raised under the return_to_review denied_policy after the gate emitted its
    permission_denied record; E3's engine-side routing translates this into a
    human interrupt.
    """
