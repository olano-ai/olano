"""Tests for global cognition system settings + budget_gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from olano.config.agent_config import AgentConfig, BudgetConfig
from olano.config.home import get_olano_paths, load_home_config, save_home_config
from olano.config.system_settings import (
    DEFAULT_INTENSITY,
    FEATURES,
    INTENSITIES,
    WEB_SEARCH_SOURCES,
    MediaSettings,
    PlaygroundSettings,
    SystemSettings,
    WebSearchSettings,
    auto_approve_enabled,
    autonomy_enabled,
    canonical_feature,
    clean_message_windows,
    cortex_enabled,
    effective_intensity,
    feature_enabled,
    load_media_settings,
    load_playground_settings,
    load_rate_limit_settings,
    load_system_settings,
    load_web_search_settings,
    playground_enabled,
    set_autonomy,
    set_cognition_feature,
    set_cognition_model,
    set_cortex_master,
    set_intensity,
    set_media_settings,
    set_playground_enabled,
    set_rate_limits,
    set_web_search_settings,
)
from olano.pulse.cost_tracker import budget_gate

# ----------------------------------------------------------------- budget_gate


def test_budget_gate_allows_when_no_limits(tmp_path: Path) -> None:
    ok, msg = budget_gate("agent", tmp_path, BudgetConfig())
    assert ok is True
    assert msg == ""


def test_budget_gate_blocks_when_daily_zero(tmp_path: Path) -> None:
    ok, msg = budget_gate("agent", tmp_path, BudgetConfig(daily_usd=0.0))
    assert ok is False
    assert "budget" in msg.lower()


def test_budget_gate_never_raises(tmp_path: Path) -> None:
    # A non-existent nested dir is created lazily; a bad budget object degrades to allow.
    ok, _ = budget_gate("agent", tmp_path / "deep" / "nested", BudgetConfig())
    assert ok is True


# ------------------------------------------------------------- system settings


def test_load_defaults_when_no_block() -> None:
    settings = load_system_settings()
    assert isinstance(settings, SystemSettings)
    # The Cortex engine is opt-in, and a fleet that never chose a cadence gets
    # the cheapest one.
    assert settings.enabled is False
    assert settings.intensity == "relaxed"
    assert settings.features.self_learning is True
    assert settings.fast_model == "openai-codex:gpt-5.4-mini"
    assert settings.deep_model == "openai-codex:gpt-5.5"


def test_load_reads_cognition_block() -> None:
    paths = get_olano_paths()
    paths.config_path.write_text(
        yaml.dump(
            {
                "default_agent": "merli",
                "cognition": {
                    "enabled": True,
                    "features": {"pulse": False, "self_learning": True},
                    "fast_model": "anthropic:claude-haiku-4-5",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = load_system_settings()
    assert settings.features.pulse is False
    assert settings.features.self_learning is True


def test_no_feature_runs_until_the_engine_is_turned_on() -> None:
    """A fresh box: the engine is off, so every feature group is off with it."""
    for feature in FEATURES:
        assert feature_enabled(feature) is False


def test_features_default_on_once_the_engine_is() -> None:
    """The feature toggles themselves still default on: turning the engine on
    for the fleet starts every group without a second visit per tile."""
    set_cortex_master(True)
    for feature in FEATURES:
        assert feature_enabled(feature) is True


def test_feature_enabled_master_switch_off() -> None:
    settings = SystemSettings(enabled=False)
    assert feature_enabled("pulse", settings=settings) is False


def test_feature_enabled_global_toggle_off() -> None:
    settings = SystemSettings(enabled=True)
    settings.features.skills_optimization = False
    assert feature_enabled("skills_optimization", settings=settings) is False
    # Legacy alias resolves to the same canonical feature.
    assert feature_enabled("learned_skills", settings=settings) is False
    assert feature_enabled("pulse", settings=settings) is True


def test_feature_enabled_unknown_raises() -> None:
    with pytest.raises(ValueError):
        feature_enabled("not_a_feature")


# ----------------------------------------------------------------- autonomy + intensity


def test_seven_named_features_present() -> None:
    # The user-facing Cortex tiles must each map to a feature toggle.
    for f in (
        "self_learning",
        "brainstorming",
        "idea_generation",
        "prototyping",
        "skills_optimization",
        "workspace_optimization",
        "memory_optimization",
    ):
        assert f in FEATURES


def test_defaults_are_engine_off_autonomous_and_relaxed() -> None:
    s = SystemSettings()
    assert s.enabled is False
    assert s.autonomous is True
    assert s.intensity == "relaxed" == DEFAULT_INTENSITY
    # Autonomy needs the engine: off by default, on the moment the engine is.
    assert autonomy_enabled(settings=s) is False
    assert autonomy_enabled(settings=SystemSettings(enabled=True)) is True


def test_intensity_coerces_unknown_to_default() -> None:
    # A malformed intensity must not blow up the whole cognition block.
    s = SystemSettings.model_validate({"intensity": "turbo"})
    assert s.intensity == "relaxed"


def test_an_unknown_intensity_resolves_to_the_default_everywhere(
    base_config: AgentConfig,
) -> None:
    """The settings model, the per-agent resolver and the orchestrator tables
    agree on one fallback, so a typo can never land on the busiest cadence."""
    from olano.cognition.orchestrator import cadence_for, deep_cap, distill_interval_seconds

    base_config.cognition.intensity = "ludicrous"
    assert effective_intensity(base_config) == DEFAULT_INTENSITY
    assert deep_cap("ludicrous") == deep_cap(DEFAULT_INTENSITY)
    assert distill_interval_seconds("ludicrous") == distill_interval_seconds(DEFAULT_INTENSITY)
    assert cadence_for("pulse", "ludicrous") == cadence_for("pulse", DEFAULT_INTENSITY)


def test_legacy_feature_aliases_resolve() -> None:
    assert canonical_feature("learned_skills") == "skills_optimization"
    assert canonical_feature("ideas_brainstorming_prototyping") == "idea_generation"


def test_autonomy_enabled_respects_master_switch() -> None:
    assert autonomy_enabled(settings=SystemSettings(enabled=False)) is False
    assert autonomy_enabled(settings=SystemSettings(autonomous=False)) is False


def test_autonomy_enabled_per_agent_override(base_config: AgentConfig) -> None:
    on = SystemSettings(enabled=True)
    assert autonomy_enabled(base_config, settings=on) is True
    base_config.cognition.autonomous = False
    assert autonomy_enabled(base_config, settings=on) is False
    base_config.cognition.autonomous = True
    base_config.cognition.enabled = False
    assert autonomy_enabled(base_config, settings=on) is False


def test_effective_intensity_per_agent_override(base_config: AgentConfig) -> None:
    assert effective_intensity(base_config) in INTENSITIES
    base_config.cognition.intensity = "relaxed"
    assert effective_intensity(base_config) == "relaxed"


def test_set_autonomy_and_intensity_round_trip() -> None:
    set_intensity("balanced")
    set_autonomy(False, master=False)
    s = load_system_settings()
    assert s.intensity == "balanced"
    assert s.autonomous is False
    # Re-enabling autonomy also clears the master kill-switch.
    set_autonomy(True)
    s2 = load_system_settings()
    assert s2.autonomous is True
    assert s2.enabled is True


def test_set_intensity_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        set_intensity("ludicrous")


def test_feature_enabled_respects_per_agent_override(base_config: AgentConfig) -> None:
    # base_config has a default CognitionConfig (all overrides None ⇒ inherit global).
    set_cortex_master(True)
    assert feature_enabled("pulse", base_config) is True
    # Explicit per-agent disable wins.
    base_config.cognition.pulse = False
    assert feature_enabled("pulse", base_config) is False
    # Master per-agent disable wins for everything.
    base_config.cognition.pulse = None
    base_config.cognition.enabled = False
    assert feature_enabled("self_learning", base_config) is False


def test_one_agent_opts_in_on_a_box_that_never_touched_the_engine(
    base_config: AgentConfig,
) -> None:
    """The default arrangement now: nothing in config.yaml, Cortex off for the
    fleet, and an agent that asks for it gets it (with the relaxed cadence)."""
    assert cortex_enabled(base_config) is False
    base_config.cognition.enabled = True
    assert cortex_enabled(base_config) is True
    assert autonomy_enabled(base_config) is True
    assert feature_enabled("pulse", base_config) is True
    assert effective_intensity(base_config) == "relaxed"


# ------------------------------------------------- overrides that turn things ON
# The gates used to short-circuit on the global value before reading the agent
# block, so an override could only ever subtract. "Cortex off across the fleet,
# on for the one agent I'm piloting" — the ordinary reason to have a per-agent
# override at all — was unreachable, while the dashboard kept offering a
# three-state control that promised it.


def test_per_agent_on_overrides_a_global_off(base_config: AgentConfig) -> None:
    """Off for all agents, on for one."""
    off = SystemSettings(enabled=False, autonomous=False, auto_approve=False)

    # Nothing set per-agent ⇒ inherit the fleet-wide off.
    assert cortex_enabled(base_config, settings=off) is False
    assert autonomy_enabled(base_config, settings=off) is False
    assert auto_approve_enabled(base_config, settings=off) is False
    assert feature_enabled("pulse", base_config, settings=off) is False

    # This one agent opts in.
    base_config.cognition.enabled = True
    base_config.cognition.autonomous = True
    base_config.cognition.auto_approve = True
    assert cortex_enabled(base_config, settings=off) is True
    assert autonomy_enabled(base_config, settings=off) is True
    assert auto_approve_enabled(base_config, settings=off) is True
    # Feature toggles still resolve on their own tier — the global feature
    # defaults are on, and the agent's master switch no longer blocks them.
    assert feature_enabled("pulse", base_config, settings=off) is True


def test_per_agent_feature_on_overrides_a_global_feature_off(base_config: AgentConfig) -> None:
    settings = SystemSettings(enabled=True)
    settings.features.idea_generation = False
    assert feature_enabled("idea_generation", base_config, settings=settings) is False
    base_config.cognition.idea_generation = True
    assert feature_enabled("idea_generation", base_config, settings=settings) is True
    # Its neighbours are untouched.
    assert feature_enabled("pulse", base_config, settings=settings) is True


def test_inherit_follows_the_global_whichever_way_it_points(base_config: AgentConfig) -> None:
    """None means inherit, not 'on' and not 'off'."""
    assert base_config.cognition.enabled is None
    assert cortex_enabled(base_config, settings=SystemSettings(enabled=True)) is True
    assert cortex_enabled(base_config, settings=SystemSettings(enabled=False)) is False

    base_config.cognition.pulse = None
    on, off = SystemSettings(enabled=True), SystemSettings(enabled=True)
    off.features.pulse = False
    assert feature_enabled("pulse", base_config, settings=on) is True
    assert feature_enabled("pulse", base_config, settings=off) is False


def test_per_agent_off_still_wins_over_a_global_on(base_config: AgentConfig) -> None:
    """The direction that always worked, and that the customer-service archetype
    relies on (it ships `cognition.enabled: false` deliberately)."""
    on = SystemSettings(enabled=True, autonomous=True, auto_approve=True)
    base_config.cognition.enabled = False
    assert cortex_enabled(base_config, settings=on) is False
    assert autonomy_enabled(base_config, settings=on) is False
    assert auto_approve_enabled(base_config, settings=on) is False
    assert feature_enabled("pulse", base_config, settings=on) is False


def test_agent_cannot_opt_past_the_deployment_entitlement(
    base_config: AgentConfig, monkeypatch
) -> None:
    """A signed deployment can switch Cortex off for the whole box. That is a
    licensing gate, not a preference — no per-agent override may escape it."""
    import olano.config.system_settings as ss

    monkeypatch.setattr(ss, "_deployment_cortex_enabled", lambda: False)
    base_config.cognition.enabled = True
    base_config.cognition.autonomous = True
    base_config.cognition.pulse = True
    on = SystemSettings(enabled=True, autonomous=True)
    assert cortex_enabled(base_config, settings=on) is False
    assert autonomy_enabled(base_config, settings=on) is False
    assert feature_enabled("pulse", base_config, settings=on) is False


# ----------------------------------------------------------------- playground


def test_playground_disabled_by_default() -> None:
    # Opt-in: a fresh home has no playground block, so it resolves to off.
    assert PlaygroundSettings().enabled is False
    assert load_playground_settings().enabled is False
    assert playground_enabled() is False


def test_set_playground_enabled_round_trip() -> None:
    set_playground_enabled(True)
    assert playground_enabled() is True
    assert load_playground_settings().enabled is True
    set_playground_enabled(False)
    assert playground_enabled() is False


def test_set_playground_preserves_other_keys() -> None:
    # Seed config.yaml with HomeConfig-style content + a cognition block first.
    save_home_config(load_home_config())
    set_cognition_feature("pulse", False)
    set_playground_enabled(True)

    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert raw["playground"]["enabled"] is True
    # Sibling blocks survive the merge-write.
    assert "default_agent" in raw
    assert raw["cognition"]["features"]["pulse"] is False


def test_load_playground_reads_block() -> None:
    paths = get_olano_paths()
    paths.config_path.write_text(
        yaml.dump({"default_agent": "merli", "playground": {"enabled": True}}),
        encoding="utf-8",
    )
    assert playground_enabled() is True


# ------------------------------------------------------ home-wide rate limits


def test_rate_limits_unlimited_by_default() -> None:
    rl = load_rate_limit_settings()
    assert rl.messages.to_limits() == []
    assert rl.per_platform == {}


def test_clean_message_windows_keeps_only_positive_ints() -> None:
    assert clean_message_windows({"per_minute": 30, "per_hour": 0, "per_day": None}) == {
        "per_minute": 30
    }
    # bool is an int subclass but never a valid cap; negatives dropped.
    assert clean_message_windows({"per_minute": True, "per_hour": -5, "per_day": 10}) == {
        "per_day": 10
    }
    assert clean_message_windows("nonsense") == {}


def test_set_rate_limits_round_trip_and_drops_empties() -> None:
    set_rate_limits(
        {"per_minute": 30, "per_hour": 0, "per_day": None},
        {"telegram": {"per_minute": 20}, "slack": {}, "dashboard": {"per_hour": 2000}},
    )
    rl = load_rate_limit_settings()
    assert rl.messages.model_dump(exclude_none=True) == {"per_minute": 30}
    dumped = {k: v.model_dump(exclude_none=True) for k, v in rl.per_platform.items()}
    # `slack: {}` (all-empty) is dropped; the others survive.
    assert dumped == {"telegram": {"per_minute": 20}, "dashboard": {"per_hour": 2000}}


def test_set_rate_limits_preserves_other_blocks() -> None:
    save_home_config(load_home_config())
    set_cognition_feature("pulse", False)
    set_rate_limits({"per_minute": 15}, {})

    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert raw["rate_limits"]["messages"] == {"per_minute": 15}
    assert "default_agent" in raw
    assert raw["cognition"]["features"]["pulse"] is False


def test_set_rate_limits_all_empty_removes_block() -> None:
    set_rate_limits({"per_minute": 10}, {})
    assert load_rate_limit_settings().messages.to_limits() == [(10, 60)]
    # Clearing everything removes the block rather than leaving a stale one.
    set_rate_limits({}, {})
    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8")) or {}
    assert "rate_limits" not in raw


def test_set_cognition_feature_preserves_other_keys() -> None:
    # Seed config.yaml with HomeConfig-style content first.
    save_home_config(load_home_config())
    set_cognition_feature("pulse", False)

    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert raw["cognition"]["features"]["pulse"] is False
    # HomeConfig keys survive the merge-write.
    assert "default_agent" in raw
    assert load_system_settings().features.pulse is False


def test_set_cognition_model_fast_and_deep() -> None:
    save_home_config(load_home_config())

    set_cognition_model("fast_model", "anthropic:claude-sonnet-4-6")
    set_cognition_model("deep_model", "anthropic:claude-opus-4-8")
    s = load_system_settings()
    assert s.fast_model == "anthropic:claude-sonnet-4-6"
    assert s.deep_model == "anthropic:claude-opus-4-8"

    # Empty fast_model resets to the default; falsy deep_model clears to None.
    set_cognition_model("fast_model", "")
    set_cognition_model("deep_model", "")
    s = load_system_settings()
    assert s.fast_model == SystemSettings().fast_model
    assert s.deep_model is None


def test_set_cognition_model_rejects_unknown_field() -> None:
    with pytest.raises(ValueError):
        set_cognition_model("nope", "x")


# ── media: block (vision / image source pins) ────────────────────────────────


def test_media_settings_default_when_no_block() -> None:
    md = load_media_settings()
    assert isinstance(md, MediaSettings)
    assert md.vision == ""  # "" = auto
    assert md.image == ""


def test_set_media_settings_round_trip() -> None:
    save_home_config(load_home_config())
    set_media_settings(vision="olano", image="openai:gpt-image-2.5-flare")
    md = load_media_settings()
    assert md.vision == "olano"
    assert md.image == "openai:gpt-image-2.5-flare"


def test_set_media_settings_none_leaves_field_untouched() -> None:
    set_media_settings(vision="anthropic:claude-haiku-4-5-20251001", image="olano")
    # Only touch vision; image must survive unchanged.
    set_media_settings(vision="olano")
    md = load_media_settings()
    assert md.vision == "olano"
    assert md.image == "olano"


def test_set_media_settings_preserves_other_blocks() -> None:
    save_home_config(load_home_config())
    set_cognition_feature("pulse", False)
    set_media_settings(vision="olano")

    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert raw["media"]["vision"] == "olano"
    assert "default_agent" in raw
    assert raw["cognition"]["features"]["pulse"] is False


def test_media_settings_strips_whitespace() -> None:
    set_media_settings(vision="  olano  ", image="  ")
    md = load_media_settings()
    assert md.vision == "olano"
    assert md.image == ""


# ── web_search: block (source pin) ───────────────────────────────────────────


def test_web_search_settings_default_when_no_block() -> None:
    ws = load_web_search_settings()
    assert isinstance(ws, WebSearchSettings)
    assert ws.provider == ""  # "" = auto


@pytest.mark.parametrize("provider", ["olano", "tavily", "disabled", ""])
def test_set_web_search_round_trip_valid_sources(provider: str) -> None:
    assert provider in WEB_SEARCH_SOURCES
    set_web_search_settings(provider=provider)
    assert load_web_search_settings().provider == provider


def test_web_search_coerces_unknown_to_auto() -> None:
    # A hand-edited unknown value fails open to "" (auto), never an error.
    assert WebSearchSettings.model_validate({"provider": "bing"}).provider == ""


def test_set_web_search_lowercases_and_strips() -> None:
    set_web_search_settings(provider="  OLANO  ")
    assert load_web_search_settings().provider == "olano"


def test_set_web_search_preserves_other_blocks() -> None:
    save_home_config(load_home_config())
    set_cognition_feature("pulse", False)
    set_web_search_settings(provider="disabled")

    paths = get_olano_paths()
    raw = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert raw["web_search"]["provider"] == "disabled"
    assert "default_agent" in raw
    assert raw["cognition"]["features"]["pulse"] is False


# ── prompt_refresh block ─────────────────────────────────────────────────────


def test_prompt_refresh_defaults() -> None:
    from olano.config.system_settings import load_prompt_refresh_settings

    s = load_prompt_refresh_settings()
    assert s.enabled is True
    assert s.check_interval_minutes == 2
    assert s.notify_threads is True


def test_prompt_refresh_interval_clamped() -> None:
    from olano.config.system_settings import PromptRefreshSettings

    assert PromptRefreshSettings(check_interval_minutes=0).check_interval_minutes == 1
    assert PromptRefreshSettings(check_interval_minutes=10**6).check_interval_minutes == 1440
    assert PromptRefreshSettings(check_interval_minutes="junk").check_interval_minutes == 2


def test_prompt_refresh_malformed_file_falls_back_to_defaults() -> None:
    from olano.config.home import get_olano_paths
    from olano.config.system_settings import load_prompt_refresh_settings

    get_olano_paths().config_path.write_text(":::not yaml:::[", encoding="utf-8")
    s = load_prompt_refresh_settings()
    assert s.enabled is True and s.check_interval_minutes == 2


def test_set_prompt_refresh_round_trip_preserves_other_keys() -> None:
    import yaml

    from olano.config.home import get_olano_paths
    from olano.config.system_settings import (
        load_prompt_refresh_settings,
        set_prompt_refresh_settings,
    )

    path = get_olano_paths().config_path
    path.write_text("cognition:\n  enabled: false\n", encoding="utf-8")
    set_prompt_refresh_settings(
        {"check_interval_minutes": 30, "notify_threads": False, "bogus_key": 1}
    )

    s = load_prompt_refresh_settings()
    assert s.check_interval_minutes == 30
    assert s.notify_threads is False
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["cognition"] == {"enabled": False}
    assert "bogus_key" not in raw["prompt_refresh"]


# ── magic links: the handshake → magic_links config-key rename ───────────────


def test_magic_link_settings_read_the_legacy_handshake_block() -> None:
    """A box provisioned before the rename keeps its settings.

    Both keys are read forever: a hand-edit or a revision restore can put the
    old spelling back long after the migration ran.
    """
    from olano.config.home import get_olano_paths
    from olano.config.system_settings import load_magic_link_settings

    get_olano_paths().config_path.write_text(
        "handshake:\n  ttl_minutes: 42\n  pin: 'off'\n", encoding="utf-8"
    )
    s = load_magic_link_settings()
    assert s.ttl_minutes == 42
    assert s.pin == "off"


def test_magic_links_key_wins_when_a_config_carries_both() -> None:
    from olano.config.home import get_olano_paths
    from olano.config.system_settings import load_magic_link_settings

    get_olano_paths().config_path.write_text(
        "handshake:\n  ttl_minutes: 42\nmagic_links:\n  ttl_minutes: 7\n", encoding="utf-8"
    )
    assert load_magic_link_settings().ttl_minutes == 7


def test_saving_migrates_the_legacy_key_and_keeps_its_values() -> None:
    """A save rewrites under the new key and drops the old one.

    Leaving both would give the file two spellings of one setting, where the
    one an operator edits may not be the one the loader reads.
    """
    import yaml

    from olano.config.home import get_olano_paths
    from olano.config.system_settings import load_magic_link_settings, set_magic_link_settings

    path = get_olano_paths().config_path
    path.write_text(
        "cognition:\n  enabled: false\nhandshake:\n  ttl_minutes: 42\n  allow_groups: true\n",
        encoding="utf-8",
    )
    set_magic_link_settings({"max_pin_attempts": 3})

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "handshake" not in raw
    assert raw["magic_links"]["ttl_minutes"] == 42
    assert raw["magic_links"]["allow_groups"] is True
    assert raw["magic_links"]["max_pin_attempts"] == 3
    assert raw["cognition"] == {"enabled": False}
    assert load_magic_link_settings().max_pin_attempts == 3


# ── background-run budget: two tiers, agent wins when it has an opinion ──────


def _cognition(**kw):
    from olano.config.agent_config import CognitionConfig

    return AgentConfig(name="a", model="fake:instant", cognition=CognitionConfig(**kw))


def test_worker_budget_defaults_when_nothing_is_configured() -> None:
    from olano.config.system_settings import effective_worker_budget
    from olano.security.delegate_guards import DEFAULT_MAX_ITERATIONS, DEFAULT_TIMEOUT_SECONDS

    assert effective_worker_budget(_cognition(), settings=SystemSettings()) == (
        DEFAULT_MAX_ITERATIONS,
        DEFAULT_TIMEOUT_SECONDS,
    )


def test_an_agent_with_no_opinion_inherits_the_home_wide_budget() -> None:
    """The case a plain ``int`` default made unreachable — ``getattr`` returned
    the class default whether or not the key was on disk, so every agent
    permanently "had an opinion" and a fleet-wide raise reached none of them."""
    from olano.config.system_settings import effective_worker_budget

    settings = SystemSettings(worker_max_iterations=250, worker_timeout_seconds=3000)
    assert effective_worker_budget(_cognition(), settings=settings) == (250, 3000)


def test_an_agent_override_wins_over_the_home_wide_budget() -> None:
    from olano.config.system_settings import effective_worker_budget

    settings = SystemSettings(worker_max_iterations=250, worker_timeout_seconds=3000)
    cfg = _cognition(worker_max_iterations=33, worker_timeout_seconds=123)
    assert effective_worker_budget(cfg, settings=settings) == (33, 123)


def test_one_half_can_override_while_the_other_inherits() -> None:
    """They are independent fields, so overriding the step budget alone must not
    drag the time limit off the global tier with it."""
    from olano.config.system_settings import effective_worker_budget

    settings = SystemSettings(worker_max_iterations=250, worker_timeout_seconds=3000)
    assert effective_worker_budget(_cognition(worker_max_iterations=33), settings=settings) == (
        33,
        3000,
    )


@pytest.mark.parametrize(
    ("steps", "seconds", "expected"),
    [(999_999, 999_999, (400, 7200)), (1, 1, (10, 30))],
)
def test_out_of_range_values_clamp_at_both_tiers(steps, seconds, expected) -> None:
    from olano.config.system_settings import effective_worker_budget

    agent = _cognition(worker_max_iterations=steps, worker_timeout_seconds=seconds)
    assert effective_worker_budget(agent, settings=SystemSettings()) == expected

    globals_ = SystemSettings(worker_max_iterations=steps, worker_timeout_seconds=seconds)
    assert effective_worker_budget(_cognition(), settings=globals_) == expected


def test_a_garbage_budget_never_resets_the_rest_of_the_cognition_block() -> None:
    """``load_system_settings`` must never raise — one bad hand-edited value
    would otherwise take every home-level cognition field down with it."""
    path = get_olano_paths().config_path
    path.write_text(
        "cognition:\n  autonomous: false\n  worker_max_iterations: lots\n", encoding="utf-8"
    )
    s = load_system_settings()
    assert s.autonomous is False  # survived
    assert s.worker_max_iterations == 100  # fell back to the default


def test_set_worker_budget_round_trip_preserves_other_keys() -> None:
    from olano.config.system_settings import set_worker_budget

    path = get_olano_paths().config_path
    path.write_text("cognition:\n  enabled: false\n", encoding="utf-8")
    set_worker_budget(max_iterations=180, timeout_seconds=2400)

    s = load_system_settings()
    assert (s.worker_max_iterations, s.worker_timeout_seconds) == (180, 2400)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["cognition"]["enabled"] is False


def test_set_worker_budget_can_move_one_half_alone() -> None:
    from olano.config.system_settings import set_worker_budget

    set_worker_budget(max_iterations=180)
    set_worker_budget(timeout_seconds=2400)
    s = load_system_settings()
    assert (s.worker_max_iterations, s.worker_timeout_seconds) == (180, 2400)


def test_set_worker_budget_clamps_rather_than_writing_a_runaway() -> None:
    from olano.config.system_settings import set_worker_budget

    set_worker_budget(max_iterations=10_000, timeout_seconds=1)
    s = load_system_settings()
    assert (s.worker_max_iterations, s.worker_timeout_seconds) == (400, 30)
