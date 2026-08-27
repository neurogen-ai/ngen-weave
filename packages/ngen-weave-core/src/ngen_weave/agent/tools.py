"""ToolSpec declarations and the ToolRegistry that validates and dispatches them."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from ngen_weave.agent.errors import UnknownToolError
from ngen_weave.errors import ConfigError, DataError

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent may call: identity, surface, schema, and body.

    Attributes:
        name: Registry-unique identifier matching [a-z][a-z0-9_-]*.
        description: Shown to the model alongside the name and schema.
        parameters_schema: JSON Schema (draft 2020-12) for the call's args dict;
            checked at registration time, enforced on every call.
        fn: Async body taking the validated args dict and returning a result dict.
    """

    name: str
    description: str
    parameters_schema: dict
    fn: Callable[[dict], Awaitable[dict]]


class ToolRegistry:
    """Holds ToolSpec entries; validates args against each spec's schema."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Add spec to the registry.

        Args:
            spec: The tool declaration; name must be unique in this registry,
                match [a-z][a-z0-9_-]*, and carry a valid JSON Schema.

        Raises:
            ConfigError: On duplicate or malformed names, or an invalid schema.
        """
        if not _TOOL_NAME_RE.fullmatch(spec.name):
            raise ConfigError(f"invalid tool name {spec.name!r}: must match [a-z][a-z0-9_-]*")
        if spec.name in self._specs:
            raise ConfigError(f"duplicate tool registration: {spec.name!r} already registered")
        try:
            Draft202012Validator.check_schema(spec.parameters_schema)
        except Exception as exc:
            raise ConfigError(f"tool {spec.name!r} has invalid parameters_schema: {exc}") from exc
        self._specs[spec.name] = spec

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return all registered specs in registration order."""
        return tuple(self._specs.values())

    async def call(self, name: str, args: dict) -> dict:
        """Validate args against the named tool's schema and invoke its fn.

        Args:
            name: Registered tool identifier.
            args: Raw arguments, unvalidated until here.

        Returns:
            The tool fn's result dict.

        Raises:
            UnknownToolError: If no tool is registered under name.
            DataError: If args violate the tool's parameters_schema.
        """
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownToolError(f"unknown tool {name!r}")
        errors = list(Draft202012Validator(spec.parameters_schema).iter_errors(args))
        if errors:
            detail = "; ".join(f"{_path(e)}: {e.message}" for e in errors[:5])
            raise DataError(f"tool {name!r} received invalid arguments: {detail}")
        return await spec.fn(args)


def _path(error: object) -> str:
    return "$" + "".join(f"[{p!r}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
