"""Tests for the workspace trash-can tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.tools.workspace_files_tools import (
    trash_empty,
    trash_list,
    trash_put,
    trash_restore,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    return tmp_path


def test_trash_put_moves_file_and_records_origin(workspace: Path) -> None:
    src = workspace / "junk.txt"
    src.write_text("bye")

    result = trash_put.func(path=str(src))

    assert "Moved to trash" in result
    assert not src.exists()
    listing = trash_list.func()
    assert "junk.txt" in listing
    assert str(src) in listing


def test_trash_restore_returns_file_to_origin(workspace: Path) -> None:
    src = workspace / "data.csv"
    src.write_text("a,b")
    trash_put.func(path=str(src))

    entry = next(
        p.name for p in (workspace / "trash").iterdir() if not p.name.endswith(".trashinfo")
    )
    result = trash_restore.func(entry_name=entry)

    assert "Restored" in result
    assert src.exists()
    assert src.read_text() == "a,b"
    assert trash_list.func() == "Trash is empty."


def test_trash_empty_requires_confirmation(workspace: Path) -> None:
    src = workspace / "temp.log"
    src.write_text("noise")
    trash_put.func(path=str(src))

    preview = trash_empty.func(confirm=False)
    assert "cannot be undone" in preview
    assert trash_list.func() != "Trash is empty."

    done = trash_empty.func(confirm=True)
    assert "Emptied trash" in done
    assert trash_list.func() == "Trash is empty."


def test_trash_put_missing_file_errors(workspace: Path) -> None:
    result = trash_put.func(path=str(workspace / "nope.txt"))
    assert result.startswith("Error")


def test_trash_targets_agent_workspace_from_request_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The active agent's workspace (from the request context) must win over the
    # legacy/global workspace resolved by a bare get_workspace().
    from olano.security.request_context import push_request_context

    legacy = tmp_path / "legacy"
    agent_ws = tmp_path / "agents" / "aurelius" / "workspace"
    agent_ws.mkdir(parents=True)
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(legacy))

    src = agent_ws / "doc.txt"
    src.write_text("x")
    with push_request_context(metadata={"workspace_root": str(agent_ws)}):
        trash_put.func(path=str(src))
        assert (agent_ws / "trash").is_dir()
        assert not (legacy / "trash").exists()


# ─────────────────────────── workspace confinement ───────────────────────────
# The trash tools move files with raw shutil calls, outside the deepagents
# backend/permission layer — so they must enforce their own boundary.


@pytest.fixture
def nested_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace that is a subdirectory of tmp_path, so tests can place
    files OUTSIDE it (tmp_path itself) to probe the confinement boundary."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(ws))
    return ws


def test_trash_put_rejects_paths_outside_workspace(nested_workspace: Path) -> None:
    secret = nested_workspace.parent / "secret.txt"
    secret.write_text("s3cr3t")

    result = trash_put.func(path=str(secret))
    assert result.startswith("Error") and "outside" in result
    assert secret.exists()

    # Relative traversal from the workspace root must not escape either.
    result = trash_put.func(path="../secret.txt")
    assert result.startswith("Error") and "outside" in result
    assert secret.exists()


def test_trash_put_rejects_symlink_escaping_workspace(nested_workspace: Path) -> None:
    secret = nested_workspace.parent / "secret.txt"
    secret.write_text("s3cr3t")
    link = nested_workspace / "innocent.txt"
    link.symlink_to(secret)

    result = trash_put.func(path=str(link))
    assert result.startswith("Error") and "outside" in result
    assert secret.exists()
    assert link.is_symlink()


def test_trash_put_rejects_workspace_root_and_trash_itself(nested_workspace: Path) -> None:
    assert trash_put.func(path=str(nested_workspace)).startswith("Error")

    (nested_workspace / "junk.txt").write_text("x")
    trash_put.func(path="junk.txt")
    assert trash_put.func(path="trash").startswith("Error")


def test_trash_restore_rejects_traversal_entry_names(nested_workspace: Path) -> None:
    victim = nested_workspace / "SOUL.md"
    victim.write_text("identity")
    (nested_workspace / "trash").mkdir()

    result = trash_restore.func(entry_name="../SOUL.md", destination="stolen.md")
    assert result.startswith("Error: invalid trash entry name")
    assert victim.exists()
    assert not (nested_workspace / "stolen.md").exists()


def test_trash_restore_rejects_destination_outside_workspace(nested_workspace: Path) -> None:
    src = nested_workspace / "data.csv"
    src.write_text("a,b")
    trash_put.func(path=str(src))
    entry = next(
        p.name for p in (nested_workspace / "trash").iterdir() if not p.name.endswith(".trashinfo")
    )

    escape = nested_workspace.parent / "out.csv"
    result = trash_restore.func(entry_name=entry, destination=str(escape))
    assert result.startswith("Error") and "outside" in result
    assert not escape.exists()
    assert (nested_workspace / "trash" / entry).exists()


def test_trash_restore_refuses_sidecar_original_path_outside_workspace(
    nested_workspace: Path,
) -> None:
    # Entries trashed before confinement (or with an edited sidecar) may record
    # an original path outside the workspace — restoring there must refuse.
    import json

    trash = nested_workspace / "trash"
    trash.mkdir()
    entry = "20240101T000000Z__evil.txt"
    (trash / entry).write_text("payload")
    outside = nested_workspace.parent / "evil.txt"
    (trash / f"{entry}.trashinfo").write_text(json.dumps({"original_path": str(outside)}))

    result = trash_restore.func(entry_name=entry)
    assert result.startswith("Error") and "outside" in result
    assert not outside.exists()
    assert (trash / entry).exists()


def test_trash_mutations_denied_on_read_only_workspace(nested_workspace: Path) -> None:
    from olano.security.request_context import push_request_context

    src = nested_workspace / "keep.txt"
    src.write_text("x")
    meta = {"workspace_root": str(nested_workspace), "workspace_access_mode": "read"}
    with push_request_context(metadata=meta):
        assert "read-only" in trash_put.func(path=str(src))
        assert "read-only" in trash_restore.func(entry_name="anything")
        assert "read-only" in trash_empty.func(confirm=True)
    assert src.exists()


def test_runner_stamps_workspace_access_mode() -> None:
    # The runner is what makes _access_mode() work at runtime: it stamps the
    # backend's access mode into every turn's request-context metadata.
    from types import SimpleNamespace

    from olano.config.backend_config import FilesystemBackendConfig, LocalShellBackendConfig
    from olano.core.runner import AgentRunner

    stub = SimpleNamespace(
        workspace=SimpleNamespace(root=Path("/ws")),
        config=SimpleNamespace(backend=FilesystemBackendConfig(access_mode="read")),
        load_thread_state=lambda thread_id: {},
    )
    meta = AgentRunner._request_context_metadata(stub, "t1")
    assert meta["workspace_access_mode"] == "read"

    stub.config = SimpleNamespace(backend=LocalShellBackendConfig(root_dir="workspace"))
    meta = AgentRunner._request_context_metadata(stub, "t1")
    assert meta["workspace_access_mode"] == "read_write"
