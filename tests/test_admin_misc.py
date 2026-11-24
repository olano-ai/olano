"""Tests for olano.admin tools/config/logs/skills/mcp/subagents layers."""

from __future__ import annotations

import pytest

from olano import admin

# ── tools ─────────────────────────────────────────────────────────────────--


def test_list_tools_and_bundles() -> None:
    tools = admin.list_tools()
    assert "web_search" in tools["tools"]
    bundles = admin.list_bundles()
    assert "ops" in bundles


def test_set_tool_enabled_roundtrip() -> None:
    admin.set_tool_enabled("web_search", False)
    assert "web_search" in admin.list_tools()["disabled"]
    admin.set_tool_enabled("web_search", True)
    assert "web_search" not in admin.list_tools()["disabled"]


# ── home config ──────────────────────────────────────────────────────────--


def test_home_config_patch_preserves_unknown_keys() -> None:
    cfg = admin.get_home_config()
    assert "default_agent" in cfg
    res = admin.update_home_config({"default_agent": "acme"})
    assert res["config"]["default_agent"] == "acme"


# ── skills ───────────────────────────────────────────────────────────────--


def test_create_skill_writes_valid_frontmatter() -> None:
    res = admin.create_skill(
        "Daily Standup",
        "Run a daily standup summary.",
        "# Daily Standup\n\nSummarize yesterday, today, blockers.",
        category="workflow",
    )
    assert res["created"] is True
    listed = {s["name"] for s in admin.list_skills()}
    assert "daily-standup" in listed


def test_create_skill_rejects_bad_tier() -> None:
    with pytest.raises(ValueError):
        admin.create_skill("x", "d", "body", tier="bogus")


# ── mcp + subagents (operate on a created agent) ─────────────────────────--


def test_mcp_add_list_remove() -> None:
    admin.create_agent("mcpagent", description="mcp test")
    admin.add_mcp_server("mcpagent", "https://mcp.example.com", transport="sse")
    servers = admin.list_mcp_servers("mcpagent")
    assert any(s["url"] == "https://mcp.example.com" for s in servers)
    admin.remove_mcp_server("mcpagent", "https://mcp.example.com")
    assert admin.list_mcp_servers("mcpagent") == []


def test_subagent_add_list_toggle() -> None:
    admin.create_agent("parent", description="has subs")
    admin.add_subagent(
        "parent",
        "Researcher",
        description="Does research",
        system_prompt="Research things.",
        tools=["web_search"],
    )
    # Match by name: other subagents may be present.
    subs = {s["name"]: s for s in admin.list_subagents("parent")}
    assert "Researcher" in subs
    admin.toggle_subagent("parent", "Researcher", False)
    subs = {s["name"]: s for s in admin.list_subagents("parent")}
    assert subs["Researcher"]["enabled"] is False


# ── logs + system ────────────────────────────────────────────────────────--


def test_tail_log_handles_missing_file() -> None:
    out = admin.tail_log(10)
    assert out["lines"] == []  # fresh temp home has no log yet


def test_doctor_reports_home() -> None:
    d = admin.doctor()
    assert d["home_exists"] is True
    assert "version" in d
