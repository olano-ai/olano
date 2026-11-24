"""Tests for per-agent runtime state (paused flag) persistence."""

from __future__ import annotations

from pathlib import Path

from olano.config.agent_state import (
    _load_runtime_state,
    is_agent_paused,
    set_agent_paused,
)


def test_default_not_paused(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    assert is_agent_paused("merli") is False


def test_set_and_get_round_trip(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    set_agent_paused("merli", True)
    assert is_agent_paused("merli") is True
    set_agent_paused("merli", False)
    assert is_agent_paused("merli") is False


def test_missing_file_is_resilient(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    assert _load_runtime_state("ghost") == {}


def test_corrupt_file_falls_back_to_empty(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    path = home / "agents" / "merli" / "runtime_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert is_agent_paused("merli") is False


def test_set_preserves_other_keys(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    path = home / "agents" / "merli" / "runtime_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"other": "keep"}', encoding="utf-8")
    set_agent_paused("merli", True)
    state = _load_runtime_state("merli")
    assert state["other"] == "keep"
    assert state["paused"] is True
