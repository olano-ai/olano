"""Picking a model an install can actually run.

AgentFather used to create agents blind: nothing in the admin toolkit could say
which providers were usable, so every new agent inherited
``anthropic:claude-sonnet-4-6`` and failed on its first turn wherever there was
no Anthropic key.
"""

from __future__ import annotations

import pytest

from olano.admin import models as admin_models


def row(
    model_id: str,
    *,
    cost_class: str = "metered",
    tier: str | None = None,
    price_input: float | None = None,
    price_output: float | None = None,
    default_for_provider: bool = False,
    recommended: bool = False,
    deprecated: bool = False,
) -> dict:
    return {
        "id": model_id,
        "model": model_id.split(":", 1)[-1],
        "cost_class": cost_class,
        "tier": tier,
        "price_input": price_input,
        "price_output": price_output,
        "default_for_provider": default_for_provider,
        "recommended": recommended,
        "deprecated": deprecated,
    }


# ── Cost ranking ──────────────────────────────────────────────────────────────


def test_subscription_and_local_models_outrank_every_metered_one():
    """No per-token charge beats any price, however small."""
    ranked = admin_models._rank(
        [
            row("openai:gpt-nano", tier="fast", price_input=0.05, price_output=0.4),
            row("openai-codex:gpt-5.5", cost_class="included", tier="flagship"),
            row("ollama:qwen3", cost_class="included", tier="local"),
        ]
    )

    assert [r["id"] for r in ranked][:2] == ["ollama:qwen3", "openai-codex:gpt-5.5"]


def test_metered_models_rank_by_blended_price():
    ranked = admin_models._rank(
        [
            row("p:expensive", tier="standard", price_input=15.0, price_output=75.0),
            row("p:cheap", tier="standard", price_input=0.1, price_output=0.4),
            row("p:mid", tier="standard", price_input=3.0, price_output=15.0),
        ]
    )

    assert [r["id"] for r in ranked] == ["p:cheap", "p:mid", "p:expensive"]


def test_a_model_with_no_price_is_never_treated_as_free():
    """The rule that matters most: an unlisted flagship must not win."""
    ranked = admin_models._rank(
        [
            row("p:unknown", cost_class="unknown"),
            row("p:cheap", tier="fast", price_input=0.05, price_output=0.4),
        ]
    )

    assert [r["id"] for r in ranked] == ["p:cheap", "p:unknown"]


def test_a_brand_new_subscription_model_ranks_by_tier_not_by_name():
    """The user's real case: a Codex model too new for the catalog — no price,
    no tier. It must beat the flagship and every metered model, but not a model
    the catalog actually knows is cheap."""
    ranked = admin_models._rank(
        [
            row(
                "openai-codex:gpt-5.5",
                cost_class="included",
                tier="flagship",
                default_for_provider=True,
            ),
            row("openai-codex:gpt-5.6-luna", cost_class="included", tier=None),
            row("openai-codex:gpt-5.4-mini", cost_class="included", tier="fast"),
            row("anthropic:opus", tier="flagship", price_input=15.0, price_output=75.0),
        ]
    )

    assert [r["id"] for r in ranked] == [
        "openai-codex:gpt-5.4-mini",
        "openai-codex:gpt-5.6-luna",
        "openai-codex:gpt-5.5",
        "anthropic:opus",
    ]


def test_ranking_is_stable_regardless_of_input_order():
    """A live API's ordering must not change which model gets recommended."""
    rows = [
        row("p:b", tier="fast", price_input=1.0, price_output=1.0),
        row("p:a", tier="fast", price_input=1.0, price_output=1.0),
    ]
    assert [r["id"] for r in admin_models._rank(rows)] == ["p:a", "p:b"]
    assert [r["id"] for r in admin_models._rank(list(reversed(rows)))] == ["p:a", "p:b"]


def test_deprecated_models_are_not_offered():
    ranked = admin_models._rank(
        [
            row("p:old", cost_class="included", tier="fast", deprecated=True),
            row("p:new", tier="standard", price_input=1.0, price_output=1.0),
        ]
    )

    assert [r["id"] for r in ranked] == ["p:new"]


def test_cost_rank_is_assigned_in_order():
    ranked = admin_models._rank(
        [row("p:a", tier="fast", price_input=5.0), row("p:b", tier="fast", price_input=1.0)]
    )
    assert [(r["id"], r["cost_rank"]) for r in ranked] == [("p:b", 0), ("p:a", 1)]


# ── Provider connection ───────────────────────────────────────────────────────


def test_a_key_in_the_environment_counts_as_connected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = admin_models.provider_connection("anthropic", set())
    assert result["connected"] is True
    assert result["auth"] == "api_key"


def test_a_key_in_the_vault_counts_as_connected(monkeypatch):
    """Secrets are no longer hydrated into os.environ, so env alone undercounts."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = admin_models.provider_connection("anthropic", {"ANTHROPIC_API_KEY"})

    assert result["connected"] is True
    assert "vault" in result["detail"]


def test_no_key_anywhere_is_reported_with_the_name_to_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = admin_models.provider_connection("anthropic", set())

    assert result["connected"] is False
    assert "ANTHROPIC_API_KEY" in result["detail"]


def test_codex_connects_by_oauth_not_by_env_var(monkeypatch):
    monkeypatch.setattr(
        "olano.models.codex_auth.auth_status", lambda: {"logged_in": True}, raising=False
    )

    result = admin_models.provider_connection("openai-codex", set())

    assert result == {
        "connected": True,
        "auth": "oauth",
        "env_var": None,
        "detail": "connected via ChatGPT/Codex OAuth",
    }


def test_an_unknown_provider_is_not_connected():
    assert admin_models.provider_connection("nope", set())["connected"] is False


# ── Model resolution for new agents ───────────────────────────────────────────


def test_no_model_asked_for_picks_the_cheapest_connected(monkeypatch):
    from olano.admin import agents as admin_agents

    monkeypatch.setattr(admin_models, "cheapest_model", lambda: "openai-codex:gpt-5.4-mini")
    monkeypatch.setattr(
        admin_agents, "cheapest_model", lambda: "openai-codex:gpt-5.4-mini", raising=False
    )

    model, note = admin_agents.resolve_model(None)

    assert model == "openai-codex:gpt-5.4-mini"
    assert note and "cheapest connected" in note


def test_an_explicit_model_on_a_disconnected_provider_is_kept_with_a_warning(monkeypatch):
    """Substituting a deliberate choice is worse than warning about it."""
    from olano.admin import agents as admin_agents

    monkeypatch.setattr(
        admin_models,
        "model_provider_connected",
        lambda m: (False, "ANTHROPIC_API_KEY is not set"),
    )

    model, note = admin_agents.resolve_model("anthropic:claude-opus-4-6")

    assert model == "anthropic:claude-opus-4-6"
    assert note and "not connected" in note


def test_an_explicit_model_on_a_connected_provider_passes_through_silently(monkeypatch):
    from olano.admin import agents as admin_agents

    monkeypatch.setattr(admin_models, "model_provider_connected", lambda m: (True, "ready"))

    assert admin_agents.resolve_model("openai:gpt-5.4") == ("openai:gpt-5.4", None)


def test_nothing_connected_falls_back_and_says_so(monkeypatch):
    from olano.admin import agents as admin_agents

    monkeypatch.setattr(admin_models, "cheapest_model", lambda: None)

    model, note = admin_agents.resolve_model(None)

    assert model is None  # AgentConfig's own default applies
    assert note and "No provider is connected" in note


# ── The assembled payload ─────────────────────────────────────────────────────


def test_list_models_shape():
    out = admin_models.list_models(connected_only=False)

    assert set(out) >= {"providers", "connected", "suggested_cheap", "suggested_default", "note"}
    assert isinstance(out["providers"], list)
    for provider in out["providers"]:
        assert {"key", "name", "connected", "auth", "detail"} <= set(provider)
        if provider["connected"]:
            assert isinstance(provider["models"], list)


def test_connected_only_hides_unusable_providers(monkeypatch):
    monkeypatch.setattr(
        admin_models,
        "provider_connection",
        lambda key, names=None: {
            "connected": key == "openai",
            "auth": "api_key",
            "env_var": None,
            "detail": "",
        },
    )

    out = admin_models.list_models(connected_only=True)

    assert [p["key"] for p in out["providers"]] == ["openai"]


@pytest.mark.parametrize("connected_only", [True, False])
def test_list_models_never_raises_without_a_catalog(monkeypatch, connected_only):
    """A catalog miss degrades to bare provider lists, it does not break the tool."""
    monkeypatch.setattr(
        "olano.catalog.models_registry.get_model_catalog",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    out = admin_models.list_models(connected_only=connected_only)

    assert "providers" in out
