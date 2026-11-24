"""Boot-time honoring of the per-agent paused flag + hidden-agent skip."""

from __future__ import annotations

import json
from pathlib import Path

from olano.config.agent_state import set_agent_paused
from olano.runtime.server import RuntimeServer


def test_paused_agent_skipped_on_default_boot(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    set_agent_paused("merli", True)

    server = RuntimeServer(agent_names=["merli", "scout"])
    # agent_names was passed, but it represents the default discovered set here;
    # simulate the "boot everything" path by clearing the explicit flag.
    server._explicit_agents = False

    assert server._should_boot_on_start("merli") is False
    assert server._should_boot_on_start("scout") is True


def test_explicitly_named_agents_ignore_paused(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    set_agent_paused("merli", True)

    server = RuntimeServer(agent_names=["merli"])
    assert server._explicit_agents is True
    # Explicit --agent merli boots even though it's paused.
    assert server._should_boot_on_start("merli") is True


def _write_agent(project_root: Path, name: str, *, hidden: bool) -> None:
    d = project_root / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    cfg: dict = {"name": name, "model": "anthropic:claude-haiku-4-5-20251001"}
    if hidden:
        cfg["hidden"] = True
    (d / "agent.json5").write_text(json.dumps(cfg), encoding="utf-8")


def test_hidden_agent_not_booted_by_default(monkeypatch, tmp_path: Path) -> None:
    """Hidden/internal (test) agents must not boot in the long-running runtime."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LIONCLAW_BOOT_HIDDEN_AGENTS", raising=False)
    monkeypatch.chdir(tmp_path)  # project-local ./agents/ is the config source
    _write_agent(tmp_path, "worker", hidden=False)
    _write_agent(tmp_path, "test-agent-openai", hidden=True)

    server = RuntimeServer(agent_names=["worker", "test-agent-openai"])
    server._explicit_agents = False  # simulate the default "boot everything" path

    assert server._agent_is_hidden("test-agent-openai") is True
    assert server._agent_is_hidden("worker") is False
    assert server._should_boot_on_start("worker") is True
    assert server._should_boot_on_start("test-agent-openai") is False


def test_hidden_agent_boots_when_opted_in(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_BOOT_HIDDEN_AGENTS", "1")
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path, "test-agent-openai", hidden=True)

    server = RuntimeServer(agent_names=["test-agent-openai"])
    server._explicit_agents = False
    assert server._should_boot_on_start("test-agent-openai") is True


def test_hidden_agent_boots_when_named_explicitly(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LIONCLAW_BOOT_HIDDEN_AGENTS", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_agent(tmp_path, "test-agent-openai", hidden=True)

    # `olano chat --agent test-agent-openai` → explicit boot wins.
    server = RuntimeServer(agent_names=["test-agent-openai"])
    assert server._explicit_agents is True
    assert server._should_boot_on_start("test-agent-openai") is True
