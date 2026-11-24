"""The pruned cartography walk + off-boot-path refresh.

Motivating incident: a working agent's workspace grew to 11 GB — three cloned
Home Assistant checkouts (883 MB ``.git`` each, one 2 GB venv), venvs named
``.venv-social`` / ``.venv-google-verify`` that no name-based skip list knew,
mypy/ruff caches — and the boot-time cartography scan, which walked all of it
with ``rglob`` (twice: once to index, once for the map), measured **130.9s of a
136.0s agent boot**. These tests pin the properties of the fix:

* the walk *prunes* instead of filtering — repo/venv/cache subtrees are never
  descended into, so their contents are never stat-ed or classified;
* directories are recognised by marker (``.git``, ``pyvenv.cfg``), not name;
* the incremental scan re-classifies only changed files and drops deleted ones;
* the map is generated from the index — no second walk;
* the boot spawns the refresh in the background (opt back to inline with
  ``LIONCLAW_SYNC_CARTOGRAPHY=1``) and never queues the same workspace twice.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olano.cartography.walker import EXTERNAL_REPO_LABEL, iter_workspace


def _mkfile(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def messy_workspace(tmp_path: Path) -> Path:
    """A workspace shaped like the incident: real files + huge prunable trees."""
    _mkfile(tmp_path / "AGENTS.md", "# Agents\n")
    _mkfile(tmp_path / "MEMORY.md", "# Memory\n")
    _mkfile(tmp_path / "projects" / "plan.md", "# Plan\n")

    # An embedded repository the agent cloned for its work.
    repo = tmp_path / "scratch" / "home-assistant-core"
    (repo / ".git").mkdir(parents=True)
    _mkfile(repo / ".git" / "config", "[core]\n")
    for i in range(25):
        _mkfile(repo / "homeassistant" / f"module_{i}.py", "pass\n")

    # A virtualenv whose name matches no skip list — only its marker gives it away.
    venv = tmp_path / ".venv-social"
    _mkfile(venv / "pyvenv.cfg", "home = /usr\n")
    for i in range(25):
        _mkfile(venv / "lib" / f"pkg_{i}.py", "pass\n")

    # A renamed npm installation tree — npm ≥7 leaves its marker inside.
    npm = tmp_path / "vendored-deps"
    _mkfile(npm / ".package-lock.json", "{}")
    for i in range(25):
        _mkfile(npm / "left-pad" / f"index_{i}.js", ";\n")

    # Well-known cache dirs.
    _mkfile(tmp_path / ".mypy_cache" / "3.11" / "foo.data.json", "{}")
    _mkfile(tmp_path / "sub" / "__pycache__" / "mod.cpython-311.pyc", "")
    _mkfile(tmp_path / "web" / "node_modules" / "react" / "index.js", ";\n")
    return tmp_path


class TestWalker:
    def test_embedded_repo_is_one_entry_and_never_descended(self, messy_workspace: Path) -> None:
        results = list(iter_workspace(messy_workspace))
        repos = [rel for kind, rel, _ in results if kind == "repo"]
        files = [rel for kind, rel, _ in results if kind == "file"]

        assert repos == ["scratch/home-assistant-core/"]
        assert not any(rel.startswith("scratch/home-assistant-core/h") for rel in files)
        assert not any(".git" in rel for rel in files)

    def test_venv_recognised_by_marker_not_name(self, messy_workspace: Path) -> None:
        files = [rel for kind, rel, _ in iter_workspace(messy_workspace) if kind == "file"]
        assert not any(rel.startswith(".venv-social/") for rel in files)

    def test_npm_tree_recognised_by_marker_not_name(self, messy_workspace: Path) -> None:
        files = [rel for kind, rel, _ in iter_workspace(messy_workspace) if kind == "file"]
        assert not any(rel.startswith("vendored-deps/") for rel in files)

    def test_cache_dirs_pruned(self, messy_workspace: Path) -> None:
        files = [rel for kind, rel, _ in iter_workspace(messy_workspace) if kind == "file"]
        assert not any(".mypy_cache" in rel or "__pycache__" in rel for rel in files)
        assert not any("node_modules" in rel for rel in files)

    def test_real_workspace_files_survive_with_stats(self, messy_workspace: Path) -> None:
        by_rel = {rel: st for kind, rel, st in iter_workspace(messy_workspace) if kind == "file"}
        assert {"AGENTS.md", "MEMORY.md", "projects/plan.md"} <= set(by_rel)
        assert by_rel["AGENTS.md"].st_size == len("# Agents\n")

    def test_workspace_root_itself_may_be_a_git_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        _mkfile(tmp_path / "MEMORY.md")
        results = list(iter_workspace(tmp_path))
        assert ("file", "MEMORY.md") in [(k, r) for k, r, _ in results]
        assert not any(kind == "repo" for kind, _, _ in results)

    def test_lionclawignore_patterns_prune_dirs_and_filter_files(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _mkfile(ws / "keep.md")
        _mkfile(ws / "big" / "huge.bin")
        _mkfile(ws / "debug.log")
        rels = [
            rel
            for kind, rel, _ in iter_workspace(ws, ignore_patterns=["big/", "*.log"])
            if kind == "file"
        ]
        assert rels == ["keep.md"]

    def test_symlinked_directory_is_not_followed(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _mkfile(ws / "real.md")
        outside = tmp_path / "outside-tree"
        _mkfile(outside / "leak.md")
        (ws / "loop").symlink_to(ws)  # would recurse forever
        (ws / "outside").symlink_to(outside)
        rels = [rel for kind, rel, _ in iter_workspace(ws) if kind == "file"]
        assert rels == ["real.md"]


class TestIncrementalScan:
    @pytest.fixture
    def workspace(self, messy_workspace: Path) -> Any:
        from olano.config.workspace import Workspace

        return Workspace(root=messy_workspace)

    def _counting_classifier(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        import olano.cartography.cartographer as cart_mod

        seen: list[str] = []
        real = cart_mod.classify_file

        def counting(path: Path, root: Path) -> Any:
            seen.append(str(path.relative_to(root)))
            return real(path, root)

        monkeypatch.setattr(cart_mod, "classify_file", counting)
        return seen

    def test_pruned_content_is_never_classified(
        self, workspace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from olano.cartography.cartographer import WorkspaceCartographer

        seen = self._counting_classifier(monkeypatch)
        WorkspaceCartographer(workspace).run_incremental()
        assert seen, "nothing was classified at all"
        assert not any("home-assistant-core/" in rel and rel.endswith(".py") for rel in seen)
        assert not any(rel.startswith(".venv-social/") for rel in seen)

    def test_second_run_classifies_nothing_unchanged(
        self, workspace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from olano.cartography.cartographer import WorkspaceCartographer

        WorkspaceCartographer(workspace).run_incremental()
        seen = self._counting_classifier(monkeypatch)
        WorkspaceCartographer(workspace).run_incremental()
        assert seen == [], f"unchanged files were re-classified: {seen}"

    def test_changed_file_is_reclassified_and_deleted_file_drops_out(
        self, workspace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from olano.cartography.cartographer import WorkspaceCartographer

        WorkspaceCartographer(workspace).run_incremental()

        target = workspace.root / "projects" / "plan.md"
        target.write_text("# Plan v2\n", encoding="utf-8")
        future = time.time() + 5
        import os

        os.utime(target, (future, future))
        (workspace.root / "MEMORY.md").unlink()

        seen = self._counting_classifier(monkeypatch)
        WorkspaceCartographer(workspace).run_incremental()

        assert seen == ["projects/plan.md"]
        index = json.loads(workspace.workspace_index_path.read_text(encoding="utf-8"))
        assert "MEMORY.md" not in index
        assert "projects/plan.md" in index

    def test_embedded_repo_lands_in_index_and_map(self, workspace: Any) -> None:
        import json

        from olano.cartography.cartographer import WorkspaceCartographer

        WorkspaceCartographer(workspace).run_incremental()
        index = json.loads(workspace.workspace_index_path.read_text(encoding="utf-8"))
        assert index["scratch/home-assistant-core/"]["label"] == EXTERNAL_REPO_LABEL

        workspace_map = workspace.workspace_map_path.read_text(encoding="utf-8")
        assert "scratch/home-assistant-core/" in workspace_map
        assert EXTERNAL_REPO_LABEL in workspace_map


class TestMapFromIndex:
    def test_map_uses_index_without_walking(self, tmp_path: Path, monkeypatch) -> None:
        import olano.cartography.map_generator as map_mod

        def boom(*a: Any, **k: Any) -> Any:
            raise AssertionError("map generation walked the tree despite having an index")

        monkeypatch.setattr(map_mod, "iter_workspace", boom)
        index = {"AGENTS.md": {"category": "governance", "label": "Agent instructions"}}
        result = map_mod.generate_workspace_map(tmp_path, index=index)
        assert "AGENTS.md" in result

    def test_map_keeps_depth_and_state_filters(self, tmp_path: Path) -> None:
        from olano.cartography.map_generator import generate_workspace_map

        index = {
            "a/b/c/d/e/too-deep.md": {"category": "document", "label": "deep"},
            ".x/state.json": {"category": "state", "label": "state artifact"},
            "TOP.md": {"category": "document", "label": "doc"},
        }
        result = generate_workspace_map(tmp_path, index=index)
        assert "TOP.md" in result
        assert "too-deep.md" not in result
        assert "state.json" not in result


class TestBootPathSpawn:
    def _workspace(self, tmp_path: Path) -> Any:
        return SimpleNamespace(root=tmp_path)

    def test_sync_env_runs_inline(self, tmp_path: Path, monkeypatch) -> None:
        import olano.core.builder as builder

        monkeypatch.setenv("LIONCLAW_SYNC_CARTOGRAPHY", "1")
        calls: list[Any] = []
        monkeypatch.setattr(builder, "_run_cartography_incremental", calls.append)
        builder._spawn_cartography_refresh(self._workspace(tmp_path))
        assert len(calls) == 1  # ran before returning, on this thread

    def test_default_runs_in_background_thread(self, tmp_path: Path, monkeypatch) -> None:
        import olano.core.builder as builder

        monkeypatch.delenv("LIONCLAW_SYNC_CARTOGRAPHY", raising=False)
        done = threading.Event()
        threads: list[str] = []

        def record(_ws: Any) -> None:
            threads.append(threading.current_thread().name)
            done.set()

        monkeypatch.setattr(builder, "_run_cartography_incremental", record)
        builder._spawn_cartography_refresh(self._workspace(tmp_path))
        assert done.wait(timeout=5), "background refresh never ran"
        assert threads[0] != threading.main_thread().name
        assert threads[0].startswith("cartography-")

    def test_same_workspace_is_never_queued_twice(self, tmp_path: Path, monkeypatch) -> None:
        import olano.core.builder as builder

        monkeypatch.delenv("LIONCLAW_SYNC_CARTOGRAPHY", raising=False)
        release = threading.Event()
        started = threading.Event()
        calls: list[Any] = []

        def slow(_ws: Any) -> None:
            calls.append(1)
            started.set()
            release.wait(timeout=10)

        monkeypatch.setattr(builder, "_run_cartography_incremental", slow)
        ws = self._workspace(tmp_path)
        builder._spawn_cartography_refresh(ws)
        assert started.wait(timeout=5)
        builder._spawn_cartography_refresh(ws)  # dedup: first still in flight
        release.set()
        deadline = time.time() + 5
        while builder._CARTOGRAPHY_INFLIGHT and time.time() < deadline:
            time.sleep(0.02)
        assert calls == [1]

        # And once the first finished, a new refresh is allowed again.
        done = threading.Event()
        monkeypatch.setattr(builder, "_run_cartography_incremental", lambda _ws: done.set())
        builder._spawn_cartography_refresh(ws)
        assert done.wait(timeout=5)
