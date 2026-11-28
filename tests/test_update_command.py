"""Tests for the olano update command (self-update path) and UpdateReport."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from olano.cli.main import cli
from olano.skills.built_in_sync import SyncReport
from olano.update import run_update
from olano.update.install_state import InstallState, read_install_state, write_install_state


def _fake_sync_report(**kw) -> SyncReport:
    defaults = {"new": 0, "updated": 0, "unchanged": 9, "removed": 0, "pinned": 0}
    defaults.update(kw)
    return SyncReport(**defaults)


# ─────────────────────────────────────────────────────────────────
# run_update unit tests
# ─────────────────────────────────────────────────────────────────


def test_check_mode_no_writes(tmp_path: Path, monkeypatch):
    # Pin the PyPI install mode: the test suite itself runs from a git
    # checkout, which the mode-aware check would otherwise detect as "source".
    monkeypatch.setenv("LIONCLAW_INSTALL_MODE", "local")
    write_install_state(tmp_path, InstallState(version="1.0.0"))
    # Far above any real release, so the check always sees an update whatever
    # version this checkout is (1.1.0 used to be "newer" until it shipped).
    with patch("olano.update._query_latest_version", return_value="9.9.0"):
        report = run_update(tmp_path, check=True)
    assert report.check_only is True
    assert report.to_version == "9.9.0"
    # version must not be updated on disk
    assert read_install_state(tmp_path).version == "1.0.0"


def test_check_mode_source_checkout_uses_git_not_pypi(tmp_path: Path, monkeypatch):
    """On a git checkout, `--check` must consult git, not PyPI (regression:
    the old engine only knew docker vs `uv tool`)."""
    from olano.platform_control.detect import InstallMode
    from olano.update.strategies import UpdateCheck

    monkeypatch.delenv("LIONCLAW_INSTALL_MODE", raising=False)
    write_install_state(tmp_path, InstallState(version="0.1.0"))
    fake = UpdateCheck(
        mode=InstallMode.SOURCE,
        current="0.1.0",
        available="def5678 fix things",
        update_available=True,
        can_apply=True,
        details="2 new commit(s) upstream",
    )
    with (
        patch("olano.update.strategies.check_update", return_value=fake) as mock_check,
        patch("olano.update._query_latest_version") as mock_pypi,
    ):
        report = run_update(tmp_path, check=True)
    mock_check.assert_called_once()
    mock_pypi.assert_not_called()
    assert report.to_version == "def5678 fix things"


def test_post_upgrade_syncs_skills_and_records_version(tmp_path: Path):
    write_install_state(tmp_path, InstallState(version="1.0.0"))
    fake_skills = _fake_sync_report(new=3)
    with (
        patch("olano.skills.built_in_sync.sync_builtin_skills", return_value=fake_skills),
        patch("olano.migrations.run_pending_migrations", return_value=["0001_test"]),
        patch("olano.update._current_version", return_value="1.1.0"),
    ):
        report = run_update(tmp_path, post_upgrade=True)
    assert report.skills.new == 3
    assert report.migrations_applied == ["0001_test"]
    state = read_install_state(tmp_path)
    assert state.version == "1.1.0"
    assert state.history[0]["version"] == "1.0.0"


def test_rollback_invokes_package_installer(tmp_path: Path):
    state = InstallState(
        version="1.1.0",
        history=[{"version": "1.0.0", "installed_at": "2026-01-01"}],
    )
    write_install_state(tmp_path, state)
    with patch("olano.update._upgrade_package") as mock_upgrade:
        report = run_update(tmp_path, rollback=True)
    mock_upgrade.assert_called_once()
    assert report.rolled_back is True
    assert report.to_version == "1.0.0"


def test_rollback_check_no_writes(tmp_path: Path):
    state = InstallState(
        version="1.1.0",
        history=[{"version": "1.0.0", "installed_at": "2026-01-01"}],
    )
    write_install_state(tmp_path, state)
    with patch("olano.update._upgrade_package") as mock_upgrade:
        report = run_update(tmp_path, rollback=True, check=True)
    mock_upgrade.assert_not_called()
    assert report.check_only is True
    assert report.rolled_back is True


def test_rollback_no_history(tmp_path: Path):
    write_install_state(tmp_path, InstallState(version="1.0.0", history=[]))
    with patch("olano.update._upgrade_package") as mock_upgrade:
        report = run_update(tmp_path, rollback=True)
    mock_upgrade.assert_not_called()
    assert report.to_version is None


# ─────────────────────────────────────────────────────────────────
# CLI integration via CliRunner
# ─────────────────────────────────────────────────────────────────


def test_cli_update_check_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_INSTALL_MODE", "local")
    runner = CliRunner()
    write_install_state(tmp_path, InstallState(version="1.0.0"))
    with patch("olano.update._query_latest_version", return_value="9.9.0"):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "update", "--check"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "9.9.0" in result.output or "Update available" in result.output


def test_cli_update_legacy_workspace_flag(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Agents")
    runner = CliRunner()
    with (
        patch("olano.archetypes.installer.sync_system_zone", return_value=[]),
        patch("olano.archetypes.installer.emit_system_update_pulse", return_value=None),
        patch("olano.skills.built_in_sync.sync_builtin_skills", return_value=_fake_sync_report()),
    ):
        result = runner.invoke(
            cli,
            ["--home", str(tmp_path), "update", "--workspace", str(workspace)],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "System zone refreshed" in result.output
