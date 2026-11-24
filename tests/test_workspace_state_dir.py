"""The per-workspace state folder is ``.olano/`` (``WORKSPACE_STATE_DIRNAME``).

Every workspace provisioned before the rename carries ``.lionclaw/`` with live
state in it (the cognition audit, the self-learning watermark, offloaded tool
results, the workspace map), so the rename is a one-time move with a link left
behind, done by the first ``Workspace`` construction after an upgrade. The
ignore file and the cartography walker follow the same two-name rule.
"""

from __future__ import annotations

from pathlib import Path

from olano.config.workspace import (
    LEGACY_WORKSPACE_IGNORE_FILENAMES,
    LEGACY_WORKSPACE_STATE_DIRNAMES,
    WORKSPACE_IGNORE_FILENAME,
    WORKSPACE_STATE_DIRNAME,
    Workspace,
    adopt_legacy_state_dir,
    workspace_ignore_file,
)

LEGACY = LEGACY_WORKSPACE_STATE_DIRNAMES[0]


def test_the_names_are_the_rename():
    assert WORKSPACE_STATE_DIRNAME == ".olano"
    assert LEGACY_WORKSPACE_STATE_DIRNAMES == (".lionclaw",)
    assert WORKSPACE_IGNORE_FILENAME == ".olanoignore"
    assert LEGACY_WORKSPACE_IGNORE_FILENAMES == (".lionclawignore",)


def test_fresh_workspace_gets_only_the_current_folder(tmp_path: Path):
    ws = Workspace(root=tmp_path / "ws")
    assert ws.state_dir == tmp_path / "ws" / ".olano"
    assert ws.state_dir.is_dir()
    assert not (tmp_path / "ws" / LEGACY).exists()


def test_legacy_folder_is_moved_once_and_linked(tmp_path: Path):
    root = tmp_path / "ws"
    legacy = root / LEGACY
    (legacy / "state" / "cognition").mkdir(parents=True)
    (legacy / "state" / "cognition" / "actions.jsonl").write_text("{}\n")
    (legacy / "workspace_map.md").write_text("# map\n")

    ws = Workspace(root=root)

    # The content moved, under the same inode tree (no copy, ownership kept).
    assert ws.state_dir.is_dir() and not ws.state_dir.is_symlink()
    assert (ws.state_dir / "state" / "cognition" / "actions.jsonl").read_text() == "{}\n"
    # The old name is a relative link, so a path recorded before the rename
    # (a checkpoint citing an offloaded result, a note) still resolves.
    assert legacy.is_symlink()
    assert legacy.readlink() == Path(".olano")
    assert (legacy / "workspace_map.md").read_text() == "# map\n"
    # Idempotent: a second construction changes nothing.
    Workspace(root=root)
    assert legacy.is_symlink() and ws.state_dir.is_dir()
    assert adopt_legacy_state_dir(root) is False


def test_adopt_reports_only_a_real_move(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    assert adopt_legacy_state_dir(root) is False  # nothing there
    (root / LEGACY).mkdir()
    assert adopt_legacy_state_dir(root) is True
    assert adopt_legacy_state_dir(root) is False  # already done


def test_both_names_present_is_left_alone(tmp_path: Path):
    """Someone already arranged things: never merge, never delete."""
    root = tmp_path / "ws"
    (root / ".olano").mkdir(parents=True)
    (root / ".olano" / "new.txt").write_text("new")
    (root / LEGACY).mkdir()
    (root / LEGACY / "old.txt").write_text("old")

    assert adopt_legacy_state_dir(root) is False
    Workspace(root=root)

    assert not (root / LEGACY).is_symlink()
    assert (root / LEGACY / "old.txt").read_text() == "old"
    assert (root / ".olano" / "new.txt").read_text() == "new"


def test_a_legacy_symlink_is_never_moved(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / LEGACY).symlink_to(elsewhere)

    assert adopt_legacy_state_dir(root) is False
    Workspace(root=root)

    assert (root / ".olano").is_dir() and not (root / ".olano").is_symlink()
    assert (root / LEGACY).is_symlink()
    assert (root / LEGACY).readlink() == elsewhere


def test_scaffold_adopts_a_legacy_folder_before_creating_the_new_one(tmp_path: Path):
    from olano.archetypes.installer import scaffold_agent_workspace

    root = tmp_path / "workspace"
    (root / LEGACY / "state" / "pulse").mkdir(parents=True)
    (root / LEGACY / "state" / "pulse" / "inbox.json").write_text("[]")

    scaffold_agent_workspace("zeta", {"description": "z", "system_prompt": "x"}, root)

    assert (root / ".olano" / "state" / "pulse" / "inbox.json").read_text() == "[]"
    assert (root / ".olano" / "workspace_map.md").exists()
    assert (root / LEGACY).is_symlink()


def test_ignore_file_current_name_wins(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    assert workspace_ignore_file(root) is None
    (root / ".lionclawignore").write_text("big/\n")
    assert workspace_ignore_file(root) == root / ".lionclawignore"
    (root / ".olanoignore").write_text("*.log\n")
    assert workspace_ignore_file(root) == root / ".olanoignore"


def test_cartographer_reads_either_ignore_file(tmp_path: Path):
    from olano.cartography.cartographer import WorkspaceCartographer

    ws = Workspace(root=tmp_path / "ws")
    (ws.root / ".lionclawignore").write_text("# comment\nbig/\n\n*.log\n")
    assert WorkspaceCartographer(ws)._load_ignore_patterns() == ["big/", "*.log"]
    (ws.root / ".olanoignore").write_text("new/\n")
    assert WorkspaceCartographer(ws)._load_ignore_patterns() == ["new/"]


def test_walker_skips_both_state_folder_names(tmp_path: Path):
    from olano.cartography.walker import SKIP_DIR_NAMES, iter_workspace

    assert {".olano", ".lionclaw"} <= SKIP_DIR_NAMES
    root = tmp_path / "ws"
    for name in (".olano", ".lionclaw"):
        (root / name).mkdir(parents=True)
        (root / name / "x.md").write_text("x")
    (root / "keep.md").write_text("k")
    assert [rel for kind, rel, _ in iter_workspace(root) if kind == "file"] == ["keep.md"]
