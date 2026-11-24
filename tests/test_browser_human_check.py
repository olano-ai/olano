"""What an agent does on a check only a person can pass: the settings and their resolution."""

from __future__ import annotations

import pytest

from olano.browser import blockpage, human_check
from olano.config.system_settings import (
    BrowserSettings,
    load_browser_settings,
    set_browser_settings,
)


def test_home_defaults_wait_five_minutes():
    settings = BrowserSettings()
    assert (settings.human_check, settings.human_check_wait) == ("wait", 5)


@pytest.mark.parametrize(
    ("given", "kept"),
    [("hold", "hold"), (" SKIP ", "skip"), ("forever", "wait"), (None, "wait")],
)
def test_home_mode_is_coerced(given, kept):
    assert BrowserSettings(human_check=given).human_check == kept


@pytest.mark.parametrize(("given", "kept"), [(0, 1), (500, 120), ("x", 5), (30, 30)])
def test_home_wait_is_clamped(given, kept):
    assert BrowserSettings(human_check_wait=given).human_check_wait == kept


def test_agent_values_are_validated_not_coerced():
    from pydantic import ValidationError

    from olano.config.agent_config import BrowserAgentConfig

    assert BrowserAgentConfig().human_check is None
    assert BrowserAgentConfig(human_check="Hold", human_check_wait="15").human_check == "hold"
    with pytest.raises(ValidationError):
        BrowserAgentConfig(human_check="forever")
    with pytest.raises(ValidationError):
        BrowserAgentConfig(human_check_wait=0)


def test_resolution_order_choice_then_agent_then_home(monkeypatch):
    from olano.config import loader
    from olano.config.agent_config import BrowserAgentConfig

    set_browser_settings({"human_check": "skip", "human_check_wait": 7})
    assert load_browser_settings().human_check == "skip"

    class _Cfg:
        browser = BrowserAgentConfig()

    cfg = _Cfg()
    monkeypatch.setattr(loader, "load_agent_config", lambda name: cfg)
    assert human_check.resolve("alice") == human_check.Policy("skip", 7)

    cfg.browser = BrowserAgentConfig(human_check="wait", human_check_wait=2)
    assert human_check.resolve("alice") == human_check.Policy("wait", 2)
    assert human_check.resolve("alice", choice="hold").mode == "hold"
    assert human_check.resolve("alice", choice="nonsense").mode == "wait"


def test_hold_is_wait_on_a_run_nobody_started_from_a_chat():
    set_browser_settings({"human_check": "hold"})
    assert human_check.resolve("", interactive=True).mode == "hold"
    assert human_check.resolve("", interactive=False) == human_check.Policy("wait", 5)


def test_wait_seconds_per_mode():
    assert human_check.Policy("wait", 5).wait_s == 300
    assert human_check.Policy("hold", 5).wait_s is None
    assert human_check.Policy("skip", 5).wait_s == 0


@pytest.mark.parametrize(
    ("title", "text", "check"),
    [
        ("Just a moment...", "Performing security verification", True),
        ("Robot check", "Please complete the CAPTCHA to continue", True),
        ("Blocked", "You've been blocked by network security. Log in to continue", False),
        ("Access denied", "Access denied", False),
    ],
)
def test_which_refusals_are_checks_a_person_passes(title, text, check):
    block = blockpage.detect("https://example.com/", title, text)
    assert block is not None
    assert block.human_check is check


def test_messages_never_suggest_passing_the_check_yourself():
    policy = human_check.Policy("wait", 5)
    for text in (
        human_check.skip_message("example.com", "Just a moment...", live=True),
        human_check.skip_message("example.com", "Just a moment...", live=False),
        human_check.skip_message("example.com", "x", live=True, by_person=True),
        human_check.timeout_message("example.com", policy),
    ):
        assert text.startswith(human_check.MARKER)
        assert "do not try to pass" in text.lower()


# ── the dashboard API ─────────────────────────────────────────────────────────


@pytest.fixture
def agent():
    from olano.config.agent_config import AgentConfig
    from olano.config.loader import save_agent_config

    save_agent_config(AgentConfig(name="scout"))
    return "scout"


@pytest.fixture
def client():
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    return TestClient(create_api(MagicMock()))


def test_api_reads_and_saves_the_agents_own_setting(client, agent):
    from olano.config.loader import load_agent_config

    url = f"/api/agents/{agent}/browser/human-check"
    body = client.get(url).json()
    assert body["human_check"] is None and body["human_check_wait"] is None
    assert body["home"] == {"human_check": "wait", "human_check_wait": 5}
    assert body["can_edit"] is True

    saved = client.put(url, json={"human_check": "hold", "human_check_wait": 20})
    assert saved.status_code == 200, saved.text
    assert (saved.json()["human_check"], saved.json()["human_check_wait"]) == ("hold", 20)
    assert load_agent_config(agent).browser.human_check == "hold"

    assert client.put(url, json={"human_check": "forever"}).status_code == 400
    assert client.put(url, json={"human_check_wait": 0}).status_code == 400
    # Empty goes back to the home setting.
    back = client.put(url, json={"human_check": None, "human_check_wait": None}).json()
    assert back["human_check"] is None and back["human_check_wait"] is None


def test_api_skip_needs_a_waiting_check(client, agent):
    from olano.browser import sessions

    url = f"/api/agents/{agent}/browser/human-check/skip"
    assert client.post(url).status_code == 409

    class _Sess:
        human_check = {"site": "example.com"}
        human_check_skipped = False

    sess = _Sess()
    sessions._sessions[agent] = sess  # type: ignore[assignment]
    try:
        assert client.post(url).json() == {"skipped": True}
        assert sess.human_check_skipped is True
    finally:
        sessions._sessions.pop(agent, None)


def test_home_config_api_validates_the_new_fields(client):
    url = "/api/system/config"
    assert client.put(url, json={"browser": {"human_check": "forever"}}).status_code == 400
    assert client.put(url, json={"browser": {"human_check_wait": 500}}).status_code == 400
    ok = client.put(url, json={"browser": {"human_check": "skip", "human_check_wait": 9}})
    assert ok.status_code == 200, ok.text
    assert load_browser_settings().human_check == "skip"
    got = client.get(url).json()["browser"]
    assert (got["human_check"], got["human_check_wait"]) == ("skip", 9)


# ── a bot check's pass is not a sign-in ──────────────────────────────────────


def test_saved_sign_ins_leave_out_bot_check_cookies():
    from olano.browser.sessions import portable_state

    state = {
        "cookies": [
            {"name": "sid", "value": "1", "domain": "example.com"},
            {"name": "cf_clearance", "value": "2", "domain": ".example.com"},
            {"name": "__cf_bm", "value": "3", "domain": ".example.com"},
            {"name": "_cfuvid", "value": "4", "domain": ".example.com"},
            {"name": "cf_chl_rc_m", "value": "5", "domain": "example.com"},
            {"name": "__cflb", "value": "6", "domain": "example.com"},
        ],
        "origins": [{"origin": "https://example.com", "localStorage": []}],
    }
    kept = portable_state(state)
    assert [c["name"] for c in kept["cookies"]] == ["sid", "__cflb"]
    assert kept["origins"] == state["origins"]
    assert len(state["cookies"]) == 6  # the caller's copy is untouched


def test_a_file_saved_before_the_rule_is_cleaned_when_loaded():
    import json

    from olano.browser import sessions

    path = sessions.state_path("scout")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "sid", "value": "1", "domain": "example.com", "path": "/"},
                    {"name": "cf_clearance", "value": "2", "domain": ".example.com", "path": "/"},
                ],
                "origins": [],
            }
        )
    )
    loaded = sessions._load_state("scout")
    assert loaded is not None
    assert [c["name"] for c in loaded["cookies"]] == ["sid"]
    path.write_text("{not json")
    assert sessions._load_state("scout") is None


def test_plain_check_parts_expand_groups_in_a_fixed_order():
    from olano.browser import plain_check

    assert plain_check.expand_parts(["copy_hook", "emulation"]) == (
        "viewport",
        "locale",
        "timezone",
        "color_scheme",
        "service_workers",
        "copy_hook",
    )
    assert plain_check.expand_parts(["all"]) == plain_check.PART_GROUPS["all"]
    assert "copy_binding" not in plain_check.expand_parts(["all"])
    with pytest.raises(ValueError):
        plain_check.expand_parts(["nope"])
