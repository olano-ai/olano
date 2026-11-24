"""Tests for olano channel add/list/remove CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from olano.cli.main import cli
from olano.config.agent_config import AgentConfig, ChannelConfig


def _seed_agent(tmp_path: Path, name: str = "test-agent", **kw) -> Path:
    """Create a minimal agent config file under <tmp_path>/agents/<name>/."""
    from olano.config.loader import save_agent_config

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(name=name, model="anthropic:claude-sonnet-4-6", **kw)
    path = save_agent_config(config, base=agents_dir)
    return path


def _home_config_mock(default_agent: str = "test-agent"):
    from olano.config.home import HomeConfig

    return HomeConfig(default_agent=default_agent)


# ─────────────────────────────────────────────────────────────────
# channel add
# ─────────────────────────────────────────────────────────────────


def test_channel_add_telegram_writes_yaml(tmp_path: Path):
    _seed_agent(tmp_path)
    runner = CliRunner()
    fake_config = {"type": "telegram", "token": "test-tok"}
    with (
        patch("olano.platforms.setup.get_setup_fn", return_value=lambda home: fake_config),
        patch("olano.cli.main.load_home_config", return_value=_home_config_mock()),
    ):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "add", "telegram"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "telegram" in result.output

    # After CLI run, re-load via the home path (env var set by --home)
    from olano.config.loader import load_agent_config

    config = load_agent_config("test-agent", source="home")
    channel_types = [c.type if hasattr(c, "type") else c for c in config.channels]
    assert "telegram" in channel_types


def test_channel_add_fails_on_verify_failure(tmp_path: Path):
    _seed_agent(tmp_path)
    runner = CliRunner()

    def bad_setup(home):
        raise ValueError("connection refused")

    with (
        patch("olano.platforms.setup.get_setup_fn", return_value=bad_setup),
        patch("olano.cli.main.load_home_config", return_value=_home_config_mock()),
    ):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "add", "telegram"],
        )
    assert result.exit_code != 0

    from olano.config.loader import load_agent_config

    config = load_agent_config("test-agent", source="home")
    assert config.channels == []


# ─────────────────────────────────────────────────────────────────
# channel list
# ─────────────────────────────────────────────────────────────────


def test_channel_list_shows_configured_channels(tmp_path: Path):
    from olano.config.loader import save_agent_config

    config = AgentConfig(
        name="test-agent",
        model="anthropic:claude-sonnet-4-6",
        channels=[ChannelConfig(type="telegram"), ChannelConfig(type="slack")],
    )
    (tmp_path / "agents").mkdir(parents=True, exist_ok=True)
    save_agent_config(config, base=tmp_path / "agents")
    runner = CliRunner()
    with patch("olano.cli.main.load_home_config", return_value=_home_config_mock()):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "list"],
            catch_exceptions=False,
        )
    assert "telegram" in result.output
    assert "slack" in result.output


def test_channel_list_empty(tmp_path: Path):
    _seed_agent(tmp_path)
    runner = CliRunner()
    with patch("olano.cli.main.load_home_config", return_value=_home_config_mock()):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "list"],
            catch_exceptions=False,
        )
    assert "No channels" in result.output


# ─────────────────────────────────────────────────────────────────
# channel remove
# ─────────────────────────────────────────────────────────────────


def test_channel_remove_idempotent(tmp_path: Path):
    from olano.config.loader import save_agent_config

    config = AgentConfig(
        name="test-agent",
        model="anthropic:claude-sonnet-4-6",
        channels=[ChannelConfig(type="telegram")],
    )
    (tmp_path / "agents").mkdir(parents=True, exist_ok=True)
    save_agent_config(config, base=tmp_path / "agents")
    runner = CliRunner()
    with patch("olano.cli.main.load_home_config", return_value=_home_config_mock()):
        # Remove once
        r1 = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "remove", "telegram"],
            catch_exceptions=False,
        )
        # Remove again (idempotent — should not error)
        r2 = runner.invoke(
            cli,
            ["--home", str(tmp_path), "channel", "remove", "telegram"],
            catch_exceptions=False,
        )
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    assert "nothing to remove" in r2.output.lower() or "not configured" in r2.output.lower()
