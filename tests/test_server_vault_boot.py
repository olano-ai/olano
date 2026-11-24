"""The daemon must unlock the credentials vault BEFORE booting agents.

Regression guard for the failure where channel tokens written as ``${NAME}``
(e.g. ``${TELEGRAM_BOT_TOKEN}``) silently fell back to a stale ``os.environ``
value because the resolver's vault was still unset when the channel booted.
``set_vault()`` was only ever called by the dashboard's HTTP unlock endpoint,
which runs *after* boot — so a per-agent vault override never reached the
channel and the bot polled the wrong account behind a green "Live" badge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.vault.resolver as resolver
from olano.runtime.server import RuntimeServer
from olano.vault.runtime import VaultUnlockResult


@pytest.fixture(autouse=True)
def _restore_vault():
    """Keep the module-global vault from leaking across tests."""
    original = resolver.get_vault()
    resolver.set_vault(None)
    try:
        yield
    finally:
        resolver.set_vault(original)


async def test_unlock_vault_for_boot_registers_vault_on_success(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    fake_vault = object()

    def fake_unlock(**kwargs):
        # The real unlock_vault_for_session registers via set_vault() on success.
        resolver.set_vault(fake_vault)
        return VaultUnlockResult(vault=fake_vault, mode="env", db_existed=True)

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)

    server = RuntimeServer(agent_names=["merli"])
    server._unlock_vault_for_boot()

    assert resolver.get_vault() is fake_vault


async def test_unlock_vault_for_boot_warns_and_continues_when_locked(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))

    def fake_unlock(**kwargs):
        return VaultUnlockResult(
            vault=None, mode="keyring", db_existed=True, error="wrong passphrase"
        )

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)

    server = RuntimeServer(agent_names=["merli"])
    with caplog.at_level("WARNING"):
        server._unlock_vault_for_boot()  # must not raise

    assert resolver.get_vault() is None
    assert any("locked" in r.message.lower() for r in caplog.records)


async def test_start_unlocks_vault_before_booting_agents(monkeypatch, tmp_path: Path) -> None:
    """The ordering guarantee: the vault is unlocked before the first agent boots."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    server = RuntimeServer(agent_names=["merli"])
    calls: list[str] = []

    class _StopBoot(Exception):
        pass

    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server.webhook_dispatcher, "start", lambda: None)
    monkeypatch.setattr(server, "_unlock_vault_for_boot", lambda: calls.append("unlock"))
    monkeypatch.setattr(server, "_should_boot_on_start", lambda n: True)

    async def fake_api() -> None:  # the API now binds before agents boot
        pass

    monkeypatch.setattr(server, "_start_api", fake_api)

    async def fake_boot(name: str) -> None:
        calls.append("boot")
        raise _StopBoot()

    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    with pytest.raises(_StopBoot):
        await server.start()

    assert calls == ["unlock", "boot"], f"vault must unlock before boot, got {calls}"


# ── #4 interactive prompt only when attended (foreground `serve`) ──────────────


async def test_unlock_for_boot_prompts_only_when_attended(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    seen: dict[str, object] = {}

    def fake_unlock(**kwargs):
        seen["allow_prompt"] = kwargs.get("allow_prompt")
        return VaultUnlockResult(vault=None, mode="none", db_existed=False)

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])

    monkeypatch.setattr(server, "_is_attended", lambda: True)
    server._unlock_vault_for_boot()
    assert seen["allow_prompt"] is True

    monkeypatch.setattr(server, "_is_attended", lambda: False)
    server._unlock_vault_for_boot()
    assert seen["allow_prompt"] is False


# ── #3 opt-in strict mode: LIONCLAW_REQUIRE_VAULT=1 ───────────────────────────


async def test_require_vault_exits_when_locked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_REQUIRE_VAULT", "1")

    def fake_unlock(**kwargs):
        return VaultUnlockResult(vault=None, mode="keyring", db_existed=True, error="wrong key")

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_is_attended", lambda: False)

    with pytest.raises(SystemExit) as exc:
        server._unlock_vault_for_boot()
    assert exc.value.code == 1


async def test_require_vault_absent_does_not_exit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LIONCLAW_REQUIRE_VAULT", raising=False)

    def fake_unlock(**kwargs):
        return VaultUnlockResult(vault=None, mode="keyring", db_existed=True, error="wrong key")

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_is_attended", lambda: False)

    server._unlock_vault_for_boot()  # must NOT raise


async def test_require_vault_does_not_exit_on_fresh_install(monkeypatch, tmp_path: Path) -> None:
    """No vault DB yet (fresh install) → strict mode must not block startup."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_REQUIRE_VAULT", "1")

    def fake_unlock(**kwargs):
        return VaultUnlockResult(vault=None, mode="none", db_existed=False)

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_is_attended", lambda: False)

    server._unlock_vault_for_boot()  # must NOT raise (nothing to unlock)


# ── #1 console confirmation line on foreground `serve` ────────────────────────


async def test_console_notice_on_success_when_attended(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    fake_vault = object()

    def fake_unlock(**kwargs):
        resolver.set_vault(fake_vault)
        return VaultUnlockResult(vault=fake_vault, mode="keyring", db_existed=True)

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_is_attended", lambda: True)

    server._unlock_vault_for_boot()
    assert "Vault unlocked" in capsys.readouterr().out


async def test_no_console_notice_when_detached(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    fake_vault = object()

    def fake_unlock(**kwargs):
        resolver.set_vault(fake_vault)
        return VaultUnlockResult(vault=fake_vault, mode="keyring", db_existed=True)

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", fake_unlock)
    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_is_attended", lambda: False)

    server._unlock_vault_for_boot()
    assert "Vault unlocked" not in capsys.readouterr().out


# ── #2 auto-reboot agents with unresolved secrets after a mid-session unlock ───


def test_flag_unresolved_secrets_returns_paths() -> None:
    from olano.runtime.server import _flag_unresolved_secrets

    cfg = {"type": "telegram", "token": "${TELEGRAM_BOT_TOKEN}", "allowed_users": ["1"]}
    unresolved = _flag_unresolved_secrets(cfg, agent="kevin", channel="telegram")
    assert "token" in unresolved

    resolved = {"type": "telegram", "token": "123:abc"}
    assert _flag_unresolved_secrets(resolved, agent="kevin", channel="telegram") == []


async def test_reboot_unresolved_restarts_booted_agents(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["kevin"])
    server._agents_with_unresolved_secrets = {"kevin"}
    server._runners = {"kevin": object()}  # booted
    seq: list[tuple[str, str]] = []

    async def fake_stop(name: str) -> None:
        seq.append(("stop", name))

    async def fake_boot(name: str) -> None:
        seq.append(("boot", name))

    monkeypatch.setattr(server, "_stop_agent", fake_stop)
    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    rebooted = await server.reboot_unresolved_secret_agents()

    assert rebooted == ["kevin"]
    assert seq == [("stop", "kevin"), ("boot", "kevin")]


async def test_reboot_skips_and_discards_unbooted_agents(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["ghost"])
    server._agents_with_unresolved_secrets = {"ghost"}
    server._runners = {}  # not booted
    called: list[str] = []

    async def fake_boot(name: str) -> None:
        called.append(name)

    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    rebooted = await server.reboot_unresolved_secret_agents()

    assert rebooted == []
    assert called == []
    assert "ghost" not in server._agents_with_unresolved_secrets
