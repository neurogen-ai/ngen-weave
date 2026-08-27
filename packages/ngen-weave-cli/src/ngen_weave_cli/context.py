"""Construction of engines, stores, and discovery maps for CLI commands.

Thin wrapper over ngen_weave.wiring so commands stay thin translations.
"""

from __future__ import annotations

from pathlib import Path

from ngen_weave.config import ResolvedConfig
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.wiring import (
    LazyProvider,
    build_service,
    build_stack,
    merged_registry,
    reset_merged_registry,
)

NGEN_WEAVE_DIR = Path(".ngen-weave")
CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}

__all__ = [
    "CONFIG_SUFFIXES",
    "LazyProvider",
    "NGEN_WEAVE_DIR",
    "_build_engine",
    "build_service",
    "default_provider",
    "merged_registry",
    "reset_merged_registry",
]


def default_provider(models_file: Path):
    """Build the LiteLLM-backed provider from models.json.

    Kept as a module attribute (not a plain re-export) so tests can monkey
    patch it here and reach every provider construction in this package.

    Raises:
        ConfigError: The file is missing or invalid.
    """
    from ngen_weave.wiring import default_provider as _default_provider

    return _default_provider(models_file)


def _build_engine(
    config: ResolvedConfig | None,
    provider: CompletionProvider | None = None,
    project: str | None = None,
) -> object:
    """Construct Engine, RunStore, artifact store, and discovery map.

    Thin wrapper over ngen_weave.wiring.build_stack; provider resolution is
    kept local so the patched default_provider above stays effective.

    Args:
        config: Resolved run configuration; None means defaults anchored at
            the working directory (used by resume/status, which outlive any
            single config file).
        provider: Completion override for tests; None builds the real one.
        project: Project name for content-addressed artifacts; None or empty
            means declared artifacts are dropped (resume/status never write).
    """
    if provider is None:
        models_file = config.models_file if config is not None else Path("models.json")
        provider = LazyProvider(lambda: default_provider(models_file))
    return build_stack(config, provider=provider, project=project)
