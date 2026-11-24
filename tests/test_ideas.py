"""Tests for ideas/brainstorming/prototyping wiring + tools (Phase 6)."""

from __future__ import annotations

from pathlib import Path

from olano.config.workspace import Workspace
from olano.security.request_context import push_request_context
from olano.tools import ideas_tools


def test_workspace_init_creates_idea_files(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path)
    ws.init()
    for fname in ("IDEAS.md", "BRAINSTORMING.md", "PROTOTYPING.md", "LEARNINGS.md"):
        assert (tmp_path / fname).exists(), f"{fname} not created"


def test_idea_add_and_list(tmp_path: Path) -> None:
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        ideas_tools.idea_add.invoke({"idea": "Build an AI agent marketplace", "tags": "ai,web3"})
        ideas_tools.idea_add.invoke({"idea": "Self-cleaning memory"})
        listed = ideas_tools.ideas_list.invoke({})
    ideas_md = (tmp_path / "IDEAS.md").read_text(encoding="utf-8")
    assert "## Inbox" in ideas_md
    assert "AI agent marketplace" in ideas_md
    assert "tags: ai,web3" in ideas_md
    assert "Self-cleaning memory" in listed


def test_brainstorm_note(tmp_path: Path) -> None:
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        ideas_tools.brainstorm_note.invoke(
            {"note": "What if pulses spawned workers?", "topic": "Pulse"}
        )
    content = (tmp_path / "BRAINSTORMING.md").read_text(encoding="utf-8")
    assert "## Pulse" in content
    assert "spawned workers" in content


def test_ideas_toolkit_registered() -> None:
    from olano.tools.registry import resolve_tools

    tools = resolve_tools(["idea_add", "ideas_list", "brainstorm_note"])
    names = {t.name for t in tools}
    assert {"idea_add", "ideas_list", "brainstorm_note"} <= names


def test_l1_injects_ideas_when_populated(tmp_path: Path) -> None:
    from olano.config.agent_config import AgentConfig, MemoryConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.core.prompt_assembler import PromptAssembler

    ws = Workspace(root=tmp_path)
    ws.init()
    # Populate IDEAS.md with real content (a bullet).
    (tmp_path / "IDEAS.md").write_text(
        "# Ideas\n\n## Inbox\n- [2026-05-30] Ship the cognition rearchitecture\n",
        encoding="utf-8",
    )
    config = AgentConfig(
        name="t",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )
    l1 = PromptAssembler(config, ws).build_l1()
    assert "## Ideas" in l1
    assert "cognition rearchitecture" in l1


def test_l1_skips_placeholder_ideas(tmp_path: Path) -> None:
    from olano.config.agent_config import AgentConfig, MemoryConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.core.prompt_assembler import PromptAssembler

    ws = Workspace(root=tmp_path)
    ws.init()  # default placeholder IDEAS.md (no bullets)
    config = AgentConfig(
        name="t",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )
    l1 = PromptAssembler(config, ws).build_l1()
    assert "## Ideas" not in l1
