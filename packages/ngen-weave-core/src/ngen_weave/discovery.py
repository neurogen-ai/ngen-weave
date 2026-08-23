"""Workflow discovery from explicit listings.

The registry gains classes only through discovery: distributions declare
workflow modules as entry points under the ngen-weave.workflows group, and the
project manifest lists module paths directly. Importing a module registers
exactly the Workflow subclasses defined in it, never foreign classes re-exported
through it, under their fully-qualified class paths. Strict mode turns import
failures into ConfigError; duplicate paths raise in both modes.

Functions:
    discover: Import listed modules and register the workflows defined in them.
    discover_entry_points: Read the package entry-point group and discover those modules.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
from collections.abc import Sequence
from types import ModuleType

from ngen_weave import registry
from ngen_weave.errors import ConfigError
from ngen_weave.workflow import Workflow, workflow_class_path

ENTRY_POINT_GROUP = "ngen-weave.workflows"


def discover(
    modules: Sequence[str],
    *,
    strict: bool = True,
    source: str = "declared module list",
) -> dict[str, type[Workflow]]:
    """Import listed modules and register the workflow classes defined in them.

    Args:
        modules: Module paths to import, in declaration order.
        strict: True turns import failures into ConfigError; False skips a
            failing module and keeps the rest. Duplicate class paths raise in
            both modes (overlapping sources).
        source: Channel label attached to this call's registrations so
            duplicate errors can name both declaring channels.

    Returns:
        This call's registrations as class-path-to-class. The full namespace is
        registry.all(); consumers merge the maps of their calls.

    Raises:
        ConfigError: An import failure in strict mode, or any duplicate class
            path, naming both declaring sources.
    """
    found: dict[str, type[Workflow]] = {}
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            if not strict:
                continue
            raise ConfigError(
                f"failed to import workflow module {module_name!r}: {exc}"
            ) from exc
        label = f"{source} ({module_name})"
        for cls in _classes_defined_in(module):
            registry.register(cls, source=label)
            found[workflow_class_path(cls)] = cls
    return found


def discover_entry_points(*, strict: bool = True) -> dict[str, type[Workflow]]:
    """Discover every workflow module declared by installed distributions.

    Reads the ngen-weave.workflows entry-point group, one module path string
    per entry. Registrations carry the entry point's name and its distribution,
    so overlapping declarations name both packages.
    """
    found: dict[str, type[Workflow]] = {}
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = ep.dist.name if ep.dist is not None else "unknown distribution"
        found.update(
            discover(
                [ep.value],
                strict=strict,
                source=f"entry point {ep.name!r} of {dist}",
            )
        )
    return found


def _classes_defined_in(module: ModuleType) -> list[type[Workflow]]:
    """Workflow subclasses defined in module, not merely imported into it.

    A class registers under its own module's path; re-exports are skipped so a
    class is never registered twice through import chains.
    """
    name = module.__name__
    return [
        obj
        for _attr, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == name and issubclass(obj, Workflow)
    ]
