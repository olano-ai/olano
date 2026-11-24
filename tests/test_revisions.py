"""Revision tracking: allowlist discipline, redaction, worker debounce."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from olano.revisions.engine import RevisionsEngine, _redact_agent_env
from olano.revisions.worker import (
    RevisionsWorker,
    get_revisions_worker,
    notify_turn,
    reset_revisions_worker,
)

pytestmark = pytest.mark.skipif(not RevisionsEngine.available(), reason="git not installed")


@pytest.fixture
def home(lionclaw_home: Path) -> Path:
    (lionclaw_home / "config.yaml").write_text("heartbeat:\n  enabled: true\n")
    ws = lionclaw_home / "agents" / "merli" / "workspace"
    (ws / "memories").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\n")
    (ws / "memories" / "2026-08-09.md").write_text("note\n")
    (lionclaw_home / "agents" / "merli" / "agent.json5").write_text(
        '{\n  name: "merli",\n  env: { KEY: "sk-secret" },\n}\n'
    )
    # Secret-bearing files that must never appear in history.
    (lionclaw_home / ".env").write_text("OPENAI_API_KEY=sk-live\n")
    vault = lionclaw_home / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "vault_boot_key").write_text("passphrase")
    (vault / "vault.db").write_text("cipher")
    return lionclaw_home


class TestEngine:
    def test_snapshot_commits_allowlist_only(self, home: Path):
        engine = RevisionsEngine(home)
        result = engine.snapshot(author="merli", reason="turn: merli")
        assert result.committed and result.error == ""
        tree = engine._git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
        assert "config.yaml" in tree
        assert "agents/merli/workspace/MEMORY.md" in tree
        assert "agents/merli/agent.json5" in tree
        # The load-bearing assertion: secrets are absent because no pattern
        # names them, not because a rule excluded them.
        assert not any("vault" in p or p == ".env" for p in tree)

    def test_env_values_never_enter_history(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot()
        stored = engine.show_file("agents/merli/agent.json5")
        assert stored is not None
        assert "sk-secret" not in stored
        assert "__vault__" in stored

    def test_no_change_no_commit(self, home: Path):
        engine = RevisionsEngine(home)
        assert engine.snapshot().committed
        again = engine.snapshot()
        assert not again.committed and again.error == ""

    def test_deletion_tracked(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot()
        (home / "agents/merli/workspace/memories/2026-08-09.md").unlink()
        assert engine.snapshot().committed
        tree = engine._git("ls-tree", "-r", "--name-only", "HEAD")
        assert "2026-08-09.md" not in tree

    def test_author_is_acting_agent(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot(author="scout", reason="turn: scout")
        entry = engine.log(limit=1)[0]
        assert entry["author"] == "scout"

    def test_oversize_file_skipped(self, home: Path):
        engine = RevisionsEngine(home, max_file_kb=1)
        (home / "agents/merli/workspace/MEMORY.md").write_text("x" * 4096)
        result = engine.snapshot()
        assert "agents/merli/workspace/MEMORY.md" in result.skipped_large

    def test_include_memory_false_drops_memory_files(self, home: Path):
        engine = RevisionsEngine(home, include_memory=False)
        engine.snapshot()
        tree = engine._git("ls-tree", "-r", "--name-only", "HEAD")
        assert "MEMORY.md" not in tree and "memories/" not in tree
        assert "config.yaml" in tree

    def test_pre_upgrade_tag(self, home: Path):
        engine = RevisionsEngine(home)
        result = engine.snapshot(tag="pre-upgrade-2.0")
        assert result.tag == "pre-upgrade-2.0"
        assert "pre-upgrade-2.0" in engine._git("tag")

    def test_log_and_show_file_roundtrip(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot(author="merli")
        (home / "config.yaml").write_text("heartbeat:\n  enabled: false\n")
        engine.snapshot(author="scout")
        log = engine.log(path="config.yaml")
        assert len(log) == 2
        old = engine.show_file("config.yaml", log[1]["commit"])
        assert old is not None and "true" in old

    def test_symlink_never_followed(self, home: Path):
        target = home / ".env"
        link = home / "agents" / "merli" / "workspace" / "sneaky.md"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("no symlink support")
        engine = RevisionsEngine(home)
        engine.snapshot()
        tree = engine._git("ls-tree", "-r", "--name-only", "HEAD")
        assert "sneaky.md" not in tree


class TestRedaction:
    def test_no_env_passthrough_verbatim(self):
        raw = '{\n  // comment kept\n  name: "x",\n}\n'
        text, redacted = _redact_agent_env(raw)
        assert not redacted and text == raw

    def test_env_values_replaced(self):
        text, redacted = _redact_agent_env('{"name": "x", "env": {"A": "1", "B": "2"}}')
        assert redacted
        data = json.loads(text.split("\n", 1)[1])
        assert data["env"] == {"A": "__vault__", "B": "__vault__"}

    def test_unparseable_fails_closed(self):
        text, redacted = _redact_agent_env("{ this is not json5 !!")
        assert redacted and text == ""


class TestWorker:
    def test_debounced_commit_with_author(self, home: Path):
        worker = RevisionsWorker(RevisionsEngine(home), debounce_seconds=5)
        worker.debounce_seconds = 0.2  # bypass the 5s floor for the test
        worker.start()
        try:
            worker.notify(author="merli", reason="turn: merli")
            deadline = time.monotonic() + 5
            engine = RevisionsEngine(home)
            while time.monotonic() < deadline and not engine.log(limit=1):
                time.sleep(0.05)
            log = engine.log(limit=1)
            assert log and log[0]["author"] == "merli"
        finally:
            worker.stop(flush=False)

    def test_multiple_authors_collapse_to_system(self, home: Path):
        worker = RevisionsWorker(RevisionsEngine(home))
        worker.notify(author="merli", reason="turn: merli")
        worker.notify(author="scout", reason="turn: scout")
        worker._flush_pending()
        entry = RevisionsEngine(home).log(limit=1)[0]
        assert entry["author"] == "olano"
        assert "merli" in entry["message"] and "scout" in entry["message"]

    def test_notify_turn_noop_without_worker(self):
        reset_revisions_worker()
        notify_turn("merli")  # must not raise, must not create a worker
        assert get_revisions_worker(start=False) is None

    def test_singleton_respects_disabled_setting(self, home: Path, monkeypatch):
        reset_revisions_worker()
        from olano.config import system_settings

        monkeypatch.setattr(
            system_settings,
            "load_revisions_settings",
            lambda override=None: system_settings.RevisionsSettings(enabled=False),
        )
        assert get_revisions_worker(start=True) is None
        reset_revisions_worker()


class TestNoUserFacingGitSurgery:
    """Conflicts are impossible by construction (linear snapshot history, no
    merge code path); the one residue a crash can leave is a git lock file."""

    def test_stale_lock_swept_and_snapshot_succeeds(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot()
        lock = engine.repo / "index.lock"
        lock.write_text("")
        stale = time.time() - 3600
        os.utime(lock, (stale, stale))
        (home / "config.yaml").write_text("heartbeat:\n  enabled: false\n")
        result = engine.snapshot()
        assert result.committed and result.error == ""
        assert not lock.exists()

    def test_fresh_lock_respected_fails_clean(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot()
        lock = engine.repo / "index.lock"
        lock.write_text("")  # mtime = now: a live writer must win
        (home / "config.yaml").write_text("heartbeat:\n  enabled: false\n")
        result = engine.snapshot()
        assert not result.committed
        assert "index.lock" in result.error or "File exists" in result.error
        assert lock.exists()  # we never clobber a live writer's lock

    def test_no_merge_machinery_in_engine(self):
        import inspect

        import olano.revisions.engine as engine_mod
        import olano.revisions.worker as worker_mod

        # The snapshot path (engine + the worker that drives it) can never
        # produce a conflict: the operations that create one must not appear.
        # Merging exists only in revisions/remote.py, reached only from an
        # explicit dashboard pull, and it never touches a working tree.
        for mod in (engine_mod, worker_mod):
            src = inspect.getsource(mod)
            for forbidden in ('"merge"', '"merge-tree"', '"rebase"', '"pull"', '"revert"'):
                assert forbidden not in src, (mod.__name__, forbidden)
