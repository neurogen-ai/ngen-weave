"""In-process registry of discovered workflow classes.

Discovery populates it; classes never register themselves by existing. Identity
is the fully-qualified class path (workflow_class_path), so same-named classes
in different modules coexist, and a duplicate path always means overlapping
discovery sources, reported with both source names.

Functions:
    register: Record a workflow class under its class path, erroring on duplicates.
    get: Look up a registered class by path, erroring with "unknown workflow".
    all: Snapshot of every registered class path and class.
    reset: Drop every registration; tests and reload tooling only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ngen_weave.errors import ConfigError
from ngen_weave.workflow import workflow_class_path

if TYPE_CHECKING:
    from ngen_weave.workflow import Workflow


@dataclass(frozen=True)
class _Registration:
    """A registered class plus the channel that declared it."""

    cls: type[Workflow]
    source: str


_REGISTRY: dict[str, _Registration] = {}


def register(cls: type[Workflow], source: str) -> None:
    """Register cls under its fully-qualified class path.

    Duplicate registration of an already-known path raises ConfigError naming
    both declaring sources; overlapping discovery is an error, never a silent
    precedence. Re-listing a module that already loaded is still a duplicate,
    because the listing itself overlaps with itself.
    """
    path = workflow_class_path(cls)
    existing = _REGISTRY.get(path)
    if existing is not None:
        raise ConfigError(
            f"duplicate workflow registration: {path} declared by both "
            f"{existing.source} and {source}"
        )
    _REGISTRY[path] = _Registration(cls, source)


def get(path: str) -> type[Workflow]:
    """Return the class registered under path.

    Raises:
        ConfigError: The path names no registered workflow.
    """
    try:
        return _REGISTRY[path].cls
    except KeyError:
        raise ConfigError(f"unknown workflow: {path}") from None


def all() -> dict[str, type[Workflow]]:
    """Snapshot of registered class paths mapped to their classes."""
    return {path: entry.cls for path, entry in _REGISTRY.items()}


def reset() -> None:
    """Drop every registration.

    Tests and reload tooling only; production code builds the registry once
    from discovery at startup.
    """
    _REGISTRY.clear()
