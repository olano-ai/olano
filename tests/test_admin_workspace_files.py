"""Cross-agent workspace file tools — AgentFather's window into other agents.

Its shell is isolated from other agents' directories by design; the privilege
to inspect and seed them lives in this runtime-side admin layer instead, so
every path is containment-checked and writes hand ownership to the target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.admin import (
    list_agent_workspace,
    read_agent_workspace_file,
    write_agent_workspace_file,
)


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    return home


@pytest.fixture
def target(home) -> Path:
    """A deployed agent with a booted workspace."""
    agent_dir = home / "agents" / "subs-mate"
    ws = agent_dir / "workspace"
    (ws / "memories").mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": "subs-mate"}), encoding="utf-8")
    (ws / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    return ws


def test_list_shows_another_agents_files(home, target):
    result = list_agent_workspace("subs-mate")

    assert result["exists"] is True
    names = {e["name"]: e["kind"] for e in result["entries"]}
    assert names["MEMORY.md"] == "file"
    assert names["memories"] == "dir"


def test_list_of_a_never_booted_agent_reports_not_exists(home):
    agent_dir = home / "agents" / "fresh"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": "fresh"}), encoding="utf-8")

    result = list_agent_workspace("fresh")

    assert result["exists"] is False
    assert result["entries"] == []


def test_read_returns_content(home, target):
    result = read_agent_workspace_file("subs-mate", "MEMORY.md")
    assert result["content"] == "# memory\n"


def test_write_creates_parents_and_hands_ownership(home, monkeypatch):
    """Seeding an agent that exists only as config: the workspace chain is
    created (mkdir -p) and every new level chowned to the sandbox user."""
    import olano.core.builder as builder

    agent_dir = home / "agents" / "fresh"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": "fresh"}), encoding="utf-8")
    chowned: list[str] = []
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: chowned.append(str(p)) or True)

    result = write_agent_workspace_file("fresh", "notes/PLAN.md", "do things\n")

    ws = agent_dir / "workspace"
    assert (ws / "notes" / "PLAN.md").read_text(encoding="utf-8") == "do things\n"
    assert result["created"] is True
    assert str(ws) in chowned
    assert str(ws / "notes") in chowned
    assert str((ws / "notes" / "PLAN.md").resolve()) in chowned


def test_write_overwrite_semantics(home, target):
    with pytest.raises(FileExistsError):
        write_agent_workspace_file("subs-mate", "MEMORY.md", "new\n", overwrite=False)

    write_agent_workspace_file("subs-mate", "MEMORY.md", "new\n")
    assert (target / "MEMORY.md").read_text(encoding="utf-8") == "new\n"


def test_traversal_and_symlink_escapes_are_refused(home, target, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    (target / "escape").symlink_to(outside)

    with pytest.raises(PermissionError, match="outside"):
        read_agent_workspace_file("subs-mate", "../agent.json5")
    with pytest.raises(PermissionError, match="outside"):
        read_agent_workspace_file("subs-mate", "escape/secret.txt")
    with pytest.raises(PermissionError, match="outside"):
        write_agent_workspace_file("subs-mate", "../../../etc/passwd", "x")
    with pytest.raises(PermissionError, match="outside"):
        list_agent_workspace("subs-mate", "escape")


def test_unknown_agent_is_a_clear_error(home):
    with pytest.raises(ValueError, match="does not exist"):
        list_agent_workspace("ghost")


def test_read_cap_protects_the_model_turn(home, target, monkeypatch):
    import olano.admin.workspace_files as wf

    monkeypatch.setattr(wf, "_READ_MAX_BYTES", 10)
    (target / "big.txt").write_text("x" * 100, encoding="utf-8")

    with pytest.raises(ValueError, match="read cap"):
        read_agent_workspace_file("subs-mate", "big.txt")


def test_write_tool_is_hitl_gated_and_reads_are_not():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"]
    assert "olano_write_agent_file" in gated
    assert "olano_read_agent_file" not in gated
    assert "olano_list_agent_workspace" not in gated
