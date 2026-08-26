"""Tests for model variant resolution and the LiteLLM adapter translation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ngen_weave.errors import ConfigError, DataError, InfraError, ProviderError
from ngen_weave.models.registry import LiteLLMProvider, ModelRegistry

MODELS = {
    "defaultVariant": "sonnet",
    "variants": {
        "sonnet": {"model": "test/sonnet", "temperature": 0.2},
        "haiku": {"model": "test/haiku"},
    },
}


@pytest.fixture()
def models_file(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(MODELS))
    return path


class TestModelRegistry:
    def test_load_reads_default_and_variants(self, models_file: Path) -> None:
        registry = ModelRegistry.load(models_file)
        assert registry.default_variant == "sonnet"

    def test_kwargs_defaults_to_default_variant(self, models_file: Path) -> None:
        assert ModelRegistry.load(models_file).kwargs() == {
            "model": "test/sonnet",
            "temperature": 0.2,
        }

    def test_kwargs_override_picks_named_variant(self, models_file: Path) -> None:
        assert ModelRegistry.load(models_file).kwargs("haiku") == {"model": "test/haiku"}

    def test_kwargs_returns_mutable_copies(self, models_file: Path) -> None:
        registry = ModelRegistry.load(models_file)
        registry.kwargs()["temperature"] = 9.9
        assert registry.kwargs()["temperature"] == 0.2

    def test_unknown_variant_is_config_error(self, models_file: Path) -> None:
        with pytest.raises(ConfigError, match="unknown variant 'turbo'"):
            ModelRegistry.load(models_file).kwargs("turbo")

    def test_missing_models_file_is_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot read"):
            ModelRegistry.load(tmp_path / "absent.json")

    @pytest.mark.parametrize(
        "data, message",
        [
            ({}, "variants"),
            ({"defaultVariant": "x", "variants": {}}, "variants"),
            (
                {"defaultVariant": "missing", "variants": {"a": {"model": "m"}}},
                "defaultVariant",
            ),
            ({"defaultVariant": "a", "variants": {"a": {}}}, "'model'"),
        ],
    )
    def test_invalid_documents_are_config_errors(
        self, tmp_path: Path, data: dict, message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            ModelRegistry(data, tmp_path / "models.json")

    def _registry(self, tmp_path: Path, variants: dict) -> ModelRegistry:
        data = {"defaultVariant": "local", "variants": {"local": variants}}
        return ModelRegistry(data, tmp_path / "models.json")

    def test_api_key_composes_litellm_prefix(self, tmp_path: Path) -> None:
        kwargs = self._registry(
            tmp_path,
            {
                "model": "qwen3:8b",
                "api": "openai-compatible",
                "api_base": "http://localhost:8080/v1",
            },
        ).kwargs()
        assert kwargs["model"] == "openai/qwen3:8b"
        assert "api" not in kwargs

    def test_api_key_with_prefixed_model_passes_through(self, tmp_path: Path) -> None:
        kwargs = self._registry(
            tmp_path, {"model": "openai/qwen3:8b", "api": "openai-compatible"}
        ).kwargs()
        assert kwargs["model"] == "openai/qwen3:8b"

    def test_legacy_prefixed_model_without_api_is_unchanged(self, models_file: Path) -> None:
        assert ModelRegistry.load(models_file).kwargs("haiku") == {"model": "test/haiku"}

    def test_unknown_api_value_is_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(
            ConfigError, match="variant 'local' has unknown api 'ollama'"
        ) as excinfo:
            self._registry(tmp_path, {"model": "m", "api": "ollama"})
        assert "accepted values" in str(excinfo.value)

    def test_api_prefixes_local_gguf_name(self, tmp_path: Path) -> None:
        kwargs = self._registry(
            tmp_path,
            {
                "model": "ggml-org/GLM-4.7-Flash-GGUF:Q8_0",
                "api": "openai-compatible",
                "api_base": "http://localhost:8080/v1",
                "api_key": "dummy",
            },
        ).kwargs()
        assert kwargs["model"] == "openai/ggml-org/GLM-4.7-Flash-GGUF:Q8_0"
        assert "api" not in kwargs


def _fake_response(content="hello", prompt=11, total=33):
    """Stand-in for a litellm ModelResponse; only accessed attributes exist."""
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, total_tokens=total),
    )


@pytest.mark.live
class TestLiteLLMProviderTranslation:
    """Exercises the adapter's translation logic against a mocked acompletion.

    No network: both the call and pricing are monkeypatched. Marked live so
    the default suite runs provider-free; run explicitly with
    `-m live` when touching the adapter.
    """

    async def test_usage_and_cost_mapping(self, models_file: Path, monkeypatch) -> None:
        import litellm

        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return _fake_response(content="hi", prompt=11, total=33)

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        monkeypatch.setattr(litellm, "completion_cost", lambda **_: 0.0033)

        provider = LiteLLMProvider(ModelRegistry.load(models_file))
        done = await provider.complete([{"role": "user", "content": "q"}], variant="haiku")

        assert captured["model"] == "test/haiku"
        assert captured["messages"] == [{"role": "user", "content": "q"}]
        assert done.text == "hi"
        assert done.tokens_in_context == 11
        assert done.tokens_total == 33
        assert done.cost_usd == pytest.approx(0.0033)

    async def test_transport_failure_raises_infra_error(
        self, models_file: Path, monkeypatch
    ) -> None:
        import litellm

        async def boom(**_):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(litellm, "acompletion", boom)
        provider = LiteLLMProvider(ModelRegistry.load(models_file))
        with pytest.raises(InfraError, match="connection reset"):
            await provider.complete([], variant="sonnet")

    async def test_unparseable_reply_raises_data_error(
        self, models_file: Path, monkeypatch
    ) -> None:
        import litellm

        class Empty:
            choices: list = []

        async def hollow(**_):
            return Empty()

        monkeypatch.setattr(litellm, "acompletion", hollow)
        provider = LiteLLMProvider(ModelRegistry.load(models_file))
        with pytest.raises(DataError, match="malformed"):
            await provider.complete([], variant="sonnet")

    async def test_unknown_price_reports_zero_cost(self, models_file: Path, monkeypatch) -> None:
        import litellm

        async def fine(**_):
            return _fake_response()

        def no_pricing(**_):
            raise ValueError("unknown model")

        monkeypatch.setattr(litellm, "acompletion", fine)
        monkeypatch.setattr(litellm, "completion_cost", no_pricing)
        provider = LiteLLMProvider(ModelRegistry.load(models_file))
        done = await provider.complete([], variant="sonnet")
        assert done.cost_usd == 0.0


class TestPermanentFailureClassification:
    """Permanent provider failures abort on the first attempt as ProviderError.

    No network and no @pytest.mark.live marker: acompletion is stubbed to
    raise the litellm exception type directly.
    """

    async def _complete_raising(self, models_file: Path, monkeypatch, exc: Exception):
        import litellm

        async def boom(**_):
            raise exc

        monkeypatch.setattr(litellm, "acompletion", boom)
        provider = LiteLLMProvider(ModelRegistry.load(models_file))
        return await provider.complete([], variant="sonnet")

    async def test_authentication_error_is_provider_error(
        self, models_file: Path, monkeypatch
    ) -> None:
        import litellm.exceptions

        exc = litellm.exceptions.AuthenticationError("bad key", "openai", "test/sonnet")
        with pytest.raises(ProviderError) as info:
            await self._complete_raising(models_file, monkeypatch, exc)
        assert "authentication rejected" in str(info.value)
        assert "'sonnet'" in str(info.value)

    @pytest.mark.parametrize(
        "exc_type, phrase",
        [
            ("NotFoundError", "model or endpoint not found"),
            ("APIConnectionError", "could not reach the provider endpoint"),
        ],
    )
    async def test_permanent_errors_are_provider_errors(
        self, models_file: Path, monkeypatch, exc_type: str, phrase: str
    ) -> None:
        import litellm.exceptions

        exc = getattr(litellm.exceptions, exc_type)("boom", "test/sonnet", "openai")
        with pytest.raises(ProviderError, match=re.escape(phrase)):
            await self._complete_raising(models_file, monkeypatch, exc)

    async def test_generic_runtime_error_still_infra_error(
        self, models_file: Path, monkeypatch
    ) -> None:
        with pytest.raises(InfraError, match="connection reset"):
            await self._complete_raising(models_file, monkeypatch, RuntimeError("connection reset"))
