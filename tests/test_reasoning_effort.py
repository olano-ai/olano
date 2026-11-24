"""Reasoning-effort capability detection, gating, and config plumbing."""

import pytest

from olano.config.agent_config import AgentConfig
from olano.core import builder as builder_mod
from olano.models.reasoning import (
    gate_reasoning_kwargs,
    reasoning_effort_info,
    resolve_reasoning_effort,
    supports_reasoning_effort,
)

CODEX = "openai-codex:gpt-5.6-terra"
ANTHROPIC = "anthropic:claude-sonnet-4-6"
DEEPSEEK = "deepseek:deepseek-v4-pro"


class TestCapabilityDetection:
    def test_codex_model_reports_levels(self):
        info = reasoning_effort_info(CODEX)
        assert info is not None
        assert "low" in info["levels"]

    def test_deepseek_has_no_profile_support(self):
        assert reasoning_effort_info(DEEPSEEK) is None
        assert not supports_reasoning_effort(DEEPSEEK)

    def test_bare_provider_string_is_unsupported(self):
        assert reasoning_effort_info("openai-codex") is None


class TestResolvePolicy:
    def test_unset_defaults_to_low_for_codex(self):
        assert resolve_reasoning_effort(CODEX, None) == "low"

    def test_explicit_level_wins(self):
        assert resolve_reasoning_effort(CODEX, "high") == "high"

    def test_invalid_level_falls_back_to_default(self):
        assert resolve_reasoning_effort(CODEX, "ultra") == "low"

    def test_unset_stays_off_for_optional_thinking_providers(self):
        # Anthropic thinking is off unless requested; an implicit default
        # would silently switch it on.
        assert resolve_reasoning_effort(ANTHROPIC, None) is None

    def test_explicit_level_respected_for_anthropic(self):
        assert resolve_reasoning_effort(ANTHROPIC, "high") == "high"

    def test_unsupported_model_always_none(self):
        assert resolve_reasoning_effort(DEEPSEEK, "low") is None


class TestGate:
    def test_drops_effort_for_unsupported_provider(self):
        out = gate_reasoning_kwargs("deepseek", "deepseek-v4-pro", {"reasoning_effort": "low"})
        assert "reasoning_effort" not in out

    def test_keeps_and_normalizes_for_codex(self):
        out = gate_reasoning_kwargs("openai-codex", "gpt-5.6-terra", {"reasoning_effort": "LOW"})
        assert out["reasoning_effort"] == "low"

    def test_drops_invalid_level_even_when_supported(self):
        out = gate_reasoning_kwargs("openai-codex", "gpt-5.6-terra", {"reasoning_effort": "ultra"})
        assert "reasoning_effort" not in out

    def test_strips_raw_reasoning_dict_for_non_openai(self):
        out = gate_reasoning_kwargs("deepseek", "deepseek-v4-pro", {"reasoning": {"effort": "low"}})
        assert out == {}

    def test_unrelated_kwargs_untouched(self):
        out = gate_reasoning_kwargs("deepseek", "deepseek-v4-pro", {"temperature": 0.2})
        assert out == {"temperature": 0.2}


class TestConfigField:
    def test_field_roundtrips(self):
        cfg = AgentConfig(name="t", reasoning_effort="high")
        assert cfg.reasoning_effort == "high"
        assert AgentConfig(**cfg.model_dump(exclude={"workspace"})).reasoning_effort == "high"

    def test_default_is_none(self):
        assert AgentConfig(name="t").reasoning_effort is None


class TestBuilderInjection:
    def test_effort_injected_for_codex(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            builder_mod, "build_model", lambda model_str, **kw: seen.update(kw) or object()
        )
        builder_mod._build_model(CODEX, {}, None)
        assert seen.get("reasoning_effort") == "low"

    def test_explicit_config_value_injected(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            builder_mod, "build_model", lambda model_str, **kw: seen.update(kw) or object()
        )
        builder_mod._build_model(CODEX, {}, "high")
        assert seen.get("reasoning_effort") == "high"

    def test_nothing_injected_for_unsupported_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            builder_mod, "build_model", lambda model_str, **kw: seen.update(kw) or object()
        )
        builder_mod._build_model(DEEPSEEK, {}, "low")
        assert "reasoning_effort" not in seen

    def test_explicit_model_kwargs_win_over_default(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            builder_mod, "build_model", lambda model_str, **kw: seen.update(kw) or object()
        )
        builder_mod._build_model(CODEX, {"reasoning_effort": "medium"}, None)
        assert seen.get("reasoning_effort") == "medium"


# A model the installed package predates: its profile row (langchain-openai
# 1.7.0) marks it as reasoning but lists no effort levels, so the package half
# of the probe is silent for it. The catalog half must answer.
PACKAGE_SILENT = "openai:gpt-6.1-sol"
# A model the package does know, so a conflicting catalog ladder must lose.
PACKAGE_KNOWN = "openai:gpt-6-sol"


class TestCatalogFallback:
    """The synced catalog's ladder answers when the installed package is silent."""

    @pytest.fixture
    def seeded_catalog(self):
        from olano.catalog import models_registry as mr

        cat = mr.get_model_catalog()
        extra = [
            mr._normalize_model(
                {
                    "id": PACKAGE_SILENT,
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                    "reasoning": True,
                    "reasoningEfforts": ["low", "medium", "HIGH", "xhigh", "max", ""],
                    "reasoningDefault": "medium",
                }
            ),
            mr._normalize_model(
                {
                    "id": PACKAGE_KNOWN,
                    "provider": "openai",
                    "model": "gpt-6-sol",
                    "reasoning": True,
                    "reasoningEfforts": ["low"],
                    "reasoningDefault": "low",
                }
            ),
        ]
        mr._set_cache({**cat, "models": [*cat["models"], *extra]}, "test")
        yield
        mr.invalidate_cache()

    def test_package_silence_is_the_baseline(self):
        assert reasoning_effort_info(PACKAGE_SILENT) is None

    def test_catalog_ladder_fills_the_gap(self, seeded_catalog):
        info = reasoning_effort_info(PACKAGE_SILENT)
        assert info == {
            "levels": ["low", "medium", "high", "xhigh", "max"],
            "provider_default": "medium",
        }
        assert supports_reasoning_effort(PACKAGE_SILENT)

    def test_every_consumer_agrees(self, seeded_catalog):
        # Resolution and the kwarg gate read the same ladder as the picker, so
        # a level the picker offers is sent and one it does not is dropped.
        assert resolve_reasoning_effort(PACKAGE_SILENT, "xhigh") == "xhigh"
        assert resolve_reasoning_effort(PACKAGE_SILENT, None) == "low"
        assert resolve_reasoning_effort(PACKAGE_SILENT, "none") == "low"
        assert gate_reasoning_kwargs("openai", "gpt-6.1-sol", {"reasoning_effort": "high"}) == {
            "reasoning_effort": "high"
        }
        assert gate_reasoning_kwargs("openai", "gpt-6.1-sol", {"reasoning_effort": "none"}) == {}

    def test_package_profile_wins_over_the_catalog(self, seeded_catalog):
        info = reasoning_effort_info(PACKAGE_KNOWN)
        assert info is not None
        assert "none" in info["levels"] and "max" in info["levels"]

    def test_no_ladder_anywhere_stays_unsupported(self, seeded_catalog):
        assert reasoning_effort_info(DEEPSEEK) is None

    def test_a_catalog_update_is_visible_without_a_restart(self, seeded_catalog):
        from olano.catalog import models_registry as mr

        assert reasoning_effort_info(PACKAGE_SILENT) is not None
        mr.invalidate_cache()
        assert reasoning_effort_info(PACKAGE_SILENT) is None
