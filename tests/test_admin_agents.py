"""Tests for the olano.admin agent CRUD service layer."""

from __future__ import annotations

import pytest

from olano import admin
from olano.config.loader import load_agent_config


def test_create_get_list_agent() -> None:
    res = admin.create_agent(
        "acme",
        model="anthropic:claude-haiku-4-5",
        description="Acme support agent",
        system_prompt="You help Acme customers.",
        tools=["web_search"],
    )
    assert res["created"] is True

    cfg = load_agent_config("acme", source="home")
    assert cfg.model == "anthropic:claude-haiku-4-5"
    # AgentConfig augments `tools` with universal defaults; ours must be present.
    assert "web_search" in cfg.tools

    full = admin.get_agent("acme")
    assert full["description"] == "Acme support agent"

    names = {a["name"] for a in admin.list_agents(source="home")}
    assert "acme" in names


def test_create_duplicate_requires_overwrite() -> None:
    admin.create_agent("dup", description="first")
    with pytest.raises(FileExistsError):
        admin.create_agent("dup", description="second")
    # overwrite succeeds and replaces
    admin.create_agent("dup", description="second", overwrite=True)
    assert admin.get_agent("dup")["description"] == "second"


def test_update_agent_merges_and_validates() -> None:
    admin.create_agent("edit", description="orig")
    admin.update_agent("edit", {"description": "updated", "tools": ["http_request"]})
    cfg = load_agent_config("edit", source="home")
    assert cfg.description == "updated"
    assert "http_request" in cfg.tools


def test_update_agent_rejects_invalid() -> None:
    admin.create_agent("bad", description="orig")
    with pytest.raises(Exception):
        # human_only must be a bool — a string fails AgentConfig validation
        admin.update_agent("bad", {"human_only": "not-a-bool-xyz"})


def test_delete_agent_removes_config() -> None:
    admin.create_agent("gone", description="temp")
    res = admin.delete_agent("gone")
    assert res["deleted"] is True
    names = {a["name"] for a in admin.list_agents(source="home")}
    assert "gone" not in names


def test_delete_agent_purges_its_checkpoint_history(monkeypatch) -> None:
    """The config and the transcripts went; the checkpoint rows stayed behind
    as orphans. They go too now, by the agent's thread prefix."""
    from olano.core import checkpoints

    purged: list[str] = []

    def fake_purge(name: str, home=None) -> int:
        purged.append(name)
        return 2

    monkeypatch.setattr(checkpoints, "delete_agent_threads", fake_purge)
    admin.create_agent("gone-too", description="temp")
    res = admin.delete_agent("gone-too")
    assert purged == ["gone-too"]
    assert res["purged_threads"] == 2


def test_delete_agent_keeps_history_when_sessions_are_kept(monkeypatch) -> None:
    from olano.core import checkpoints

    monkeypatch.setattr(
        checkpoints, "delete_agent_threads", lambda *_a, **_k: pytest.fail("must not purge")
    )
    admin.create_agent("kept", description="temp")
    res = admin.delete_agent("kept", purge_sessions=False)
    assert res["purged_threads"] == 0


def test_delete_missing_agent_raises() -> None:
    with pytest.raises(FileNotFoundError):
        admin.delete_agent("never-existed")
