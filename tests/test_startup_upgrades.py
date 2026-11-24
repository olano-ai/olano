"""Boot-time auto-upgrade (olano/update/auto.py) + config migrations."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from olano.migrations import pending_migration_ids, run_pending_migrations
from olano.update import auto
from olano.update.install_state import read_install_state

MIGRATION = importlib.import_module("olano.migrations.0001_agentfather_platform_command")


AGENTFATHER_JSON5 = """{
  // hand-written comment that a rewrite is allowed to drop
  "name": "agentfather",
  "model": "anthropic:claude-sonnet-4-6",
  "human_only": true,
}
"""

OTHER_AGENT_JSON5 = """{
  "name": "merli",
  "model": "anthropic:claude-sonnet-4-6",
}
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "agentfather").mkdir(parents=True)
    (tmp_path / "agents" / "agentfather" / "agent.json5").write_text(AGENTFATHER_JSON5)
    (tmp_path / "agents" / "merli").mkdir(parents=True)
    (tmp_path / "agents" / "merli" / "agent.json5").write_text(OTHER_AGENT_JSON5)
    return tmp_path


def _config(home: Path, agent: str) -> dict:
    from olano.config.loader import _load_json5

    return _load_json5((home / "agents" / agent / "agent.json5").read_text())


class TestAgentfatherPlatformMigration:
    def test_enables_platform_for_agentfather(self, home: Path) -> None:
        MIGRATION.apply(home)
        assert _config(home, "agentfather")["commands"]["enabled"]["platform"] is True

    def test_leaves_other_agents_alone(self, home: Path) -> None:
        before = (home / "agents" / "merli" / "agent.json5").read_text()
        MIGRATION.apply(home)
        assert (home / "agents" / "merli" / "agent.json5").read_text() == before

    def test_idempotent(self, home: Path) -> None:
        MIGRATION.apply(home)
        after_first = (home / "agents" / "agentfather" / "agent.json5").read_text()
        MIGRATION.apply(home)
        assert (home / "agents" / "agentfather" / "agent.json5").read_text() == after_first

    def test_respects_explicit_owner_choice(self, home: Path) -> None:
        path = home / "agents" / "agentfather" / "agent.json5"
        path.write_text('{"name": "agentfather", "commands": {"enabled": {"platform": false}}}')
        MIGRATION.apply(home)
        assert _config(home, "agentfather")["commands"]["enabled"]["platform"] is False

    def test_detects_by_archetype_field(self, home: Path) -> None:
        (home / "agents" / "father2").mkdir()
        (home / "agents" / "father2" / "agent.json5").write_text(
            '{"name": "father2", "archetype": "agentfather"}'
        )
        MIGRATION.apply(home)
        assert _config(home, "father2")["commands"]["enabled"]["platform"] is True

    def test_missing_agents_dir_is_a_noop(self, tmp_path: Path) -> None:
        MIGRATION.apply(tmp_path)  # must not raise

    def test_unparseable_config_is_skipped(self, home: Path) -> None:
        (home / "agents" / "broken").mkdir()
        (home / "agents" / "broken" / "agent.json5").write_text("{ not json5 at all !!!")
        MIGRATION.apply(home)  # must not raise
        assert _config(home, "agentfather")["commands"]["enabled"]["platform"] is True


class TestMigrationRunner:
    def test_migration_is_discovered(self) -> None:
        assert MIGRATION.ID in pending_migration_ids([])

    def test_applied_migrations_are_skipped(self, home: Path) -> None:
        # 0001 is recorded as applied, so it must not re-run (other, later
        # migrations may still be pending — assert on 0001 specifically).
        applied_now = run_pending_migrations(home, [MIGRATION.ID])
        assert MIGRATION.ID not in applied_now
        assert "commands" not in _config(home, "agentfather")

    def test_pending_then_recorded(self, home: Path) -> None:
        newly = run_pending_migrations(home, [])
        assert MIGRATION.ID in newly
        assert pending_migration_ids(newly) == []


class TestRunStartupUpgrades:
    @pytest.fixture(autouse=True)
    def _stub_heavy_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Skill sync and the all-agents refresh reach outside the tmp home
        # (real skill payloads / list_agents over real search paths) — stub
        # them so the test exercises marker + migration bookkeeping only.
        monkeypatch.setattr(auto, "sync_builtin_skills", lambda home: None)
        monkeypatch.setattr(auto, "refresh_all_agents", lambda **kw: [])
        monkeypatch.setattr(auto, "code_marker", lambda: "9.9.9@abc1234")
        monkeypatch.setattr(auto, "current_version", lambda: "9.9.9")

    def test_first_boot_applies_and_records(self, home: Path) -> None:
        report = auto.run_startup_upgrades(home)
        assert report is not None
        assert MIGRATION.ID in report.migrations_applied
        assert _config(home, "agentfather")["commands"]["enabled"]["platform"] is True
        state = read_install_state(home)
        assert state.boot_marker == "9.9.9@abc1234"
        assert MIGRATION.ID in state.applied_migrations
        assert state.version == "9.9.9"

    def test_second_boot_is_a_noop(self, home: Path) -> None:
        assert auto.run_startup_upgrades(home) is not None
        assert auto.run_startup_upgrades(home) is None

    def test_code_change_triggers_rerun_without_migrations(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert auto.run_startup_upgrades(home) is not None
        monkeypatch.setattr(auto, "code_marker", lambda: "9.9.9@def5678")
        report = auto.run_startup_upgrades(home)
        assert report is not None
        assert report.migrations_applied == []  # already applied — only refresh reran
        assert read_install_state(home).boot_marker == "9.9.9@def5678"

    def test_failed_migration_is_not_recorded(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(home_path: Path, applied: list[str]) -> list[str]:
            raise RuntimeError("migration exploded")

        monkeypatch.setattr(auto, "run_pending_migrations", boom)
        with pytest.raises(RuntimeError):
            auto.run_startup_upgrades(home)
        state = read_install_state(home)
        assert state.boot_marker is None  # nothing recorded → retried next boot
        assert state.applied_migrations == []

    def test_summary_mentions_migrations(self, home: Path) -> None:
        report = auto.run_startup_upgrades(home)
        assert report is not None
        assert MIGRATION.ID in report.summary()
