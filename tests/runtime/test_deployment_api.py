"""HTTP-level tests for deployment-settings enforcement (403s + structure).

Unlike most runtime API tests these deliberately do NOT monkeypatch
``_read_agent_config`` / ``_write_agent_config``: the whole point is to prove
the real persistence path enforces the caps. Agents are real ``agent.json5``
files in a scratch LIONCLAW_HOME; the deployment is managed-local with a
test-signed license.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from olano.deployment import enforcement, resolver
from olano.deployment.envelope import sign_license
from olano.orchestration.event_bus import EventBus
from olano.runtime.api import create_api
from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import WebhookDispatcher

KEY_ID = "settings-test-2026-07"
DEP_ID = "dep_api_test"

_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


def mint(settings: dict) -> str:
    payload = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "deployment_id": DEP_ID,
        "plan": "pro",
        "policy_revision": 1,
        "issued_at": 1_780_000_000,
        "expires_at": 4_000_000_000,
        "settings": settings,
    }
    return sign_license(payload, _PRIVATE, key_id=KEY_ID)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.delenv("LIONCLAW_LICENSE", raising=False)
    monkeypatch.delenv("LIONCLAW_LICENSE_PATH", raising=False)
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    # A managed posture defaults multi-user mode ON (unset flag ⇒ login gate),
    # which would 401 every anonymous request below. These tests are about the
    # entitlement layer, not auth — pin the operator's explicit single-user
    # choice, which always wins over the managed default.
    from olano.config.home import update_home_config

    update_home_config({"multi_user_enabled": False})
    resolver.invalidate()
    enforcement.invalidate_counts()
    yield tmp_path
    resolver.invalidate()
    enforcement.invalidate_counts()


def manage(home: Path, settings: dict) -> None:
    (home / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(mint(settings) + "\n")
    resolver.invalidate()
    enforcement.invalidate_counts()


def add_agent(name: str, **fields) -> Path:
    from olano.config.home import get_olano_paths

    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(json.dumps({"name": name, **fields}))
    enforcement.invalidate_counts()
    return path


@pytest.fixture
def client(home: Path, tmp_path: Path) -> TestClient:
    runtime = MagicMock()
    runtime._runners = {}
    runtime._agent_resources = {}
    runtime._blocked_agents = {}
    runtime.registry.get.return_value = None
    runtime.webhook_dispatcher = WebhookDispatcher(
        EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db")
    )
    app = create_api(runtime)
    return TestClient(app, raise_server_exceptions=False)


def assert_denied(resp, code: str) -> None:
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["message"]
    assert detail["remediation"]


class TestCreateAgent:
    def test_over_cap_403(self, home, client):
        manage(home, {"resources": {"max_agents": 1}})
        add_agent("existing")
        resp = client.post("/api/agents", json={"name": "second", "model": "anthropic:x"})
        assert_denied(resp, "deployment_limit_max_agents")

    def test_agentfather_disabled_403(self, home, client):
        manage(home, {"features": {"father_agent": False}})
        resp = client.post("/api/agents", json={"name": "agentfather", "model": "anthropic:x"})
        assert_denied(resp, "deployment_feature_father_agent_disabled")

    def test_under_cap_succeeds(self, home, client):
        manage(home, {"resources": {"max_agents": 2}})
        add_agent("existing")
        resp = client.post("/api/agents", json={"name": "second", "model": "anthropic:x"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

    def test_open_posture_unrestricted(self, home, client):
        for i in range(5):
            add_agent(f"agent{i}")
        resp = client.post("/api/agents", json={"name": "sixth", "model": "anthropic:x"})
        assert resp.status_code == 200, resp.text


class TestChannelCaps:
    def test_add_channel_per_agent_cap_403(self, home, client):
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        add_agent("a", channels=[{"type": "telegram"}])
        resp = client.post("/api/agents/a/channels", json={"type": "slack", "config": {}})
        assert_denied(resp, "deployment_limit_max_connectors_per_agent")

    def test_add_channel_total_cap_403(self, home, client):
        manage(home, {"resources": {"max_connectors_total": 2}})
        add_agent("a", channels=[{"type": "telegram"}])
        add_agent("b", channels=[{"type": "discord"}])
        resp = client.post("/api/agents/b/channels", json={"type": "slack", "config": {}})
        assert_denied(resp, "deployment_limit_max_connectors_total")

    def test_add_channel_under_cap_succeeds(self, home, client):
        manage(home, {"resources": {"max_connectors_per_agent": 2}})
        add_agent("a", channels=[{"type": "telegram"}])
        resp = client.post("/api/agents/a/channels", json={"type": "slack", "config": {}})
        assert resp.status_code == 200, resp.text

    def test_delete_channel_always_allowed(self, home, client):
        # Shrinking works even when already over a shrunken cap.
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        add_agent("a", channels=[{"type": "telegram"}, {"type": "slack"}])
        resp = client.delete("/api/agents/a/channels/slack")
        assert resp.status_code == 200, resp.text


class TestMcpWebhookCaps:
    def test_add_mcp_total_cap_403(self, home, client):
        manage(home, {"resources": {"max_mcp_servers_total": 1}})
        add_agent("a", mcp_servers=[{"name": "x", "transport": "stdio", "command": "x"}])
        add_agent("b")
        resp = client.post(
            "/api/agents/b/mcp",
            json={"name": "y", "transport": "http", "url": "https://example.com/mcp"},
        )
        assert_denied(resp, "deployment_limit_max_mcp_servers_total")

    def test_add_webhook_total_cap_403(self, home, client):
        manage(home, {"resources": {"max_webhooks_total": 0}})
        add_agent("a")
        resp = client.post(
            "/api/agents/a/webhooks",
            json={"direction": "inbound", "name": "hook"},
        )
        assert_denied(resp, "deployment_limit_max_webhooks_total")


class TestRawConfig:
    def test_raw_get_403_when_feature_off(self, home, client):
        manage(home, {"features": {"agent_config_raw": False}})
        add_agent("a")
        resp = client.get("/api/agents/a/config/raw")
        assert_denied(resp, "deployment_feature_agent_config_raw_disabled")

    def test_raw_get_allowed_by_default(self, home, client):
        manage(home, {})
        add_agent("a")
        resp = client.get("/api/agents/a/config/raw")
        assert resp.status_code == 200

    def test_raw_put_growing_channels_over_cap_403(self, home, client):
        manage(home, {"resources": {"max_connectors_per_agent": 1}})
        add_agent("a", channels=[{"type": "telegram"}])
        grown = {
            "name": "a",
            "channels": [{"type": "telegram"}, {"type": "slack"}],
        }
        resp = client.put("/api/agents/a/config/raw", json={"content": json.dumps(grown)})
        assert_denied(resp, "deployment_limit_max_connectors_per_agent")


class TestModelSelection:
    def test_model_change_403_when_locked(self, home, client):
        manage(home, {"features": {"model_selection": False}})
        add_agent("a", model="anthropic:claude-sonnet-4-6")
        resp = client.put("/api/agents/a", json={"model": "openai:gpt-5"})
        assert_denied(resp, "deployment_feature_model_selection_disabled")

    def test_non_model_edit_still_allowed(self, home, client):
        manage(home, {"features": {"model_selection": False}})
        add_agent("a", model="anthropic:claude-sonnet-4-6")
        resp = client.put("/api/agents/a", json={"description": "new description"})
        assert resp.status_code == 200, resp.text

    def test_catalog_marks_locked(self, home, client):
        manage(home, {"features": {"model_selection": False}})
        resp = client.get("/api/catalog/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["model_selection_locked"] is True
        assert all(p.get("locked") for p in body["providers"])


class TestUsersCap:
    def test_create_user_403_when_capped(self, home, client):
        manage(home, {"resources": {"max_users": 0}})
        resp = client.post(
            "/api/admin/users",
            json={"email": "x@example.com", "password": "supersecret123", "is_admin": False},
        )
        assert_denied(resp, "deployment_limit_max_users")


class TestQuarantineApi:
    def test_creations_403_in_quarantine(self, home, client):
        (home / "installation.json").write_text(
            json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID})
        )
        resolver.invalidate()
        enforcement.invalidate_counts()
        resp = client.post("/api/agents", json={"name": "newbie", "model": "anthropic:x"})
        assert_denied(resp, "deployment_quarantined")


class TestStatusCapabilities:
    def test_tab_flags_reflect_settings(self, home, client):
        manage(home, {"tabs": {"insights": False, "cortex": False}})
        caps = client.get("/api/status").json()["capabilities"]
        assert caps["tab_insights"] is False
        assert caps["tab_cortex"] is False
        assert caps["tab_overview"] is True
        assert caps["deployment_enforced"] is True
        assert caps["deployment_quarantined"] is False

    def test_terminal_capability_folds_in_deployment_flag(self, home, client):
        manage(home, {"features": {"terminal": False}})
        caps = client.get("/api/status").json()["capabilities"]
        assert caps["terminal_enabled"] is False
        assert caps["agent_config_raw_enabled"] is True

    def test_open_posture_flags(self, home, client):
        caps = client.get("/api/status").json()["capabilities"]
        assert caps["deployment_enforced"] is False
        assert caps["deployment_quarantined"] is False
        assert caps["tab_terminal"] is True

    def test_terminal_sandbox_flag_is_false_on_an_open_box(self, home, client):
        # Open posture: the Terminal tab is a host shell in the agent's workspace.
        caps = client.get("/api/status").json()["capabilities"]
        assert caps["terminal_sandbox"] is False

    def test_terminal_sandbox_flag_follows_the_sandbox_posture(self, home, client, monkeypatch):
        # The flag is a straight read of the runtime's posture predicate — the
        # same one the WebSocket handler uses to decide host shell vs docker exec
        # — so the dashboard's header can never disagree with what it gets.
        from olano.runtime import terminal as terminal_mod

        monkeypatch.setattr(terminal_mod, "terminal_uses_sandbox", lambda: True)
        caps = client.get("/api/status").json()["capabilities"]
        assert caps["terminal_sandbox"] is True


class TestTerminalSocketRefusals:
    def test_missing_sandbox_closes_with_4503_after_accepting(self, home, client, monkeypatch):
        """A stopped sandbox must reach the browser as close code 4503.

        The handler used to ``close(code=4503)`` BEFORE ``accept()``; a close
        before the handshake is an HTTP 403 rejection, the code is discarded and
        the browser's ``onclose`` fires with 1006 — indistinguishable from a
        dropped network, so the dashboard reconnected five times into the same
        missing container. Accept-then-close is what lets the tab say "sandbox
        isn't running".
        """
        from starlette.websockets import WebSocketDisconnect

        from olano.runtime import terminal as terminal_mod

        monkeypatch.setattr(terminal_mod, "terminal_enabled", lambda: True)
        monkeypatch.setattr(terminal_mod, "terminal_uses_sandbox", lambda: True)
        monkeypatch.setattr(terminal_mod, "sandbox_container_ref", lambda agent: None)

        # The handshake succeeds (that is the point); the close arrives as the
        # first frame. Depending on the Starlette version the test client either
        # raises WebSocketDisconnect for it or hands the close message back.
        code: int | None = None
        try:
            with client.websocket_connect("/api/terminal/ws?agent=merli") as ws:
                msg = ws.receive()
                assert msg["type"] == "websocket.close", msg
                code = msg.get("code")
        except WebSocketDisconnect as exc:
            code = exc.code
        assert code == 4503


class TestSystemDeploymentEndpoint:
    def test_shape_enforced(self, home, client):
        manage(
            home,
            {
                "resources": {"max_agents": 3},
                "usage": {"cost_total": {"value": 100_000_000, "window": "monthly"}},
                "features": {"terminal": False},
                "tabs": {"insights": False},
            },
        )
        add_agent("a")
        body = client.get("/api/system/deployment").json()
        assert body["posture"] == "enforced"
        assert body["source"] == "local_license"
        assert body["plan"] == "pro"
        assert body["policy_revision"] == 1
        agents_row = next(r for r in body["resources"] if r["key"] == "max_agents")
        assert agents_row["limit"] == 3
        assert agents_row["current"] == 1
        cost_row = next(u for u in body["usage"] if u["key"] == "cost_total")
        assert cost_row["limit"] == 100_000_000
        assert cost_row["window"] == "monthly"
        assert cost_row["unit"] == "micro_usd"
        assert body["features"]["terminal"] is False
        assert body["features"]["shell"] is True
        assert body["tabs"]["insights"] is False
        assert body["blocked_agents"] == {}

    def test_shape_open(self, home, client):
        body = client.get("/api/system/deployment").json()
        assert body["posture"] == "open"
        assert body["source"] == "none"

    def test_refresh_endpoint(self, home, client):
        manage(home, {})
        resp = client.post("/api/system/deployment/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["posture"] == "enforced"


class TestHostExecLanes:
    """Every lane that runs a command IN THE RUNTIME CONTAINER must be denied
    under a managed posture.

    The runtime container holds the product source (/app) and the vault, and on
    a managed deployment the dashboard admin is the *customer* — so an
    unsandboxed host command there is a source/secret exfiltration path. Agent
    code execution goes to the isolated sandbox instead (see
    test_managed_exec_isolation.py); these are the human-driven HTTP lanes.

    Regression: /api/shell/run (the chat "!" shell) gated only on
    ``terminal_enabled()``, which stays TRUE under a managed posture so the
    Terminal tab can open a PTY into the agent's sandbox. That let a customer
    run `cat /app/...` and `printenv` on the runtime host.
    """

    # Managed, with every execution-related entitlement granted — the posture
    # alone must deny these lanes, regardless of feature flags.
    PERMISSIVE = {"features": {"terminal": True, "shell": True, "sandbox_exec": True}}

    def test_shell_run_denied_under_managed_posture(self, home, client):
        manage(home, self.PERMISSIVE)
        resp = client.post("/api/shell/run", json={"command": "cat /app/pyproject.toml"})
        assert resp.status_code == 403, resp.text
        assert "managed deployment" in resp.json()["detail"]

    def test_shell_exec_denied_under_managed_posture(self, home, client):
        manage(home, self.PERMISSIVE)
        resp = client.post("/api/shell/exec", json={"command": "pip install requests"})
        assert resp.status_code == 403, resp.text
        assert "managed deployment" in resp.json()["detail"]

    def test_shell_stream_denied_under_managed_posture(self, home, client):
        manage(home, self.PERMISSIVE)
        resp = client.get("/api/shell/stream", params={"command": "pip install requests"})
        # The SSE lane opens 200 and reports the denial as an event.
        assert resp.status_code == 200, resp.text
        assert "managed deployment" in resp.text

    def test_shell_run_still_works_on_open_posture(self, home, client):
        """Unmanaged (the operator's own box): the "!" shell stays a host shell."""
        resp = client.post("/api/shell/run", json={"command": "echo hello-from-host"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["exit_code"] == 0
        assert "hello-from-host" in "\n".join(body["head"] + body["tail"])


class TestShellStreamCrossSite:
    """``GET /api/shell/stream`` runs a command, so a link on another site must
    never be able to trigger it in a signed-in admin's browser. The dashboard's
    own EventSource is same-origin and carries no ``Origin`` header."""

    def test_cross_site_fetch_metadata_is_refused(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "pip install requests"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 200, resp.text
        assert "cross_site_request_refused" in resp.text

    def test_foreign_origin_is_refused(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "pip install requests"},
            headers={"Origin": "https://evil.example"},
        )
        assert "cross_site_request_refused" in resp.text

    @pytest.mark.parametrize("mode", ["navigate", "no-cors"])
    def test_a_navigation_or_embed_from_a_sibling_site_is_refused(self, home, client, mode):
        """cust-a.olano.run linking to cust-b.olano.run is ``same-site``."""
        resp = client.get(
            "/api/shell/stream",
            params={"command": "pip install requests"},
            headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": mode},
        )
        assert "cross_site_request_refused" in resp.text

    def test_the_dashboards_own_event_source_is_fine(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "rm -rf /"},
            headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
        )
        assert "cross_site_request_refused" not in resp.text
        assert "not in allowlist" in resp.text

    def test_same_origin_reaches_the_command_validator(self, home, client):
        # A command outside the allowlist proves the request got past the
        # cross-site gate and was judged on its own merits.
        resp = client.get(
            "/api/shell/stream",
            params={"command": "rm -rf /"},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert "cross_site_request_refused" not in resp.text
        assert "not in allowlist" in resp.text

    def test_loopback_origin_is_always_fine(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "rm -rf /"},
            headers={"Origin": "http://localhost:5173"},
        )
        assert "cross_site_request_refused" not in resp.text

    def test_foreign_referer_is_refused_without_fetch_metadata(self, home, client):
        """Safari before 16.4 sends no Sec-Fetch-* and no Origin on a GET."""
        resp = client.get(
            "/api/shell/stream",
            params={"command": "pip install requests"},
            headers={"Referer": "https://evil.example/lure", "User-Agent": "Mozilla/5.0"},
        )
        assert "cross_site_request_refused" in resp.text

    def test_own_referer_is_fine_without_fetch_metadata(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "rm -rf /"},
            headers={"Referer": "http://testserver/tools", "User-Agent": "Mozilla/5.0"},
        )
        assert "cross_site_request_refused" not in resp.text
        assert "not in allowlist" in resp.text

    def test_browser_with_no_provenance_is_refused(self, home, client):
        """A lure page that suppresses its Referer on an old browser."""
        resp = client.get(
            "/api/shell/stream",
            params={"command": "pip install requests"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605"},
        )
        assert "cross_site_request_refused" in resp.text

    def test_cli_client_with_no_provenance_is_fine(self, home, client):
        resp = client.get(
            "/api/shell/stream",
            params={"command": "rm -rf /"},
            headers={"User-Agent": "curl/8.4.0"},
        )
        assert "cross_site_request_refused" not in resp.text
        assert "not in allowlist" in resp.text

    @pytest.mark.parametrize(
        "command",
        ["pip install x\nrm -rf /", "pip install x\rrm -rf /", "pip install x & rm -rf /"],
    )
    def test_newline_and_ampersand_are_compound(self, home, client, command):
        resp = client.get("/api/shell/stream", params={"command": command})
        assert "Compound shell commands" in resp.text
