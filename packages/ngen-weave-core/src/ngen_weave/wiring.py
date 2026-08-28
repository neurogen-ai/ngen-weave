"""Composition root assembling engines, stores, discovery maps, and services entirely from core."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from ngen_weave.artifacts import ArtifactStore
from ngen_weave.config import ResolvedConfig, RunSettings
from ngen_weave.constants import NGEN_WEAVE_DIR
from ngen_weave.discovery import discover_entry_points
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.manifest import discover_from_manifest, load_project_manifest
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.models.registry import LiteLLMProvider, ModelRegistry
from ngen_weave.service import RunService
from ngen_weave.workflow import Workflow

_cached_registry: dict[str, type[Workflow]] | None = None


def reset_merged_registry() -> None:
    """Drop the cached discovery map; tests call this between cases."""
    global _cached_registry
    _cached_registry = None


def merged_registry() -> dict[str, type[Workflow]]:
    """Build the merged discovery map every consumer resolves names through.

    Sources in order: installed distributions' `ngen-weave.workflows` entry
    points, then the optional project manifest `ngen-weave.json` whose
    `modules` list names local workflow modules. The returned map is the only
    namespace commands read. Cached per process, matching the registry's own
    process-global, duplicate-rejecting semantics.
    """
    global _cached_registry
    if _cached_registry is not None:
        return _cached_registry
    found = dict(discover_entry_points())
    found.update(discover_from_manifest(load_project_manifest()))
    _cached_registry = found
    return found


def default_provider(models_file: Path) -> LiteLLMProvider:
    """Build the LiteLLM-backed provider from models.json.

    Raises:
        ConfigError: The file is missing or invalid.
    """
    if not models_file.is_file():
        raise ConfigError(
            f"models file not found at {models_file}; create one with at least "
            "defaultVariant and variants"
        )
    return LiteLLMProvider(ModelRegistry.load(models_file))


class LazyProvider:
    """CompletionProvider that defers models.json loading to first use.

    Resume and status construct an engine without touching a provider; they
    must not fail on a missing models.json when no model call will happen.
    """

    def __init__(self, loader) -> None:
        self._loader = loader
        self._inner: CompletionProvider | None = None

    async def complete(self, messages: list[dict], *, variant: str | None = None):
        """Load the real provider once, then delegate."""
        if self._inner is None:
            self._inner = self._loader()
        return await self._inner.complete(messages, variant=variant)


@dataclass
class AppStack:
    """Engine plus its supporting stores, constructed together from one config."""

    engine: Engine
    store: RunStore
    artifacts: ArtifactStore | None
    discovery_map: dict[str, type[Workflow]]


def build_stack(
    config: ResolvedConfig | None = None,
    *,
    provider: CompletionProvider | None = None,
    project: str | None = None,
    models_file: Path | None = None,
    db_path: Path | None = None,
) -> AppStack:
    """Construct Engine, RunStore, artifact store, and the merged discovery map.

    Args:
        config: Resolved run configuration; None means defaults anchored at
            the working directory (used by resume/status, which outlive any
            single config file).
        provider: Completion override for tests; None defers to a LazyProvider
            over the configured models file.
        project: Project name for content-addressed artifacts; None or empty
            means declared artifacts are dropped (resume/status never write).
        models_file: Override for the LazyProvider's models.json location;
            wins over both the config value and the default.
        db_path: Override for the LangGraph checkpointer database path.
    """
    settings = config.run if config is not None else RunSettings()
    if db_path is not None:
        settings = dataclasses.replace(settings, db_path=db_path)
    store = RunStore(NGEN_WEAVE_DIR / "runs")
    artifacts = ArtifactStore(NGEN_WEAVE_DIR / "projects", project) if project else None
    if provider is None:
        resolved_models_file = models_file or (
            config.models_file if config is not None else Path("models.json")
        )
        provider = LazyProvider(lambda: default_provider(resolved_models_file))
    engine = Engine(
        provider,
        store,
        checkpointer=settings.checkpointer,
        db_path=settings.db_path,
        max_retries=settings.max_retries,
        retry_backoff_ms=settings.retry_backoff_ms,
        artifacts=artifacts,
        settings=settings,
    )
    return AppStack(
        engine=engine, store=store, artifacts=artifacts, discovery_map=merged_registry()
    )


def build_service(
    *,
    config_path: Path | None = None,
    provider: CompletionProvider | None = None,
    project: str | None = None,
    models_file: Path | None = None,
    db_path: Path | None = None,
) -> RunService:
    """Assemble the default local RunService from configuration and discovery.

    Loads `config_path` when given, wires config, merged discovery, engine,
    and store through build_stack, and wraps them in LocalRunService. The
    optional models-file and checkpoint-db overrides win over any configured
    values.

    Raises:
        ConfigError: Unknown keys or unresolvable workflow/model references
            when config_path is given.
    """
    from ngen_weave.local_service import LocalRunService

    stack = build_stack(
        _load_config(config_path),
        provider=provider,
        project=project,
        models_file=models_file,
        db_path=db_path,
    )
    return LocalRunService(stack.engine, stack.store, stack.discovery_map)


def _load_config(config_path: Path | None) -> ResolvedConfig | None:
    """Resolve the optional config path against the merged registry."""
    from ngen_weave.config import load_config

    return load_config(config_path, merged_registry()) if config_path is not None else None
