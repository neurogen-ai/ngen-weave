"""Error taxonomy for ngen-weave.

Every error raised by ngen-weave derives from NgWeaveError, letting callers
catch one root class while the taxonomy drives behavior: ConfigError kills a
run or validation before it starts, DataError flows through the graph without
retry, InfraError is retryable per engine policy.

Classes:
    NgWeaveError: Root exception for all ngen-weave errors.
    ConfigError: Static problem such as a bad graph, duplicate registration, or invalid config.
    DataError: Schema-invalid output or control failure; flows through the graph, never retried.
    InfraError: Transport or API failure eligible for retry under the engine's policy.
"""


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


class InfraError(NgWeaveError):
    """Transport or API failure, such as timeouts and provider outages.

    Retryable per the engine's policy (default 3 attempts, exponential backoff).
    """
