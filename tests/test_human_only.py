"""Tests for the human_only access gate on talk_to_agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olano.config.agent_config import AgentConfig
from olano.tools import talk_to


def _fake_runtime(configs: dict[str, AgentConfig]) -> SimpleNamespace:
    runners = {name: SimpleNamespace(config=cfg) for name, cfg in configs.items()}
    return SimpleNamespace(_runners=runners)


def test_human_only_field_defaults_false() -> None:
    assert AgentConfig(name="plain").human_only is False


def test_peer_blocked_from_human_only_target(monkeypatch: pytest.MonkeyPatch) -> None:
    father = AgentConfig(name="agentfather", human_only=True)
    worker = AgentConfig(name="worker")
    monkeypatch.setattr(
        talk_to, "_RUNTIME", _fake_runtime({"agentfather": father, "worker": worker})
    )
    err = talk_to._peer_policy_error("worker", "agentfather")
    assert err is not None
    assert "human-only" in err
    assert talk_to.can_message("worker", "agentfather") is False


def test_non_human_only_target_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    a = AgentConfig(name="a")
    b = AgentConfig(name="b")
    monkeypatch.setattr(talk_to, "_RUNTIME", _fake_runtime({"a": a, "b": b}))
    assert talk_to._peer_policy_error("a", "b") is None
