"""Run configuration: YAML/JSON files resolving to ResolvedConfig.

Data only; structure stays in code and this layer accepts no code-bearing members.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ngen_weave.errors import ConfigError
from ngen_weave.workflow import Workflow

_TOP_LEVEL_KEYS = frozenset({"workflow", "params", "models", "models_file", "run"})
_RUN_KEYS = frozenset({"checkpointer", "db_path", "max_retries", "retry_backoff_ms", "budget"})
_BUDGET_KEYS = frozenset({"cost_usd", "steps"})


@dataclass(frozen=True)
class Budget:
    """Run spending caps under run.budget; at least one limit required.

    cost_usd caps accumulated model-call spend (compared against the store's
    incrementally maintained totals); steps caps node_activation counts. A
    breach pauses the run at the next activation boundary instead of failing
    it, so a resume with a raised cap continues from the same checkpoint.
    """

    cost_usd: float | None = None
    steps: int | None = None


@dataclass(frozen=True)
class RunSettings:
    """Checkpointer choice, database path, retry policy knobs, and budget."""

    checkpointer: str = "sqlite"
    db_path: Path = Path(".ngen-weave/checkpoints.db")
    max_retries: int = 3
    retry_backoff_ms: int = 1000
    budget: Budget | None = None


@dataclass(frozen=True)
class ResolvedConfig:
    """Everything Engine construction needs, fully validated at load time.

    Attributes:
        workflow: The registered workflow class named by the config.
        params: Data-only tunables passed to the workflow factory.
        models: Class-path -> variant-name bindings; source for compile-time
            model resolution where an exact leaf key beats an enclosing
            composite key beats the default variant.
        models_file: Path to models.json, resolved against the config file's
            directory when relative.
        run: Checkpointer and retry policy settings.
    """

    workflow: type[Workflow]
    params: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    models_file: Path = Path("models.json")
    run: RunSettings = field(default_factory=RunSettings)


def _load_document(path: Path) -> dict:
    """Parse a YAML or JSON config file into a plain dict."""
    suffix = path.suffix.lower()
    text = path.read_text()
    if suffix == ".json":
        document = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        import yaml

        document = yaml.safe_load(text)
    else:
        raise ConfigError(f"{path}: unsupported extension {suffix!r}; use .yaml, .yml, or .json")
    if not isinstance(document, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return document


def _resolve_workflow_class(path: str, registry: dict[str, type[Workflow]]) -> type[Workflow]:
    """Look up a class path in registry, falling back to importing it.

    Registry hits are the normal case; the import fallback serves configs
    written before their module was listed for discovery. Class paths match
    concrete classes exactly.
    """
    cls = registry.get(path)
    if cls is not None:
        return cls
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        obj: Any = module
        for segment in parts[split:]:
            try:
                obj = getattr(obj, segment)
            except AttributeError:
                break
        else:
            if isinstance(obj, type) and issubclass(obj, Workflow):
                return obj
    raise ConfigError(f"unknown workflow: {path}")


def _load_variant_names(models_file: Path, source: Path) -> dict[str, Any]:
    """Load models.json and return its variant table; missing file is fatal here."""
    if not models_file.is_file():
        raise ConfigError(
            f"{source}: models.json not found at {models_file} (referenced by the models section)"
        )
    data = json.loads(models_file.read_text())
    variants = data.get("variants")
    if not isinstance(variants, dict):
        raise ConfigError(f"{models_file}: missing a 'variants' mapping")
    return variants


def _parse_run(raw: Any, source: Path) -> RunSettings:
    """Validate the run section into RunSettings."""
    if raw is None:
        return RunSettings()
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: 'run' must be a mapping")
    unknown = set(raw) - _RUN_KEYS
    if unknown:
        raise ConfigError(f"{source}: unknown run keys: {sorted(unknown)}")
    checkpointer = raw.get("checkpointer", "sqlite")
    if checkpointer not in {"sqlite", "memory"}:
        raise ConfigError(
            f"{source}: run.checkpointer must be 'sqlite' or 'memory', got {checkpointer!r}"
        )
    db_path = raw.get("db_path", ".ngen-weave/checkpoints.db")
    if not isinstance(db_path, str):
        raise ConfigError(f"{source}: run.db_path must be a string path")
    max_retries = raw.get("max_retries", 3)
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ConfigError(f"{source}: run.max_retries must be a non-negative integer")
    retry_backoff_ms = raw.get("retry_backoff_ms", 1000)
    if (
        not isinstance(retry_backoff_ms, int)
        or isinstance(retry_backoff_ms, bool)
        or retry_backoff_ms <= 0
    ):
        raise ConfigError(f"{source}: run.retry_backoff_ms must be a positive integer")
    # An explicitly present run.budget must parse (an empty or null mapping is
    # itself invalid: at least one limit is required whenever the key exists).
    budget = _parse_budget(raw["budget"], source) if "budget" in raw else None
    return RunSettings(
        checkpointer=checkpointer,
        db_path=Path(db_path),
        max_retries=max_retries,
        retry_backoff_ms=retry_backoff_ms,
        budget=budget,
    )


def _parse_budget(raw: Any, source: Path) -> Budget:
    """Validate one run.budget mapping into a Budget.

    Both fields are optional but at least one must be present when the key is;
    unknown keys are rejected like every other config key.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: run.budget must be a mapping")
    unknown = set(raw) - _BUDGET_KEYS
    if unknown:
        raise ConfigError(f"{source}: unknown run.budget keys: {sorted(unknown)}")
    cost_usd = raw.get("cost_usd")
    steps = raw.get("steps")
    cost_ok = isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and cost_usd > 0
    steps_ok = isinstance(steps, int) and not isinstance(steps, bool) and steps > 0
    if cost_usd is not None and not cost_ok:
        raise ConfigError(f"{source}: run.budget.cost_usd must be a positive number")
    if steps is not None and not steps_ok:
        raise ConfigError(f"{source}: run.budget.steps must be a positive integer")
    if cost_usd is None and steps is None:
        raise ConfigError(f"{source}: run.budget requires at least one of 'cost_usd' or 'steps'")
    return Budget(cost_usd=None if cost_usd is None else float(cost_usd), steps=steps)


def load_config(path: Path | str, registry: dict[str, type[Workflow]]) -> ResolvedConfig:
    """Parse and validate the config file at path against a discovery map.

    Args:
        path: YAML or JSON config file (.yaml/.yml/.json chosen by extension).
        registry: Merged discovery map of class paths to workflow classes;
            config lookups hit it before falling back to imports.

    Returns:
        A fully validated ResolvedConfig; relative models_file and run.db_path
        stay relative so callers anchor them where they belong (config dir and
        working directory respectively).

    Raises:
        ConfigError: Unknown keys, unknown workflow path, unresolvable model
            binding keys, or binding values absent from models.json.
    """
    path = Path(path)
    source = path
    document = _load_document(path)
    unknown = set(document) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"{source}: unknown keys: {sorted(unknown)}")

    workflow_path = document.get("workflow")
    if not isinstance(workflow_path, str):
        raise ConfigError(f"{source}: 'workflow' must be a class-path string")
    workflow_cls = _resolve_workflow_class(workflow_path, registry)

    params = document.get("params") or {}
    if not isinstance(params, dict):
        raise ConfigError(f"{source}: 'params' must be a mapping")

    models = document.get("models") or {}
    if not isinstance(models, dict):
        raise ConfigError(f"{source}: 'models' must be a mapping")

    models_file_raw = document.get("models_file", "models.json")
    if not isinstance(models_file_raw, str):
        raise ConfigError(f"{source}: 'models_file' must be a string path")
    models_file = Path(models_file_raw)
    if not models_file.is_absolute():
        models_file = (path.parent / models_file).resolve()

    if models:
        variants = _load_variant_names(models_file, source)
        for key, variant in models.items():
            _resolve_workflow_class(key, registry)  # binding keys must name real classes
            if not isinstance(variant, str) or variant not in variants:
                raise ConfigError(
                    f"{source}: models binding {key!r} names variant {variant!r}, "
                    f"which is not in {models_file}"
                )

    return ResolvedConfig(
        workflow=workflow_cls,
        params=params,
        models=models,
        models_file=models_file,
        run=_parse_run(document.get("run"), source),
    )
