"""config.yaml → browser: the settings block and the gates on top of it."""

from __future__ import annotations

import pytest

from olano.browser import policy
from olano.config.home import get_olano_paths
from olano.config.system_settings import (
    BrowserSettings,
    load_browser_settings,
    set_browser_settings,
)


@pytest.fixture
def box(monkeypatch):
    """A controllable host: features, size, Docker and free memory."""
    state = {
        "features": {"browser": True, "browser_live": True},
        "ncpu": 4,
        "mem": 8 * policy.GIB,
        "docker": True,
        "free": 4 * policy.GIB,
    }
    monkeypatch.setattr(policy, "_feature", lambda name: state["features"].get(name, True))
    monkeypatch.setattr(policy, "_host", lambda: (state["ncpu"], state["mem"]))
    monkeypatch.setattr(policy, "_docker_available", lambda: state["docker"])
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: state["free"])
    return state


# ── the settings block ───────────────────────────────────────────────────────


def test_defaults_are_off():
    s = load_browser_settings()
    assert (s.mode, s.max_sessions, s.idle_timeout, s.provider) == ("off", 1, 10, "local")


def test_coercion_never_widens():
    s = BrowserSettings(mode="LIVE", provider="Remote", max_sessions=99, idle_timeout=0)
    assert (s.mode, s.provider, s.max_sessions, s.idle_timeout) == ("live", "remote", 8, 1)
    junk = BrowserSettings(mode="turbo", provider="moon", max_sessions="x", idle_timeout="y")
    assert (junk.mode, junk.provider, junk.max_sessions, junk.idle_timeout) == (
        "off",
        "local",
        1,
        10,
    )


def test_malformed_block_reads_as_off():
    path = get_olano_paths().config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("browser: [not, a, mapping]\n")
    assert load_browser_settings().mode == "off"
    path.write_text(":::not yaml:::[")
    assert load_browser_settings().mode == "off"


def test_set_round_trip_preserves_other_blocks():
    import yaml

    path = get_olano_paths().config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("issues:\n  enabled: true\n")
    set_browser_settings({"mode": "headless", "max_sessions": 3, "bogus": 1})
    raw = yaml.safe_load(path.read_text())
    assert raw["issues"] == {"enabled": True}
    assert raw["browser"]["mode"] == "headless"
    assert raw["browser"]["max_sessions"] == 3
    assert "bogus" not in raw["browser"]


# ── effective availability ───────────────────────────────────────────────────


def test_off_unless_asked(box):
    avail = policy.browser_availability()
    assert not avail.enabled and avail.code == "disabled"
    assert not policy.browser_tools_allowed()


def test_headless_on_a_capable_box(box):
    set_browser_settings({"mode": "headless"})
    avail = policy.browser_availability()
    assert (avail.mode, avail.code) == ("headless", "ok")
    assert policy.browser_tools_allowed()


def test_one_gigabyte_box_gets_no_local_browser(box):
    box["mem"] = int(0.95 * policy.GIB)
    set_browser_settings({"mode": "headless"})
    avail = policy.browser_availability()
    assert (avail.mode, avail.code) == ("off", "host_too_small")
    assert "memory" in avail.reason


def test_remote_provider_skips_the_hardware_gate(box):
    box["mem"] = int(0.95 * policy.GIB)
    set_browser_settings({"mode": "headless", "provider": "remote"})
    assert policy.browser_availability().mode == "headless"


def test_live_degrades_to_headless_when_the_box_is_too_small(box):
    box["ncpu"], box["mem"] = 1, 2 * policy.GIB
    set_browser_settings({"mode": "live"})
    avail = policy.browser_availability()
    assert (avail.mode, avail.code) == ("headless", "host_too_small_for_live")


def test_live_degrades_without_the_plan_entitlement(box):
    box["features"]["browser_live"] = False
    set_browser_settings({"mode": "live"})
    avail = policy.browser_availability()
    assert (avail.mode, avail.code) == ("headless", "live_not_entitled")


def test_live_degrades_without_docker(box):
    box["docker"] = False
    set_browser_settings({"mode": "live"})
    assert policy.browser_availability().code == "live_needs_docker"


def test_no_browser_entitlement_means_off(box):
    box["features"]["browser"] = False
    set_browser_settings({"mode": "headless"})
    avail = policy.browser_availability()
    assert (avail.mode, avail.code) == ("off", "not_entitled")


def test_live_on_a_pro_sized_box(box):
    box["ncpu"], box["mem"] = 2, int(3.8 * policy.GIB)  # a "4 GB" machine
    set_browser_settings({"mode": "live"})
    assert policy.browser_availability().mode == "live"


# ── enabling ─────────────────────────────────────────────────────────────────


def test_enable_refusal_on_a_one_gigabyte_box(box):
    box["mem"] = int(0.95 * policy.GIB)
    assert "at least" in policy.enable_refusal("headless")
    assert policy.enable_refusal("headless", "remote") is None


def test_enable_refusal_live_needs_two_cpus_and_memory(box):
    box["ncpu"] = 1
    assert "2 CPUs" in policy.enable_refusal("live")
    box["ncpu"], box["mem"] = 2, 2 * policy.GIB
    assert "memory" in policy.enable_refusal("live")


def test_enable_refusal_checks_memory_free_right_now(box):
    box["free"] = 100 * policy.MIB
    assert "free right now" in policy.enable_refusal("headless")


def test_enable_refusal_live_not_in_plan(box):
    box["features"]["browser_live"] = False
    assert "not included" in policy.enable_refusal("live")
    assert policy.enable_refusal("headless") is None


def test_filter_browser_tool_names(box):
    names = ["web_search", "browser_navigate", "page_screenshot"]
    assert policy.filter_browser_tool_names(names) == ["web_search"]
    set_browser_settings({"mode": "headless"})
    assert policy.filter_browser_tool_names(names) == names


def test_known_features_lists_the_browser_flags():
    from olano.deployment.schema import KNOWN_FEATURES

    assert "browser" in KNOWN_FEATURES and "browser_live" in KNOWN_FEATURES


def test_dashboard_mirror_of_the_gate_lists_the_same_tools():
    """views/browser/approval.ts must name exactly ALWAYS_ASK_TOOL_NAMES (every
    browser tool plus the fleet tool that changes an agent's browser
    auto-approval rules), or the chat's "Accept all" could approve something
    the server always asks."""
    import re
    from pathlib import Path

    ts = (
        Path(__file__).resolve().parents[1] / "olano/dashboard_2.0/src/views/browser/approval.ts"
    ).read_text()
    block = ts.split("new Set([", 1)[1].split("])", 1)[0]
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(policy.ALWAYS_ASK_TOOL_NAMES)
    assert policy.BROWSER_TOOL_NAMES < policy.ALWAYS_ASK_TOOL_NAMES
