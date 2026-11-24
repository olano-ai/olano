"""Tests for the olano.subagents package — registry, overrides, installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.subagents._schema import ToolkitAgentOverride
from olano.subagents.installer import install_toolkit_agent_as_top_level
from olano.subagents.registry import (
    effective_security,
    list_toolkit_agents,
    load_toolkit_agent,
    resolve_as_subagent,
)


class TestAutoGeneration:
    def test_every_registered_toolkit_has_an_agent(self):
        from olano.tools.registry import _TOOLKITS

        names = {d.toolkit for d in list_toolkit_agents(catalog_only=False)}
        for toolkit in _TOOLKITS:
            assert toolkit in names, f"Missing ToolkitAgent for toolkit '{toolkit}'"

    def test_catalog_filter_hides_infrastructure(self):
        catalog = {d.toolkit for d in list_toolkit_agents(catalog_only=True)}
        # Infrastructure toolkits are present but excluded from the catalog.
        for hidden in ("time", "scheduler", "knowledge", "secrets"):
            assert hidden not in catalog

    def test_auto_generated_def_has_no_explicit_security(self):
        # GitHub has no hand-authored def → security None → uses default at install
        defn = load_toolkit_agent("GitHubAgent")
        assert defn.security is None
        # The default applied on resolve enables HITL on writes
        sec = effective_security(defn)
        assert sec.hitl_write_tools is True


class TestHandAuthoredOverridesAuto:
    def test_stripe_explicit_security_block(self):
        defn = load_toolkit_agent("StripeAgent")
        assert defn.security is not None
        assert defn.security.pii_scan_output is True

    def test_alpaca_default_tools_all_expands(self):
        sub = resolve_as_subagent("AlpacaAgent")
        assert "alpaca_get_account" in sub.tools
        assert "alpaca_place_order" in sub.tools


class TestAliasResolution:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("StripeAgent", "StripeAgent"),
            ("stripe", "StripeAgent"),
            ("STRIPE", "StripeAgent"),
            ("googleworkspace", "GoogleWorkspaceAgent"),
            ("ms365", "Microsoft365Agent"),
            ("office365", "Microsoft365Agent"),
            ("intuit", "QuickBooksAgent"),
        ],
    )
    def test_aliases_resolve(self, alias, expected):
        assert load_toolkit_agent(alias).name == expected

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            load_toolkit_agent("NotAThingAgent")


class TestOverrideApplication:
    def test_disabled_tools_removed(self):
        sub = resolve_as_subagent(
            "AlpacaAgent",
            ToolkitAgentOverride(
                name="AlpacaAgent",
                disabled_tools=["alpaca_place_order", "alpaca_cancel_order"],
            ),
        )
        assert "alpaca_place_order" not in sub.tools
        assert "alpaca_cancel_order" not in sub.tools
        # Read-only tools survive
        assert "alpaca_get_account" in sub.tools

    def test_extra_tools_appended(self):
        sub = resolve_as_subagent(
            "AsanaAgent",
            ToolkitAgentOverride(name="AsanaAgent", extra_tools=["web_search"]),
        )
        assert "web_search" in sub.tools

    def test_model_override(self):
        sub = resolve_as_subagent(
            "StripeAgent",
            ToolkitAgentOverride(name="StripeAgent", model="anthropic:claude-haiku-4-5"),
        )
        assert sub.model == "anthropic:claude-haiku-4-5"


class TestBuiltinSubagentsIntegration:
    def test_dict_form_in_agent_config(self):
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig(
            name="t",
            builtin_subagents=[
                "ops",
                {"name": "AlpacaAgent", "disabled_tools": ["alpaca_place_order"]},
            ],
        )
        names = [s.name for s in cfg.subagents]
        assert "OpsManager" in names
        assert "AlpacaAgent" in names
        alpaca = next(s for s in cfg.subagents if s.name == "AlpacaAgent")
        assert "alpaca_place_order" not in alpaca.tools


class TestInstaller:
    def test_writes_a_valid_agent_json5(self, tmp_path: Path):
        path = install_toolkit_agent_as_top_level(
            "AlpacaAgent",
            install_name="AlpacaPaper",
            disabled_tools=["alpaca_place_order", "alpaca_cancel_order"],
            agents_dir=tmp_path,
        )
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "AlpacaPaper"
        assert data["toolkit_agent"] == "AlpacaAgent"
        assert "alpaca_place_order" not in data["tools"]
        assert "alpaca_get_account" in data["tools"]
        # Hand-authored security block survived round-trip
        assert data["security"]["hitl_write_tools"] is True
        assert data["security"]["pii_scan_output"] is True

    def test_refuses_to_overwrite_by_default(self, tmp_path: Path):
        install_toolkit_agent_as_top_level("StripeAgent", install_name="s", agents_dir=tmp_path)
        with pytest.raises(FileExistsError):
            install_toolkit_agent_as_top_level("StripeAgent", install_name="s", agents_dir=tmp_path)

    def test_overwrite_flag_replaces(self, tmp_path: Path):
        install_toolkit_agent_as_top_level("StripeAgent", install_name="s", agents_dir=tmp_path)
        path = install_toolkit_agent_as_top_level(
            "StripeAgent", install_name="s", overwrite=True, agents_dir=tmp_path
        )
        assert path.exists()
