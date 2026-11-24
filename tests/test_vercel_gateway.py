"""Tests for the Vercel AI Gateway provider (OpenAI-compatible)."""

from __future__ import annotations

import pytest


def test_vercel_provider_registered() -> None:
    from olano.models import PROVIDERS

    info = PROVIDERS["vercel"]
    assert info.env_var == "AI_GATEWAY_API_KEY"
    assert info.package == "langchain-openai"
    # creator/model addressing — model names carry a slash
    assert all("/" in m for m in info.default_models)


def test_parse_model_string_handles_slash_model() -> None:
    from olano.models import parse_model_string

    assert parse_model_string("vercel:anthropic/claude-sonnet-4-6") == (
        "vercel",
        "anthropic/claude-sonnet-4-6",
    )


def test_vercel_provider_builds_chat_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    import langchain_openai

    from olano.models import build_model

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "sk-gw-test")
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    model = build_model("vercel:anthropic/claude-sonnet-4-6")

    assert isinstance(model, FakeChatOpenAI)
    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["api_key"].get_secret_value() == "sk-gw-test"
    assert captured["base_url"] == "https://ai-gateway.vercel.sh/v1"


def test_vercel_provider_honors_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import langchain_openai

    from olano.models import build_model

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "sk-gw-test")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://gw.internal/v1")

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    build_model("vercel:openai/gpt-5.4")

    assert captured["base_url"] == "https://gw.internal/v1"


def test_vercel_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.models import build_model

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)

    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY"):
        build_model("vercel:openai/gpt-5.4")
