"""Saving a connector through the API must not write the token to disk.

End-to-end complement to tests/test_channel_secrets.py: the absorb step hangs
off ``_write_agent_config``, so it has to hold for every endpoint that touches
an agent config, not just the connector form.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

TOKEN = "8182517777:AAHrealbuttestonlyt5jQ"


class FakeVault:
    def __init__(self) -> None:
        self.stored: dict[tuple[str, str | None], str] = {}

    def set(self, name: str, value: str, agent: str | None = None, **kw: Any) -> None:
        self.stored[(name, agent)] = value


@pytest.fixture
def vault(monkeypatch) -> FakeVault:
    v = FakeVault()
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: v)
    return v


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    from unittest.mock import MagicMock

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    runtime._channels = []
    return TestClient(create_api(runtime))


def _seed_agent(tmp_path, name: str = "marisol") -> None:
    d = tmp_path / "home" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json5").write_text(
        f'{{"name": "{name}", "model": "deepseek:deepseek-v4-flash", "channels": []}}',
        encoding="utf-8",
    )


def _config_text(tmp_path, name: str = "marisol") -> str:
    return (tmp_path / "home" / "agents" / name / "agent.json5").read_text(encoding="utf-8")


def test_adding_a_telegram_channel_vaults_the_token(client, tmp_path, vault):
    _seed_agent(tmp_path)

    resp = client.post(
        "/api/agents/marisol/channels",
        json={"type": "telegram", "config": {"token": TOKEN, "allowed_users": ["5550001234"]}},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["vaulted"] == [
        {"channel": "telegram", "field": "token", "key": "TELEGRAM_BOT_TOKEN"}
    ]
    assert body["vault_locked"] is False
    # The credential is in the vault, scoped to this agent...
    assert vault.stored == {("TELEGRAM_BOT_TOKEN", "marisol"): TOKEN}
    # ...and nowhere on disk.
    text = _config_text(tmp_path)
    assert TOKEN not in text
    # Not even a reference: the connector resolves TELEGRAM_BOT_TOKEN by
    # convention, so the config carries no credential field at all.
    assert "token" not in text
    assert "5550001234" in text  # non-secret connector settings still saved


def test_updating_a_channel_vaults_a_rotated_token(client, tmp_path, vault):
    _seed_agent(tmp_path)
    client.post(
        "/api/agents/marisol/channels",
        json={"type": "telegram", "config": {"token": "old-token"}},
    )

    resp = client.put(
        "/api/agents/marisol/channels/telegram",
        json={"type": "telegram", "config": {"token": "rotated-token"}},
    )

    assert resp.status_code == 200, resp.text
    assert vault.stored[("TELEGRAM_BOT_TOKEN", "marisol")] == "rotated-token"
    assert "rotated-token" not in _config_text(tmp_path)


def test_raw_config_put_is_covered_too(client, tmp_path, vault):
    """The raw editor is an admin surface, not a plaintext-token loophole."""
    _seed_agent(tmp_path)

    resp = client.put(
        "/api/agents/marisol/config/raw",
        json={
            "content": (
                "{\n"
                "  // marisol talks on telegram\n"
                '  "name": "marisol",\n'
                '  "model": "deepseek:deepseek-v4-flash",\n'
                f'  "channels": [{{"type": "telegram", "token": "{TOKEN}"}}],\n'
                "}\n"
            )
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["vaulted"] == [
        {"channel": "telegram", "field": "token", "key": "TELEGRAM_BOT_TOKEN"}
    ]
    assert vault.stored == {("TELEGRAM_BOT_TOKEN", "marisol"): TOKEN}

    text = _config_text(tmp_path)
    assert TOKEN not in text
    assert '"token"' not in text
    # The whole reason this path writes verbatim: comments must survive.
    assert "// marisol talks on telegram" in text


def test_a_reference_typed_by_hand_is_preserved(client, tmp_path, vault):
    _seed_agent(tmp_path)

    resp = client.post(
        "/api/agents/marisol/channels",
        json={"type": "telegram", "config": {"token": "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"}},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["vaulted"] == []
    assert vault.stored == {}
    assert "${vault:MARISOL_TELEGRAM_BOT_TOKEN}" in _config_text(tmp_path)
