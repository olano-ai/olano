"""Tests for the block-routing config patch (``olano/admin/config.py``).

The old wrapper patched 16 root keys and echoed every requested key as
"updated" — these pin the two fixes: every settings block is reachable, and
the result reports honestly (``applied`` vs ``ignored``).
"""

from __future__ import annotations

from olano import admin
from olano.config import system_settings as ss
from olano.config.home import read_home_config_doc


def _raw() -> dict:
    return read_home_config_doc()


def test_root_patch_preserves_foreign_top_level_keys() -> None:
    ss.set_issues_settings({"poll_minutes": 5})
    res = admin.update_home_config({"default_agent": "merli"})
    assert res["applied"] == ["default_agent"]
    assert res["updated"] == ["default_agent"]  # back-compat alias, now honest
    raw = _raw()
    assert raw["default_agent"] == "merli"
    assert raw["issues"]["poll_minutes"] == 5


def test_unknown_key_is_reported_not_swallowed() -> None:
    res = admin.update_home_config({"speach": {"tts": {}}})
    assert res["applied"] == []
    assert res["ignored"][0]["key"] == "speach"
    assert "writable" in res["ignored"][0]["reason"]
    assert "speach" not in _raw()


def test_block_value_must_be_an_object() -> None:
    res = admin.update_home_config({"issues": "off"})
    assert res["applied"] == []
    assert res["ignored"][0]["key"] == "issues"


def test_magic_links_patch_preserves_siblings() -> None:
    ss.set_magic_link_settings({"ttl_minutes": 30})
    res = admin.update_home_config({"magic_links": {"enabled": False}})
    assert res["applied"] == ["magic_links"]
    settings = ss.load_magic_link_settings()
    assert settings.enabled is False
    assert settings.ttl_minutes == 30


def test_new_setters_round_trip_and_report_unknown_fields() -> None:
    res = admin.update_home_config(
        {
            "groups": {"enabled": True},
            "issues": {"poll_minutes": 7},
            "revisions": {"include_memory": False},
            "heartbeat": {"interval_minutes": 30},
        }
    )
    assert res["applied"] == ["groups", "heartbeat", "issues", "revisions"]
    assert ss.load_group_thread_settings().enabled is True
    assert ss.load_issues_settings().poll_minutes == 7
    assert ss.load_revisions_settings().include_memory is False
    assert ss.load_heartbeat_settings().interval_minutes == 30

    res = admin.update_home_config({"issues": {"bogus": 1}})
    assert res["applied"] == []
    assert res["ignored"] == [{"key": "issues.bogus", "reason": "unknown field"}]


def test_disk_monitor_patch_preserves_defaults() -> None:
    res = admin.update_home_config({"disk_monitor": {"warn_percent": 77}})
    assert res["applied"] == ["disk_monitor"]
    settings = ss.load_disk_monitor_settings()
    assert settings.warn_percent == 77
    assert settings.cleanup.enabled is True  # sibling defaults intact


def test_speech_rejects_unknown_provider_loudly() -> None:
    # The model would coerce an unknown provider to "disabled" — the patch
    # refuses instead, so a typo can't silently switch a service off.
    res = admin.update_home_config({"speech": {"stt": {"provider": "not-real"}}})
    assert res["applied"] == []
    assert res["ignored"][0]["key"] == "speech.stt.provider"

    res = admin.update_home_config({"speech": {"tts": {"provider": "openai"}}})
    assert res["applied"] == ["speech"]
    assert ss.load_speech_settings().tts.provider == "openai"


def test_web_search_rejects_unknown_source() -> None:
    res = admin.update_home_config({"web_search": {"provider": "bing"}})
    assert res["applied"] == []
    assert res["ignored"][0]["key"] == "web_search.provider"

    res = admin.update_home_config({"web_search": {"provider": "disabled"}})
    assert res["applied"] == ["web_search"]
    assert ss.load_web_search_settings().provider == "disabled"


def test_cognition_routes_to_the_granular_setters() -> None:
    res = admin.update_home_config(
        {
            "cognition": {
                "fast_model": "anthropic:claude-haiku-4-5-20251001",
                "intensity": "balanced",
                "features": {"pulse": False},
                "worker_max_iterations": 120,
            }
        }
    )
    assert res["applied"] == ["cognition"]
    settings = ss.load_system_settings()
    assert settings.fast_model == "anthropic:claude-haiku-4-5-20251001"
    assert settings.intensity == "balanced"
    assert settings.features.pulse is False
    assert settings.worker_max_iterations == 120

    res = admin.update_home_config({"cognition": {"intensity": "warp"}})
    assert res["applied"] == []
    assert res["ignored"][0]["key"] == "cognition.intensity"


def test_default_models_bad_value_lands_in_ignored_not_an_exception() -> None:
    res = admin.update_home_config(
        {"default_models": {"pro": "anthropic:claude-sonnet-4-6", "turbo": "default:pro"}}
    )
    assert res["applied"] == ["default_models"]  # `pro` landed
    assert any(e["key"] == "default_models.turbo" for e in res["ignored"])
    assert ss.load_default_models()["pro"] == "anthropic:claude-sonnet-4-6"


def test_credits_display_locked_is_refused_with_the_reason(monkeypatch) -> None:
    monkeypatch.setattr(ss, "cost_display_locked", lambda: True)
    res = admin.update_home_config({"credits": {"display": "usd"}})
    assert res["applied"] == []
    assert "plan" in res["ignored"][0]["reason"]


def test_credits_display_writes_when_unlocked() -> None:
    res = admin.update_home_config({"credits": {"display": "credits"}})
    assert res["applied"] == ["credits"]
    assert ss.load_credits_settings().display == "credits"


def test_restart_required_is_reported_for_the_port_keys() -> None:
    res = admin.update_home_config({"dashboard_port": 3300, "issues": {"poll_minutes": 9}})
    assert res["restart_required"] == ["dashboard_port"]
    assert set(res["applied"]) == {"dashboard_port", "issues"}


def test_get_home_config_carries_blocks_and_writable() -> None:
    data = admin.get_home_config()
    assert data["_path"].endswith("config.yaml")
    assert "default_agent" in data  # root keys stay top-level (back-compat)
    blocks = data["blocks"]
    for key in ("speech", "cognition", "disk_monitor", "issues", "revisions", "heartbeat"):
        assert key in blocks
    assert "issues" in data["writable"]["blocks"]
    assert "default_agent" in data["writable"]["root"]
    assert data["restart_required"] == [
        "runtime_api_port",
        "dashboard_port",
        "multi_user_enabled",
    ]
