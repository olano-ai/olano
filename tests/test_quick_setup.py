"""Tests for the non-interactive `olano quick-setup` bootstrap command."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from olano.cli.main import cli
from olano.cli.quick_setup import DEFAULT_ADMIN_EMAIL, run_quick_setup
from olano.config.home import get_olano_paths, load_home_config
from olano.config.loader import list_agents
from olano.users import initial_password
from olano.users.store import get_user_store, reset_user_store
from olano.vault.runtime import read_boot_key, unlock_vault_for_session
from olano.vault.store import VaultStore, default_vault_path


@pytest.fixture(autouse=True)
def _reset_user_store(monkeypatch: pytest.MonkeyPatch):
    """Drop the cached per-home user store before/after each test, and keep the
    developer's own vault key out of the vault step (it decides whether a
    passphrase is generated)."""
    monkeypatch.delenv("LIONCLAW_VAULT_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_VAULT_PASSWORD", raising=False)
    reset_user_store()
    yield
    reset_user_store()


@pytest.fixture(autouse=True)
def _stub_catalog_fetch(monkeypatch: pytest.MonkeyPatch):
    """Stub the external catalog clones so tests never touch git/the network.

    quick-setup now calls ``fetch_lionclaw_agents`` (the archetype pack) and
    ``fetch_olano_skills`` (the skills pack) — both real ``git clone``s. Patch
    the source modules they import lazily so every test stays offline.
    """
    calls: dict[str, list[dict]] = {"archetypes": [], "skills": []}

    def _fake_fetch(**kwargs):
        calls["archetypes"].append(kwargs)
        return True, "Pack cloned (stubbed)."

    def _fake_skills_fetch(home=None, **kwargs):
        calls["skills"].append({"home": home, **kwargs})
        return True, "Skill pack cloned (stubbed)."

    monkeypatch.setattr("olano.archetypes.sync.fetch_lionclaw_agents", _fake_fetch)
    monkeypatch.setattr("olano.skills.sync.fetch_olano_skills", _fake_skills_fetch)
    return calls


def _assert_fully_provisioned(home: Path, *, agent: str = "merli") -> None:
    # 1. Home scaffold exists.
    paths = get_olano_paths()
    assert paths.root == home
    assert paths.agents_dir.is_dir()
    assert paths.config_path.exists()

    # 2. Workspace seeded for the agent (AGENTS.md + MEMORY.md + system knowledge).
    workspace = paths.agent_workspace_dir(agent)
    assert (workspace / "AGENTS.md").exists()
    assert (workspace / "MEMORY.md").exists()
    assert (workspace / "memories").is_dir()
    assert (workspace / "system" / "knowledge" / "USER_GUIDE.md").exists()

    # 3. Agent is listed and is the home default.
    assert agent in list_agents()
    assert load_home_config().default_agent == agent

    # 4. An admin user exists and multi-user mode is on.
    store = get_user_store()
    assert store.db.count_users() >= 1
    admin = store.db.get_user_by_email(DEFAULT_ADMIN_EMAIL)
    assert admin is not None
    assert admin.is_admin
    assert load_home_config().multi_user_enabled is True

    # 5. Vault is initialized.
    assert default_vault_path().exists()

    # 6. Built-in skills were synced into the managed skills dir.
    skills_dir = paths.managed_skills_dir
    assert skills_dir.is_dir()
    synced = [d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    assert len(synced) >= 1


def test_run_quick_setup_core(lionclaw_home: Path) -> None:
    result = run_quick_setup(log=lambda _msg: None)

    assert result.home == lionclaw_home
    statuses = dict(result.steps)
    assert statuses["home"] == "ready"
    assert statuses["agent"] == "installed"
    assert statuses["admin"] == "created"
    assert statuses["vault"] == "initialized"
    assert statuses["skills"] == "synced"
    assert statuses["skills_catalog"] == "fetched"
    assert statuses["catalog"] == "fetched"

    _assert_fully_provisioned(lionclaw_home)

    # Nobody supplied a password: a one-time one was generated, reported on the
    # result (the CLI prints it once) and kept beside the home, owner-readable
    # only, until the first password change.
    generated = result.generated_admin_password
    assert generated and len(generated) >= 16
    admin = get_user_store().authenticate(DEFAULT_ADMIN_EMAIL, generated)
    assert admin is not None and admin.is_admin
    assert result.initial_password_path == initial_password.path()
    assert initial_password.read() == (DEFAULT_ADMIN_EMAIL, generated)
    assert (initial_password.path().stat().st_mode & 0o777) == 0o600

    # Nobody supplied a vault passphrase either: generated, and persisted to
    # the boot-key file, which is what `olano serve` unlocks the vault from.
    assert result.vault_passphrase_generated and result.vault_unlock == "bootfile"
    assert result.vault_password and read_boot_key() == result.vault_password
    unlocked = unlock_vault_for_session(register=False)
    assert unlocked.vault is not None and unlocked.mode == "bootfile"


def test_run_quick_setup_is_idempotent(lionclaw_home: Path) -> None:
    first = run_quick_setup(log=lambda _msg: None)
    reset_user_store()
    second = run_quick_setup(log=lambda _msg: None)

    statuses = dict(second.steps)
    assert statuses["agent"] == "skipped"
    assert statuses["admin"] == "skipped"
    assert statuses["vault"] == "skipped"

    # Still exactly one admin — re-run did not duplicate the account.
    assert get_user_store().db.count_users() == 1
    _assert_fully_provisioned(lionclaw_home)

    # And nothing was re-generated: the note and the boot key are the first run's.
    assert second.generated_admin_password is None
    assert initial_password.read() == (DEFAULT_ADMIN_EMAIL, first.generated_admin_password)
    assert second.vault_passphrase_generated is False
    assert second.vault_unlock == "bootfile" and read_boot_key() == first.vault_password


def test_env_vault_key_is_used_and_no_boot_key_is_written(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compose posture: LIONCLAW_VAULT_KEY baked into the container env is
    what the runtime unlocks with, so the vault is created under it and no
    second key lands on disk."""
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "from-the-env")
    result = run_quick_setup(sync_catalogs=False, log=lambda _msg: None)
    assert dict(result.steps)["vault"] == "initialized"
    assert result.vault_unlock == "env" and result.vault_passphrase_generated is False
    assert read_boot_key() is None
    # The vault was created under the env key: opening with it verifies cleanly.
    VaultStore.open(
        passphrase="from-the-env", db_path=default_vault_path(), use_keyring=False
    ).verify_key()


def test_an_existing_vault_nothing_on_the_box_can_open_is_left_alone(lionclaw_home: Path) -> None:
    """An operator's own vault (passphrase typed at `olano serve`): a re-run
    with no passphrase must neither re-key it nor plant a boot key for it."""
    VaultStore.open(
        passphrase="operators-own", db_path=default_vault_path(), use_keyring=False
    ).set("PROBE", "v")
    result = run_quick_setup(sync_catalogs=False, log=lambda _msg: None)
    assert dict(result.steps)["vault"] == "skipped"
    assert result.vault_unlock == "none" and result.vault_password is None
    assert read_boot_key() is None
    assert (
        VaultStore.open(
            passphrase="operators-own", db_path=default_vault_path(), use_keyring=False
        ).get("PROBE")
        == "v"
    )


def test_an_explicit_password_leaves_no_note_behind(lionclaw_home: Path) -> None:
    result = run_quick_setup(
        admin_password="chosen-by-the-operator", sync_catalogs=False, log=lambda _msg: None
    )
    assert dict(result.steps)["admin"] == "created"
    assert result.generated_admin_password is None and result.initial_password_path is None
    assert initial_password.read() is None


def test_quick_setup_cli_yes(lionclaw_home: Path) -> None:
    result = CliRunner().invoke(cli, ["quick-setup", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Quick-setup complete" in result.output
    # The generated password is printed exactly once, with where it is kept,
    # and nothing in the output calls anything insecure or asks for an export:
    # the vault passphrase went to the boot-key file.
    assert result.output.count("One-time password") == 1
    # The panel folds a long path across lines (pytest's tmp paths are long),
    # so compare against the output with the box drawing and whitespace removed.
    flat = "".join(ch for ch in result.output if not ch.isspace() and ch not in "│╭╮╰╯─")
    assert str(initial_password.path()) in flat
    assert "INSECURE" not in result.output and "88888888" not in result.output
    assert "export OLANO_VAULT_KEY" not in result.output
    _, noted = initial_password.read() or ("", "")
    assert noted and noted in result.output
    _assert_fully_provisioned(lionclaw_home)


def test_quick_setup_cli_custom_creds(lionclaw_home: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "quick-setup",
            "--yes",
            "--admin-email",
            "ops@example.com",
            "--admin-password",
            "str0ng-pass!",
            "--admin-name",
            "Ops",
            "--vault-password",
            "vaulty-pass!",
        ],
    )
    assert result.exit_code == 0, result.output
    # Nothing generated: no one-time password, no note, no boot key. The
    # operator's own vault passphrase has to reach `olano serve` through the
    # env, so that export line is the one thing the CLI still has to say.
    assert "One-time password" not in result.output
    assert initial_password.read() is None
    assert read_boot_key() is None
    assert "export OLANO_VAULT_KEY=vaulty-pass!" in result.output

    store = get_user_store()
    admin = store.db.get_user_by_email("ops@example.com")
    assert admin is not None and admin.is_admin
    assert store.authenticate("ops@example.com", "str0ng-pass!") is not None
    assert default_vault_path().exists()


def test_quick_setup_cli_aborts_without_yes(lionclaw_home: Path) -> None:
    # Declining the confirmation prompt cancels cleanly without provisioning.
    result = CliRunner().invoke(cli, ["quick-setup"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert get_user_store().db.count_users() == 0


def test_quick_setup_fetches_catalog(lionclaw_home: Path, _stub_catalog_fetch) -> None:
    # quick-setup must attempt to clone the external expert catalog (archetype
    # pack) and the external skills catalog exactly once each.
    run_quick_setup(log=lambda _msg: None)
    assert len(_stub_catalog_fetch["archetypes"]) == 1
    assert len(_stub_catalog_fetch["skills"]) == 1


def test_sync_catalogs_off_stops_after_the_vault(lionclaw_home: Path, _stub_catalog_fetch) -> None:
    """The runtime's first-boot call must never wait on git: with the flag off
    the run ends at the vault and the pack clones are not attempted."""
    result = run_quick_setup(sync_catalogs=False, log=lambda _msg: None)
    statuses = dict(result.steps)
    assert statuses["admin"] == "created"
    assert statuses["vault"] == "initialized"
    assert "skills" not in statuses and "catalog" not in statuses
    assert _stub_catalog_fetch["archetypes"] == [] and _stub_catalog_fetch["skills"] == []
    assert result.ok


# ── the admin account always lands ───────────────────────────────────────────
#
# On a freshly provisioned managed box the deployment settings can still resolve
# to the quarantine posture (artifact not fetched yet), under which the config
# write guard refuses to create any agent. The AgentFather install raised, the
# run died before its admin step, and the owner's first visit to the dashboard
# was the account-creation screen — no temporary password asked, because there
# was no account it could have belonged to.


def _refuse_install(monkeypatch: pytest.MonkeyPatch, message: str) -> dict:
    """Make the archetype install raise until ``switch["refuse"]`` is cleared.

    A toggle rather than ``monkeypatch.undo()``: undo would also drop the
    autouse home fixture's ``LIONCLAW_HOME`` and point the re-run at the
    developer's real home.
    """
    from olano.archetypes import installer

    real = installer.install_archetype
    switch = {"refuse": True}

    def _gate(*a, **k):
        if switch["refuse"]:
            raise RuntimeError(message)
        return real(*a, **k)

    monkeypatch.setattr("olano.archetypes.installer.install_archetype", _gate)
    return switch


def test_a_refused_agent_install_does_not_abort_the_admin_step(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_install(monkeypatch, "This deployment is quarantined — creating agents is disabled")
    result = run_quick_setup(log=lambda _msg: None)

    statuses = dict(result.steps)
    assert statuses["agent"].startswith("failed: This deployment is quarantined")
    assert statuses["admin"] == "created"
    assert statuses["vault"] == "initialized"
    assert result.failed_steps() == [
        ("agent", "This deployment is quarantined — creating agents is disabled")
    ]
    assert not result.ok
    # The security-critical half is in place: an account exists and gates the box.
    store = get_user_store()
    assert store.db.count_users() == 1
    assert load_home_config().multi_user_enabled is True
    assert "merli" not in list_agents(include_hidden=True)


def test_a_rerun_retries_only_the_failed_step(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = _refuse_install(monkeypatch, "not yet")
    run_quick_setup(log=lambda _msg: None)
    switch["refuse"] = False
    reset_user_store()

    second = run_quick_setup(log=lambda _msg: None)
    statuses = dict(second.steps)
    assert statuses["agent"] == "installed"
    assert statuses["admin"] == "skipped"
    assert second.ok
    assert get_user_store().db.count_users() == 1
    _assert_fully_provisioned(lionclaw_home)


def test_a_vault_failure_is_reported_not_raised(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_k):
        raise ValueError("wrong passphrase")

    monkeypatch.setattr("olano.vault.store.VaultStore.open", _boom)
    result = run_quick_setup(log=lambda _msg: None)
    assert dict(result.steps)["vault"] == "failed: wrong passphrase"
    assert dict(result.steps)["admin"] == "created"
    assert result.failed_steps() == [("vault", "wrong passphrase")]


def test_an_admin_failure_is_recorded_and_the_vault_still_lands(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self, *_a, **_k):
        raise RuntimeError("users.db is read-only")

    monkeypatch.setattr("olano.users.store.UserStore.bootstrap_admin", _boom)
    result = run_quick_setup(log=lambda _msg: None)
    statuses = dict(result.steps)
    assert statuses["admin"] == "failed: users.db is read-only"
    assert statuses["agent"] == "installed"
    assert statuses["vault"] == "initialized"
    assert result.failed_steps() == [("admin", "users.db is read-only")]


def test_quick_setup_cli_exits_nonzero_when_a_step_failed(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy scripts read a non-zero exit as 'continue' — but the operator
    must see WHICH step failed, after the admin has been announced."""
    _refuse_install(monkeypatch, "quarantined")
    result = CliRunner().invoke(cli, ["quick-setup", "--yes"])
    assert result.exit_code == 1, result.output
    assert "Quick-setup complete" in result.output
    assert "Some steps failed" in result.output
    assert "agent" in result.output and "quarantined" in result.output
    assert get_user_store().db.count_users() == 1


def test_skills_sync_cli_populates_home(lionclaw_home: Path) -> None:
    result = CliRunner().invoke(cli, ["skills", "sync"])
    assert result.exit_code == 0, result.output
    assert "Skills:" in result.output

    skills_dir = get_olano_paths().managed_skills_dir
    synced = [d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    assert len(synced) >= 1


# ── the settings settle before the one step that is judged by them ───────────


def _posture(posture, *, error=None, source="quarantine", mode="cloud"):
    from olano.deployment.resolver import DeploymentStatus
    from olano.deployment.schema import DeploymentSettings

    return DeploymentStatus(
        posture=posture, source=source, settings=DeploymentSettings(), mode=mode, error=error
    )


def test_the_agent_install_waits_for_a_managed_box_to_receive_its_settings(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: quick-setup reached the install 10s after the runtime came
    up, before the settings artifact, and the 'no artifact yet' quarantine
    refused it. It now waits (bounded) and installs against the real posture."""
    from olano.deployment import resolver

    current = {"status": _posture("quarantine", error="no_artifact")}
    monkeypatch.setattr(resolver, "deployment_status", lambda **kw: current["status"])
    waited: list[float] = []

    def _wait(limit, **kw):
        waited.append(limit)
        current["status"] = _posture("open", mode=None, source="none")
        return current["status"]

    monkeypatch.setattr(resolver, "wait_until_settled", _wait)
    lines: list[str] = []
    result = run_quick_setup(sync_catalogs=False, log=lines.append)
    assert waited == [resolver.DEFAULT_SETTLE_SECONDS]
    assert dict(result.steps)["agent"] == "installed"
    assert any("Waiting up to" in line for line in lines)
    assert any("arrived" in line for line in lines)


def test_no_wait_when_the_posture_is_already_settled(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.deployment import resolver

    monkeypatch.setattr(
        resolver, "deployment_status", lambda **kw: _posture("open", mode=None, source="none")
    )

    def _never(*a, **k):
        raise AssertionError("waited on a settled box")

    monkeypatch.setattr(resolver, "wait_until_settled", _never)
    lines: list[str] = []
    result = run_quick_setup(sync_catalogs=False, log=lines.append)
    assert dict(result.steps)["agent"] == "installed"
    assert not any("Waiting" in line for line in lines), "a normal run's transcript is unchanged"


def test_a_revoked_box_does_not_wait_for_settings_that_will_never_come(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.deployment import resolver

    monkeypatch.setattr(
        resolver, "deployment_status", lambda **kw: _posture("quarantine", error="revoked: 401")
    )

    def _never(*a, **k):
        raise AssertionError("waited on a revoked box")

    monkeypatch.setattr(resolver, "wait_until_settled", _never)
    run_quick_setup(sync_catalogs=False, log=lambda _m: None)  # must not raise


def test_a_zero_settle_timeout_disables_the_wait(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.deployment import resolver

    monkeypatch.setenv(resolver.SETTLE_TIMEOUT_ENV, "0")
    monkeypatch.setattr(
        resolver, "deployment_status", lambda **kw: _posture("quarantine", error="no_artifact")
    )

    def _never(*a, **k):
        raise AssertionError("waited with the wait disabled")

    monkeypatch.setattr(resolver, "wait_until_settled", _never)
    run_quick_setup(sync_catalogs=False, log=lambda _m: None)


def test_settle_settings_false_never_consults_the_settle_helpers(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.deployment import resolver

    def _never(*a, **k):
        raise AssertionError("consulted with settle_settings=False")

    monkeypatch.setattr(resolver, "settled", _never)
    monkeypatch.setattr(resolver, "wait_until_settled", _never)
    result = run_quick_setup(sync_catalogs=False, settle_settings=False, log=lambda _m: None)
    assert dict(result.steps)["agent"] == "installed"
