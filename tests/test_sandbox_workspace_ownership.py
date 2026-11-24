"""The sandboxed agent (uid 1000) must own what the runtime (root) wrote for it.

The runtime creates and seeds an agent's workspace as root, then bind-mounts it
into a container whose user is uid 1000. A bind mount preserves host ownership,
so anything the runtime wrote is read-only — or, at 0600, invisible — to the
agent. Verified against a real container: chowning only the top directory left
`edit_file MEMORY.md`, `write_file memories/<today>.md` and
`mkdir /workspace/system/…` all failing with "permission denied", and the chat
`!` run log (written 0600) unreadable by the very agent it exists for.

`os.chown` is stubbed rather than exercised for real so these run the same
whether or not the test process is root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import olano.core.builder as builder


@pytest.fixture
def chowned(monkeypatch) -> list[tuple[str, int, int, bool]]:
    """Record every chown the code attempts, and let them all succeed."""
    calls: list[tuple[str, int, int, bool]] = []
    real_chown = os.chown

    def fake_chown(path, uid, gid, *, follow_symlinks=True):
        calls.append((str(path), uid, gid, follow_symlinks))
        try:  # keep on-disk reality consistent when we can (root); ignore otherwise
            st = Path(path).lstat()
            real_chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass

    monkeypatch.setattr(builder.os, "chown", fake_chown)
    return calls


def _seed_workspace(root: Path) -> Path:
    """A workspace shaped like one the runtime just seeded."""
    ws = root / "workspace"
    (ws / "memories").mkdir(parents=True)
    (ws / "system" / "knowledge").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    (ws / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (ws / "memories" / "2026-07-28.md").write_text("today\n", encoding="utf-8")
    (ws / "system" / "knowledge" / "USER_GUIDE.md").write_text("guide\n", encoding="utf-8")
    return ws


def test_recursive_reowns_everything_the_runtime_seeded(chowned, tmp_path):
    ws = _seed_workspace(tmp_path)

    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    owned = {Path(p).relative_to(ws).as_posix() for p, *_ in chowned if Path(p) != ws}
    assert owned == {
        "MEMORY.md",
        "AGENTS.md",
        "memories",
        "memories/2026-07-28.md",
        "system",
        "system/knowledge",
        "system/knowledge/USER_GUIDE.md",
    }
    assert all(
        uid == builder.SANDBOX_UID and gid == builder.SANDBOX_GID for _, uid, gid, _ in chowned
    )


def test_non_recursive_touches_only_the_top_directory(chowned, tmp_path):
    """The shared tool layer (pip/npm trees) and operator-declared shared sources
    must NOT be walked — one is huge, the other isn't ours to re-own."""
    ws = _seed_workspace(tmp_path)

    builder._ensure_sandbox_shared_dir(ws)

    assert [Path(p) for p, *_ in chowned] == [ws]


def test_symlinks_are_chowned_as_links_never_followed(chowned, tmp_path):
    """`workspace/shared/*` are projections into <home>/shared. Following them
    would hand the shared source itself to one agent's sandbox uid."""
    ws = tmp_path / "workspace"
    (ws / "shared").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    (ws / "shared" / "manual").symlink_to(outside)

    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    assert all(follow is False for *_, follow in chowned)
    touched = {Path(p) for p, *_ in chowned}
    assert ws / "shared" / "manual" in touched  # the link itself
    assert outside / "secret.txt" not in touched  # never descended through it


def test_already_owned_entries_are_skipped(monkeypatch, chowned, tmp_path):
    """Steady state is a no-op walk — re-owning on every agent build would be
    pointless churn on a large workspace."""
    ws = _seed_workspace(tmp_path)

    class _Stat:
        st_uid = builder.SANDBOX_UID
        st_gid = builder.SANDBOX_GID

    monkeypatch.setattr(Path, "lstat", lambda self: _Stat())

    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    assert [Path(p) for p, *_ in chowned] == [ws]  # only the top dir, unconditionally


def test_walk_is_bounded_and_says_what_it_skipped(monkeypatch, chowned, tmp_path, caplog):
    """A workspace someone filled with node_modules must not stall a build — and
    the truncation must be visible, not silent."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    for i in range(12):
        (ws / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(builder, "_SANDBOX_CHOWN_MAX_ENTRIES", 5)

    with caplog.at_level("WARNING"):
        builder._ensure_sandbox_shared_dir(ws, recursive=True)

    assert len(chowned) == 6  # the top dir + the 5 allowed entries
    assert "stopped re-owning" in caplog.text


def test_chown_failure_is_swallowed(monkeypatch, tmp_path):
    """Not-root (a self-host install) must degrade, never raise mid-build."""
    ws = _seed_workspace(tmp_path)

    def deny(*a, **k):
        raise PermissionError("not root")

    monkeypatch.setattr(builder.os, "chown", deny)

    builder._ensure_sandbox_shared_dir(ws, recursive=True)  # must not raise
    assert ws.stat().st_mode & 0o777 == 0o777  # fell back to world-writable


def test_chown_for_sandbox_reports_success(chowned, tmp_path):
    target = tmp_path / "run.log"
    target.write_text("x", encoding="utf-8")

    assert builder.chown_for_sandbox(target) is True
    assert chowned == [(str(target), builder.SANDBOX_UID, builder.SANDBOX_GID, False)]


def test_chown_for_sandbox_reports_failure(monkeypatch, tmp_path):
    target = tmp_path / "run.log"
    target.write_text("x", encoding="utf-8")

    def deny(*a, **k):
        raise PermissionError("not root")

    monkeypatch.setattr(builder.os, "chown", deny)

    assert builder.chown_for_sandbox(target) is False
