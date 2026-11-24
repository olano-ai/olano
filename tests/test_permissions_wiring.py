"""Tests for _build_permissions() backend/path wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from olano.config.agent_config import AgentConfig, SharedPathConfig
from olano.config.backend_config import (
    DaytonaBackendConfig,
    FilesystemBackendConfig,
    LocalShellBackendConfig,
    ModalBackendConfig,
    OlanoDockerBackendConfig,
    StateBackendConfig,
)
from olano.core.builder import _build_permissions


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "MEMORY.md").write_text("# mem\n", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    return tmp_path


def _config(workspace: Path, backend, **kwargs) -> AgentConfig:
    return AgentConfig(
        name="test",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(workspace),
        backend=backend,
        memory={"markdown": False, "rag": False, "self_learning": False},
        **kwargs,
    )


class TestFilesystemPermissions:
    def test_filesystem_includes_workspace(self, workspace: Path) -> None:
        config = _config(workspace, FilesystemBackendConfig(root_dir=str(workspace)))
        perms = _build_permissions(config, workspace)
        assert perms is not None
        assert any(str(workspace.resolve()) in p for perm in perms for p in perm.paths)

    def test_local_shell_returns_none(self, workspace: Path) -> None:
        # LocalShellBackend implements SandboxBackendProtocol; deepagents rejects permissions= for it.
        config = _config(workspace, LocalShellBackendConfig(root_dir=str(workspace)))
        perms = _build_permissions(config, workspace)
        assert perms is None

    def test_shared_paths_appended(self, workspace: Path, tmp_path: Path) -> None:
        shared = tmp_path / "shared-data"
        shared.mkdir()
        config = _config(
            workspace,
            FilesystemBackendConfig(root_dir=str(workspace)),
            shared_paths=[SharedPathConfig(source=str(shared), target="/shared/data")],
        )
        perms = _build_permissions(config, workspace)
        assert perms is not None
        assert any(str(shared.resolve()) in p for perm in perms for p in perm.paths)

    def test_root_dir_slash_returns_none(self, workspace: Path) -> None:
        config = _config(workspace, FilesystemBackendConfig(root_dir="/"))
        perms = _build_permissions(config, workspace)
        assert perms is None

    def test_skill_dirs_readable_but_not_writable(self, tmp_path: Path) -> None:
        from deepagents.middleware.filesystem import _check_fs_permission

        # The skills dir must live OUTSIDE the workspace to exercise the
        # read-only grant (inside it the rw rule would allow writes anyway).
        workspace = tmp_path / "ws"
        workspace.mkdir()
        skills = tmp_path / "managed-skills"
        skills.mkdir()
        config = _config(workspace, FilesystemBackendConfig(root_dir=str(workspace)))
        perms = _build_permissions(config, workspace, [str(skills)])
        assert perms is not None
        skill_md = str(skills.resolve() / "pdf" / "SKILL.md")
        assert _check_fs_permission(perms, "read", skill_md) == "allow"
        assert _check_fs_permission(perms, "write", skill_md) == "deny"
        # `ls` on the skill root itself must also pass (bare-dir pattern).
        assert _check_fs_permission(perms, "read", str(skills.resolve())) == "allow"

    def test_workspace_root_itself_is_listable(self, workspace: Path) -> None:
        from deepagents.middleware.filesystem import _check_fs_permission

        config = _config(workspace, FilesystemBackendConfig(root_dir=str(workspace)))
        perms = _build_permissions(config, workspace)
        assert perms is not None
        assert _check_fs_permission(perms, "read", str(workspace.resolve())) == "allow"

    def test_paths_outside_workspace_still_denied(self, tmp_path: Path) -> None:
        from deepagents.middleware.filesystem import _check_fs_permission

        workspace = tmp_path / "ws"
        workspace.mkdir()
        skills = tmp_path / "managed-skills"
        skills.mkdir()
        config = _config(workspace, FilesystemBackendConfig(root_dir=str(workspace)))
        perms = _build_permissions(config, workspace, [str(skills)])
        assert perms is not None
        assert _check_fs_permission(perms, "read", "/etc/passwd") == "deny"
        assert _check_fs_permission(perms, "write", "/etc/passwd") == "deny"


class TestSandboxPermissions:
    def test_lionclaw_docker_returns_none(self, workspace: Path) -> None:
        config = _config(workspace, OlanoDockerBackendConfig())
        perms = _build_permissions(config, workspace)
        assert perms is None

    def test_daytona_returns_none(self, workspace: Path) -> None:
        config = _config(workspace, DaytonaBackendConfig())
        perms = _build_permissions(config, workspace)
        assert perms is None

    def test_modal_returns_none(self, workspace: Path) -> None:
        config = _config(workspace, ModalBackendConfig())
        perms = _build_permissions(config, workspace)
        assert perms is None

    def test_state_backend_returns_none(self, workspace: Path) -> None:
        config = _config(workspace, StateBackendConfig())
        perms = _build_permissions(config, workspace)
        assert perms is None


class TestRemoteSandboxGuard:
    def test_remote_backend_with_shared_paths_raises(self, workspace: Path) -> None:
        from olano.core.builder import _prepare_agent_build

        config = _config(
            workspace,
            DaytonaBackendConfig(),
            shared_paths=[SharedPathConfig(source=str(workspace), target="/shared/ws")],
        )
        with (
            patch(
                "olano.core.builder._build_model",
                return_value=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
            ),
            pytest.raises(ValueError, match="shared_paths"),
        ):
            _prepare_agent_build(config)
