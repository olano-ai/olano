"""Revision history: commit details + diffs, and the GitHub remote sync.

The remote is a local bare repository standing in for GitHub; "edits made on
GitHub" are commits pushed to it from a separate clone. Everything below the
URL (fetch, push, merge-tree, applying a tree to the live home) is the same
code a real github.com remote runs through.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from olano.revisions import remote
from olano.revisions.engine import RevisionsEngine, is_tracked_path
from olano.revisions.remote import RemoteError, RemoteSync

pytestmark = pytest.mark.skipif(not RevisionsEngine.available(), reason="git not installed")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "someone",
    "GIT_AUTHOR_EMAIL": "someone@example.com",
    "GIT_COMMITTER_NAME": "someone",
    "GIT_COMMITTER_EMAIL": "someone@example.com",
}
MEMORY = "agents/merli/workspace/MEMORY.md"
AGENT = "agents/merli/agent.json5"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
        check=True,
    )
    return proc.stdout


@pytest.fixture
def home(lionclaw_home: Path) -> Path:
    (lionclaw_home / "config.yaml").write_text("heartbeat:\n  enabled: true\n")
    ws = lionclaw_home / "agents" / "merli" / "workspace"
    (ws / "memories").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\nline one\nline two\nline three\n")
    (ws / "SOUL.md").write_text("calm\n")
    (lionclaw_home / AGENT).write_text('{\n  name: "merli",\n  env: { KEY: "sk-secret" },\n}\n')
    (lionclaw_home / ".env").write_text("OPENAI_API_KEY=sk-live\n")
    return lionclaw_home


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True
    )
    return path


@pytest.fixture
def engine(home: Path) -> RevisionsEngine:
    eng = RevisionsEngine(home)
    assert eng.snapshot(author="merli", reason="first").committed
    return eng


@pytest.fixture
def syncer(engine: RevisionsEngine, origin: Path) -> RemoteSync:
    s = RemoteSync(engine, str(origin), branch="main")
    s.push()
    return s


class Upstream:
    """A clone of the remote: what a person editing on GitHub would do."""

    def __init__(self, origin: Path, work: Path) -> None:
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
        self.work = work

    def write(self, rel: str, text: str) -> Upstream:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return self

    def delete(self, rel: str) -> Upstream:
        (self.work / rel).unlink()
        return self

    def push(self, message: str = "edit on github") -> str:
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", message)
        _git(self.work, "push", "-q", "origin", "HEAD:main")
        return _git(self.work, "rev-parse", "HEAD").strip()


@pytest.fixture
def upstream(syncer: RemoteSync, origin: Path, tmp_path: Path) -> Upstream:
    return Upstream(origin, tmp_path / "github-clone")


# ── the allowlist as a gate on names ───────────────────────────────────────


class TestTrackedPath:
    @pytest.mark.parametrize(
        "rel",
        [
            "config.yaml",
            AGENT,
            MEMORY,
            "agents/merli/workspace/memories/2026-01-01.md",
            "agents/merli/workspace/skills/a/b/SKILL.md",
            "agents/merli/workspace/skills/SKILL.md",
            "workspace/SOUL.md",
            # documents, data, scripts, media anywhere in a workspace
            "agents/merli/workspace/notes/x.md",
            "agents/merli/workspace/reports/q3.pdf",
            "agents/merli/workspace/reports/q3.docx",
            "agents/merli/workspace/data/rows.json",
            "agents/merli/workspace/data/settings.yaml",
            "agents/merli/workspace/data/table.csv",
            "agents/merli/workspace/scripts/build.py",
            "agents/merli/workspace/scripts/run.sh",
            "agents/merli/workspace/audio/memo.mp3",
            "agents/merli/workspace/video/demo.mp4",
            "agents/merli/workspace/video/clip.MOV",
            "agents/merli/workspace/charts/sales.png",
            "skills/charts/scripts/chart.py",
            "skills/charts/SKILL.md",
        ],
    )
    def test_tracked(self, rel: str):
        assert is_tracked_path(rel)

    @pytest.mark.parametrize(
        "rel",
        [
            ".env",
            "README.md",
            "vault/vault.db",
            "agents/merli/workspace/../../.env",
            # logs, transcripts, databases, archives, unknown types
            "agents/merli/workspace/run.log",
            "agents/merli/workspace/data.jsonl",
            "agents/merli/workspace/cache.db",
            "agents/merli/workspace/bundle.zip",
            "agents/merli/sessions/thread.jsonl",
            "agents/merli/connectors/github.json",
            # hidden, dependency, cache and private directories
            "agents/merli/workspace/.olano/notes.md",
            "agents/merli/workspace/.env",
            "agents/merli/workspace/app/node_modules/x/index.js",
            "agents/merli/workspace/app/__pycache__/m.py",
            "agents/merli/workspace/customers/c1.md",
            "agents/merli/workspace/rag/index.json",
            # a config/data file named like a credential
            "agents/merli/workspace/client_secret.json",
            "agents/merli/workspace/token.json",
            "agents/merli/workspace/credentials.yaml",
            "/etc/passwd",
            "agents/merli/workspace/.git/config.md",
            "agents/merli/agent.json5/../x.md",
            "",
        ],
    )
    def test_not_tracked(self, rel: str):
        assert not is_tracked_path(rel)

    def test_memory_exclusion(self):
        assert not is_tracked_path(MEMORY, include_memory=False)
        assert is_tracked_path("agents/merli/workspace/SOUL.md", include_memory=False)


# ── commit details and per-file diffs ──────────────────────────────────────


class TestCommitDetails:
    def test_first_commit_lists_every_file_as_added(self, engine: RevisionsEngine):
        details = engine.commit_details("HEAD")
        assert details is not None
        assert details["parents"] == []
        assert {f["status"] for f in details["files"]} == {"added"}
        assert MEMORY in {f["path"] for f in details["files"]}
        assert details["stats"]["added"] == details["stats"]["files"]

    def test_modified_added_deleted_with_line_counts(self, engine: RevisionsEngine, home: Path):
        (home / MEMORY).write_text("# memory\nline one\nline 2\nline three\nline four\n")
        (home / "agents/merli/workspace/IDEAS.md").write_text("idea\n")
        (home / "agents/merli/workspace/SOUL.md").unlink()
        engine.snapshot(author="scout", reason="turn: scout")
        details = engine.commit_details("HEAD")
        assert details is not None
        by_path = {f["path"]: f for f in details["files"]}
        assert by_path[MEMORY]["status"] == "modified"
        assert (by_path[MEMORY]["additions"], by_path[MEMORY]["deletions"]) == (2, 1)
        assert by_path["agents/merli/workspace/IDEAS.md"]["status"] == "added"
        assert by_path["agents/merli/workspace/SOUL.md"]["status"] == "deleted"
        assert details["stats"] == {
            "files": 3,
            "added": 1,
            "modified": 1,
            "deleted": 1,
            "additions": 3,
            "deletions": 2,
        }
        assert details["author"] == "scout" and details["message"] == "turn: scout"

    def test_file_diff_is_a_git_patch(self, engine: RevisionsEngine, home: Path):
        (home / "config.yaml").write_text("heartbeat:\n  enabled: false\n")
        engine.snapshot()
        diff = engine.file_diff("HEAD", "config.yaml")
        assert diff is not None
        assert diff["patch"].startswith("diff --git a/config.yaml b/config.yaml")
        assert "-  enabled: true" in diff["patch"] and "+  enabled: false" in diff["patch"]
        assert not diff["binary"] and not diff["truncated"]

    def test_full_context_shows_the_whole_file(self, engine: RevisionsEngine, home: Path):
        (home / MEMORY).write_text("# memory\nline one\nline two\nline three\nadded\n")
        engine.snapshot()
        short = engine.file_diff("HEAD", MEMORY, context=0)
        full = engine.file_diff("HEAD", MEMORY, context=100000)
        assert short is not None and full is not None
        assert " # memory" not in short["patch"] and " # memory" in full["patch"]

    def test_root_commit_diffs_against_the_empty_tree(self, engine: RevisionsEngine):
        diff = engine.file_diff("HEAD", MEMORY)
        assert diff is not None and "+line one" in diff["patch"]

    def test_option_shaped_revisions_are_refused(self, engine: RevisionsEngine):
        with pytest.raises(ValueError):
            engine.commit_details("--output=/tmp/x")
        assert engine.show_file("config.yaml", "-p") is None
        assert engine.commit_details("0" * 40) is None

    def test_log_carries_parents(self, engine: RevisionsEngine):
        assert engine.log(limit=1)[0]["parents"] == []


# ── push ───────────────────────────────────────────────────────────────────


class TestPush:
    def test_push_sends_history(self, syncer: RemoteSync, engine: RevisionsEngine, origin: Path):
        assert _git(origin, "rev-parse", "main").strip() == engine._head()
        assert syncer.counts() == {"ahead": 0, "behind": 0}
        state = remote.load_state(engine)
        assert state["last_push_commit"] == engine._head() and not state["needs_pull"]

    def test_unpushed_commits_are_counted(self, syncer: RemoteSync, engine, home):
        (home / MEMORY).write_text("changed\n")
        engine.snapshot()
        assert syncer.counts()["ahead"] == 1
        assert syncer.unpushed() == [engine._head()]

    def test_diverged_push_is_rejected_then_force_wins(
        self, syncer: RemoteSync, upstream: Upstream, engine, home, origin
    ):
        upstream.write("agents/merli/workspace/SOUL.md", "upstream\n").push()
        (home / MEMORY).write_text("local\n")
        engine.snapshot()
        with pytest.raises(RemoteError) as err:
            syncer.push()
        assert err.value.code == "rejected"
        syncer.push(force=True)
        assert _git(origin, "rev-parse", "main").strip() == engine._head()

    def test_tags_ride_along(self, syncer: RemoteSync, engine, home, origin):
        (home / MEMORY).write_text("v2\n")
        engine.snapshot(tag="pre-upgrade-9.9")
        syncer.push()
        assert "pre-upgrade-9.9" in _git(origin, "tag")


# ── pull ───────────────────────────────────────────────────────────────────


class TestPull:
    def test_fast_forward_applies_files_and_keeps_remote_ids(
        self, syncer: RemoteSync, upstream: Upstream, engine, home
    ):
        theirs = (
            upstream.write("agents/merli/workspace/SOUL.md", "brave\n")
            .write("agents/merli/workspace/IDEAS.md", "new idea\n")
            .push()
        )
        result = syncer.pull(author="human-owner")
        assert result["status"] == "pulled" and result["kind"] == "fast_forward"
        assert (home / "agents/merli/workspace/SOUL.md").read_text() == "brave\n"
        assert (home / "agents/merli/workspace/IDEAS.md").read_text() == "new idea\n"
        assert engine._head() == theirs  # a true fast-forward: nothing to push back
        assert syncer.counts() == {"ahead": 0, "behind": 0}

    def test_nothing_new_is_up_to_date(self, syncer: RemoteSync):
        assert syncer.pull()["status"] == "up_to_date"

    def test_untracked_and_secret_paths_are_never_written(
        self, syncer: RemoteSync, upstream: Upstream, engine, home
    ):
        (home / ".env").write_text("OPENAI_API_KEY=sk-live\n")
        upstream.write("README.md", "hello\n").write(".env", "OPENAI_API_KEY=stolen\n")
        upstream.write("vault/vault_boot_key", "x")
        upstream.write("agents/merli/workspace/node_modules/evil.js", "x")
        upstream.write("agents/merli/workspace/.olano/state.md", "x")
        theirs = upstream.write("agents/merli/workspace/SOUL.md", "ok\n").push()
        result = syncer.pull()
        assert (home / ".env").read_text() == "OPENAI_API_KEY=sk-live\n"
        assert not (home / "README.md").exists() and not (home / "vault/vault_boot_key").exists()
        assert not (home / "agents/merli/workspace/node_modules").exists()
        assert not (home / "agents/merli/workspace/.olano").exists()
        ignored = {i["path"] for i in result["ignored"]}
        assert {"README.md", ".env", "vault/vault_boot_key"} <= ignored
        assert (home / "agents/merli/workspace/SOUL.md").read_text() == "ok\n"
        # The recorded tree is what is live, on top of what GitHub had.
        assert engine._head() != theirs
        assert engine._git("rev-parse", "HEAD^").strip() == theirs
        tree = engine._git("ls-tree", "-r", "--name-only", "HEAD").split()
        assert "README.md" not in tree and ".env" not in tree

    def test_agent_env_is_restored_from_the_live_file(
        self, syncer: RemoteSync, upstream: Upstream, engine, home
    ):
        stored = json.loads((upstream.work / AGENT).read_text().split("\n", 1)[1])
        assert stored["env"] == {"KEY": "__vault__"}
        stored["model"] = "olano:pro"
        stored["env"]["NEW_ONE"] = "__vault__"
        upstream.write(AGENT, json.dumps(stored, indent=2) + "\n").push()
        result = syncer.pull()
        live = json.loads((home / AGENT).read_text())
        assert live["model"] == "olano:pro"
        assert live["env"] == {"KEY": "sk-secret"}  # restored; unknown key dropped
        assert any("NEW_ONE" in w for w in result["warnings"])
        assert result["agents_changed"] == ["merli"]
        assert "sk-secret" not in (engine.show_file(AGENT) or "")

    def test_config_keeps_the_live_revisions_block(
        self, syncer: RemoteSync, upstream: Upstream, home
    ):
        block = {"remote": {"url": "https://github.com/me/home.git", "sync": "daily"}}
        (home / "config.yaml").write_text(
            yaml.dump({"heartbeat": {"enabled": True}, "revisions": block})
        )
        syncer.engine.snapshot()
        syncer.push()
        _git(upstream.work, "pull", "-q", "origin", "main")
        # An older config, with no revisions block, comes back from GitHub.
        upstream.write("config.yaml", "heartbeat:\n  enabled: false\n").push()
        assert syncer.pull()["kind"] == "fast_forward"
        live = yaml.safe_load((home / "config.yaml").read_text())
        assert live["heartbeat"]["enabled"] is False
        assert live["revisions"] == block

    def test_deleting_an_agent_config_upstream_is_not_applied(
        self, syncer: RemoteSync, upstream: Upstream, home
    ):
        upstream.delete(AGENT).delete("agents/merli/workspace/SOUL.md").push()
        result = syncer.pull()
        assert (home / AGENT).exists()
        assert not (home / "agents/merli/workspace/SOUL.md").exists()
        assert result["kept"] == [AGENT]
        assert result["deleted"] == ["agents/merli/workspace/SOUL.md"]

    def test_clean_merge_of_both_sides(self, syncer: RemoteSync, upstream: Upstream, engine, home):
        theirs = upstream.write("agents/merli/workspace/SOUL.md", "from github\n").push()
        (home / "agents/merli/workspace/IDEAS.md").write_text("from this box\n")
        engine.snapshot()
        ours = engine._head()
        result = syncer.pull()
        assert result["kind"] == "merged"
        assert (home / "agents/merli/workspace/SOUL.md").read_text() == "from github\n"
        assert (home / "agents/merli/workspace/IDEAS.md").read_text() == "from this box\n"
        parents = engine._git("show", "-s", "--format=%P", "HEAD").split()
        assert parents == [ours, theirs]
        syncer.push()  # the merge goes up without force

    def test_a_file_changed_mid_pull_aborts_everything(
        self, syncer: RemoteSync, upstream: Upstream, home, monkeypatch
    ):
        upstream.write(MEMORY, "github\n").write("agents/merli/workspace/SOUL.md", "g\n").push()
        real_fetch = RemoteSync.fetch

        def racing_fetch(self: RemoteSync) -> str | None:
            out = real_fetch(self)
            (home / MEMORY).write_text("an agent wrote this just now\n")
            return out

        monkeypatch.setattr(RemoteSync, "fetch", racing_fetch)
        with pytest.raises(RemoteError) as err:
            syncer.pull()
        assert err.value.code == "changed_during_pull"
        assert (home / MEMORY).read_text() == "an agent wrote this just now\n"
        assert (home / "agents/merli/workspace/SOUL.md").read_text() == "calm\n"

    def test_invalid_config_from_upstream_writes_nothing(
        self, syncer: RemoteSync, upstream: Upstream, home
    ):
        upstream.write("config.yaml", "heartbeat: [unclosed\n").write(MEMORY, "x\n").push()
        with pytest.raises(RemoteError) as err:
            syncer.pull()
        assert err.value.code == "invalid_file"
        assert "line one" in (home / MEMORY).read_text()


class TestConflicts:
    @pytest.fixture
    def conflicted(self, syncer: RemoteSync, upstream: Upstream, engine, home) -> dict:
        upstream.write(MEMORY, "# memory\nline one\nGITHUB\nline three\n").push()
        (home / MEMORY).write_text("# memory\nline one\nLOCAL\nline three\n")
        engine.snapshot()
        result = syncer.pull()
        assert result["status"] == "conflicts"
        return result

    def test_conflict_parks_a_merge_and_touches_nothing(self, conflicted, engine, home):
        assert [f["path"] for f in conflicted["merge"]["files"]] == [MEMORY]
        assert "LOCAL" in (home / MEMORY).read_text()
        assert remote.load_merge(engine) is not None

    def test_merge_file_offers_both_sides_and_markers(self, conflicted, syncer: RemoteSync):
        detail = syncer.merge_file(MEMORY)
        assert "LOCAL" in detail["ours"] and "GITHUB" in detail["theirs"]
        assert "<<<<<<< this box" in detail["merged"] and ">>>>>>> GitHub" in detail["merged"]
        assert "-LOCAL" in detail["patch"] and "+GITHUB" in detail["patch"]

    def test_pull_refuses_while_a_merge_is_pending(self, conflicted, syncer: RemoteSync):
        with pytest.raises(RemoteError) as err:
            syncer.pull()
        assert err.value.code == "merge_pending"

    def test_markers_left_in_the_text_are_refused(self, conflicted, syncer: RemoteSync):
        text = syncer.merge_file(MEMORY)["merged"]
        with pytest.raises(RemoteError) as err:
            syncer.resolve_merge({MEMORY: {"action": "content", "content": text}})
        assert err.value.code == "markers_left"

    def test_every_file_needs_a_decision(self, conflicted, syncer: RemoteSync):
        with pytest.raises(RemoteError) as err:
            syncer.resolve_merge({})
        assert err.value.code == "unresolved"

    def test_edited_resolution_is_applied_and_merged(
        self, conflicted, syncer: RemoteSync, engine, home
    ):
        merged = "# memory\nline one\nLOCAL and GITHUB\nline three\n"
        result = syncer.resolve_merge(
            {MEMORY: {"action": "content", "content": merged}}, author="human-owner"
        )
        assert result["status"] == "pulled"
        assert (home / MEMORY).read_text() == merged
        assert remote.load_merge(engine) is None
        assert len(engine._git("show", "-s", "--format=%P", "HEAD").split()) == 2
        syncer.push()

    def test_take_theirs(self, conflicted, syncer: RemoteSync, home):
        syncer.resolve_merge({MEMORY: {"action": "theirs"}})
        assert "GITHUB" in (home / MEMORY).read_text()

    def test_agents_kept_working_while_the_user_resolved(
        self, conflicted, syncer: RemoteSync, engine, home
    ):
        (home / "agents/merli/workspace/IDEAS.md").write_text("written during the merge\n")
        syncer.resolve_merge({MEMORY: {"action": "ours"}})
        assert "LOCAL" in (home / MEMORY).read_text()
        assert (
            home / "agents/merli/workspace/IDEAS.md"
        ).read_text() == "written during the merge\n"
        assert remote.load_merge(engine) is None

    def test_abort_leaves_the_box_as_it_was(self, conflicted, engine, home, monkeypatch):
        monkeypatch.setattr(remote, "home_engine", lambda: engine)
        remote.abort_merge()
        assert remote.load_merge(engine) is None
        assert "LOCAL" in (home / MEMORY).read_text()


# ── connect / settings / automatic sync ────────────────────────────────────


class TestConnect:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("me/home", ("me", "home")),
            ("https://github.com/me/home", ("me", "home")),
            ("https://github.com/me/home.git", ("me", "home")),
            ("git@github.com:me/home.git", ("me", "home")),
            ("github.com/me/my.repo/", ("me", "my.repo")),
        ],
    )
    def test_parse(self, raw: str, want: tuple[str, str]):
        assert remote.parse_github_repo(raw) == want

    @pytest.mark.parametrize(
        "raw", ["", "me", "https://gitlab.com/me/home", "me/..", "-me/home", "me/home/extra"]
    )
    def test_parse_rejects(self, raw: str):
        with pytest.raises(RemoteError):
            remote.parse_github_repo(raw)

    def test_public_repo_needs_confirmation(self, home: Path, monkeypatch):
        monkeypatch.setattr(
            remote, "github_repo_info", lambda *a: {"private": False, "permissions": {"push": True}}
        )
        with pytest.raises(RemoteError) as err:
            remote.connect(repo="me/home", token="t")
        assert err.value.code == "public_repo"

    def test_non_empty_repo_is_refused(self, home: Path, monkeypatch):
        monkeypatch.setattr(remote, "github_repo_info", lambda *a: {"private": True})
        monkeypatch.setattr(RemoteSync, "remote_heads", lambda self: {"refs/heads/main": "a" * 40})
        with pytest.raises(RemoteError) as err:
            remote.connect(repo="me/home", token="t")
        assert err.value.code == "not_empty"

    def test_read_only_token_is_refused(self, home: Path, monkeypatch):
        monkeypatch.setattr(
            remote, "github_repo_info", lambda *a: {"private": True, "permissions": {"push": False}}
        )
        with pytest.raises(RemoteError) as err:
            remote.connect(repo="me/home", token="t")
        assert err.value.code == "no_push"

    def test_connect_pushes_and_saves_settings(self, home: Path, origin: Path, monkeypatch):
        from olano.config.system_settings import load_revisions_settings

        monkeypatch.setattr(remote, "github_repo_info", lambda *a: {"private": True})
        monkeypatch.setattr(remote, "github_url", lambda owner, repo: str(origin))
        stored: dict[str, str] = {}
        monkeypatch.setattr(remote, "store_token", lambda t: stored.setdefault("t", t))
        result = remote.connect(repo="me/home", token="ghp_x", sync="on_commit")
        assert result["connected"] and stored == {"t": "ghp_x"}
        settings = load_revisions_settings().remote
        assert settings.url == str(origin) and settings.sync == "on_commit"
        assert "ghp_x" not in (home / "config.yaml").read_text()
        assert _git(origin, "rev-parse", "main").strip()

    def test_token_rides_an_env_header_scoped_to_github(self):
        env = remote._auth_env("ghp_secret")
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]  # base64, never the raw token
        assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestAutoSync:
    @pytest.fixture
    def connected(self, home: Path, origin: Path, monkeypatch) -> RevisionsEngine:
        from olano.config.system_settings import set_revisions_settings

        set_revisions_settings({"remote": {"url": str(origin), "sync": "on_commit"}})
        monkeypatch.setattr(remote, "resolve_token", lambda auth: "")
        eng = RevisionsEngine(home)
        eng.snapshot()
        return eng

    def test_on_commit_pushes(self, connected: RevisionsEngine, origin: Path):
        remote.auto_sync(committed=True)
        assert _git(origin, "rev-parse", "main").strip() == connected._head()

    def test_manual_never_pushes(self, connected: RevisionsEngine, origin: Path):
        remote.update_remote(sync="manual")
        remote.auto_sync(committed=True)
        assert (
            subprocess.run(
                ["git", "--git-dir", str(origin), "rev-parse", "--verify", "main"],
                capture_output=True,
            ).returncode
            != 0
        )

    def test_daily_waits_a_day(self, connected: RevisionsEngine, origin: Path):
        remote.update_remote(sync="daily")
        remote.auto_sync(committed=False)
        first = _git(origin, "rev-parse", "main").strip()
        (connected.home / MEMORY).write_text("later\n")
        connected.snapshot()
        remote.auto_sync(committed=True)
        assert _git(origin, "rev-parse", "main").strip() == first

    def test_a_rejected_push_stops_automatic_pushing(
        self, connected: RevisionsEngine, origin: Path, tmp_path: Path
    ):
        remote.auto_sync(committed=True)
        Upstream(origin, tmp_path / "clone").write("agents/merli/workspace/SOUL.md", "x\n").push()
        (connected.home / MEMORY).write_text("local\n")
        connected.snapshot()
        remote.auto_sync(committed=True)
        state = remote.load_state(connected)
        assert state["needs_pull"] and state["last_error_code"] == "rejected"

    def test_disconnect_forgets_everything_local(self, connected: RevisionsEngine, monkeypatch):
        from olano.config.system_settings import load_revisions_settings

        remote.auto_sync(committed=True)
        forgot: list[bool] = []
        monkeypatch.setattr(remote, "forget_token", lambda: forgot.append(True))
        remote.disconnect()
        assert load_revisions_settings().remote.url == ""
        assert forgot == [True]
        assert remote.load_state(connected) == {}


# ── HTTP surface ───────────────────────────────────────────────────────────


@pytest.fixture
def client(engine: RevisionsEngine):
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return TestClient(create_api(runtime))


class TestRoutes:
    def test_commit_and_diff(self, client, engine: RevisionsEngine, home: Path):
        (home / MEMORY).write_text("changed\n")
        engine.snapshot()
        head = engine._head()
        res = client.get(f"/api/revisions/commit?rev={head}")
        assert res.status_code == 200
        assert res.json()["files"][0]["path"] == MEMORY
        res = client.get(f"/api/revisions/diff?rev={head}&path={MEMORY}")
        assert res.status_code == 200 and "+changed" in res.json()["patch"]
        assert client.get("/api/revisions/commit?rev=--help").status_code == 400
        assert client.get(f"/api/revisions/commit?rev={'0' * 40}").status_code == 404

    def test_remote_status_when_not_connected(self, client):
        res = client.get("/api/revisions/remote")
        assert res.status_code == 200
        assert res.json()["connected"] is False

    def test_errors_carry_a_code(self, client):
        res = client.post("/api/revisions/remote/connect", json={"repo": "not a repo"})
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "invalid_repo"

    def test_remote_routes_are_admin_only(self):
        from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

        assert any("/api/revisions/remote".startswith(p) for p in _ADMIN_ONLY_PREFIXES)
        assert not any("/api/revisions/commit".startswith(p) for p in _ADMIN_ONLY_PREFIXES)


# ── one remote operation at a time (several admins, the worker, processes) ──


class TestSingleOperation:
    @pytest.fixture
    def slow_push(self, engine: RevisionsEngine, origin: Path, monkeypatch):
        """Each real network push counted, and slowed so they overlap."""
        import threading
        import time

        monkeypatch.setattr(remote, "home_engine", lambda: engine)
        calls: list[str] = []
        started = threading.Event()
        real = RemoteSync._net_streaming

        def spy(self, *args, **kwargs):
            if args and args[0] == "push" and "--porcelain" in args:
                calls.append(threading.current_thread().name)
                started.set()
                time.sleep(0.5)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(RemoteSync, "_net_streaming", spy)
        return calls, started

    def _push(self, origin: Path, by: str):
        return remote.run_op("push", lambda eng: RemoteSync(eng, str(origin)).push(), by=by)

    def test_concurrent_pushes_send_once_and_name_who_is_running(self, slow_push, origin: Path):
        import threading

        calls, started = slow_push
        results: dict[str, object] = {}

        def admin(name: str) -> None:
            try:
                results[name] = self._push(origin, f"human-{name}")
            except RemoteError as exc:
                results[name] = exc

        first = threading.Thread(target=admin, args=("alice",), name="alice")
        first.start()
        assert started.wait(5)
        others = [threading.Thread(target=admin, args=(n,), name=n) for n in ("bob", "carol")]
        busy_seen = remote.status()["busy"]
        for t in others:
            t.start()
        for t in [first, *others]:
            t.join()

        assert calls == ["alice"]  # one push reached the remote
        assert busy_seen and busy_seen["op"] == "push" and busy_seen["by"] == "human-alice"
        for name in ("bob", "carol"):
            err = results[name]
            assert isinstance(err, RemoteError) and err.code == "busy"
            assert "human-alice" in str(err)
        assert remote.status()["busy"] is None
        # The busy refusal is not a failure, so it leaves no error behind.
        assert not remote.load_state(remote.home_engine()).get("last_error")

    def test_pushing_what_github_has_sends_nothing(self, slow_push, origin: Path):
        calls, _ = slow_push
        assert self._push(origin, "human-alice")["pushed"] is True
        again = self._push(origin, "human-bob")
        assert again["pushed"] is False and again["reason"] == "already pushed"
        assert len(calls) == 1

    def test_another_process_holding_the_lock_is_refused(self, engine, origin, monkeypatch):
        import fcntl

        monkeypatch.setattr(remote, "home_engine", lambda: engine)
        engine.ensure_repo()
        with (engine.repo / "olano-sync.lock").open("a+") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RemoteError) as err:
                self._push(origin, "human-alice")
            assert err.value.code == "busy" and "Another Olano process" in str(err.value)
        assert self._push(origin, "human-alice")["pushed"] is True  # released with the fd

    def test_a_failure_releases_the_slot(self, engine, monkeypatch):
        monkeypatch.setattr(remote, "home_engine", lambda: engine)

        def boom(_eng):
            raise RemoteError("nope", "git")

        with pytest.raises(RemoteError):
            remote.run_op("push", boom)
        assert remote.current_operation() is None
        assert remote.run_op("push", lambda _eng: "ok") == "ok"

    def test_automatic_push_steps_aside_while_an_admin_syncs(
        self, home: Path, origin: Path, monkeypatch
    ):
        from olano.config.system_settings import set_revisions_settings

        set_revisions_settings({"remote": {"url": str(origin), "sync": "on_commit"}})
        monkeypatch.setattr(remote, "resolve_token", lambda auth: "")
        eng = RevisionsEngine(home)
        eng.snapshot()
        monkeypatch.setattr(remote, "home_engine", lambda: eng)
        with remote.remote_operation(eng, "pull", "human-alice"):
            remote.auto_sync(committed=True)  # must not wait, must not error
        state = remote.load_state(eng)
        assert not state.get("last_error") and not state.get("last_auto_attempt_ts")
        remote.auto_sync(committed=True)  # free again: it pushes
        assert _git(origin, "rev-parse", "main").strip() == eng._head()


# ── logs, binaries and oversized files ─────────────────────────────────────


class TestDocumentsMediaAndLimits:
    def _files(self, home: Path) -> dict[str, bytes]:
        ws = home / "agents/merli/workspace"
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20
        files = {
            "agents/merli/workspace/charts/sales.png": png,
            "agents/merli/workspace/reports/q3.pdf": b"%PDF-1.7\n\0\x01binary",
            "agents/merli/workspace/reports/q3.docx": b"PK\x03\x04" + b"\0" * 64,
            "agents/merli/workspace/audio/memo.mp3": b"ID3\x04" + b"\xff" * 64,
            "agents/merli/workspace/video/demo.mp4": b"\0\0\0\x18ftypmp42" + b"\0" * 64,
            "agents/merli/workspace/video/clip.mov": b"\0\0\0\x14ftypqt  " + b"\0" * 64,
            "agents/merli/workspace/data/rows.json": b'{"rows": [1, 2]}\n',
            "agents/merli/workspace/scripts/build.py": b"print('hi')\n",
        }
        for rel, data in files.items():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_bytes(data)
        (home / "agents/merli/workspace/scripts/build.py").chmod(0o755)
        # never tracked
        (ws / "run.log").write_text("line\n")
        (ws / "data.jsonl").write_text("{}\n")
        (ws / "cache.db").write_bytes(b"SQLite format 3\0")
        venv = ws / "tools" / "venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr\n")
        (venv / "lib.py").write_text("x = 1\n")
        repo = ws / "vendored"
        (repo / ".git").mkdir(parents=True)
        (repo / "README.md").write_text("someone else's repo\n")
        (home / "logs").mkdir(exist_ok=True)
        (home / "logs" / "olano.log").write_text("boot\n")
        return files

    def test_documents_scripts_and_media_are_kept_byte_for_byte(self, home: Path):
        files = self._files(home)
        engine = RevisionsEngine(home)
        assert engine.snapshot().committed
        tree = engine._git("ls-tree", "-r", "HEAD")
        for rel, data in files.items():
            assert engine.show_blob(rel) == data, rel
        assert "100755 blob" in next(ln for ln in tree.splitlines() if "build.py" in ln)
        names = engine._git("ls-tree", "-r", "--name-only", "HEAD").split()
        for never in ("run.log", "data.jsonl", "cache.db", "lib.py", "vendored", "olano.log"):
            assert not any(never in n for n in names), never
        details = engine.commit_details("HEAD")
        assert details is not None
        pdf = next(f for f in details["files"] if f["path"].endswith("q3.pdf"))
        assert pdf["binary"] is True

    def test_unchanged_files_are_not_read_again(self, home: Path, monkeypatch):
        self._files(home)
        engine = RevisionsEngine(home)
        engine.snapshot()
        calls: list[str] = []
        real = engine._hash_file
        monkeypatch.setattr(
            engine, "_hash_file", lambda rel, path, **kw: calls.append(rel) or real(rel, path, **kw)
        )
        (home / MEMORY).write_text("changed\n")
        assert engine.snapshot().committed
        assert calls == [MEMORY]

    def test_default_and_ceiling_are_githubs_per_file_limit(self):
        from olano.config.system_settings import RevisionsSettings

        assert RevisionsSettings().max_file_kb == 50 * 1024  # under GitHub's warning
        assert RevisionsSettings(max_file_kb=80 * 1024).max_file_kb == 80 * 1024
        assert RevisionsSettings(max_file_kb=10**9).max_file_kb == 100 * 1024  # hard limit
        assert RevisionsEngine(Path("/tmp"), max_file_kb=10**9).max_file_bytes == 100 * 1024 * 1024

    def test_settings_writes_only_what_differs_from_defaults(self, home: Path):
        from olano.config.system_settings import load_revisions_settings, set_revisions_settings

        set_revisions_settings({"debounce_seconds": 60})
        block = yaml.safe_load((home / "config.yaml").read_text())["revisions"]
        assert block == {"debounce_seconds": 60}
        assert load_revisions_settings().max_file_kb == 50 * 1024

    def test_a_file_that_outgrows_the_cap_keeps_its_last_version(self, home: Path):
        engine = RevisionsEngine(home, max_file_kb=16)
        engine.snapshot()
        before = engine.show_file(MEMORY)
        (home / MEMORY).write_text("y" * 20_000)
        result = engine.snapshot()
        assert result.skipped_large == [MEMORY]
        assert engine.show_file(MEMORY) == before  # not recorded as deleted
        assert not result.committed
        assert engine.held_back() == [{"path": MEMORY, "reason": "too_large"}]
        (home / MEMORY).write_text("small again\n")
        assert engine.snapshot().committed
        assert engine.show_file(MEMORY) == "small again\n"
        assert engine.held_back() == []

    def test_a_file_never_recorded_stays_out_while_oversized(self, home: Path):
        engine = RevisionsEngine(home, max_file_kb=16)
        engine.snapshot()
        (home / "agents/merli/workspace/IDEAS.md").write_text("z" * 20_000)
        engine.snapshot()
        assert engine.show_file("agents/merli/workspace/IDEAS.md") is None

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-ant-api03-" + "a" * 40,
            "ghp_" + "b" * 36,
            "AKIA" + "C" * 16,
            "xoxb-1234567890-abcdefghij",
            "-----BEGIN RSA PRIVATE KEY-----",
            "postgres://admin:hunter2@db.internal:5432/app",
        ],
    )
    def test_a_file_with_a_credential_is_held_back(self, home: Path, secret: str):
        engine = RevisionsEngine(home)
        script = home / "agents/merli/workspace/scripts/sync.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('v1')\n")
        engine.snapshot()
        script.write_text(f"KEY = '{secret}'\n")
        result = engine.snapshot()
        assert {"path": "agents/merli/workspace/scripts/sync.py", "reason": "secret"} in (
            result.held_back
        )
        assert engine.show_file("agents/merli/workspace/scripts/sync.py") == "print('v1')\n"
        assert secret not in engine._git("log", "-p", "--all")

    def test_ordinary_text_is_not_mistaken_for_a_credential(self, home: Path):
        engine = RevisionsEngine(home)
        notes = home / "agents/merli/workspace/notes.md"
        notes.write_text(
            "Use the sk- prefix for keys. Ask for a token. postgres://localhost/app\n"
            "password policy: rotate every 90 days\n"
        )
        engine.snapshot()
        assert engine.held_back() == []
        assert engine.show_file("agents/merli/workspace/notes.md") is not None

    def test_agent_config_with_a_credential_outside_env_is_held_back(self, home: Path):
        engine = RevisionsEngine(home)
        engine.snapshot()
        (home / AGENT).write_text(
            '{ name: "merli", env: {}, mcp_servers: { x: { headers: '
            '{ Authorization: "Bearer sk-proj-' + "d" * 30 + '" } } } }\n'
        )
        result = engine.snapshot()
        assert {"path": AGENT, "reason": "secret"} in result.held_back
        assert "sk-proj-" not in (engine.show_file(AGENT) or "")

    def test_pull_writes_documents_and_media_byte_for_byte(
        self, syncer: RemoteSync, upstream: Upstream, home: Path
    ):
        png = b"\x89PNG\r\n\x1a\n\0\xff\xfe" * 100
        (upstream.work / "agents/merli/workspace/charts").mkdir(parents=True)
        (upstream.work / "agents/merli/workspace/charts/a.png").write_bytes(png)
        (upstream.work / "agents/merli/workspace/run.sh").write_text("#!/bin/sh\necho hi\n")
        (upstream.work / "agents/merli/workspace/run.sh").chmod(0o755)
        upstream.push()
        result = syncer.pull()
        assert (home / "agents/merli/workspace/charts/a.png").read_bytes() == png
        assert os.access(home / "agents/merli/workspace/run.sh", os.X_OK)
        assert "agents/merli/workspace/charts/a.png" in result["written"]

    def test_a_binary_conflict_is_resolved_by_picking_a_side(
        self, syncer: RemoteSync, upstream: Upstream, engine, home: Path
    ):
        rel = "agents/merli/workspace/charts/a.png"
        (home / rel).parent.mkdir(parents=True)
        (home / rel).write_bytes(b"\x89PNG base\0")
        engine.snapshot()
        syncer.push()
        _git(upstream.work, "pull", "-q", "origin", "main")
        (upstream.work / rel).write_bytes(b"\x89PNG github\0")
        upstream.push()
        (home / rel).write_bytes(b"\x89PNG local\0")
        engine.snapshot()
        assert syncer.pull()["status"] == "conflicts"
        detail = syncer.merge_file(rel)
        assert detail["binary"] is True and detail["merged"] == "" and detail["sizes"]["ours"]
        syncer.resolve_merge({rel: {"action": "theirs"}})
        assert (home / rel).read_bytes() == b"\x89PNG github\0"

    def test_held_back_files_are_reported_by_the_log_route(self, client, home: Path):
        from olano.config.system_settings import set_revisions_settings

        set_revisions_settings({"max_file_kb": 16})
        (home / MEMORY).write_text("y" * 20_000)
        body = client.get("/api/revisions/log").json()
        assert body["held_back"] == [{"path": MEMORY, "reason": "too_large"}]
        assert body["max_file_kb"] == 16

    def test_binary_files_are_viewed_as_a_download(self, client, engine, home: Path):
        rel = "agents/merli/workspace/charts/a.png"
        (home / rel).parent.mkdir(parents=True)
        (home / rel).write_bytes(b"\x89PNG\0\x01\x02")
        engine.snapshot()
        meta = client.get(f"/api/revisions/file?path={rel}").json()
        assert meta["binary"] is True and meta["size"] == 7
        raw = client.get(f"/api/revisions/raw?path={rel}")
        assert raw.status_code == 200 and raw.content == b"\x89PNG\0\x01\x02"
        assert raw.headers["content-disposition"].startswith("attachment")
        assert raw.headers["x-content-type-options"] == "nosniff"


# ── what the dashboard draws while an operation runs ─────────────────────────


class TestProgress:
    def test_a_push_reports_gits_own_upload_progress(
        self, syncer: RemoteSync, engine: RevisionsEngine, home: Path, monkeypatch
    ):
        ws = home / "agents" / "merli" / "workspace"
        for i in range(40):
            (ws / f"note{i}.md").write_text(f"note {i}\n" * 500)
        engine.snapshot(author="merli", reason="many notes")
        seen: list[dict] = []
        real = remote.report_progress

        def spy(step: str, **kw):
            real(step, **kw)
            seen.append(dict(remote.current_operation()["progress"]))

        monkeypatch.setattr(remote, "report_progress", spy)
        with remote.remote_operation(engine, "push", "human-alice"):
            assert remote.current_operation()["progress"]["steps"] == ["upload"]
            assert syncer.push()["pushed"] is True
        assert remote.current_operation() is None  # the record goes with the slot
        assert {p["step"] for p in seen} == {"upload"}
        percents = [p["percent"] for p in seen]
        assert percents[0] == 0 and percents[-1] == 100
        assert percents == sorted(percents)  # never moves backwards
        assert any(p.get("phase") == "writing" and p.get("total") for p in seen)

    def test_a_pull_walks_its_steps_in_order(
        self, syncer: RemoteSync, upstream: Upstream, engine, monkeypatch
    ):
        upstream.write(MEMORY, "# memory\nfrom github\n").push()
        steps: list[str] = []
        monkeypatch.setattr(remote, "report_progress", lambda step, **kw: steps.append(step))
        with remote.remote_operation(engine, "pull", "human-alice"):
            assert remote.current_operation()["progress"]["steps"] == [
                "snapshot",
                "download",
                "apply",
            ]
            syncer.pull()
        order = list(dict.fromkeys(steps))
        assert order == ["snapshot", "download", "apply"]

    def test_progress_outside_an_operation_is_ignored(self):
        remote.report_progress("upload", percent=50)
        assert remote.current_operation() is None

    @pytest.mark.parametrize(
        ("line", "phase", "done", "total", "transfer"),
        [
            (
                "Writing objects:  37% (370/1000), 12.00 MiB | 3.00 MiB/s",
                "writing",
                370,
                1000,
                "12.00 MiB | 3.00 MiB/s",
            ),
            (
                "Writing objects: 100% (1000/1000), 410.10 MiB | 3.00 MiB/s, done.",
                "writing",
                1000,
                1000,
                "410.10 MiB | 3.00 MiB/s",
            ),
            ("Compressing objects: 100% (12/12), done.", "compressing", 12, 12, ""),
            (
                "Receiving objects:  80% (8/10), 1.00 KiB | 1.00 KiB/s",
                "receiving",
                8,
                10,
                "1.00 KiB | 1.00 KiB/s",
            ),
        ],
    )
    def test_gits_progress_lines_are_parsed(self, line, phase, done, total, transfer, monkeypatch):
        got: list[dict] = []
        monkeypatch.setattr(remote, "report_progress", lambda step, **kw: got.append(kw))
        phases = (
            remote._UPLOAD_PHASES
            if phase in ("writing", "compressing")
            else remote._DOWNLOAD_PHASES
        )
        remote._progress_reader("upload", phases)(line)
        assert got and got[0]["phase"] == phase
        assert (got[0]["done"], got[0]["total"]) == (done, total)
        assert got[0]["transfer"] == transfer

    def test_a_push_counts_as_knowing_githubs_state(self, syncer: RemoteSync, engine):
        syncer.push()
        state = remote.load_state(engine)
        assert state["last_fetch_at"] == state["last_push_at"]


class TestLogPaging:
    def test_pages_and_total(self, engine: RevisionsEngine, home: Path):
        ws = home / "agents" / "merli" / "workspace"
        for i in range(12):
            (ws / "IDEAS.md").write_text(f"idea {i}\n")
            engine.snapshot(author="merli", reason=f"turn {i}")
        assert engine.count() == 13
        first = engine.log(limit=10)
        second = engine.log(limit=10, skip=10)
        assert [e["message"] for e in first][:2] == ["turn 11", "turn 10"]
        assert [e["message"] for e in second] == ["turn 1", "turn 0", "first"]
        assert engine.count("agents/merli/workspace/IDEAS.md") == 12

    def test_the_log_route_returns_a_page_and_the_total(
        self, client, engine: RevisionsEngine, home: Path
    ):
        ws = home / "agents" / "merli" / "workspace"
        for i in range(4):
            (ws / "IDEAS.md").write_text(f"idea {i}\n")
            engine.snapshot(author="merli", reason=f"turn {i}")
        body = client.get("/api/revisions/log", params={"limit": 2, "skip": 2}).json()
        assert body["total"] == 5 and body["skip"] == 2
        assert [e["message"] for e in body["entries"]] == ["turn 1", "turn 0"]
