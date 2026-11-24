from __future__ import annotations

from pathlib import Path

from olano.config.agent_config import AgentConfig
from olano.config.workspace import (
    _FIRST_RUN_MARKER,
    BOOTSTRAP_DEFAULTS,
    Workspace,
    is_scaffold_default,
)
from olano.core.prompt_assembler import PromptAssembler


def test_is_scaffold_default_recognizes_freshly_initialized_files(tmp_path: Path) -> None:
    """Every file Workspace.init() seeds must be detected as a pristine scaffold."""
    workspace = Workspace(root=tmp_path)
    workspace.init()
    for fname in BOOTSTRAP_DEFAULTS:
        assert is_scaffold_default(fname, workspace.read_file(fname)), fname


def test_is_scaffold_default_ignores_edges_and_first_run_marker() -> None:
    agents = BOOTSTRAP_DEFAULTS["AGENTS.md"]
    # Trailing-newline / whitespace differences must not defeat detection.
    assert is_scaffold_default("AGENTS.md", agents.strip())
    assert is_scaffold_default("AGENTS.md", "\n" + agents + "\n")
    # The optional first-run marker is stripped before comparing.
    # Use the constant, never a copy of its text: the marker is written into
    # files on deployed hosts, so the literal must not drift from the source.
    assert is_scaffold_default("IDENTITY.md", _FIRST_RUN_MARKER + BOOTSTRAP_DEFAULTS["IDENTITY.md"])


def test_is_scaffold_default_false_for_edited_and_unknown() -> None:
    # An authored AGENTS.md is not a scaffold.
    assert not is_scaffold_default("AGENTS.md", "# Agent Instructions\n\nDo the real work.\n")
    # A file with no known default is never a scaffold.
    assert not is_scaffold_default("RANDOM.md", "anything")


def test_read_file_returns_directory_preview_for_directory(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path)
    workspace.init()

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "alpha.txt").write_text("hello", encoding="utf-8")
    (shared_dir / "nested").mkdir()

    content = workspace.read_file("shared")

    assert "[Directory 'shared']" in content
    assert "- alpha.txt" in content
    assert "- nested/" in content


def test_read_file_returns_placeholder_for_binary_symlink(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path)
    workspace.init()

    external_binary = tmp_path / "external-binary"
    external_binary.write_bytes(b"\xcf\xfa\xed\xfe\x00binary")

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "tool").symlink_to(external_binary)

    content = workspace.read_file("shared/tool")

    assert content == "[Binary file 'shared/tool' omitted from text context.]"


def test_build_l2_handles_shared_directory_and_binary_file(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path)
    workspace.init()

    external_binary = tmp_path / "external-binary"
    external_binary.write_bytes(b"\xcf\xfa\xed\xfe\x00binary")

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "tool").symlink_to(external_binary)
    (shared_dir / "notes.txt").write_text("hello", encoding="utf-8")

    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="merli",
        model="anthropic:test",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path)),
    )
    assembler = PromptAssembler(config, workspace)

    l2 = assembler.build_l2(file_paths=["shared", "shared/tool"])

    assert "## shared" in l2
    assert "[Directory 'shared']" in l2
    assert "notes.txt" in l2
    assert "[Binary file 'shared/tool' omitted from text context.]" in l2


def test_init_scaffolds_reports_and_uploads_with_readmes(tmp_path: Path) -> None:
    """The output conventions the prompt names exist on disk from the first boot."""
    workspace = Workspace(root=tmp_path)
    workspace.init()
    for folder in ("reports", "uploads", "scratch", "downloads", "trash"):
        readme = tmp_path / folder / "README.md"
        assert readme.is_file(), folder
    assert "One canonical file per deliverable" in (tmp_path / "reports" / "README.md").read_text()
    assert "Inbound only" in (tmp_path / "uploads" / "README.md").read_text()
    # Re-running init never clobbers an operator's own README.
    (tmp_path / "reports" / "README.md").write_text("mine", encoding="utf-8")
    workspace.init()
    assert (tmp_path / "reports" / "README.md").read_text() == "mine"
