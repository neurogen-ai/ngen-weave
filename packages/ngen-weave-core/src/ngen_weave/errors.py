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


class InfraError(NgWeaveError):
    """Transport or API failure, such as timeouts and provider outages.

    Retryable per the engine's policy (default 3 attempts, exponential backoff).
    """
