"""Channel credentials live in the vault, never in agent.json5.

Prod found six agent.json5 files holding live Telegram bot tokens (2026-07-30).
The docs had always said "prefer an environment reference", but that asked the
user to invent a key, add it to the vault, and type ``${vault:NAME}`` back into
the form — so nobody did. The runtime now does it on save, and every connector
resolves its own credential from the vault by convention, so the config has
nothing to say about it at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from olano.config.channel_secrets import (
    absorb_channel_secrets,
    base_channel_type,
    canonical_secret_key,
    credential_keys,
    hydrate_channel_credentials,
    is_redundant_reference,
    is_reference,
)


class FakeVault:
    """Records what was stored, and under which agent scope."""

    def __init__(self, fail: bool = False) -> None:
        self.stored: dict[tuple[str, str | None], str] = {}
        self.fail = fail

    def set(self, name: str, value: str, agent: str | None = None, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("vault write failed")
        self.stored[(name, agent)] = value


TOKEN = "8182517777:AAHrealbuttestonlyt5jQ"


# ── Key mapping ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ch_type,field_name,expected",
    [
        ("telegram", "token", "TELEGRAM_BOT_TOKEN"),
        ("platform:telegram", "token", "TELEGRAM_BOT_TOKEN"),  # same credential
        ("platform:slack", "app_token", "SLACK_APP_TOKEN"),
        ("slack", "bot_token", "SLACK_BOT_TOKEN"),
        ("platform:debox", "api_secret", "DEBOX_APP_SECRET"),
        ("platform:api_server", "api_key", "OLANO_API_KEY"),
        ("telegram", "allowed_users", None),  # not a secret
        ("webhook", "token", None),  # unknown channel
    ],
)
def test_canonical_key_lookup(ch_type, field_name, expected):
    assert canonical_secret_key(ch_type, field_name) == expected


def test_base_channel_type_strips_the_platform_prefix():
    assert base_channel_type("platform:telegram") == "telegram"
    assert base_channel_type("telegram") == "telegram"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("${vault:TELEGRAM_BOT_TOKEN}", True),
        ("${TELEGRAM_BOT_TOKEN}", True),
        (TOKEN, False),
        ("", False),
        (None, False),
    ],
)
def test_is_reference(value, expected):
    assert is_reference(value) is expected


# ── Absorbing ─────────────────────────────────────────────────────────────────


def test_literal_token_moves_to_a_per_agent_override():
    data = {"channels": [{"type": "telegram", "token": TOKEN, "allowed_users": ["1"]}]}
    vault = FakeVault()

    report = absorb_channel_secrets("marisol", data, vault=vault)

    assert "token" not in data["channels"][0]  # resolved by convention at boot
    assert vault.stored == {("TELEGRAM_BOT_TOKEN", "marisol"): TOKEN}
    assert report.vaulted == [("telegram", "token", "TELEGRAM_BOT_TOKEN")]
    assert data["channels"][0]["allowed_users"] == ["1"]  # non-secrets untouched


def test_two_agents_share_the_key_name_and_keep_separate_values():
    """The whole point of per-agent overrides: one obvious name, N values."""
    vault = FakeVault()
    a = {"channels": [{"type": "telegram", "token": "token-a"}]}
    b = {"channels": [{"type": "platform:telegram", "token": "token-b"}]}

    absorb_channel_secrets("marisol", a, vault=vault)
    absorb_channel_secrets("andrew", b, vault=vault)

    assert vault.stored == {
        ("TELEGRAM_BOT_TOKEN", "marisol"): "token-a",
        ("TELEGRAM_BOT_TOKEN", "andrew"): "token-b",
    }
    assert "token" not in a["channels"][0]
    assert "token" not in b["channels"][0]


def test_a_reference_to_another_key_is_left_alone():
    """Pointing at a non-canonical key is a deliberate choice — never touch it."""
    data = {"channels": [{"type": "telegram", "token": "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"}]}
    vault = FakeVault()

    report = absorb_channel_secrets("marisol", data, vault=vault)

    assert vault.stored == {}
    assert not report.changed
    assert data["channels"][0]["token"] == "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"


@pytest.mark.parametrize("value", ["${vault:TELEGRAM_BOT_TOKEN}", "${TELEGRAM_BOT_TOKEN}"])
def test_a_reference_to_the_canonical_key_is_dropped(value):
    """It says exactly what the convention already says — nothing to keep."""
    data = {"channels": [{"type": "telegram", "token": value, "allowed_users": ["1"]}]}
    vault = FakeVault()

    report = absorb_channel_secrets("marisol", data, vault=vault)

    assert "token" not in data["channels"][0]
    assert data["channels"][0]["allowed_users"] == ["1"]
    assert vault.stored == {}  # already vaulted; nothing to write
    assert report.stripped == [("telegram", "token", "TELEGRAM_BOT_TOKEN")]
    assert report.changed


def test_absorbing_twice_is_a_no_op():
    data = {"channels": [{"type": "telegram", "token": TOKEN}]}
    vault = FakeVault()

    absorb_channel_secrets("marisol", data, vault=vault)
    second = absorb_channel_secrets("marisol", data, vault=vault)

    assert not second.changed
    assert len(vault.stored) == 1


def test_multiple_secret_fields_on_one_channel():
    data = {"channels": [{"type": "platform:slack", "token": "xoxb-1", "app_token": "xapp-1"}]}
    vault = FakeVault()

    absorb_channel_secrets("kev", data, vault=vault)

    assert vault.stored == {
        ("SLACK_BOT_TOKEN", "kev"): "xoxb-1",
        ("SLACK_APP_TOKEN", "kev"): "xapp-1",
    }


def test_a_locked_vault_never_loses_the_token(monkeypatch):
    """No vault ⇒ leave the literal exactly where it was and say so."""
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: None)
    data = {"channels": [{"type": "telegram", "token": TOKEN}]}

    report = absorb_channel_secrets("marisol", data, vault=None)

    assert report.vault_available is False
    assert data["channels"][0]["token"] == TOKEN
    assert report.left_in_config == [("telegram", "token")]
    assert not report.changed


def test_a_failed_vault_write_leaves_that_field_untouched():
    data = {"channels": [{"type": "telegram", "token": TOKEN}]}

    report = absorb_channel_secrets("marisol", data, vault=FakeVault(fail=True))

    assert data["channels"][0]["token"] == TOKEN
    assert report.left_in_config == [("telegram", "token")]
    assert not report.changed


def test_dry_run_reports_without_writing():
    data = {"channels": [{"type": "telegram", "token": TOKEN}]}
    vault = FakeVault()

    report = absorb_channel_secrets("marisol", data, vault=vault, dry_run=True)

    assert report.vaulted == [("telegram", "token", "TELEGRAM_BOT_TOKEN")]
    assert vault.stored == {}
    assert data["channels"][0]["token"] == TOKEN  # untouched


@pytest.mark.parametrize(
    "channels",
    [
        None,
        [],
        ["telegram"],  # bare string form carries no credential
        [{"type": "telegram"}],  # no token set yet
    ],
)
def test_nothing_to_do_shapes(channels):
    data: dict[str, Any] = {} if channels is None else {"channels": channels}
    report = absorb_channel_secrets("marisol", data, vault=FakeVault())
    assert not report.changed


# ── Hydrating (the read side) ─────────────────────────────────────────────────


class FakeResolvingVault:
    """Vault with per-agent overrides and common values, like the real one."""

    def __init__(self, common=None, overrides=None) -> None:
        self.common = common or {}
        self.overrides = overrides or {}  # (name, agent) → value

    def get(self, name, agent=None, **kw):
        if agent and (name, agent) in self.overrides:
            return self.overrides[(name, agent)]
        if name in self.common:
            return self.common[name]
        raise KeyError(name)

    def list_names(self, category=None):
        return [{"name": n, "category": "general"} for n in self.common]

    def list_overrides(self, name=None, agent=None):
        return [
            {"name": n, "agent": a}
            for (n, a) in self.overrides
            if (name is None or n == name) and (agent is None or a == agent)
        ]


@pytest.fixture
def resolving_vault(monkeypatch):
    def _install(common=None, overrides=None):
        vault = FakeResolvingVault(common, overrides)
        monkeypatch.setattr("olano.vault.resolver._vault_instance", vault, raising=False)
        return vault

    return _install


def test_hydrate_prefers_the_per_agent_override(resolving_vault):
    resolving_vault(
        common={"TELEGRAM_BOT_TOKEN": "shared"},
        overrides={("TELEGRAM_BOT_TOKEN", "marisol"): "marisols-own"},
    )
    cfg = {"allowed_users": ["1"]}

    result = hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert cfg["token"] == "marisols-own"
    assert result.ok
    assert result.injected == [("token", "TELEGRAM_BOT_TOKEN", "vault:agent")]


def test_hydrate_falls_back_to_the_common_value(resolving_vault):
    resolving_vault(common={"TELEGRAM_BOT_TOKEN": "shared"})
    cfg: dict = {}

    result = hydrate_channel_credentials("platform:telegram", cfg, agent="andrew")

    assert cfg["token"] == "shared"
    assert result.injected == [("token", "TELEGRAM_BOT_TOKEN", "vault:common")]


def test_hydrate_falls_back_to_the_environment(resolving_vault, monkeypatch):
    resolving_vault()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    cfg: dict = {}

    hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert cfg["token"] == "from-env"


def test_hydrate_falls_back_to_a_renamed_keys_old_name(resolving_vault):
    """A renamed credential must never silently un-authenticate a connector.

    ``api_server`` is the sharp case: its credential is deliberately NOT
    required (an open loopback server is legitimate), so a lookup that misses
    produces no ``missing`` entry, no warning and no boot refusal — the
    connector just comes up with bearer auth switched off. The fallback is what
    makes the rename safe on a box whose vault still holds the old row.
    """
    resolving_vault(overrides={("LIONCLAW_API_KEY", "marisol"): "still-here"})
    cfg: dict = {}

    result = hydrate_channel_credentials("platform:api_server", cfg, agent="marisol")

    assert cfg["api_key"] == "still-here"
    # Reported under the name it actually came from, so diagnostics do not
    # claim a row exists under the canonical name when it does not.
    assert result.injected == [("api_key", "LIONCLAW_API_KEY", "vault:agent")]


def test_hydrate_prefers_the_canonical_name_over_the_legacy_one(resolving_vault):
    resolving_vault(
        overrides={
            ("OLANO_API_KEY", "marisol"): "current",
            ("LIONCLAW_API_KEY", "marisol"): "stale",
        }
    )
    cfg: dict = {}

    hydrate_channel_credentials("platform:api_server", cfg, agent="marisol")

    assert cfg["api_key"] == "current"


def test_hydrate_reports_a_required_credential_it_cannot_find(resolving_vault, monkeypatch):
    resolving_vault()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg: dict = {}

    result = hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert not result.ok
    assert result.missing == [("token", "TELEGRAM_BOT_TOKEN")]
    assert "token" not in cfg


def test_hydrate_never_overwrites_an_explicit_value(resolving_vault):
    """A config that still carries a literal (pre-migration) keeps working."""
    resolving_vault(overrides={("TELEGRAM_BOT_TOKEN", "marisol"): "vaulted"})
    cfg = {"token": "literal-still-in-config"}

    result = hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert cfg["token"] == "literal-still-in-config"
    assert result.injected == []


def test_hydrate_treats_an_unresolved_placeholder_as_absent(resolving_vault):
    """A locked vault leaves ${...} behind — that is not a token. Naming this
    field's own key, it resolves to exactly what the convention would give."""
    resolving_vault(overrides={("TELEGRAM_BOT_TOKEN", "marisol"): "vaulted"})
    cfg = {"token": "${vault:TELEGRAM_BOT_TOKEN}"}

    hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert cfg["token"] == "vaulted"


def test_an_unresolved_reference_to_another_key_is_never_substituted(resolving_vault):
    """The operator pointed this connector at a specific key. If that did not
    resolve, the vault is locked or the entry is missing — quietly falling back
    to the canonical credential would authenticate as a DIFFERENT bot."""
    resolving_vault(common={"TELEGRAM_BOT_TOKEN": "somebody-elses-bot"})
    cfg = {"token": "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"}

    result = hydrate_channel_credentials("telegram", cfg, agent="marisol")

    assert cfg["token"] == "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"  # left for the caller to flag
    assert result.injected == []
    assert result.missing == []  # not "missing" — it is pointed somewhere real


def test_hydrate_leaves_optional_credentials_missing_without_complaint(resolving_vault):
    resolving_vault()
    cfg: dict = {}

    result = hydrate_channel_credentials("email", cfg, agent="marisol")

    assert result.ok  # email can authenticate anonymously
    assert cfg == {}


def test_slack_injects_the_field_the_adapter_reads(resolving_vault):
    """CHANNEL_SECRET_KEYS aliases slack.token → SLACK_BOT_TOKEN; the adapter
    reads bot_token, so only that one is injected."""
    resolving_vault(common={"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"})
    cfg: dict = {}

    hydrate_channel_credentials("slack", cfg, agent="kev")

    assert cfg == {"bot_token": "xoxb-1", "app_token": "xapp-1"}
    assert "token" not in cfg


def test_credential_keys_are_the_contract_the_ui_shows():
    assert credential_keys("telegram") == {"token": "TELEGRAM_BOT_TOKEN"}
    assert credential_keys("platform:telegram") == {"token": "TELEGRAM_BOT_TOKEN"}
    assert credential_keys("slack") == {
        "bot_token": "SLACK_BOT_TOKEN",
        "app_token": "SLACK_APP_TOKEN",
    }
    assert credential_keys("platform:api_server") == {"api_key": "OLANO_API_KEY"}
    assert credential_keys("webhook") == {}


@pytest.mark.parametrize(
    "value,key,expected",
    [
        ("${vault:TELEGRAM_BOT_TOKEN}", "TELEGRAM_BOT_TOKEN", True),
        ("${TELEGRAM_BOT_TOKEN}", "TELEGRAM_BOT_TOKEN", True),
        ("  ${TELEGRAM_BOT_TOKEN}  ", "TELEGRAM_BOT_TOKEN", True),
        ("${vault:MARISOL_TOKEN}", "TELEGRAM_BOT_TOKEN", False),
        ("8182:AAH", "TELEGRAM_BOT_TOKEN", False),
    ],
)
def test_is_redundant_reference(value, key, expected):
    assert is_redundant_reference(value, key) is expected
