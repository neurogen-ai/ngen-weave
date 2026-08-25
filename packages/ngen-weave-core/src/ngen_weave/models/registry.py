"""Model variants loaded from project-root models.json.

LiteLLMProvider is the only module importing litellm; adapters translate, never extend.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from ngen_weave.errors import ConfigError, DataError, InfraError
from ngen_weave.models.provider import Completion


class ModelRegistry:
    """Loads models.json and resolves variant call kwargs."""

    def __init__(self, data: dict, path: Path) -> None:
        """Validate raw models.json content against its source path."""
        self.path = path
        self.default = data.get("defaultVariant")
        variants = data.get("variants")
        if not isinstance(variants, dict) or not variants:
            raise ConfigError(f"{path}: 'variants' must be a non-empty mapping")
        if not isinstance(self.default, str) or self.default not in variants:
            raise ConfigError(f"{path}: defaultVariant must name an entry in 'variants'")
        for name, entry in variants.items():
            if not isinstance(entry, dict) or "model" not in entry:
                raise ConfigError(f"{path}: variant {name!r} needs at least a 'model' key")
        self.variants = variants

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        """Parse models.json from disk; parse failures are ConfigError."""
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read models file {path}: {exc}") from exc
        return cls(data, path)

    @property
    def default_variant(self) -> str:
        """The variant used when neither config bindings nor callers name one."""
        return self.default

    def kwargs(self, variant: str | None = None) -> dict:
        """Call kwargs for variant (a copy; callers may mutate freely)."""
        name = variant if variant is not None else self.default
        entry = self.variants.get(name)
        if entry is None:
            raise ConfigError(f"{self.path}: unknown variant {name!r}")
        return dict(entry)


class LiteLLMProvider:
    """CompletionProvider backed by litellm.acompletion (structural protocol impl).

    Transport-level call failures raise InfraError (retryable); replies that
    cannot be parsed into text and usage accounting raise DataError. Auth
    stays in provider environment variables; nothing here reads keys.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    async def complete(self, messages: list[dict], *, variant: str | None = None) -> Completion:
        """Run one completion under the named (or default) variant."""
        import litellm

        kwargs = self.registry.kwargs(variant)
        try:
            response = await litellm.acompletion(messages=messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            raise InfraError(f"model call failed for variant {variant!r}: {exc}") from exc

        try:
            choice = response.choices[0]
            text: str | None = choice.message.content
            usage: Any = response.usage
            tokens_in_context = int(usage.prompt_tokens)
            tokens_total = int(usage.total_tokens)
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise DataError(f"malformed completion reply for variant {variant!r}: {exc}") from exc
        if text is None:
            raise DataError(f"completion reply for variant {variant!r} carried no message text")

        cost = 0.0  # unknown-price models still run; they just report zero cost
        with suppress(Exception):  # pricing metadata is advisory only
            cost = float(litellm.completion_cost(completion_response=response))
        return Completion(
            text=text,
            tokens_in_context=tokens_in_context,
            tokens_total=tokens_total,
            cost_usd=cost,
        )
