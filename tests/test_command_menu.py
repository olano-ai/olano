"""Per-agent command menus — what connectors advertise vs. what the gate runs.

The dashboard Commands tab controls which built-ins an agent exposes and adds
custom commands, and ``platforms.inbound.maybe_handle_command`` honours it. The
menus published to connectors did not: Telegram's ``/`` list and Discord's slash
tree were built from a hardcoded list, so every agent advertised every command —
including the sensitive ones that are off by default on messaging platforms —
and tapping one answered "🔒 … is disabled for this agent". Custom commands
never appeared at all.

These tests pin the two ends together: the catalog is the single source, and the
menu is derived from it *through* the agent's config.
"""

from __future__ import annotations

from olano.cli.commands import (
    SLASH_COMMAND_DESCRIPTIONS,
    agent_menu_commands,
    gateway_command_registry,
    slack_subcommand_map,
    telegram_menu_commands,
    telegram_unlistable_custom,
)
from olano.config.agent_config import CommandsConfig, CustomCommand
from olano.config.commands_catalog import COMMAND_SPECS, SENSITIVE_COMMANDS


def _names(commands=None, **kwargs) -> list[str]:
    return [entry.name for entry in agent_menu_commands(commands, **kwargs)]


class TestCatalogIsTheOnlySource:
    """``SLASH_COMMAND_DESCRIPTIONS`` is derived, not hand-maintained.

    It used to be a second hand-written list of the same 32 commands with
    different wording and no notion of which are sensitive — which is how the
    published menus drifted away from the exposure config in the first place.
    """

    def test_names_match_the_catalog(self) -> None:
        assert set(SLASH_COMMAND_DESCRIPTIONS) == {f"/{s.name}" for s in COMMAND_SPECS}

    def test_descriptions_match_the_catalog(self) -> None:
        for spec in COMMAND_SPECS:
            assert SLASH_COMMAND_DESCRIPTIONS[f"/{spec.name}"] == spec.description


class TestDefaultAgentMenu:
    def test_sensitive_commands_are_not_advertised(self) -> None:
        names = set(_names(CommandsConfig()))
        assert not (names & SENSITIVE_COMMANDS), (
            "a chat menu must not offer commands the gate refuses by default"
        )
        # …while the ordinary read-only ones are all there.
        assert {"help", "status", "context", "tools", "new"} <= names

    def test_terminal_only_commands_are_gone_everywhere(self) -> None:
        # /stop, /exit, /quit and /bash belonged to the removed terminal chat.
        for owner_console in (False, True):
            names = set(_names(CommandsConfig(), owner_console=owner_console))
            assert not (names & {"stop", "exit", "quit", "bash"})

    def test_no_config_keeps_every_builtin(self) -> None:
        """A caller with no agent in hand still gets the full list."""
        assert "model" in _names(None)


class TestExposureOverrides:
    def test_enabling_and_disabling_both_reach_the_menu(self) -> None:
        cfg = CommandsConfig(enabled={"model": True, "help": False})
        names = _names(cfg)
        assert "model" in names, "an owner-enabled sensitive command must be advertised"
        assert "help" not in names, "an owner-disabled command must be hidden"

    def test_aliases_resolve_to_the_canonical_name(self) -> None:
        # `enabled` folds aliases onto canonical names; /models is /model.
        cfg = CommandsConfig(enabled={"models": True})
        assert "model" in _names(cfg)

    def test_owner_console_keeps_sensitive_but_honours_an_explicit_off(self) -> None:
        """The dashboard chat rule: on by default, only an explicit off hides."""
        cfg = CommandsConfig(enabled={"memory": False})
        names = _names(cfg, owner_console=True)
        assert {"model", "mode", "share"} <= set(names)
        assert "memory" not in names


class TestCustomCommands:
    def test_custom_commands_are_advertised_with_their_description(self) -> None:
        cfg = CommandsConfig(
            custom=[CustomCommand(name="standup", prompt="Summarize. {args}", description="Daily")]
        )
        entry = next(e for e in agent_menu_commands(cfg) if e.name == "standup")
        assert entry.custom is True
        assert entry.description == "Daily"

    def test_blank_description_falls_back_to_the_prompt(self) -> None:
        """A menu row with no text is useless; the prompt's first line says more."""
        cfg = CommandsConfig(custom=[CustomCommand(name="brief", prompt="Write a brief.\nrest")])
        entry = next(e for e in agent_menu_commands(cfg) if e.name == "brief")
        assert entry.description == "Write a brief."

    def test_custom_commands_reach_the_slack_subcommand_map(self) -> None:
        cfg = CommandsConfig(custom=[CustomCommand(name="standup", prompt="Summarize.")])
        assert slack_subcommand_map(cfg)["standup"] == "/standup"

    def test_slack_keeps_disabled_builtins_so_the_gate_can_refuse_them(self) -> None:
        """Dropping them would forward `/olano model gpt-4` to the agent as prose."""
        cfg = CommandsConfig(enabled={"model": False})
        assert slack_subcommand_map(cfg)["model"] == "/model"


class TestTelegramMenu:
    def test_menu_matches_the_agent_config(self) -> None:
        cfg = CommandsConfig(
            enabled={"model": True},
            custom=[CustomCommand(name="standup", prompt="Summarize.", description="Daily")],
        )
        names = {name for name, _desc in telegram_menu_commands(commands=cfg)[0]}
        assert "model" in names
        assert "standup" in names
        assert "mode" not in names  # sensitive, left off

    def test_hyphenated_custom_names_are_dropped_and_reported(self) -> None:
        """Custom names may contain '-'; Telegram's menu names may not.

        One invalid name makes setMyCommands reject the WHOLE batch, so it has
        to be filtered — and reported, or the owner just sees it missing.
        """
        cfg = CommandsConfig(
            custom=[
                CustomCommand(name="my-cmd", prompt="Do it."),
                CustomCommand(name="my_cmd", prompt="Do it."),
            ]
        )
        names = {name for name, _ in telegram_menu_commands(commands=cfg)[0]}
        assert "my-cmd" not in names
        assert "my_cmd" in names
        assert telegram_unlistable_custom(cfg) == ["my-cmd"]

    def test_descriptions_are_clamped_to_40_chars(self) -> None:
        for _name, desc in telegram_menu_commands()[0]:
            assert len(desc) <= 40

    def test_custom_commands_survive_the_cap(self) -> None:
        """Built-ins are trimmed first — the owner added the custom ones on purpose."""
        cfg = CommandsConfig(
            custom=[CustomCommand(name="standup", prompt="Summarize.", description="Daily")]
        )
        menu, hidden = telegram_menu_commands(max_commands=3, commands=cfg)
        assert len(menu) == 3
        assert "standup" in {name for name, _ in menu}
        assert hidden > 0


class TestGatewayRegistry:
    def test_registry_follows_the_agent_config(self) -> None:
        cfg = CommandsConfig(
            enabled={"share": True},
            custom=[CustomCommand(name="standup", prompt="Summarize.")],
        )
        entries = {e.name: e for e in gateway_command_registry(cfg)}
        assert "share" in entries
        assert entries["standup"].custom is True
        assert "olano" not in entries  # still sensitive, still off

    def test_builtin_entries_keep_their_arg_hints(self) -> None:
        entries = {e.name: e for e in gateway_command_registry()}
        assert entries["tools"].args_hint == "compact|verbose|full"
        assert entries["help"].args_hint == ""
