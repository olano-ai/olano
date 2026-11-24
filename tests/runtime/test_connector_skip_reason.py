"""A connector skipped at boot for a missing credential says so, and how to fix it.

The runtime skips a connector whose credential resolves to nothing (the agent
itself still boots). Its diagnostics then had no adapter to ask and could only
say the adapter was not running and point at the terminal logs, and the bell
told the operator to run a shell command. On a managed box there
is neither a terminal log nor a shell to read or run. Both now name the missing
key and the dashboard route: open the connector, enter the value, restart.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from olano.runtime.api import create_api
from olano.runtime.server import _missing_credentials_reason, _report_missing_channel_credentials

AGENT = "marcus"
CHANNEL = "platform:whatsapp_twilio"
MISSING = [("auth_token", "TWILIO_AUTH_TOKEN")]


def test_the_reason_names_the_key_and_the_dashboard_route_never_the_cli():
    reason = _missing_credentials_reason(MISSING)
    assert "TWILIO_AUTH_TOKEN" in reason
    assert "Open this connector" in reason
    assert "restart the agent" in reason
    assert "olano vault" not in reason
    assert "`" not in reason


def test_the_bell_says_the_same(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("olano.runtime.notifications.notify", lambda **kw: captured.update(kw))

    _report_missing_channel_credentials(AGENT, CHANNEL, MISSING)

    assert captured["body"] == _missing_credentials_reason(MISSING)
    assert captured["dedupe_key"] == f"channel_credential_missing:{AGENT}:{CHANNEL}"


def _client(lionclaw_home, skipped: dict) -> TestClient:
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "channels": [{"type": CHANNEL, "extra": {"account_sid": "AC" + "0" * 32}}],
            }
        ),
        encoding="utf-8",
    )
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    runtime._agent_resources = {AGENT: {"channels": [], "platforms": []}}
    runtime._skipped_channels = skipped
    return TestClient(create_api(runtime))


def test_the_connector_health_names_the_missing_credential(lionclaw_home):
    reason = _missing_credentials_reason(MISSING)
    client = _client(lionclaw_home, {AGENT: {CHANNEL: reason}})

    body = client.get(f"/api/agents/{AGENT}/connectors/health").json()

    (row,) = body["connectors"]
    assert row["type"] == CHANNEL
    assert row["runtime_attached"] is False
    assert row["snapshot"]["checks"] == [{"id": "credentials", "ok": False, "message": reason}]


def test_a_connector_not_running_for_another_reason_keeps_the_generic_check(lionclaw_home):
    client = _client(lionclaw_home, {})

    body = client.get(f"/api/agents/{AGENT}/connectors/health").json()

    (check,) = body["connectors"][0]["snapshot"]["checks"]
    assert check["id"] == "runtime" and check["ok"] is False
