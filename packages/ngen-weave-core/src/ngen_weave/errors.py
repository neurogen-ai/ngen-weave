"""Error taxonomy for ngen-weave."""


class NgWeaveError(Exception):
    """Root exception for all ngen-weave errors."""


class ConfigError(NgWeaveError):
    """Static problem: bad graphs, duplicate registrations, bad config.

    Raised at import time by structural validation and discovery, or when a run
    config references unknown workflows or variants. Kills the run with a report;
    never retried.
    """


class DataError(NgWeaveError):
    """Schema-invalid output or failed control logic.

    Flows through the graph as an ordinary node failure and never retries,
    distinguishing content problems from transport problems.
    """


class ProviderError(ConfigError):
    """Deterministic provider failure: auth rejected, unknown model or
    endpoint, unreachable host. Raised once at the completion boundary;
    retrying cannot help. The message names the variant and the fix."""


class AgentReplyError(NgWeaveError):
    """A model reply that fails its output schema. Retryable: one malformed
    sample must not kill a run when a re-ask may succeed. The message carries
    the schema violations and a truncated reply so exhaustion is diagnosable."""


class InfraError(NgWeaveError):
    """Transport or API failure, such as timeouts and provider outages.

    Retryable per the engine's policy (default 3 attempts, exponential backoff).
    """
