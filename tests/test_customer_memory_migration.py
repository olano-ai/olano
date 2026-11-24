"""Migration 0006 — per-customer memory moves into the agent's own workspace.

`<home>/customers/<agent>/<user_id>/` → `<home>/agents/<agent>/workspace/customers/<user_id>/`.
A support bot must keep recognising the people it has already talked to across
the upgrade, and no customer directory may be overwritten or lost on the way.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from olano.migrations import pending_migration_ids, run_pending_migrations

MIGRATION = importlib.import_module("olano.migrations.0006_customer_memory_agent_workspace")


def _legacy(home: Path, agent: str, customer: str) -> Path:
    return home / "customers" / agent / customer


def _new(home: Path, agent: str, customer: str) -> Path:
    return home / "agents" / agent / "workspace" / "customers" / customer


def _seed(home: Path, agent: str, customer: str, text: str) -> Path:
    d = _legacy(home, agent, customer)
    (d / "memories").mkdir(parents=True)
    (d / "MEMORY.md").write_text(text, encoding="utf-8")
    (d / "memories" / "2026-07-01.md").write_text(f"log: {text}", encoding="utf-8")
    return d


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "supportbot").mkdir(parents=True)
    (tmp_path / "agents" / "supportbot" / "agent.json5").write_text(
        '{"name": "supportbot", "memory": {"per_customer": true}}', encoding="utf-8"
    )
    return tmp_path


class TestCustomerStoreRelocation:
    def test_moves_every_customer_into_the_agent_workspace(self, home: Path) -> None:
        _seed(home, "supportbot", "alice", "Order 111 is delayed")
        _seed(home, "supportbot", "bob", "Wants a refund on 222")

        MIGRATION.apply(home)

        assert _new(home, "supportbot", "alice").joinpath("MEMORY.md").read_text() == (
            "Order 111 is delayed"
        )
        assert _new(home, "supportbot", "bob").joinpath("MEMORY.md").read_text() == (
            "Wants a refund on 222"
        )
        # Daily logs travel with the store, not just MEMORY.md.
        assert _new(home, "supportbot", "alice").joinpath("memories/2026-07-01.md").exists()
        # And the emptied legacy tree is pruned all the way up.
        assert not (home / "customers").exists()

    def test_migrated_store_is_what_the_tools_read(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end point of the migration: `recall` finds pre-upgrade facts."""
        from olano.memory.customer_memory import make_customer_memory_tools
        from olano.security.request_context import push_request_context

        _seed(home, "supportbot", "alice", "# Memory\n\n- Order 111 is delayed\n")
        MIGRATION.apply(home)

        monkeypatch.setenv("LIONCLAW_HOME", str(home))
        tools = make_customer_memory_tools("supportbot")
        recall = next(t for t in tools if t.name == "recall")
        with push_request_context(source="channel", user_id="alice"):
            assert "111" in recall.invoke({"query": "order"})

    def test_migrates_agents_with_no_config_left(self, home: Path) -> None:
        """Data for a deleted agent is still preserved rather than dropped."""
        _seed(home, "ghostbot", "carol", "still here")
        MIGRATION.apply(home)
        assert _new(home, "ghostbot", "carol").joinpath("MEMORY.md").read_text() == "still here"

    def test_existing_destination_is_never_overwritten(self, home: Path) -> None:
        _seed(home, "supportbot", "alice", "legacy copy")
        dest = _new(home, "supportbot", "alice")
        dest.mkdir(parents=True)
        (dest / "MEMORY.md").write_text("newer copy", encoding="utf-8")

        MIGRATION.apply(home)

        assert (dest / "MEMORY.md").read_text() == "newer copy"
        # The legacy copy is left in place for the operator, not deleted.
        assert _legacy(home, "supportbot", "alice").joinpath("MEMORY.md").read_text() == (
            "legacy copy"
        )

    def test_unexpected_stray_file_is_left_alone(self, home: Path) -> None:
        (home / "customers" / "supportbot").mkdir(parents=True)
        stray = home / "customers" / "supportbot" / "notes.txt"
        stray.write_text("not a customer store", encoding="utf-8")

        MIGRATION.apply(home)

        assert stray.exists()

    def test_idempotent(self, home: Path) -> None:
        _seed(home, "supportbot", "alice", "hello")
        MIGRATION.apply(home)
        MIGRATION.apply(home)  # must not raise, and must not resurrect anything
        assert _new(home, "supportbot", "alice").joinpath("MEMORY.md").read_text() == "hello"
        assert not (home / "customers").exists()

    def test_no_legacy_tree_is_a_noop(self, home: Path) -> None:
        MIGRATION.apply(home)
        assert not (home / "customers").exists()
        assert not (home / "agents" / "supportbot" / "workspace" / "customers").exists()

    def test_empty_home_is_a_noop(self, tmp_path: Path) -> None:
        MIGRATION.apply(tmp_path)  # must not raise

    def test_failed_move_leaves_the_legacy_copy_readable(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(home, "supportbot", "alice", "keep me")
        _seed(home, "supportbot", "bob", "me too")

        calls: list[str] = []

        def flaky_move(src: str, dst: str) -> None:
            calls.append(src)
            if src.endswith("alice"):
                raise OSError("cross-device copy died")
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(src).rename(dst)

        monkeypatch.setattr(MIGRATION.shutil, "move", flaky_move)
        MIGRATION.apply(home)  # one bad folder must not abort the pass

        assert len(calls) == 2
        assert _legacy(home, "supportbot", "alice").joinpath("MEMORY.md").read_text() == "keep me"
        assert _new(home, "supportbot", "bob").joinpath("MEMORY.md").read_text() == "me too"


class TestMigrationRunner:
    def test_migration_is_discovered(self) -> None:
        assert MIGRATION.ID in pending_migration_ids([])

    def test_runner_applies_it(self, home: Path) -> None:
        _seed(home, "supportbot", "alice", "hello")
        assert MIGRATION.ID in run_pending_migrations(home, [])
        assert _new(home, "supportbot", "alice").joinpath("MEMORY.md").exists()

    def test_runner_skips_an_applied_migration(self, home: Path) -> None:
        _seed(home, "supportbot", "alice", "hello")
        run_pending_migrations(home, [MIGRATION.ID])
        assert _legacy(home, "supportbot", "alice").exists()
