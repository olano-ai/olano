"""Catalog ↔ dispatcher sync + CommandsConfig behavior."""

from __future__ import annotations

import pytest

from olano.cli.commands import CommandDispatcher
from olano.config.agent_config import CommandsConfig, CustomCommand
from olano.config.commands_catalog import (
    ALIAS_TO_CANONICAL,
    COMMAND_BY_NAME,
    COMMAND_SPECS,
    SENSITIVE_COMMANDS,
    canonical_command,
    default_enabled_map,
    default_exposed,
)


def _dispatcher_names() -> set[str]:
    # All names the dispatcher resolves.
    return {name.lstrip("/") for name in CommandDispatcher._handlers}


class TestCatalogSync:
    def test_every_catalog_name_is_a_real_command(self) -> None:
        dispatcher = _dispatcher_names()
        for name, canonical in ALIAS_TO_CANONICAL.items():
            assert name in dispatcher, f"catalog lists /{name} but the dispatcher doesn't"
            assert canonical in COMMAND_BY_NAME

    def test_every_dispatcher_command_is_catalogued(self) -> None:
        catalog_names = set(ALIAS_TO_CANONICAL)
        missing = _dispatcher_names() - catalog_names
        assert not missing, f"dispatcher commands missing from the catalog: {sorted(missing)}"

    def test_groups_are_known(self) -> None:
        from olano.config.commands_catalog import COMMAND_GROUPS

        groups = {g for g, _ in COMMAND_GROUPS}
        for spec in COMMAND_SPECS:
            assert spec.group in groups, f"/{spec.name} has unknown group {spec.group!r}"


class TestDefaults:
    def test_sensitive_default_off(self) -> None:
        for name in (
            "model",
            "mode",
            "trust",
            "reload-vault",
            "share",
            "self-improve",
            "olano",
        ):
            assert name in SENSITIVE_COMMANDS
            assert default_exposed(name) is False

    def test_safe_default_on(self) -> None:
        for name in ("help", "status", "tools", "memory", "new", "verbosity", "pulse"):
            assert default_exposed(name) is True

    def test_canonical_resolves_aliases(self) -> None:
        assert canonical_command("models") == "model"
        assert canonical_command("clear") == "new"
        assert canonical_command("v") == "verbose"
        assert canonical_command("unknown-thing") == "unknown-thing"

    def test_default_map_covers_all(self) -> None:
        m = default_enabled_map()
        assert set(m) == {s.name for s in COMMAND_SPECS}


class TestCommandsConfig:
    def test_empty_uses_catalog_defaults(self) -> None:
        cfg = CommandsConfig()
        assert cfg.is_exposed("help") is True
        assert cfg.is_exposed("trust") is False
        # aliases resolve to canonical
        assert cfg.is_exposed("models") is False  # alias of model (sensitive)

    def test_enabled_override(self) -> None:
        cfg = CommandsConfig(enabled={"trust": True, "memory": False})
        assert cfg.is_exposed("trust") is True
        assert cfg.is_exposed("memory") is False

    def test_alias_key_folds_to_canonical(self) -> None:
        cfg = CommandsConfig(enabled={"models": True})  # alias of model
        assert cfg.enabled == {"model": True}
        assert cfg.is_exposed("model") is True

    def test_custom_command_normalizes_name(self) -> None:
        cfg = CommandsConfig(custom=[CustomCommand(name="/StandUp", prompt="do it")])
        assert cfg.custom[0].name == "standup"
        assert cfg.find_custom("standup") is not None
        assert cfg.find_custom("/standup") is not None

    def test_custom_rejects_builtin_collision(self) -> None:
        with pytest.raises(ValueError, match="collides"):
            CommandsConfig(custom=[CustomCommand(name="help", prompt="x")])
        # aliases collide too
        with pytest.raises(ValueError, match="collides"):
            CommandsConfig(custom=[CustomCommand(name="clear", prompt="x")])

    def test_custom_rejects_bad_name(self) -> None:
        with pytest.raises(ValueError):
            CommandsConfig(custom=[CustomCommand(name="bad name!", prompt="x")])

    def test_custom_rejects_empty_prompt(self) -> None:
        with pytest.raises(ValueError):
            CommandsConfig(custom=[CustomCommand(name="ok", prompt="   ")])

    def test_custom_rejects_duplicates(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            CommandsConfig(
                custom=[
                    CustomCommand(name="a", prompt="x"),
                    CustomCommand(name="a", prompt="y"),
                ]
            )

    def test_roundtrips_through_model_dump(self) -> None:
        cfg = CommandsConfig(
            enabled={"trust": True},
            custom=[CustomCommand(name="standup", prompt="summarize {args}", description="d")],
        )
        restored = CommandsConfig.model_validate(cfg.model_dump())
        assert restored.is_exposed("trust") is True
        assert restored.find_custom("standup").prompt == "summarize {args}"
