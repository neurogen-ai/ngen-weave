"""Construction of engines, stores, and discovery maps for CLI commands.

Single place assembling Engine and RunStore from configuration, so commands
stay thin translations and tests inject a fake provider here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ngen_weave.artifacts import ArtifactStore
from ngen_weave.config import ResolvedConfig, RunSettings
from ngen_weave.discovery import discover, discover_entry_points
from ngen_weave.engine.runner import Engine
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.models.registry import LiteLLMProvider, ModelRegistry
from ngen_weave.workflow import Workflow

NGEN_WEAVE_DIR = Path(".ngen-weave")
MANIFEST = Path("ngen-weave.json")
CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}

_cached_registry: dict[str, type[Workflow]] | None = None


@dataclass
class AppContext:
    """Engine plus its run store, constructed together from one config."""

    engine: Engine
    store: RunStore
    artifacts: ArtifactStore | None


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
    if MANIFEST.is_file():
        try:
            manifest = json.loads(MANIFEST.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{MANIFEST}: cannot read project manifest: {exc}") from exc
        modules = manifest.get("modules", [])
        if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
            raise ConfigError(f"{MANIFEST}: 'modules' must be a list of module paths")
        found.update(discover(modules, source=f"project manifest {MANIFEST}"))
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
            "defaultVariant and variants, or pass -c with models_file set"
        )
    return LiteLLMProvider(ModelRegistry.load(models_file))


class LazyProvider:
    """CompletionProvider that defers models.json loading to first use.

    Resume and status construct an engine without a config file; they must
    not fail on a missing models.json when no model call will happen.
    """

    def __init__(self, models_file: Path) -> None:
        self._models_file = models_file
        self._inner: LiteLLMProvider | None = None

    async def complete(self, messages: list[dict], *, variant: str | None = None):
        """Load the real provider once, then delegate."""
        if self._inner is None:
            self._inner = default_provider(self._models_file)
        return await self._inner.complete(messages, variant=variant)


def _build_engine(
    config: ResolvedConfig | None,
    provider: CompletionProvider | None = None,
    project: str | None = None,
) -> AppContext:
    """Construct Engine, RunStore, and artifact store; provider defaults to LiteLLM.

    Args:
        config: Resolved run configuration; None means defaults anchored at
            the working directory (used by resume/status, which outlive any
            single config file).
        provider: Completion override for tests; None builds the real one.
        project: Project name for content-addressed artifacts; None or empty
            means declared artifacts are dropped (resume/status never write).
    """
    settings = config.run if config is not None else RunSettings()
    store = RunStore(NGEN_WEAVE_DIR / "runs")
    artifacts = ArtifactStore(NGEN_WEAVE_DIR / "projects", project) if project else None
    if provider is None:
        models_file = config.models_file if config is not None else Path("models.json")
        provider = LazyProvider(models_file)
    engine = Engine(
        provider,
        store,
        checkpointer=settings.checkpointer,
        db_path=settings.db_path,
        max_retries=settings.max_retries,
        retry_backoff_ms=settings.retry_backoff_ms,
        artifacts=artifacts,
    )
    return AppContext(engine=engine, store=store, artifacts=artifacts)
