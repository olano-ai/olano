"""Connectors publish the menu *this agent* exposes, and only when it changed.

The Telegram ``/``-autocomplete list and the Discord slash-command tree used to
be built from a hardcoded catalog with no agent in hand, so every bot offered
every command — sensitive ones included — and tapping one answered
"🔒 … is disabled for this agent". Custom commands never appeared at all.

Publishing is also on the hot path of a dashboard save (the Commands tab
re-publishes on every write), so an unchanged config must not cost a network
call — Discord's ``tree.sync()`` in particular is rate-limited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from olano.config.agent_config import CommandsConfig, CustomCommand
from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter


def _adapter(monkeypatch, commands: CommandsConfig) -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="bot-token"), agent_name="menu-agent"
    )
    adapter._bot = AsyncMock()

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.commands = commands
    monkeypatch.setattr("olano.config.loader.load_agent_config", lambda _name: cfg)
    return adapter


def _published(adapter: TelegramAdapter) -> set[str]:
    (batch,), _kwargs = adapter._bot.set_my_commands.await_args
    return {c.command for c in batch}


class TestTelegramPublish:
    async def test_menu_is_the_agents_own(self, monkeypatch) -> None:
        adapter = _adapter(
            monkeypatch,
            CommandsConfig(
                enabled={"model": True},
                custom=[CustomCommand(name="standup", prompt="Summarize.", description="Daily")],
            ),
        )

        assert await adapter.publish_command_menu() is True

        names = _published(adapter)
        assert "standup" in names, "the owner's custom command belongs in the menu"
        assert "model" in names, "explicitly enabled, so it should be offered"
        assert "mode" not in names, "sensitive and untouched — must stay hidden"
        assert "status" in names

    async def test_unchanged_config_skips_the_network_call(self, monkeypatch) -> None:
        adapter = _adapter(monkeypatch, CommandsConfig())

        assert await adapter.publish_command_menu() is True
        assert adapter._bot.set_my_commands.await_count == 1

        assert await adapter.publish_command_menu() is False
        assert adapter._bot.set_my_commands.await_count == 1, "re-published an identical menu"

        # …but an explicit force always pushes (a fresh connect must re-register).
        assert await adapter.publish_command_menu(force=True) is True
        assert adapter._bot.set_my_commands.await_count == 2

    async def test_a_failed_publish_is_never_fatal(self, monkeypatch) -> None:
        """A bot works fine without an autocomplete hint; connect() must not die."""
        adapter = _adapter(monkeypatch, CommandsConfig())
        adapter._bot.set_my_commands.side_effect = RuntimeError("Telegram is down")

        assert await adapter.publish_command_menu() is False

    async def test_no_bot_yet_is_a_no_op(self, monkeypatch) -> None:
        adapter = _adapter(monkeypatch, CommandsConfig())
        adapter._bot = None

        assert await adapter.publish_command_menu() is False


class TestDiscordTreeSignature:
    """Discord rebuilds+syncs only when what it would advertise actually moved."""

    def _adapter(self, monkeypatch, commands: CommandsConfig):
        discord_mod = pytest.importorskip("olano.platforms.discord")

        adapter = discord_mod.DiscordAdapter(
            PlatformConfig(enabled=True, token="tok"), agent_name="menu-agent"
        )

        class _Cfg:
            pass

        cfg = _Cfg()
        cfg.commands = commands
        monkeypatch.setattr("olano.config.loader.load_agent_config", lambda _name: cfg)
        return adapter

    def test_signature_follows_the_agent_config(self, monkeypatch) -> None:
        plain = self._adapter(monkeypatch, CommandsConfig())
        base = {name for name, _desc in plain._command_menu_signature()}
        assert "model" not in base

        enriched = self._adapter(
            monkeypatch,
            CommandsConfig(
                enabled={"model": True},
                custom=[CustomCommand(name="standup", prompt="Summarize.")],
            ),
        )
        names = {name for name, _desc in enriched._command_menu_signature()}
        assert {"model", "standup"} <= names

    def test_unresolvable_config_degrades_to_no_agent(self, monkeypatch) -> None:
        """A broken agent.json5 must not empty the menu — it falls back."""
        adapter = self._adapter(monkeypatch, CommandsConfig())

        def _boom(_name):
            raise ValueError("unparseable")

        monkeypatch.setattr("olano.config.loader.load_agent_config", _boom)
        assert adapter._agent_commands_config() is None
        assert {name for name, _ in adapter._command_menu_signature()}
