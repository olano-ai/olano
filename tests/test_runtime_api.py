"""Tests for the runtime API (FastAPI endpoints)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_runtime(tmp_workspace):
    """A minimal mock RuntimeServer instance."""
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🤖"
    config.description = "Test agent"
    config.channels = []

    mock_runner = MagicMock()
    mock_runner.config = config
    mock_runner.ainvoke = AsyncMock(return_value="Hello from agent!")

    mock_scheduler = MagicMock()
    mock_scheduler.list_jobs.return_value = [
        {"id": "job1", "name": "test_job", "next_run": "2026-04-01"}
    ]

    from olano.admin.memory import MemoryMonitor

    gw = MagicMock()
    gw._runners = {"test_agent": mock_runner}
    gw._channels = []
    gw.scheduler = mock_scheduler
    gw.get_runner.return_value = mock_runner
    # Real monitor so the POST /api/diagnostics/memory/monitor handler returns a
    # JSON-serialisable snapshot rather than a MagicMock.
    gw._memory_monitor = MemoryMonitor()

    return gw


@pytest.fixture
def api_client(mock_runtime, tmp_workspace, monkeypatch):
    """Create a FastAPI TestClient bound to a mock runtime."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    app = create_api(mock_runtime)
    return TestClient(app)


def test_status_endpoint(api_client):
    resp = api_client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] == "running"
    assert "agents" in data


def test_memory_diagnostics_endpoint(api_client):
    resp = api_client.get("/api/diagnostics/memory?gc=false&tracemalloc=false")
    assert resp.status_code == 200
    data = resp.json()
    assert "source" in data
    assert "process" in data


def test_memory_monitor_toggle_endpoint(api_client):
    on = api_client.post("/api/diagnostics/memory/monitor", json={"action": "on"})
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    off = api_client.post("/api/diagnostics/memory/monitor", json={"action": "off"})
    assert off.status_code == 200
    assert off.json()["enabled"] is False


def test_hitl_policy_endpoint(api_client):
    resp = api_client.get("/api/hitl/policy")
    assert resp.status_code == 200
    data = resp.json()
    # Mirrors olano.security.hitl_policy so the dashboard can compute
    # effective approval state without duplicating the map.
    assert "wallet_send" in data["categories"]["financial"]
    assert "task" in data["categories"]["delegation"]
    assert "secret_write" in data["profiles"]["developer"]
    assert "write_external" in data["profiles"]["safe"]
    assert "delegation" in data["profiles"]["production"]
    assert data["write_toggle_category"] == "write_external"


def test_hitl_pending_and_resolve_endpoints(api_client):
    from olano.security import hitl
    from olano.security.hitl import ApprovalRequest

    req = ApprovalRequest(
        id="abc12345",
        agent_name="aurelius",
        action="wallet_send",
        description="send 0.5 ETH",
        payload={"origin": {"root_source": "cognition", "call_path": ["x"]}, "args": {}},
        risk="high",
    )
    hitl._pending["abc12345"] = req
    try:
        resp = api_client.get("/api/hitl/pending")
        assert resp.status_code == 200
        items = resp.json()["pending"]
        assert any(i["id"] == "abc12345" and i["root_source"] == "cognition" for i in items)

        ok = api_client.post("/api/hitl/resolve", json={"id": "abc12345", "approved": True})
        assert ok.status_code == 200
        assert req.approved is True

        missing = api_client.post("/api/hitl/resolve", json={"id": "nope", "approved": True})
        assert missing.status_code == 404
    finally:
        hitl._pending.pop("abc12345", None)


def test_agents_endpoint(api_client):
    # Patch list_agents so the test is isolated from any on-disk agents/ folder
    # in the cwd — we only want to assert the booted (mocked) agent shows up.
    with patch("olano.config.loader.list_agents", return_value=[]):
        resp = api_client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    booted = [a for a in agents if a.get("running")]
    assert len(booted) == 1
    assert booted[0]["name"] == "test_agent"
    assert booted[0]["emoji"] == "🤖"


def test_system_config_rate_limits_round_trip(api_client):
    # Default home has no rate_limits block → unlimited (empty maps).
    resp = api_client.get("/api/system/config")
    assert resp.status_code == 200
    assert resp.json()["rate_limits"] == {"messages": {}, "per_platform": {}}

    # PUT persists the windows and drops empty / null entries.
    resp = api_client.put(
        "/api/system/config",
        json={
            "rate_limits": {
                "messages": {"per_minute": 30, "per_hour": None},
                "per_platform": {"telegram": {"per_minute": 20}, "slack": {}},
            }
        },
    )
    assert resp.status_code == 200, resp.text
    rl = resp.json()["rate_limits"]
    assert rl["messages"] == {"per_minute": 30}
    assert rl["per_platform"] == {"telegram": {"per_minute": 20}}


def test_system_config_rate_limits_rejects_negative(api_client):
    resp = api_client.put(
        "/api/system/config",
        json={"rate_limits": {"messages": {"per_minute": -5}}},
    )
    assert resp.status_code == 400


def test_credits_endpoint_defaults_to_usd_at_the_launch_rate(api_client, monkeypatch):
    """The dashboard reads this on every view that renders money, so it must
    answer with a usable rate even on a box that has never reached the cloud."""
    from olano.catalog import credits_registry

    # The rate is module-cached across the process, so a catalog test that ran
    # earlier in the session would otherwise decide this one's `source`.
    credits_registry.invalidate_cache()
    monkeypatch.setattr(credits_registry, "_fetch_from_cloud", lambda: None)
    monkeypatch.setattr(credits_registry, "_last_attempt", credits_registry.NEVER_ATTEMPTED)

    resp = api_client.get("/api/credits")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display"] == "usd"
    assert body["credits_per_usd"] == credits_registry.BUNDLED_RATE["creditsPerUsd"]
    assert body["unit_name_plural"] == "credits"
    assert body["source"] == "bundled"
    credits_registry.invalidate_cache()


def test_credits_endpoint_reports_which_billing_books_exist(api_client, monkeypatch):
    """The dashboard renders one section per book, so it needs the plan shape.

    An unmanaged install is pure BYOK: no credit account exists, and every
    credits number the Insights page could draw would be a constant zero.
    """
    body = api_client.get("/api/credits").json()
    assert body["credits_enabled"] is False  # test box: no deployment token
    assert body["byok_enabled"] is True

    # Managed entry plan: credits, but the plan withholds BYOK entirely.
    from olano.deployment import enforcement as dep_enforcement
    from olano.deployment import resolver as dep_resolver
    from olano.deployment import sources as dep_sources
    from olano.runtime import api as api_mod

    monkeypatch.setattr(
        dep_resolver, "deployment_status", lambda: SimpleNamespace(posture="managed")
    )
    # A managed posture defaults multi-user mode ON (unset flag ⇒ login gate),
    # which would 401 the anonymous calls below; this test is about credits
    # flags, so pin the single-user posture explicitly.
    monkeypatch.setattr("olano.runtime.auth.multi_user_enabled", lambda: False)
    monkeypatch.setattr(dep_sources, "deployment_token", lambda: "tok")
    monkeypatch.setattr(dep_enforcement, "feature_enabled", lambda name: name != "byok")
    monkeypatch.setattr(api_mod.dep_enforcement, "feature_enabled", lambda name: name != "byok")

    body = api_client.get("/api/credits").json()
    assert body["credits_enabled"] is True
    assert body["byok_enabled"] is False


def test_credits_endpoint_enables_credits_for_a_pasted_account_key(api_client, monkeypatch):
    """A self-hosted box with an Olano API key spends from the account wallet,
    so the credits book exists; BYOK stays on (no plan withholds it there)."""
    from olano.deployment import resolver as dep_resolver

    monkeypatch.setattr(
        dep_resolver,
        "deployment_status",
        lambda now=None: SimpleNamespace(
            posture="open", settings=SimpleNamespace(feature_enabled=lambda name: True)
        ),
    )
    assert api_client.get("/api/credits").json()["credits_enabled"] is False
    monkeypatch.setenv("OLANO_CREDITS_KEY", "sk-olano-" + "a" * 40)
    body = api_client.get("/api/credits").json()
    assert body["credits_enabled"] is True
    assert body["byok_enabled"] is True


def test_system_config_cost_display_round_trip(api_client):
    from olano.catalog import credits_registry

    resp = api_client.get("/api/system/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits"]["display"] == "usd"
    # The rate is catalog data surfaced read-only alongside the writable mode.
    assert body["credits"]["credits_per_usd"] == credits_registry.BUNDLED_RATE["creditsPerUsd"]
    assert body["cost_display_modes"] == ["usd", "credits"]

    resp = api_client.put("/api/system/config", json={"credits": {"display": "credits"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["credits"]["display"] == "credits"
    assert api_client.get("/api/credits").json()["display"] == "credits"


def test_cost_display_locked_on_managed_boxes(api_client, monkeypatch):
    """Managed posture: the unit is signed cloud policy — GET advertises the
    lock and PUT refuses the write with an explanation, so the dashboard can
    say who owns the setting instead of silently ignoring the user."""
    from olano.config import system_settings

    monkeypatch.setattr(system_settings, "cost_display_locked", lambda: True)
    monkeypatch.setattr(system_settings, "cost_display_mode", lambda override=None: "credits")

    body = api_client.get("/api/credits").json()
    assert body["display"] == "credits"
    assert body["display_locked"] is True

    cfg = api_client.get("/api/system/config").json()
    assert cfg["credits"]["display_locked"] is True

    resp = api_client.put("/api/system/config", json={"credits": {"display": "usd"}})
    assert resp.status_code == 403
    assert "cloud.olano.ai" in resp.json()["detail"]


def test_system_config_cost_display_rejects_unknown_unit(api_client):
    resp = api_client.put("/api/system/config", json={"credits": {"display": "pesos"}})
    assert resp.status_code == 400
    # The rejected write must not have moved the stored mode.
    assert api_client.get("/api/system/config").json()["credits"]["display"] in ("usd", "credits")


def test_system_config_speech_round_trip(api_client):
    # Default: both speech services disabled, providers list present.
    resp = api_client.get("/api/system/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["speech"]["stt"]["provider"] == "disabled"
    assert body["speech"]["tts"]["provider"] == "disabled"
    assert "custom" in body["speech_providers"]
    # Model/voice dropdown options for the dashboard come from speech_catalog.
    cat = body["speech_catalog"]
    assert "qwen3-tts-flash" in cat["tts"]["qwen"]["models"]
    assert "Cherry" in cat["tts"]["qwen"]["voices"]
    assert "qwen" not in cat["stt"]  # TTS-only
    assert "custom" not in cat["tts"]  # UI falls back to free text

    # PUT persists provider + refinements per section.
    resp = api_client.put(
        "/api/system/config",
        json={
            "speech": {
                "stt": {"provider": "groq", "model": "whisper-large-v3"},
                "tts": {"provider": "openai", "voice": "shimmer"},
            }
        },
    )
    assert resp.status_code == 200, resp.text
    sp = resp.json()["speech"]
    assert sp["stt"] == {
        "provider": "groq",
        "model": "whisper-large-v3",
        "url": "",
        "api_key_name": "",
        "voice": "",
        "audio_format": "",
    }
    assert sp["tts"]["provider"] == "openai"
    assert sp["tts"]["voice"] == "shimmer"
    # Blank = per-provider auto (Opus on OpenAI — the only container the chat
    # platforms can render as a native voice bubble).
    assert sp["tts"]["audio_format"] == ""


def test_system_config_speech_rejects_bad_provider_and_missing_url(api_client):
    resp = api_client.put(
        "/api/system/config",
        json={"speech": {"stt": {"provider": "bananas"}}},
    )
    assert resp.status_code == 400

    resp = api_client.put(
        "/api/system/config",
        json={"speech": {"stt": {"provider": "custom", "url": ""}}},
    )
    assert resp.status_code == 400


def test_stop_persists_paused(api_client, mock_runtime):
    """Stopping an agent writes paused=True so it stays down after restart."""
    from olano.config.agent_state import is_agent_paused

    mock_runtime._stop_agent = AsyncMock()
    mock_runtime._agent_resources = {}
    resp = api_client.post("/api/agents/test_agent/stop")
    assert resp.status_code == 200
    assert is_agent_paused("test_agent") is True


def test_start_clears_paused(api_client):
    """Explicitly starting an agent clears the persisted paused flag."""
    from olano.config.agent_state import is_agent_paused, set_agent_paused

    set_agent_paused("test_agent", True)
    resp = api_client.post("/api/agents/test_agent/start")
    assert resp.status_code == 200
    assert is_agent_paused("test_agent") is False


def test_reload_vault_locked_returns_503(api_client, monkeypatch):
    """/reload-vault with no unlockable vault → 503 so the chat shows 'locked'."""
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_agent_config_path", lambda name, **kw: None)
    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: {})
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: None)

    class _Res:
        vault = None

    monkeypatch.setattr("olano.vault.runtime.unlock_vault_for_session", lambda **kw: _Res())

    resp = api_client.post("/api/agents/test_agent/reload-vault")
    assert resp.status_code == 503
    assert "locked" in resp.json()["detail"].lower()


def test_reload_vault_rehydrates_and_reboots(api_client, mock_runtime, monkeypatch):
    """/reload-vault re-hydrates env, reboots the running agent, reports secrets."""
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_agent_config_path", lambda name, **kw: None)
    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: {})

    class _Vault:
        def list_names(self):
            return [{"name": "OPENAI_API_KEY"}, {"name": "ANTHROPIC_API_KEY"}]

    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: _Vault())
    monkeypatch.setattr(
        "olano.vault.runtime.hydrate_env_from_vault",
        lambda vault, overwrite=False: ["OPENAI_API_KEY"],
    )

    mock_runtime._agent_resources = {}
    mock_runtime._stop_agent = AsyncMock()
    # _boot_agent must leave the agent registered in _runners for success.
    mock_runtime._boot_agent = AsyncMock()

    resp = api_client.post("/api/agents/test_agent/reload-vault")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["secrets"] == 2
    assert data["rebooted"] is True
    mock_runtime._stop_agent.assert_awaited_once()
    mock_runtime._boot_agent.assert_awaited_once()


# ── MCP status / health / reload endpoints ────────────────────────────────────


def _oauth_server_cfg() -> dict:
    return {
        "mcp_servers": [
            {
                "url": "https://mcp.hubspot.com",
                "transport": "streamable_http",
                "enabled": True,
                "auth": "oauth",
                "oauth_client_id": "x",
                "oauth_client_secret": "y",
            }
        ]
    }


# ── PATCH /api/agents/{name}/mcp/tools ────────────────────────────────────────
#
# The per-tool switches write through this narrow route rather than the
# full-entry PUT, whose `_mcp_entry_from_body` rebuilds the stored server from
# the request model alone — so a tool toggle through it silently drops any
# stored field the model does not name, and round-trips `oauth_client_secret`
# through the browser on every click.


@pytest.fixture
def mcp_agent_on_disk(api_client, tmp_workspace):
    """A real `agent.json5` with one OAuth MCP server, for the write path."""
    import json

    home = tmp_workspace / "home"
    path = home / "agents" / "mcp_agent" / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": "mcp_agent",
                "model": "anthropic:claude-haiku-4-5-20251001",
                "mcp_servers": [
                    {
                        "url": "https://mcp.klaviyo.com/mcp",
                        "transport": "streamable_http",
                        "enabled": True,
                        "auth": "oauth",
                        "oauth_client_id": "cid",
                        "oauth_client_secret": "shhh",
                        "disabled_tools": ["get_flow"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _server_entry(path):
    from olano.config.json5_fast import loads as json5_loads

    return json5_loads(path.read_text(encoding="utf-8"))["mcp_servers"][0]


def test_mcp_tools_patch_replaces_the_deny_list(api_client, mcp_agent_on_disk):
    url = "https://mcp.klaviyo.com/mcp"
    resp = api_client.patch(
        f"/api/agents/mcp_agent/mcp/tools?url={url}",
        json={"disabled_tools": ["get_lists", "get_campaign", "get_lists", " "]},
    )
    assert resp.status_code == 200
    # Deduped, whitespace-only entries dropped, sorted — so the file is stable
    # regardless of the order the dashboard flipped the switches in.
    assert resp.json()["disabled_tools"] == ["get_campaign", "get_lists"]
    assert _server_entry(mcp_agent_on_disk)["disabled_tools"] == ["get_campaign", "get_lists"]


def test_mcp_tools_patch_empty_drops_the_key(api_client, mcp_agent_on_disk):
    """ "Enable all" removes `disabled_tools` rather than storing `[]` — the
    convention `_mcp_entry_from_body` and the add/edit modal already follow."""
    url = "https://mcp.klaviyo.com/mcp"
    resp = api_client.patch(
        f"/api/agents/mcp_agent/mcp/tools?url={url}", json={"disabled_tools": []}
    )
    assert resp.status_code == 200
    assert "disabled_tools" not in _server_entry(mcp_agent_on_disk)


def test_mcp_tools_patch_leaves_the_rest_of_the_entry_alone(api_client, mcp_agent_on_disk):
    """The whole point of the narrow route: a tool toggle must not be able to
    disturb the server's transport or OAuth credentials."""
    url = "https://mcp.klaviyo.com/mcp"
    before = _server_entry(mcp_agent_on_disk)
    resp = api_client.patch(
        f"/api/agents/mcp_agent/mcp/tools?url={url}", json={"disabled_tools": ["get_flow"]}
    )
    assert resp.status_code == 200

    after = _server_entry(mcp_agent_on_disk)
    assert {k: v for k, v in after.items() if k != "disabled_tools"} == {
        k: v for k, v in before.items() if k != "disabled_tools"
    }
    assert after["oauth_client_secret"] == "shhh"


def test_mcp_tools_patch_404s_on_an_unknown_server(api_client, mcp_agent_on_disk):
    resp = api_client.patch(
        "/api/agents/mcp_agent/mcp/tools?url=https://nope.example/mcp",
        json={"disabled_tools": []},
    )
    assert resp.status_code == 404


def test_mcp_status_cheap_is_connection_free(api_client, monkeypatch):
    """Default /mcp/status must NOT open a live connection — `authorized` comes
    straight from stored tokens (the fix for the 'connects again every visit'
    churn and the false auth_required flicker)."""
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())
    monkeypatch.setattr(
        mcp_setup,
        "oauth_token_state",
        lambda agent, url: {
            "state": "live",
            "authorized": True,
            "expires_at": None,
            "has_refresh_token": True,
        },
    )

    async def _boom(*a, **k):
        raise AssertionError("probe_server must not run on a cheap status load")

    monkeypatch.setattr(mcp_setup, "probe_server", _boom)

    resp = api_client.get("/api/agents/test_agent/mcp/status")
    assert resp.status_code == 200
    row = resp.json()["servers"][0]
    assert row["authorized"] is True
    assert row["status"] == "authorized"


def test_mcp_status_probe_true_connects(api_client, monkeypatch):
    """`?probe=true` does the live connect via probe_server."""
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())
    monkeypatch.setattr(
        mcp_setup,
        "oauth_token_state",
        lambda agent, url: {
            "state": "live",
            "authorized": True,
            "expires_at": None,
            "has_refresh_token": True,
        },
    )

    called = {}

    async def _probe(agent, entry, **k):
        called["yes"] = True
        return {
            "ok": True,
            "status": "connected",
            "message": "ok",
            "needs_oauth": True,
            "authorized": True,
            "tools": [],
        }

    monkeypatch.setattr(mcp_setup, "probe_server", _probe)

    resp = api_client.get("/api/agents/test_agent/mcp/status?probe=true")
    assert resp.status_code == 200
    assert called.get("yes") is True
    assert resp.json()["servers"][0]["status"] == "connected"


def test_mcp_status_cheap_uses_live_tools_when_running(api_client, monkeypatch):
    """A *running* agent's cheap /mcp/status must surface the live bound toolset
    (cached from the last build/reload, no network) so the setup card matches the
    chat MCP indicator's tool count and renders per-tool toggles — instead of the
    empty "0 tools" list that used to flicker away after every refresh."""
    import olano.core.builder as builder
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    cfg = _oauth_server_cfg()
    cfg["mcp_servers"][0]["disabled_tools"] = ["beta"]
    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: cfg)
    monkeypatch.setattr(
        mcp_setup,
        "oauth_token_state",
        lambda agent, url: {
            "state": "live",
            "authorized": True,
            "expires_at": None,
            "has_refresh_token": True,
        },
    )

    async def _boom(*a, **k):
        raise AssertionError("probe_server must not run on a cheap status load")

    monkeypatch.setattr(mcp_setup, "probe_server", _boom)
    # The live summary the running agent caches: alpha kept, beta disabled-out.
    monkeypatch.setattr(
        builder,
        "get_mcp_summary",
        lambda name: [
            {
                "url": "https://mcp.hubspot.com",
                "ok": True,
                "status": "connected",
                "tools": ["alpha"],
            }
        ],
    )

    row = api_client.get("/api/agents/test_agent/mcp/status").json()["servers"][0]
    assert row["status"] == "connected"
    assert row["ok"] is True
    by_name = {t["name"]: t["enabled"] for t in row["tools"]}
    assert by_name == {"alpha": True, "beta": False}


def test_mcp_reload_invokes_runtime(api_client, mock_runtime, monkeypatch):
    """POST /mcp/reload delegates to RuntimeServer.reload_agent_mcp."""
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())
    mock_runtime.reload_agent_mcp = AsyncMock(
        return_value={"ok": True, "running": True, "servers": []}
    )

    resp = api_client.post("/api/agents/test_agent/mcp/reload")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_runtime.reload_agent_mcp.assert_awaited_once()


def test_mcp_health_reports_overall(api_client, monkeypatch):
    """/mcp/health returns a compact per-server snapshot + overall color."""
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())
    monkeypatch.setattr(
        mcp_setup,
        "oauth_token_state",
        lambda agent, url: {
            "state": "live",
            "authorized": True,
            "expires_at": None,
            "has_refresh_token": True,
        },
    )

    resp = api_client.get("/api/agents/test_agent/mcp/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["servers"][0]["url"] == "https://mcp.hubspot.com"
    # Authorized but not yet live-probed → ok unknown → degraded (yellow).
    assert data["overall"] == "yellow"


# ── MCP OAuth redirect-URI resolution (relay-aware) ───────────────────────────


def test_mcp_oauth_redirect_uri_origin_without_relay(api_client, monkeypatch):
    """With no relay configured, the redirect URI the dashboard tells users to
    register is the browser origin's same-origin callback (single-box / public
    host behaviour)."""
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)

    resp = api_client.get("/api/mcp/oauth/redirect-uri?redirect_base=http://localhost:3201")
    assert resp.status_code == 200
    assert resp.json() == {
        "redirect_uri": "http://localhost:3201/api/mcp/oauth/callback",
        "via": "origin",
    }


def test_mcp_oauth_redirect_uri_prefers_relay(api_client, monkeypatch):
    """A relay-backed deployment surfaces the relay's shared https callback (the
    URI the user must register with HubSpot) — NOT the localhost dashboard origin
    the browser sends, which the provider rejects / can't reach."""
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.olano.ai")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")

    resp = api_client.get("/api/mcp/oauth/redirect-uri?redirect_base=http://localhost:3201")
    assert resp.status_code == 200
    assert resp.json() == {
        "redirect_uri": "https://auth.olano.ai/mcp/callback",
        "via": "relay",
    }


def test_mcp_oauth_start_uses_relay_when_configured(api_client, monkeypatch):
    """POST /mcp/oauth/start must route through the relay-aware driver when a relay
    is configured, so the provider redirect targets the relay callback instead of
    the unreachable localhost origin the browser sent as redirect_base."""
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.olano.ai")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")
    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())

    captured: dict = {}

    def _fake_authorize(agent, entry):
        captured["agent"] = agent
        captured["entry"] = entry
        return mcp_setup.OAuthFlowSession(
            session_id="sess-1", agent=agent, url=entry["url"], transport="streamable_http"
        )

    def _boom(*a, **k):
        raise AssertionError("start_oauth_flow (origin path) must not run when a relay is set")

    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _fake_authorize)
    monkeypatch.setattr(mcp_setup, "start_oauth_flow", _boom)

    resp = api_client.post(
        "/api/agents/test_agent/mcp/oauth/start",
        json={
            "url": "https://mcp.hubspot.com",
            "transport": "streamable_http",
            "auth": "oauth",
            "oauth_client_id": "cid",
            "oauth_client_secret": "sec",
            "redirect_base": "http://localhost:3201",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["redirect_uri"] == "https://auth.olano.ai/mcp/callback"
    assert captured["agent"] == "test_agent"
    assert captured["entry"]["url"] == "https://mcp.hubspot.com"


def test_mcp_oauth_start_uses_origin_without_relay(api_client, monkeypatch):
    """Without a relay, the start endpoint keeps the unchanged same-origin path:
    the redirect URI is derived from the browser origin the dashboard sent."""
    import olano.core.mcp_setup as mcp_setup
    from olano.runtime import api as api_module

    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    monkeypatch.setattr(api_module, "_read_agent_config", lambda name: _oauth_server_cfg())

    captured: dict = {}

    def _fake_start(agent, entry, *, redirect_uri):
        captured["redirect_uri"] = redirect_uri
        return mcp_setup.OAuthFlowSession(
            session_id="sess-2", agent=agent, url=entry["url"], transport="streamable_http"
        )

    def _boom(*a, **k):
        raise AssertionError("relay driver must not run when no relay is configured")

    monkeypatch.setattr(mcp_setup, "start_oauth_flow", _fake_start)
    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _boom)

    resp = api_client.post(
        "/api/agents/test_agent/mcp/oauth/start",
        json={
            "url": "https://mcp.stripe.com",
            "transport": "streamable_http",
            "auth": "oauth",
            "redirect_base": "https://dash.example.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["redirect_uri"] == "https://dash.example.com/api/mcp/oauth/callback"
    assert captured["redirect_uri"] == "https://dash.example.com/api/mcp/oauth/callback"


def test_create_agent_broadcasts_roster_update(api_client):
    """Creating an agent must broadcast an `agents_update` SSE frame so the
    sidebar agent picker (CurrentAgentProvider) refreshes live instead of only
    after a full page reload. Regression guard for the dropdown-not-refreshing
    bug — the frontend reloads its roster on this frame."""
    import asyncio

    from olano.runtime import api as api_module

    # Subscribe a queue exactly like /api/stream does, then assert the frame lands.
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    api_module._subscribers.append(q)
    try:
        resp = api_client.post("/api/agents", json={"name": "fresh_agent"})
        assert resp.status_code == 200, resp.text
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
    finally:
        api_module._subscribers.remove(q)

    roster = [f for f in frames if f.get("type") == "agents_update"]
    assert roster, f"expected an agents_update frame, got {[f.get('type') for f in frames]}"
    assert roster[-1]["data"]["name"] == "fresh_agent"


def test_create_agent_defaults_to_the_fast_tier_alias(api_client):
    """POST /api/agents without a model writes `default:turbo` — unconditionally.
    The managed/BYOK fork lives inside the alias resolver now
    (`system_settings.builtin_default_model_targets`), not at create time, so
    the file carries the alias and re-points when the deployment retargets it."""
    import json

    assert api_client.post("/api/agents", json={"name": "model_default_probe"}).status_code == 200
    raw = json.loads(api_client.get("/api/agents/model_default_probe/config/raw").json()["content"])
    assert raw["model"] == "default:turbo"


def test_create_agent_an_explicit_model_always_wins(api_client, monkeypatch):
    """An explicit choice always wins over the alias default — managed or not."""
    import json

    from olano.config import system_settings

    monkeypatch.setattr(system_settings, "managed_olano_default", lambda: True)

    assert (
        api_client.post(
            "/api/agents",
            json={"name": "model_explicit_probe", "model": "openai:gpt-5.2"},
        ).status_code
        == 200
    )
    raw = json.loads(
        api_client.get("/api/agents/model_explicit_probe/config/raw").json()["content"]
    )
    assert raw["model"] == "openai:gpt-5.2"


def test_create_agent_announces_new_teammate(mock_runtime, api_client):
    """Creating an agent must auto-boot it and broadcast an `agent_joined` frame
    so humans get a welcome toast and the new teammate comes online (and shows
    up in every peer's roster). Guards the 'announce a new team member' feature."""
    import asyncio

    from olano.runtime import api as api_module

    # Auto-boot registers the new agent in _runners (mirrors _boot_agent success).
    async def _boot(name: str) -> None:
        mock_runtime._runners[name] = MagicMock()

    mock_runtime._boot_agent = AsyncMock(side_effect=_boot)

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    api_module._subscribers.append(q)
    try:
        resp = api_client.post(
            "/api/agents", json={"name": "newbie", "description": "A fresh hire"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["online"] is True
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
    finally:
        api_module._subscribers.remove(q)

    mock_runtime._boot_agent.assert_awaited_once_with("newbie")
    joined = [f for f in frames if f.get("type") == "agent_joined"]
    assert joined, f"expected an agent_joined frame, got {[f.get('type') for f in frames]}"
    assert joined[-1]["data"]["name"] == "newbie"
    assert joined[-1]["data"]["online"] is True


def test_generate_agent_prompt(api_client):
    """The 'Generate with AI' button drafts a system prompt from the agent's
    name, description, and tools via an LLM. Guards the meta-prompt wiring."""
    from langchain_core.messages import AIMessage

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        return_value=AIMessage(content="You are Fred, a research agent.")
    )

    with patch("olano.models.build_model", return_value=fake_model) as build:
        resp = api_client.post(
            "/api/agents/generate-prompt",
            json={
                "name": "fred",
                "description": "web research specialist",
                "tools": ["web_search", "web_fetch"],
                "model": "anthropic:claude-sonnet-4-6",
                "kind": "agent",
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["system_prompt"] == "You are Fred, a research agent."
    assert data["model"] == "anthropic:claude-sonnet-4-6"
    build.assert_called_once_with("anthropic:claude-sonnet-4-6")
    # The meta-prompt must carry the agent's name + tools into the LLM call.
    msgs = fake_model.ainvoke.await_args.args[0]
    human = msgs[-1].content
    assert "fred" in human
    assert "web_search" in human and "web_fetch" in human


def test_generate_agent_prompt_strips_code_fence(api_client):
    """Models sometimes wrap output in a ``` fence despite instructions — the
    endpoint strips it so the textarea gets clean prompt text."""
    from langchain_core.messages import AIMessage

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        return_value=AIMessage(content="```\nYou are a helpful agent.\n```")
    )
    with patch("olano.models.build_model", return_value=fake_model):
        resp = api_client.post(
            "/api/agents/generate-prompt",
            json={"name": "helper", "description": "does things"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["system_prompt"] == "You are a helpful agent."


def test_generate_agent_prompt_requires_context(api_client):
    """Refuse to call the model with nothing to go on (no name, no description)."""
    resp = api_client.post("/api/agents/generate-prompt", json={"name": "", "description": ""})
    assert resp.status_code == 400


def test_generate_agent_prompt_surfaces_model_error(api_client):
    """A model/credential failure is surfaced as a 502 so the dashboard can toast
    the real reason instead of a generic error."""
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(side_effect=RuntimeError("ANTHROPIC_API_KEY not set"))
    with patch("olano.models.build_model", return_value=fake_model):
        resp = api_client.post(
            "/api/agents/generate-prompt",
            json={"name": "fred", "description": "research"},
        )
    assert resp.status_code == 502
    assert "ANTHROPIC_API_KEY not set" in resp.json()["detail"]


def test_get_agent_resolves_effective_toolset(api_client):
    """`GET /api/agents/{name}` must expose the tools the agent actually runs with.

    agent.json5's `tools` only lists *extra* tools, so it is empty for a default
    agent — which made the dashboard `/tools` command report "No tools
    configured" even though the agent has the full universal + ops + built-in
    toolset. The endpoint now returns a display-only `tools_resolved` list
    (without mutating the raw `tools`) so the listing matches `whoami`.
    """
    resp = api_client.post("/api/agents", json={"name": "tool_probe", "tools": []})
    assert resp.status_code == 200, resp.text

    resp = api_client.get("/api/agents/tool_probe")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Raw configured list is untouched (empty) — round-tripped saves mirror
    # agent.json5 exactly, so the Config tab must keep seeing only the extras.
    assert data["tools"] == []

    resolved = data["tools_resolved"]
    # Universal defaults and deepagents built-ins all surface even though none
    # were explicitly configured. The ops tools are top-level defaults too
    # (they no longer ride an OpsManager subagent).
    for expected in (
        "whoami",
        "web_search",
        "talk_to_agent",
        "read_file",
        "task",
        "scheduler_create",
        "heartbeat_add",
        "approvals_list",
    ):
        assert expected in resolved, f"{expected} missing from {resolved}"
    # The resolved list is strictly richer than the raw configured list.
    assert len(resolved) > len(data["tools"])


def test_update_agent_persists_visibility_and_disabled_tools(api_client):
    """PUT /api/agents/{name} accepts `agent_visibility` + `disabled_tools`, and
    disabling `talk_to_agent` actually revokes it (the 'removed the tool but it
    kept talking' bug — a plain `tools` edit can't drop a universal default)."""
    resp = api_client.post("/api/agents", json={"name": "vis_probe", "tools": []})
    assert resp.status_code == 200, resp.text

    # Baseline: talk_to_agent is auto-injected into the effective toolset.
    assert "talk_to_agent" in api_client.get("/api/agents/vis_probe").json()["tools_resolved"]

    resp = api_client.put(
        "/api/agents/vis_probe",
        json={
            "agent_visibility": {"mode": "list", "agents": ["marcus", "emma"]},
            "disabled_tools": ["talk_to_agent"],
        },
    )
    assert resp.status_code == 200, resp.text

    data = api_client.get("/api/agents/vis_probe").json()
    assert data["agent_visibility"] == {"mode": "list", "agents": ["marcus", "emma"]}
    assert data["disabled_tools"] == ["talk_to_agent"]
    # The removal sticks: talk_to_agent is no longer in the effective toolset.
    assert "talk_to_agent" not in data["tools_resolved"]


def test_catalog_tools_exposes_agent_defaults(api_client):
    """The Tools tab needs to know which tools are auto-injected defaults so it
    can route their removal into `disabled_tools`."""
    data = api_client.get("/api/catalog/tools").json()
    defaults = data.get("agent_defaults")
    assert isinstance(defaults, list) and "talk_to_agent" in defaults
    # Document readers and the ops tools are defaults too, so unticking one on
    # the Tools tab records it in `disabled_tools` and ticking it re-adds it.
    assert "document_to_text" in defaults
    assert "scheduler_create" in defaults
    assert "approvals_approve" in defaults


def test_agent_raw_config_roundtrips_verbatim(api_client):
    """The Config tab edits agent.json5 directly. `GET …/config/raw` returns the
    verbatim file text (no injected/computed fields like `tools_resolved`), and
    `PUT …/config/raw` writes it back byte-for-byte after validation — so what the
    user sees and saves is exactly the file on disk."""
    import json

    resp = api_client.post("/api/agents", json={"name": "raw_probe", "tools": ["safe_shell"]})
    assert resp.status_code == 200, resp.text

    got = api_client.get("/api/agents/raw_probe/config/raw")
    assert got.status_code == 200, got.text
    content = got.json()["content"]
    # The raw text is the file itself — no enriched/computed fields leak in.
    assert "tools_resolved" not in content
    assert "subagents_resolved" not in content
    assert json.loads(content)["tools"] == ["safe_shell"]

    # Edit with a comment + reordering; PUT must persist it byte-for-byte.
    edited = (
        "{\n"
        "  // CEO config — hand-edited\n"
        '  "name": "raw_probe",\n'
        '  "model": "anthropic:claude-haiku-4-5-20251001",\n'
        '  "tools": ["safe_shell", "web_search"]\n'
        "}\n"
    )
    save = api_client.put("/api/agents/raw_probe/config/raw", json={"content": edited})
    assert save.status_code == 200, save.text
    assert api_client.get("/api/agents/raw_probe/config/raw").json()["content"] == edited

    # Invalid JSON5 is rejected (422) without touching disk.
    bad = api_client.put("/api/agents/raw_probe/config/raw", json={"content": "{ not valid"})
    assert bad.status_code == 422
    assert api_client.get("/api/agents/raw_probe/config/raw").json()["content"] == edited

    # A name that disagrees with the URL is rejected.
    renamed = api_client.put(
        "/api/agents/raw_probe/config/raw",
        json={
            "content": '{"name": "someone_else", "model": "anthropic:claude-haiku-4-5-20251001"}'
        },
    )
    assert renamed.status_code == 422


def test_agent_list_carries_the_display_name(api_client):
    """The dashboard card renders `display_name || name`, so the list endpoint
    has to send it — otherwise a renamed agent keeps showing its handle on the
    card while the detail page shows the new name, and the two surfaces
    disagree with no error anywhere."""
    assert (
        api_client.post(
            "/api/agents", json={"name": "listed_probe", "display_name": "Listed Probe"}
        ).status_code
        == 200
    )
    rows = api_client.get("/api/agents").json()
    row = next(r for r in rows if r["name"] == "listed_probe")
    assert row["display_name"] == "Listed Probe"


def test_display_name_is_patchable_and_the_handle_is_not(api_client):
    """`display_name` is the half that renames; `name` must stay refused."""
    assert api_client.post("/api/agents", json={"name": "patch_probe"}).status_code == 200

    ok = api_client.put("/api/agents/patch_probe", json={"display_name": "Patch Probe"})
    assert ok.status_code == 200, ok.text
    assert api_client.get("/api/agents/patch_probe").json()["display_name"] == "Patch Probe"

    # Clearing it is a real operation, not a no-op: back to the handle.
    api_client.put("/api/agents/patch_probe", json={"display_name": ""})
    assert api_client.get("/api/agents/patch_probe").json()["display_name"] == ""

    # The handle stays immutable.
    api_client.put("/api/agents/patch_probe", json={"name": "renamed_probe"})
    assert api_client.get("/api/agents/patch_probe").json()["name"] == "patch_probe"


def test_agent_config_schema_endpoint(api_client):
    """The schema-driven Advanced editor is fed by GET …/config/schema: the full
    AgentConfig JSON Schema plus which fields are read-only vs. owned by a
    dedicated tab. This is what lets the dashboard render a control for EVERY
    agent.json5 field automatically."""
    resp = api_client.post("/api/agents", json={"name": "schema_probe"})
    assert resp.status_code == 200, resp.text

    got = api_client.get("/api/agents/schema_probe/config/schema")
    assert got.status_code == 200, got.text
    body = got.json()

    props = body["schema"]["properties"]
    # Every AgentConfig field the raw editor could set is described here, incl.
    # the long-tail ones that had no bespoke control (the whole point).
    for field in ("human_only", "hidden", "iteration_budget", "budget", "wallet"):
        assert field in props, f"{field} missing from schema"

    # Identity/derived fields are flagged read-only so the panel hides them.
    assert "name" in body["read_only"]
    assert "workspace" in body["read_only"]
    # `display_name` is the mutable half of the identity pair and must NOT
    # join them — a display name the editor refuses to write is no better
    # than not having one. It points at the Overview identity card instead.
    assert "display_name" in props
    assert "display_name" not in body["read_only"]
    assert body["managed"]["display_name"] == "overview"
    # Fields with a richer dedicated editor point the panel at that tab.
    assert body["managed"]["mcp_servers"] == "mcp"
    assert body["managed"]["tools"] == "tools"

    # Unknown agent → 404.
    assert api_client.get("/api/agents/nope_probe/config/schema").status_code == 404


def test_update_agent_persists_arbitrary_schema_field(api_client):
    """PUT /api/agents/{name} accepts ANY AgentConfig field, not just the handful
    the typed request used to whitelist — so the schema-driven editor can save
    e.g. human_only, which previously required hand-editing the raw file."""
    import json

    assert api_client.post("/api/agents", json={"name": "put_probe"}).status_code == 200

    # human_only was never in AgentUpdateRequest's field list before.
    save = api_client.put("/api/agents/put_probe", json={"human_only": True})
    assert save.status_code == 200, save.text
    content = api_client.get("/api/agents/put_probe/config/raw").json()["content"]
    assert json.loads(content)["human_only"] is True


def test_update_agent_persists_default_verbosity(api_client):
    """The Access & Safety verbosity select round-trips through PUT, and an
    alias typed into the raw editor is normalized to a canonical mode."""
    import json

    assert api_client.post("/api/agents", json={"name": "verb_probe"}).status_code == 200

    save = api_client.put("/api/agents/verb_probe", json={"default_verbosity": "quiet"})
    assert save.status_code == 200, save.text
    content = api_client.get("/api/agents/verb_probe/config/raw").json()["content"]
    assert json.loads(content)["default_verbosity"] == "quiet"

    # Aliases are accepted and canonicalized rather than rejected.
    save = api_client.put("/api/agents/verb_probe", json={"default_verbosity": "full"})
    assert save.status_code == 200, save.text
    assert api_client.get("/api/agents/verb_probe").json()["default_verbosity"] == "verbose"


def test_update_agent_ignores_immutable_fields(api_client):
    """Identity/derived fields (name, workspace, …) are never patchable via the
    typed editor — renaming has its own flow."""
    import json

    assert api_client.post("/api/agents", json={"name": "immut_probe"}).status_code == 200

    save = api_client.put(
        "/api/agents/immut_probe",
        json={"name": "hijacked", "hidden": True},
    )
    assert save.status_code == 200, save.text
    data = json.loads(api_client.get("/api/agents/immut_probe/config/raw").json()["content"])
    # The mutable field applied; the immutable `name` did not.
    assert data["hidden"] is True
    assert data["name"] == "immut_probe"


def test_update_agent_persists_acp_toggle(api_client):
    """The Advanced tab's "Acp Enabled" switch must reach agent.json5.

    `acp_enabled` / `acp_port` were listed in `_LEGACY_AGENT_KEYS` even though
    AgentConfig still declares them and `_boot_agent_locked` starts the ACP
    server from them, so the write path silently dropped both: the PUT returned
    200, the toast said "Saved acp_enabled", and the reload showed the toggle
    back off. Guard both directions.
    """
    import json

    assert api_client.post("/api/agents", json={"name": "acp_probe"}).status_code == 200

    save = api_client.put("/api/agents/acp_probe", json={"acp_enabled": True, "acp_port": 8790})
    assert save.status_code == 200, save.text

    content = json.loads(api_client.get("/api/agents/acp_probe/config/raw").json()["content"])
    assert content["acp_enabled"] is True
    assert content["acp_port"] == 8790
    # And the read path serves it back (it used to strip — and rewrite the file).
    assert api_client.get("/api/agents/acp_probe").json()["acp_enabled"] is True

    off = api_client.put("/api/agents/acp_probe", json={"acp_enabled": False})
    assert off.status_code == 200, off.text
    assert (
        json.loads(api_client.get("/api/agents/acp_probe/config/raw").json()["content"])[
            "acp_enabled"
        ]
        is False
    )


def test_agent_config_schema_documents_acp_fields(api_client):
    """Schema-driven controls get their help text from the field description, and
    every dashboard-editable field must carry one (CLAUDE.md "Dashboard UX")."""
    assert api_client.post("/api/agents", json={"name": "acp_schema_probe"}).status_code == 200

    props = api_client.get("/api/agents/acp_schema_probe/config/schema").json()["schema"][
        "properties"
    ]
    for field in ("acp_enabled", "acp_port"):
        assert len(props[field].get("description", "")) > 80, f"{field} needs help text"


def test_update_agent_clears_override_with_explicit_null(api_client):
    """Sending a field as explicit null clears an override (restores the default),
    which the schema-driven editor relies on for nullable fields."""
    import json

    assert api_client.post("/api/agents", json={"name": "null_probe"}).status_code == 200

    api_client.put("/api/agents/null_probe", json={"top_level_memory_tools": ["memory_save"]})
    data = json.loads(api_client.get("/api/agents/null_probe/config/raw").json()["content"])
    assert data["top_level_memory_tools"] == ["memory_save"]

    api_client.put("/api/agents/null_probe", json={"top_level_memory_tools": None})
    data = json.loads(api_client.get("/api/agents/null_probe/config/raw").json()["content"])
    assert data["top_level_memory_tools"] is None


def test_customer_service_toggle_resets_stale_memory_tool_allowlists(api_client):
    """Enabling customer_service.enabled used to leave top_level_memory_tools
    (a stale pin from before the switch, e.g. inherited from the `default`/
    `merli` archetypes) untouched — it then filtered the newly-built
    remember/recall/recall_all/forget_me down to nothing, since none of the
    old names matched. Flipping the switch must reset the allow-list so the
    switch actually grants the tools it advertises."""
    resp = api_client.post(
        "/api/agents",
        json={
            "name": "cs_toggle_probe",
            "top_level_memory_tools": ["user_profile_save", "identity_preference_save"],
        },
    )
    assert resp.status_code == 200, resp.text

    put = api_client.put(
        "/api/agents/cs_toggle_probe", json={"customer_service": {"enabled": True}}
    )
    assert put.status_code == 200, put.text

    data = api_client.get("/api/agents/cs_toggle_probe").json()
    assert data["customer_service"]["enabled"] is True
    assert data["top_level_memory_tools"] is None
    assert data["top_level_rag_tools"] is None
    assert data["top_level_graph_tools"] is None

    internal = {row["name"]: row for row in data["internal_tools"]}
    assert internal["remember"]["on"] is True

    # Flipping back off must reset it again — the same bug in reverse would
    # otherwise filter the shared markdown tools down to nothing.
    put_off = api_client.put(
        "/api/agents/cs_toggle_probe", json={"customer_service": {"enabled": False}}
    )
    assert put_off.status_code == 200, put_off.text
    data = api_client.get("/api/agents/cs_toggle_probe").json()
    assert data["top_level_memory_tools"] is None


def test_customer_service_toggle_respects_explicit_allowlist_in_same_patch(api_client):
    """If the same PUT that flips the switch also sets top_level_memory_tools
    explicitly, that's a deliberate choice, not a stale leftover — don't
    stomp on it."""
    assert api_client.post("/api/agents", json={"name": "cs_explicit_probe"}).status_code == 200

    put = api_client.put(
        "/api/agents/cs_explicit_probe",
        json={
            "customer_service": {"enabled": True},
            "top_level_memory_tools": ["remember", "recall"],
        },
    )
    assert put.status_code == 200, put.text
    data = api_client.get("/api/agents/cs_explicit_probe").json()
    assert data["top_level_memory_tools"] == ["remember", "recall"]


def test_enabling_multi_user_fills_the_blank_prompt_extension(api_client):
    """Flipping the switch from the dashboard must leave the agent with the
    support-desk rules in the box, persisted to agent.json5 — the blank field
    was how operators shipped a shared bot with no privacy or scope rules at
    all. Operator wording, once written, is never touched again."""
    import json

    from olano.config.customer_service import DEFAULT_MULTI_USER_PROMPT_EXTENSION

    assert api_client.post("/api/agents", json={"name": "cs_default_probe"}).status_code == 200

    # Exactly what the Multi-user tab sends when the toggle is switched on.
    put = api_client.put(
        "/api/agents/cs_default_probe", json={"customer_service": {"enabled": True}}
    )
    assert put.status_code == 200, put.text

    raw = json.loads(api_client.get("/api/agents/cs_default_probe/config/raw").json()["content"])
    assert raw["customer_service"]["prompt_extension"] == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    data = api_client.get("/api/agents/cs_default_probe").json()
    assert data["customer_service"]["prompt_extension"] == DEFAULT_MULTI_USER_PROMPT_EXTENSION

    # An operator's own text replaces it and survives verbatim.
    mine = "Only answer questions about ACME lawn mowers."
    assert (
        api_client.put(
            "/api/agents/cs_default_probe",
            json={"customer_service": {"enabled": True, "prompt_extension": mine}},
        ).status_code
        == 200
    )
    data = api_client.get("/api/agents/cs_default_probe").json()
    assert data["customer_service"]["prompt_extension"] == mine

    # Clearing the box restores the default rather than running with no rules.
    assert (
        api_client.put(
            "/api/agents/cs_default_probe",
            json={"customer_service": {"enabled": True, "prompt_extension": "  "}},
        ).status_code
        == 200
    )
    data = api_client.get("/api/agents/cs_default_probe").json()
    assert data["customer_service"]["prompt_extension"] == DEFAULT_MULTI_USER_PROMPT_EXTENSION

    # Off means off: no support-desk rules land on a single-user agent.
    assert (
        api_client.put(
            "/api/agents/cs_default_probe",
            json={"customer_service": {"enabled": False, "prompt_extension": ""}},
        ).status_code
        == 200
    )
    data = api_client.get("/api/agents/cs_default_probe").json()
    assert data["customer_service"]["prompt_extension"] == ""


def test_customer_service_defaults_endpoint_names_the_blank_field_default(api_client):
    """The dashboard preselects the variant the server says is the default."""
    from olano.config.customer_service import DEFAULT_MULTI_USER_PROMPT_EXTENSION

    body = api_client.get("/api/customer-service/defaults").json()

    variant = body["default_prompt_extension_variant"]
    assert body["suggested_prompt_extensions"][variant] == DEFAULT_MULTI_USER_PROMPT_EXTENSION


def test_customer_service_switch_works_for_per_customer_configured_agents(api_client):
    """An agent that entered CS mode via memory.per_customer directly (the
    customer-service archetype, or a hand-edited config) has no
    customer_service block in its file. The switch must still (a) READ as on —
    per-customer isolation IS customer-service mode — and (b) actually turn
    the mode OFF: without clearing the per_customer mirror, the validator
    flips the switch straight back on and the agent silently stays in CS mode
    while the UI says off."""
    import json

    from olano.config.customer_service import DEFAULT_MULTI_USER_PROMPT_EXTENSION

    resp = api_client.post("/api/agents", json={"name": "cs_archetype_probe"})
    assert resp.status_code == 200, resp.text
    # Simulate the archetype shape: per_customer on, a pinned customer
    # allow-list, and no customer_service block at all. (markdown stays on so
    # the shared tools have something to come back to after leaving CS mode —
    # per_customer wins while it's set.)
    put = api_client.put(
        "/api/agents/cs_archetype_probe",
        json={
            "memory": {"markdown": True, "per_customer": True},
            "top_level_memory_tools": ["remember", "recall", "forget_me"],
        },
    )
    assert put.status_code == 200, put.text
    raw = json.loads(api_client.get("/api/agents/cs_archetype_probe/config/raw").json()["content"])
    # The switch itself is still never written — the mirror does that work. The
    # only thing multi-user mode persists is the blank prompt extension's
    # support-desk default, so the file says what the agent actually runs with.
    assert "enabled" not in (raw.get("customer_service") or {})
    assert raw["customer_service"]["prompt_extension"] == DEFAULT_MULTI_USER_PROMPT_EXTENSION

    # (a) The switch reads ON even though the file never wrote it.
    data = api_client.get("/api/agents/cs_archetype_probe").json()
    assert data["customer_service"]["enabled"] is True
    assert data["customer_service"]["prompt_extension"] == DEFAULT_MULTI_USER_PROMPT_EXTENSION
    internal = {row["name"]: row for row in data["internal_tools"]}
    assert internal["remember"]["on"] is True

    # (b) Turning the switch off — exactly what the dashboard card sends —
    # leaves CS mode for real: per_customer cleared, the stale archetype
    # allow-list reset (it named only customer tools, which would zero out the
    # shared markdown tools), and the shared memory tools come back on.
    put_off = api_client.put(
        "/api/agents/cs_archetype_probe",
        json={"customer_service": {"enabled": False}},
    )
    assert put_off.status_code == 200, put_off.text
    raw = json.loads(api_client.get("/api/agents/cs_archetype_probe/config/raw").json()["content"])
    assert raw["memory"]["per_customer"] is False
    assert raw["top_level_memory_tools"] is None

    data = api_client.get("/api/agents/cs_archetype_probe").json()
    assert not (data.get("customer_service") or {}).get("enabled")
    internal = {row["name"]: row for row in data["internal_tools"]}
    assert internal["memory_save"]["on"] is True
    assert internal["remember"]["on"] is False
    assert "Customer Service" in str(internal["remember"]["note"])


def test_customer_service_off_respects_explicit_per_customer_in_same_patch(api_client):
    """A PUT that sets enabled=false while ALSO explicitly setting
    memory.per_customer is a deliberate (if odd) choice — the handler must not
    override the explicit memory value."""
    import json

    assert api_client.post("/api/agents", json={"name": "cs_explicit_mem_probe"}).status_code == 200
    api_client.put(
        "/api/agents/cs_explicit_mem_probe", json={"customer_service": {"enabled": True}}
    )

    put = api_client.put(
        "/api/agents/cs_explicit_mem_probe",
        json={
            "customer_service": {"enabled": False},
            "memory": {"markdown": False, "per_customer": True},
        },
    )
    assert put.status_code == 200, put.text
    raw = json.loads(
        api_client.get("/api/agents/cs_explicit_mem_probe/config/raw").json()["content"]
    )
    # The explicit per_customer wins; the agent stays in CS mode (and the
    # switch reads back on, mirroring the effective state).
    assert raw["memory"]["per_customer"] is True
    data = api_client.get("/api/agents/cs_explicit_mem_probe").json()
    assert data["customer_service"]["enabled"] is True


def test_update_agent_rejects_invalid_value(api_client):
    """An invalid value for a real field is rejected (422) without touching disk —
    validation happens on the merged AgentConfig, so bad input can't corrupt it."""
    import json

    assert api_client.post("/api/agents", json={"name": "bad_probe"}).status_code == 200
    before = api_client.get("/api/agents/bad_probe/config/raw").json()["content"]

    bad = api_client.put("/api/agents/bad_probe", json={"iteration_budget": "not-an-int"})
    assert bad.status_code == 422
    # File untouched.
    assert api_client.get("/api/agents/bad_probe/config/raw").json()["content"] == before
    json.loads(before)  # sanity: still valid JSON


def test_resolve_effective_tool_names_adds_builtins_and_respects_disabled():
    from olano.config.ops_defaults import resolve_effective_tool_names

    out = resolve_effective_tool_names(
        ["web_search", "scheduler_create", "web_search"],  # contains a duplicate
        disabled_tools=["execute", "glob"],
    )

    # Configured tools come first, de-duplicated, order preserved.
    assert out[:2] == ["web_search", "scheduler_create"]
    # Deepagents built-ins are appended …
    assert "read_file" in out
    assert "task" in out
    # … minus any disabled ones.
    assert "execute" not in out
    assert "glob" not in out
    # No duplicates overall.
    assert len(out) == len(set(out))


def test_chat_endpoint(api_client):
    resp = api_client.post(
        "/api/chat",
        json={
            "message": "Hello",
            "agent": "test_agent",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert data["response"] == "Hello from agent!"
    assert "thread_id" in data


def test_chat_unknown_agent(api_client):
    resp = api_client.post(
        "/api/chat",
        json={
            "message": "Hello",
            "agent": "nonexistent_agent",
        },
    )
    assert resp.status_code == 404


def _stub_stream(runner, captured):
    """Replace runner.astream_events with an async-gen stub that records kwargs."""

    def fake_events(payload, **kwargs):
        captured.update(kwargs)

        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events


def test_chat_stream_binds_agent_admin_into_the_turn_context(api_client, mock_runtime):
    """The SSE body runs after Starlette's middleware has exited, so it sees
    the unbound (fail-open) default context. The self_config ownership gate
    reads `agent_admin` from there, so it must be re-bound with the surface —
    otherwise the gate silently passes for every dashboard human, which is the
    one failure a passing unit test elsewhere would not catch.
    """
    from olano.security.request_context import context_agent_admin

    runner = mock_runtime._runners["test_agent"]
    seen: dict = {}

    def fake_events(payload, **kwargs):
        seen["agent_admin"] = context_agent_admin()

        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "Hi", "agent": "test_agent"})
    assert resp.status_code == 200
    # Single-user install: the implicit owner administers every agent. The
    # point of the assertion is that it is a real bool, not None (unbound).
    assert seen["agent_admin"] is True


def test_chat_stream_honors_model_override(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post(
        "/api/chat/stream",
        json={
            "message": "Hello",
            "agent": "test_agent",
            "model": "anthropic:claude-opus-4-6",
        },
    )
    assert resp.status_code == 200
    # The override is threaded into the runner…
    assert captured.get("override_model") == "anthropic:claude-opus-4-6"
    # …and the start frame advertises the effective (override) model.
    assert '"type": "start"' in resp.text
    assert "anthropic:claude-opus-4-6" in resp.text


def test_chat_stream_override_equal_to_config_is_ignored(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post(
        "/api/chat/stream",
        json={
            "message": "Hello",
            "agent": "test_agent",
            "model": runner.config.model,  # same as configured → no override
        },
    )
    assert resp.status_code == 200
    assert captured.get("override_model") is None


def test_chat_stream_threads_session_mode(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Hello", "agent": "test_agent", "mode": "manual"},
    )
    assert resp.status_code == 200
    assert captured.get("session_mode") == "manual"


def test_chat_stream_plan_mode_prepends_preamble(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    payloads: list = []

    def fake_events(payload, **kwargs):
        payloads.append(payload)
        captured.update(kwargs)

        async def gen():
            yield ("text", "ok")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Do a thing", "agent": "test_agent", "mode": "plan"},
    )
    assert resp.status_code == 200
    assert captured.get("session_mode") == "plan"
    # The planning preamble is passed as a separate per-turn directive, NOT baked
    # into the message — so the logged history (and the chat bubble) stay clean.
    assert "Plan mode" in str(captured.get("turn_directive"))
    blob = str(payloads[0])
    assert "Plan mode" not in blob
    assert "Do a thing" in blob


def test_chat_stream_act_mode_threads_session_mode(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Run it", "agent": "test_agent", "mode": "act"},
    )
    assert resp.status_code == 200
    assert captured.get("session_mode") == "act"
    # Act, like Auto, approves interrupts itself, but the handler is still
    # wired: the runner calls it for what only a person may approve (a browser
    # submit, purchase or send). Without it those approvals went to the
    # Approvals inbox and the chat sat until the HITL timeout rejected them.
    assert callable(captured.get("approval_handler"))
    assert captured.get("turn_directive") is None


def test_chat_stream_auto_mode_still_wires_the_approval_handler(api_client, mock_runtime):
    """A browser approval in the default Auto mode must be asked in the chat.

    The runner approves every interrupt itself in Auto mode except what only a
    person may approve, and asks the handler for those. With no handler they
    took the non-interactive path (the Approvals inbox), the chat showed no
    card, and the turn waited out the HITL timeout before being rejected.
    """
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Submit the form", "agent": "test_agent", "mode": "auto"},
    )
    assert resp.status_code == 200
    assert captured.get("session_mode") == "auto"
    assert callable(captured.get("approval_handler"))


def test_chat_stream_runner_error_becomes_error_frame(api_client, mock_runtime):
    # An unbuildable override (or any stream failure) surfaces as an in-band
    # error frame, not a 500.
    runner = mock_runtime._runners["test_agent"]

    def failing_events(payload, **kwargs):
        async def gen():
            raise RuntimeError("Could not load model 'bogus:nope'")
            yield  # pragma: no cover — unreachable, makes this an async generator

        return gen()

    runner.astream_events = failing_events

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Hello", "agent": "test_agent", "model": "bogus:nope"},
    )
    assert resp.status_code == 200
    assert '"type": "error"' in resp.text
    assert "Could not load model" in resp.text


def test_scheduler_tasks_endpoint(api_client, tmp_path, monkeypatch):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    db.create_task("Test task", "Check something", schedule="0 9 * * *")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.get("/api/scheduler/tasks")

    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["name"] == "Test task"


def test_create_scheduler_task(api_client):
    with patch("olano.runtime.api.SchedulerDB") as mock_db_class:
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.create_task.return_value = "abc123"
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={
                "name": "New task",
                "prompt": "Do something",
                "schedule": "0 9 * * *",
            },
        )

    assert resp.status_code == 200


def test_create_scheduler_task_with_model_and_effort_overrides(api_client):
    """The per-job overrides reach the store, validated against the model's ladder."""
    with (
        patch("olano.runtime.api.SchedulerDB") as mock_db_class,
        patch("olano.admin.models.model_provider_connected", return_value=(True, "ok")),
    ):
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.create_task.return_value = "abc123"
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={
                "name": "Weekly report",
                "prompt": "Write it",
                "schedule": "0 9 * * 1",
                "model": "openai:gpt-6-sol",
                "reasoning_effort": "HIGH",
            },
        )
    assert resp.status_code == 200, resp.text
    kwargs = mock_db.create_task.call_args.kwargs
    assert kwargs["model"] == "openai:gpt-6-sol"
    assert kwargs["reasoning_effort"] == "high"


def test_create_scheduler_task_rejects_an_effort_the_model_does_not_accept(api_client):
    with (
        patch("olano.runtime.api.SchedulerDB") as mock_db_class,
        patch("olano.admin.models.model_provider_connected", return_value=(True, "ok")),
    ):
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={
                "name": "Weekly report",
                "prompt": "Write it",
                "schedule": "0 9 * * 1",
                "model": "openai:gpt-6-sol",
                "reasoning_effort": "ultra",
            },
        )
    assert resp.status_code == 422
    assert "does not accept reasoning_effort 'ultra'" in resp.json()["detail"]
    mock_db.create_task.assert_not_called()


def test_update_scheduler_task_clears_an_override_with_an_empty_string(api_client, tmp_path):
    """Omitted keeps, "" clears: the dashboard sends both overrides on every save."""
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    task_id = db.create_task(
        "Report",
        "Write it",
        schedule="0 9 * * 1",
        model="openai:gpt-6-sol",
        reasoning_effort="high",
    )
    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.put(
            f"/api/scheduler/tasks/{task_id}", json={"name": "Report v2", "reasoning_effort": ""}
        )
    assert resp.status_code == 200, resp.text
    task = db.get_task(task_id)
    assert task["name"] == "Report v2"
    assert task["model"] == "openai:gpt-6-sol"  # omitted → kept
    assert task["reasoning_effort"] == ""  # "" → cleared


def test_delete_scheduler_task(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    task_id = db.create_task("Delete me", "Prompt", schedule="0 9 * * *")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.delete(f"/api/scheduler/tasks/{task_id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_nonexistent_task(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.delete("/api/scheduler/tasks/nonexistent-id")

    assert resp.status_code == 404


def test_config_schedule_jobs_surface_and_edit(api_client):
    """agent.json5 `schedule` entries show up in the merged tasks list (tagged
    source=config) and edit/delete write back to the file."""
    import json

    from olano.config.home import get_olano_paths

    adir = get_olano_paths().agents_dir / "sicoper"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "sicoper",
                "model": "anthropic:claude-haiku-4-5-20251001",
                "schedule": [
                    {
                        "cron": "0 10 * * *",
                        "message": "morning sweep",
                        "name": "AM",
                        "timezone": "America/Costa_Rica",
                    },
                    {"cron": "0 18 * * *", "message": "evening sweep"},
                ],
            }
        ),
        encoding="utf-8",
    )

    resp = api_client.get("/api/scheduler/tasks")
    assert resp.status_code == 200
    cfg_tasks = {t["id"]: t for t in resp.json() if t.get("source") == "config"}
    assert "cfg:sicoper:0" in cfg_tasks and "cfg:sicoper:1" in cfg_tasks
    assert cfg_tasks["cfg:sicoper:0"]["schedule"] == "0 10 * * *"
    assert cfg_tasks["cfg:sicoper:0"]["name"] == "AM"
    assert cfg_tasks["cfg:sicoper:0"]["agent_name"] == "sicoper"

    # Pause entry 1 → persisted to the file.
    resp = api_client.put("/api/scheduler/tasks/cfg:sicoper:1", json={"enabled": False})
    assert resp.status_code == 200
    data = json.loads((adir / "agent.json5").read_text(encoding="utf-8"))
    assert data["schedule"][1]["enabled"] is False

    # Delete entry 0 → file left with just the (paused) evening entry.
    resp = api_client.delete("/api/scheduler/tasks/cfg:sicoper:0")
    assert resp.status_code == 200
    data = json.loads((adir / "agent.json5").read_text(encoding="utf-8"))
    assert len(data["schedule"]) == 1
    assert data["schedule"][0]["message"] == "evening sweep"


def test_config_schedule_job_missing_agent_404(api_client):
    resp = api_client.put("/api/scheduler/tasks/cfg:ghost:0", json={"enabled": False})
    assert resp.status_code == 404


def test_run_task_now_no_active_scheduler_409(api_client):
    with patch("olano.scheduler.cron.get_active_scheduler", return_value=None):
        resp = api_client.post("/api/scheduler/tasks/cfg:cindy:0/run")
    assert resp.status_code == 409


def test_run_task_now_config_job(api_client):
    fake_sched = MagicMock()
    fake_sched.run_config_job_now.return_value = (True, "queued")
    with patch("olano.scheduler.cron.get_active_scheduler", return_value=fake_sched):
        resp = api_client.post("/api/scheduler/tasks/cfg:cindy:0/run")
    assert resp.status_code == 200
    fake_sched.run_config_job_now.assert_called_once_with("cfg:cindy:0")


def test_run_task_now_db_task(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "run.db")
    tid = db.create_task("Nightly", "do it", schedule="0 9 * * *")
    fake_sched = MagicMock()
    fake_sched.run_db_task_now.return_value = (True, "queued")
    with (
        patch("olano.runtime.api.SchedulerDB", return_value=db),
        patch("olano.scheduler.cron.get_active_scheduler", return_value=fake_sched),
    ):
        resp = api_client.post(f"/api/scheduler/tasks/{tid}/run")
    assert resp.status_code == 200
    fake_sched.run_db_task_now.assert_called_once_with(tid)


def test_run_task_now_db_task_missing_404(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "run.db")
    fake_sched = MagicMock()
    with (
        patch("olano.runtime.api.SchedulerDB", return_value=db),
        patch("olano.scheduler.cron.get_active_scheduler", return_value=fake_sched),
    ):
        resp = api_client.post("/api/scheduler/tasks/does-not-exist/run")
    assert resp.status_code == 404


def test_list_files_endpoint(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Notes", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    assert resp.status_code == 200
    files = resp.json()
    names = {f["name"] for f in files}
    assert "notes.md" in names or "MEMORY.md" in names


def test_list_files_omits_dotfiles_by_default(api_client, tmp_workspace):
    """A workspace root carries `.olano/` (and often a `.git/`), so listing
    dot-entries by default buries the files people actually work on."""
    (tmp_workspace / ".env").write_text("KEY=1", encoding="utf-8")
    (tmp_workspace / ".config").mkdir()
    (tmp_workspace / "notes.md").write_text("# Notes", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    assert resp.status_code == 200
    names = {f["name"] for f in resp.json()}
    assert "notes.md" in names
    assert ".env" not in names
    assert ".config" not in names


def test_list_files_includes_dotfiles_when_asked(api_client, tmp_workspace):
    """`?hidden=true` is what the dashboard's Files toggle sends. It reveals
    nothing new — reads and writes already resolve `.env` by path — it just
    makes those entries discoverable in the tree."""
    (tmp_workspace / ".env").write_text("KEY=1", encoding="utf-8")
    (tmp_workspace / ".config").mkdir()
    (tmp_workspace / "notes.md").write_text("# Notes", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files", params={"hidden": "true"})

    assert resp.status_code == 200
    entries = {f["name"]: f for f in resp.json()}
    assert "notes.md" in entries
    assert entries[".env"]["type"] == "file"
    assert entries[".env"]["path"] == ".env"
    assert entries[".config"]["type"] == "directory"


def test_list_files_hidden_flag_applies_to_subdirectories(api_client, tmp_workspace):
    """The tree lists one directory per request, so the flag has to survive the
    `?path=` hop — otherwise only the root would honour the toggle."""
    sub = tmp_workspace / "sub"
    sub.mkdir()
    (sub / ".env").write_text("KEY=1", encoding="utf-8")
    (sub / "visible.md").write_text("hi", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        default = api_client.get("/api/files", params={"path": "sub"})
        shown = api_client.get("/api/files", params={"path": "sub", "hidden": "true"})

    assert {f["name"] for f in default.json()} == {"visible.md"}
    assert {f["name"] for f in shown.json()} == {".env", "visible.md"}


def test_read_file_endpoint(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Hello\nTest content.", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/notes.md")

    assert resp.status_code == 200
    data = resp.json()
    assert "Hello" in data["content"]


def test_read_nonexistent_file(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/missing.txt")

    assert resp.status_code == 404


# ── shared/* projections (symlinks out of the workspace) ─────────────────────
#
# `sync_workspace_share_projections` builds `workspace/shared/<name>` as a
# symlink into `<home>/shared`, so it resolves OUTSIDE the workspace root. The
# Files endpoints' containment check refused every one of them with a 403,
# while the parent listing still showed the folder (entry paths there are built
# lexically) — so the dashboard's tree listed `shared/default` and could not
# open it. The frontend then retried the failure in a loop; these tests pin the
# server half.


@pytest.fixture
def shared_projection(tmp_path_factory, monkeypatch):
    """The real boot layout, built by the real projection code.

    Production nests the workspace under the home (`<home>/agents/<n>/workspace`
    or `<home>/workspace`) while the default grant's source is `<home>/shared`
    — a *sibling*, so `workspace/shared/default` resolves outside the workspace
    root. That escape is the whole bug, so the layout has to be faithful: a
    home-inside-the-workspace shortcut makes these tests pass against the
    unfixed code.

    `sync_workspace_share_projections` lays the symlink down (rather than the
    test hand-rolling one), and the agent's config is written to disk because
    resolving the mounts needs `find_agent_config_path` to see it.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.loader import write_agent_config_text
    from olano.runtime.api import _default_agent_name
    from olano.security.sharing import resolve_shared_mounts, sync_workspace_share_projections

    home = tmp_path_factory.mktemp("olano-home")
    monkeypatch.setenv("LIONCLAW_HOME", str(home))

    agent = _default_agent_name()
    root = home / "agents" / agent / "workspace"
    (root / "memories").mkdir(parents=True)
    (root / "notes.md").write_text("# Notes", encoding="utf-8")

    cfg_path = home / "agents" / agent / "agent.json5"
    write_agent_config_text(
        cfg_path,
        f'{{ "name": "{agent}", "model": "anthropic:claude-haiku-4-5-20251001" }}',
    )
    config = AgentConfig(name=agent, model="anthropic:claude-haiku-4-5-20251001")

    mounts = resolve_shared_mounts(config)
    sync_workspace_share_projections(root, mounts)
    source = home / "shared"  # the default grant's source
    (source / "nested").mkdir(parents=True, exist_ok=True)
    (source / "handoff.md").write_text("shared note", encoding="utf-8")

    link = root / "shared" / "default"
    assert link.is_symlink(), "projection must be a symlink for this to test anything"
    assert source not in root.parents and root not in source.parents

    return SimpleNamespace(source=source, link=link, root=root, agent=agent)


def test_list_files_opens_shared_projection(api_client, shared_projection):
    with patch("olano.runtime.api._workspace_root", return_value=shared_projection.root):
        parent = api_client.get("/api/files?path=shared")
        opened = api_client.get("/api/files?path=shared/default")

    # The tree offers the folder…
    assert parent.status_code == 200
    assert "default" in {e["name"] for e in parent.json()}
    # …so opening it must work, not 403.
    assert opened.status_code == 200, opened.json()
    entries = {e["name"]: e for e in opened.json()}
    assert {"handoff.md", "nested"} <= set(entries)
    # Paths stay workspace-relative (built lexically, never from resolve()), so
    # the tree can keep drilling into the projection.
    assert entries["nested"]["path"] == "shared/default/nested"


def test_read_file_inside_shared_projection(api_client, shared_projection):
    with patch("olano.runtime.api._workspace_root", return_value=shared_projection.root):
        resp = api_client.get("/api/files/shared/default/handoff.md")
        raw = api_client.get("/api/files-raw/shared/default/handoff.md")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["content"] == "shared note"
    assert raw.status_code == 200


def test_write_inside_shared_projection(api_client, shared_projection):
    # The default grant is rw, so the dashboard may write there — the agent can.
    with patch("olano.runtime.api._workspace_root", return_value=shared_projection.root):
        resp = api_client.put("/api/files/shared/default/from-dashboard.md", json={"content": "hi"})

    assert resp.status_code == 200, resp.json()
    assert (shared_projection.source / "from-dashboard.md").read_text() == "hi"


def test_cannot_delete_shared_projection_root(api_client, shared_projection):
    # Deleting the projection would take the whole cross-agent share, not one
    # agent's file — refused like the workspace root is.
    with patch("olano.runtime.api._workspace_root", return_value=shared_projection.root):
        resp = api_client.delete("/api/files/shared/default")

    assert resp.status_code == 403
    assert (shared_projection.source / "handoff.md").is_file()


@pytest.mark.parametrize(
    "path",
    ["../../../etc/passwd", "..", "shared/default/../../..", "/etc"],
)
def test_list_files_still_refuses_escapes(api_client, shared_projection, path):
    """Admitting the share sources must not admit anything else: `..` traversal
    and absolute paths still cannot enumerate /app, /opt/olano or the home tree."""
    with patch("olano.runtime.api._workspace_root", return_value=shared_projection.root):
        resp = api_client.get("/api/files", params={"path": path})
        # Asking for dot-entries is a listing preference, not an access change.
        hidden = api_client.get("/api/files", params={"path": path, "hidden": "true"})

    assert resp.status_code == 403, resp.json()
    assert hidden.status_code == 403, hidden.json()


def test_shared_projection_roots_empty_without_config(tmp_workspace):
    """No resolvable agent config ⇒ no extra roots (historical containment)."""
    from olano.runtime.api import _shared_projection_roots

    with patch("olano.runtime.api._agent_config_for", side_effect=FileNotFoundError):
        assert _shared_projection_roots("ghost") == []


def test_list_files_includes_abs_path(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Notes", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    assert resp.status_code == 200
    notes = next(f for f in resp.json() if f["name"] == "notes.md")
    assert notes["abs_path"] == str((tmp_workspace / "notes.md").resolve())


def test_delete_file_endpoint(api_client, tmp_workspace):
    target = tmp_workspace / "delete-me.txt"
    target.write_text("bye", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.delete("/api/files/delete-me.txt")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert not target.exists()


def test_delete_directory_recursively(api_client, tmp_workspace):
    sub = tmp_workspace / "sub"
    sub.mkdir()
    (sub / "child.txt").write_text("x", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.delete("/api/files/sub")

    assert resp.status_code == 200
    assert not sub.exists()


def test_delete_missing_file(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.delete("/api/files/nope.txt")

    assert resp.status_code == 404


def test_delete_root_forbidden(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.delete("/api/files/")

    assert resp.status_code in (403, 404)


def test_duplicate_file_endpoint(api_client, tmp_workspace):
    (tmp_workspace / "doc.md").write_text("hello", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/duplicate", json={"path": "doc.md"})

    assert resp.status_code == 200
    assert resp.json()["path"] == "doc copy.md"
    assert (tmp_workspace / "doc copy.md").read_text(encoding="utf-8") == "hello"


def test_duplicate_increments_name(api_client, tmp_workspace):
    (tmp_workspace / "doc.md").write_text("hello", encoding="utf-8")
    (tmp_workspace / "doc copy.md").write_text("hello", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/duplicate", json={"path": "doc.md"})

    assert resp.status_code == 200
    assert resp.json()["path"] == "doc copy 2.md"


def test_move_file_into_directory(api_client, tmp_workspace):
    (tmp_workspace / "doc.md").write_text("hello", encoding="utf-8")
    (tmp_workspace / "dest").mkdir()

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/move", json={"source": "doc.md", "dest": "dest/doc.md"})

    assert resp.status_code == 200
    assert not (tmp_workspace / "doc.md").exists()
    assert (tmp_workspace / "dest" / "doc.md").read_text(encoding="utf-8") == "hello"


def test_move_conflict(api_client, tmp_workspace):
    (tmp_workspace / "a.md").write_text("a", encoding="utf-8")
    (tmp_workspace / "b.md").write_text("b", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/move", json={"source": "a.md", "dest": "b.md"})

    assert resp.status_code == 409


def test_move_folder_into_itself_forbidden(api_client, tmp_workspace):
    (tmp_workspace / "folder").mkdir()

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post(
            "/api/files/move", json={"source": "folder", "dest": "folder/folder"}
        )

    assert resp.status_code == 400


def test_create_file_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/create", json={"path": "new.md", "type": "file"})

    assert resp.status_code == 200
    assert resp.json()["path"] == "new.md"
    target = tmp_workspace / "new.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == ""


def test_create_folder_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/create", json={"path": "newdir", "type": "directory"})

    assert resp.status_code == 200
    assert (tmp_workspace / "newdir").is_dir()


def test_create_nested_file_creates_parents(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/create", json={"path": "a/b/c.txt", "type": "file"})

    assert resp.status_code == 200
    assert (tmp_workspace / "a" / "b" / "c.txt").is_file()


def test_create_existing_conflict(api_client, tmp_workspace):
    (tmp_workspace / "dup.md").write_text("x", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/files/create", json={"path": "dup.md", "type": "file"})

    assert resp.status_code == 409


def test_memory_read_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/memory")

    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data


def test_memory_append_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post(
            "/api/memory",
            json={
                "content": "User prefers dark mode",
                "section": "Preferences",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_memory_write_endpoint(api_client, tmp_workspace):
    new_doc = "# Memory\n\n## User Profile\n- [2026-06-08] Edited inline from the dashboard.\n"
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.put("/api/memory", json={"content": new_doc})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["size"] == len(new_doc)

        # The whole document is replaced, not appended to.
        read_back = api_client.get("/api/memory")

    assert read_back.status_code == 200
    assert read_back.json()["content"] == new_doc


def test_notes_crud_roundtrip(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        # Empty store lists cleanly.
        resp = api_client.get("/api/notes")
        assert resp.status_code == 200
        assert resp.json() == {"notes": [], "count": 0, "link_count": 0, "query": ""}

        # Create two notes, the second wikilinking the first.
        resp = api_client.post(
            "/api/notes",
            json={"title": "Kubernetes", "content": "Container orchestration.", "tags": ["infra"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = api_client.post(
            "/api/notes",
            json={"title": "Deploys", "content": "We ship on [[Kubernetes]]."},
        )
        assert resp.status_code == 200
        assert resp.json()["links"] == ["Kubernetes"]

        # List shows both plus the link count.
        data = api_client.get("/api/notes").json()
        assert {n["title"] for n in data["notes"]} == {"Kubernetes", "Deploys"}
        assert data["count"] == 2
        assert data["link_count"] == 1

        # Entry read carries content + the bidirectional link graph.
        entry = api_client.get("/api/notes/entry", params={"title": "Kubernetes"}).json()
        assert entry["content"] == "Container orchestration."
        assert entry["tags"] == ["infra"]
        assert entry["backlinks"] == ["Deploys"]
        outward = api_client.get("/api/notes/entry", params={"title": "Deploys"}).json()
        assert outward["outlinks"] == ["Kubernetes"]

        # Update preserves the creation stamp and replaces content.
        created = entry["created"]
        resp = api_client.post(
            "/api/notes",
            json={"title": "Kubernetes", "content": "Updated body.", "tags": ["infra", "k8s"]},
        )
        assert resp.status_code == 200
        entry = api_client.get("/api/notes/entry", params={"title": "Kubernetes"}).json()
        assert entry["content"] == "Updated body."
        assert entry["created"] == created
        assert entry["tags"] == ["infra", "k8s"]

        # FTS search finds by body text.
        hits = api_client.get("/api/notes", params={"q": "ship"}).json()
        assert [n["title"] for n in hits["notes"]] == ["Deploys"]

        # Delete removes note, file and index rows.
        resp = api_client.delete("/api/notes/entry", params={"title": "Deploys"})
        assert resp.status_code == 200
        assert not (tmp_workspace / "notes" / "Deploys.md").exists()
        data = api_client.get("/api/notes").json()
        assert data["count"] == 1
        assert data["link_count"] == 0


def test_notes_entry_missing_is_404(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        assert api_client.get("/api/notes/entry", params={"title": "nope"}).status_code == 404
        assert api_client.delete("/api/notes/entry", params={"title": "nope"}).status_code == 404


def test_notes_blank_title_rejected(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post("/api/notes", json={"title": "   ", "content": "x"})
    assert resp.status_code == 400


def test_logs_endpoint(api_client):
    resp = api_client.get("/api/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "logs" in data


def test_catalog_providers_includes_openai_codex_auth(api_client):
    resp = api_client.get("/api/catalog/providers")

    assert resp.status_code == 200
    providers = {p["key"]: p for p in resp.json()["providers"]}
    codex = providers["openai-codex"]
    assert codex["auth"]["type"] == "device_code"
    assert codex["auth"]["connected"] is False
    assert "gpt-5.3-codex" in codex["models"]


def test_catalog_providers_hides_retired_olano_tiers(api_client):
    """A withdrawn tier must not reach a picker — not even from the cloud.

    `olano:auto` still resolves for agents configured with it, but the gateway
    doesn't route per request yet, so nothing may offer it. The synced catalog
    is the sneaky path: it can list a tier this build has since withdrawn, and
    the payload takes catalog order first — so the filter has to run after the
    merge, not on the local list alone.
    """
    from olano.catalog import models_registry

    # `reasoning` is in the fixture on purpose: it is the top tier's RETIRED id
    # (renamed to `max`). A catalog that has not re-synced can still carry it,
    # and this build must not offer a tier it cannot build — same filter that
    # withholds the hidden `auto`.
    olano_tiers = [
        {"id": f"olano:{t}", "provider": "olano", "model": t, "modality": "chat"}
        for t in ("auto", "turbo", "pro", "max", "reasoning")
    ]
    catalog = {"revision": 1, "source": "test", "providers": [], "models": olano_tiers}
    with patch.object(models_registry, "get_model_catalog", return_value=catalog):
        resp = api_client.get("/api/catalog/providers")
    assert resp.status_code == 200
    olano = {p["key"]: p for p in resp.json()["providers"]}["olano"]
    assert "auto" not in olano["models"]
    assert "reasoning" not in olano["models"]
    assert olano["models"][:1] == ["turbo"]
    assert {"turbo", "pro", "max"} <= set(olano["models"])


def test_catalog_providers_basic_skips_model_data(api_client):
    """?fields=basic returns the provider list without the per-model payload.

    Building that payload probes every model for reasoning-effort support —
    importing a langchain provider package and instantiating a chat class per
    model name — which costs ~10s across the catalog on a cold process. The
    onboarding picker renders names and key/auth state only, so it asks for the
    cheap shape. Everything it *does* read has to survive the trim.
    """
    resp = api_client.get("/api/catalog/providers?fields=basic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["fields"] == "basic"
    providers = {p["key"]: p for p in body["providers"]}
    # The expensive half is gone…
    assert all(p["models"] == [] for p in providers.values())
    assert all(not p["model_meta"] for p in providers.values())
    # …and everything the picker binds to is still here.
    anthropic = providers["anthropic"]
    assert anthropic["name"]
    assert anthropic["env_var"] == "ANTHROPIC_API_KEY"
    assert "env_set" in anthropic
    assert providers["openai-codex"]["auth"]["type"] == "device_code"
    assert "model_selection_locked" in body


def test_catalog_providers_defaults_to_full_model_data(api_client):
    """No ?fields → the full payload, so existing callers are untouched."""
    body = api_client.get("/api/catalog/providers").json()

    assert body["fields"] == "full"
    providers = {p["key"]: p for p in body["providers"]}
    assert "gpt-5.3-codex" in providers["openai-codex"]["models"]


def test_catalog_providers_env_set_reflects_key_presence(api_client, monkeypatch):
    """env_set is True only when the provider's API key is in env (or vault).

    The chat model picker uses this to hide providers the user hasn't
    authenticated, so the signal must track the actual key.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    providers = {p["key"]: p for p in api_client.get("/api/catalog/providers").json()["providers"]}
    assert providers["anthropic"]["env_set"] is False
    # Keyless providers (no env_var) are always "set".
    assert providers["ollama"]["env_set"] is True

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    providers = {p["key"]: p for p in api_client.get("/api/catalog/providers").json()["providers"]}
    assert providers["anthropic"]["env_set"] is True


def test_openai_codex_auth_status_endpoint(api_client):
    with (
        patch(
            "olano.models.codex_auth.auth_status",
            return_value={"logged_in": True, "token_preview": "tok…"},
        ),
        patch(
            "olano.models.codex_auth.get_codex_model_ids",
            return_value=["gpt-5.3-codex"],
        ),
    ):
        resp = api_client.get("/api/auth/openai-codex/status")

    assert resp.status_code == 200
    assert resp.json()["logged_in"] is True
    assert resp.json()["models"] == ["gpt-5.3-codex"]


def test_openai_codex_start_endpoint_uses_device_flow(api_client):
    session = {
        "session_id": "sess_123",
        "status": "pending",
        "user_code": "ABCD-EFGH",
        "verification_url": "https://auth.openai.com/codex/device",
        "interval": 5,
    }
    with patch("olano.runtime.api._start_codex_device_flow", AsyncMock(return_value=session)):
        resp = api_client.post("/api/auth/openai-codex/start")

    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sess_123"
    assert resp.json()["user_code"] == "ABCD-EFGH"


def test_openai_codex_poll_endpoint_returns_models_when_approved(api_client):
    from olano.models.codex_auth import _DEVICE_SESSIONS as codex_auth_sessions

    codex_auth_sessions.clear()
    codex_auth_sessions["sess_approved"] = {
        "session_id": "sess_approved",
        "status": "approved",
        "error_message": None,
    }
    with patch(
        "olano.models.codex_auth.get_codex_model_ids",
        return_value=["gpt-5.4-mini", "gpt-5.3-codex"],
    ):
        resp = api_client.get("/api/auth/openai-codex/poll/sess_approved")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["models"] == ["gpt-5.4-mini", "gpt-5.3-codex"]


# ── Boot-failure diagnostics ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("If using default api base, DEEPSEEK_API_KEY must be set.", "DEEPSEEK_API_KEY"),
        (
            "Did not find openai_api_key, please add an environment variable `OPENAI_API_KEY`.",
            "OPENAI_API_KEY",
        ),
        ("Value error, ANTHROPIC_API_KEY must be set", "ANTHROPIC_API_KEY"),
        ("invalid model name: foo-bar-9", None),
        ("Unauthorized (401): bad token", None),
    ],
)
def test_extract_secret_name(msg, expected):
    from olano.runtime.api import _extract_secret_name

    assert _extract_secret_name(msg) == expected


@pytest.mark.parametrize(
    "msg,is_secret",
    [
        ("If using default api base, DEEPSEEK_API_KEY must be set.", True),
        ("Credential not found in vault", True),
        ("Unauthorized (401): bad token", True),
        ("invalid model name: foo-bar-9", False),
        ("Connection refused", False),
    ],
)
def test_looks_like_secret_error(msg, is_secret):
    from olano.runtime.api import _looks_like_secret_error

    assert _looks_like_secret_error(msg) is is_secret


@pytest.mark.parametrize(
    "shorthand,expected_type",
    [
        ("filesystem+shell", "local_shell"),
        ("filesystem", "filesystem"),
        ("read", "filesystem"),
        ("state", "state"),
        ("store", "store"),
    ],
)
def test_normalize_backend_shorthands(shorthand, expected_type):
    """Friendly env shorthands normalize to the modern block form.

    "state" → "Virtual Files (Temporary)" and "store" → "Virtual Files (Persistent)"
    are the two virtual (no host filesystem) environments."""
    from olano.runtime.api import _normalize_backend

    data = {"backend": shorthand}
    rewritten = _normalize_backend(data)
    assert rewritten is True
    assert data["backend"]["type"] == expected_type


def test_unknown_backend_shorthand_falls_back():
    """An unrecognized shorthand falls back to local_shell so the agent isn't lost."""
    from olano.runtime.api import _normalize_backend

    data = {"backend": "totally-unknown"}
    _normalize_backend(data)
    assert data["backend"]["type"] == "local_shell"


def test_collapse_subagent_refs_drops_builtin_mirrors():
    """`from_builtin` display mirrors (e.g. OpsManager, no system_prompt) must be
    stripped before save so they don't fail SubAgentConfig validation."""
    from olano.runtime.api import _collapse_subagent_refs

    data = {
        "_running": True,
        "name": "merli",
        "subagents": [
            {
                "name": "OpsManager",
                "description": "x",
                "emoji": "🛠️",
                "tools": [],
                "from_builtin": "ops",
            },
            {"name": "Real", "description": "d", "system_prompt": "p"},
        ],
        "builtin_subagents": ["ops"],
    }

    out = _collapse_subagent_refs(data)

    assert [s["name"] for s in out["subagents"]] == ["Real"]
    assert "_running" not in out
    assert out["builtin_subagents"] == ["ops"]


def test_collapsed_config_passes_agent_validation():
    """A GET-shaped config (with builtin mirrors) round-trips through the schema
    once the display mirrors are collapsed."""
    from olano.config.agent_config import AgentConfig
    from olano.runtime.api import _collapse_subagent_refs

    data = {
        "name": "merli",
        "model": "anthropic:claude-haiku-4-5-20251001",
        "subagents": [
            {
                "name": "OpsManager",
                "description": "x",
                "emoji": "🛠️",
                "tools": [],
                "from_builtin": "ops",
            },
        ],
        "builtin_subagents": ["ops"],
    }

    AgentConfig(**_collapse_subagent_refs(data))


def test_resolve_subagents_for_display_preserves_source():
    """The resolved view is display-only: `subagents` / `builtin_subagents` in the
    source config must come back byte-identical so the Config tab mirrors
    agent.json5 and delete/edit can route by the original refs."""
    from olano.runtime.api import _resolve_subagents_for_display

    data = {
        "name": "aurelius",
        "subagents": [{"from_archetype": "nonexistent-archetype-xyz"}],
        "builtin_subagents": ["ops"],
    }

    out = _resolve_subagents_for_display(data)

    # Source lists are untouched (no expansion written back).
    assert out["subagents"] == [{"from_archetype": "nonexistent-archetype-xyz"}]
    assert out["builtin_subagents"] == ["ops"]

    resolved = {s["source"]: s for s in out["subagents_resolved"]}
    # Archetype ref surfaces with its stable delete key (the from_archetype value).
    assert resolved["archetype"]["ref"] == "nonexistent-archetype-xyz"
    assert resolved["archetype"]["from_archetype"] == "nonexistent-archetype-xyz"
    # Built-in OpsManager resolves deterministically (no archetype file needed).
    assert resolved["builtin"]["ref"] == "ops"
    assert resolved["builtin"]["name"] == "OpsManager"


def test_collapse_drops_subagents_resolved():
    """The display-only `subagents_resolved` key must never be persisted."""
    from olano.runtime.api import _collapse_subagent_refs

    data = {
        "name": "aurelius",
        "subagents": [{"from_archetype": "it-admin"}],
        "builtin_subagents": ["ops"],
        "subagents_resolved": [{"name": "IT Admin", "ref": "it-admin"}],
    }

    out = _collapse_subagent_refs(data)

    assert "subagents_resolved" not in out
    # The real source refs survive collapse unchanged.
    assert out["subagents"] == [{"from_archetype": "it-admin"}]
    assert out["builtin_subagents"] == ["ops"]


def test_list_threads_hides_compaction_predecessor(api_client):
    """When a thread is auto-compacted, the runner forks a successor seeded with a
    '[Conversation compacted]\\n\\nPrior thread: <old_id>' entry. The frozen
    predecessor must not surface alongside the live successor as a duplicate."""
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)

    old_id = "11111111-1111-1111-1111-111111111111"
    new_id = "22222222-2222-2222-2222-222222222222"

    (sessions / f"{old_id}.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-03T00:00:00Z",
                "role": "user",
                "content": "so what's the price of bitcoin",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sessions / f"{new_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-06-03T00:01:00Z",
                        "role": "system",
                        "content": f"[Conversation compacted]\n\nPrior thread: {old_id}\n\nsummary",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-06-03T00:02:00Z",
                        "role": "user",
                        "content": "so what's the price of bitcoin",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    resp = api_client.get("/api/agents/test_agent/threads")
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert new_id in ids
    assert old_id not in ids


def test_get_thread_reads_gzipped_session(api_client):
    """Housekeeping compresses old sessions to <thread>.jsonl.gz; the dashboard
    detail endpoint must transparently decompress them instead of 404ing."""
    import gzip
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "44444444-4444-4444-4444-444444444444"
    lines = (
        "\n".join(
            [
                json.dumps(
                    {"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "hello there"}
                ),
                json.dumps({"ts": "2026-06-03T00:00:01Z", "role": "assistant", "content": "hi"}),
            ]
        )
        + "\n"
    )
    (sessions / f"{tid}.jsonl.gz").write_bytes(gzip.compress(lines.encode("utf-8")))

    resp = api_client.get(f"/api/agents/test_agent/threads/{tid}")
    assert resp.status_code == 200
    assert [m["content"] for m in resp.json()["messages"]] == ["hello there", "hi"]


def test_get_thread_never_attaches_media_named_only_in_a_tool_result(api_client, tmp_workspace):
    """A file a TOOL named is not a hand-off — see `media_refs.SCAN_TOOL_RESULTS`.

    The client folds a tool result's media into the assistant message, so this
    used to put a card under the answer for every path an intermediate tool
    printed: reading a document that cites a path attached that document's
    subject. Only the agent's own reply hands a file over now, and the reload
    has to agree with the live stream or a refresh would grow cards the turn
    never showed.
    """
    import json

    from olano.config.home import get_olano_paths

    shots = tmp_workspace / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    img = shots / "shot.png"
    img.write_bytes(b"\x89PNG")

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "66666666-6666-6666-6666-666666666666"
    lines = (
        "\n".join(
            [
                json.dumps(
                    {"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "screenshot pls"}
                ),
                json.dumps(
                    {
                        "ts": "2026-06-03T00:00:01Z",
                        "role": "tool_result",
                        "name": "page_screenshot",
                        "content": f"page_screenshot: Screenshot saved: {img} (0.1 KB)",
                    }
                ),
                json.dumps({"ts": "2026-06-03T00:00:02Z", "role": "assistant", "content": "done"}),
            ]
        )
        + "\n"
    )
    (sessions / f"{tid}.jsonl").write_text(lines, encoding="utf-8")

    resp = api_client.get(f"/api/agents/test_agent/threads/{tid}")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    tool_results = [m for m in messages if m["role"] == "tool_result"]
    assert tool_results, "tool_result message missing"
    assert "media" not in tool_results[0]
    # …and nothing else in the turn grew a card from it either: the assistant
    # row says "done" and names no file.
    assert not any(m.get("media") for m in messages)


def test_get_thread_attaches_media_for_files_named_in_the_reply(api_client, tmp_workspace):
    """The agent's own `Saved: …` line is enough: a reopened thread shows the
    chart inline and the spreadsheet as a card, without a tool result naming it."""
    import json

    from olano.config.home import get_olano_paths

    reports = tmp_workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "accounts.png").write_bytes(b"\x89PNG")
    (reports / "accounts.csv").write_text("a,b\n", encoding="utf-8")

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "77777777-7777-7777-7777-777777777777"
    row = {
        "ts": "2026-06-03T00:00:02Z",
        "role": "assistant",
        "content": "Here you go.\n\nSaved: reports/accounts.png\nSaved: reports/accounts.csv",
        "segments": [
            {"text": "Here you go.", "after": None},
            {"text": "Saved: reports/accounts.png\nSaved: reports/accounts.csv", "after": "c1"},
        ],
    }
    (sessions / f"{tid}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    resp = api_client.get(f"/api/agents/test_agent/threads/{tid}")
    assert resp.status_code == 200
    msg = resp.json()["messages"][0]
    assert msg["role"] == "assistant"
    kinds = {m["path"]: m["kind"] for m in msg["media"]}
    assert kinds == {"reports/accounts.png": "image", "reports/accounts.csv": "document"}
    assert msg["media"][0]["files_url"] == "/files?path=reports/accounts.png&agent=test_agent"
    # The ordered segments ride through the projection for interleaved rendering.
    assert msg["segments"][1]["after"] == "c1"


def test_chat_stream_survives_a_reply_that_looks_like_a_path(api_client, mock_runtime):
    """A finished answer must never be undone by the scan that decorates it.

    Media detection runs on the agent's own reply, and the token regex matches
    money as readily as filenames: "13 open at ~$2.115M" used to reach
    ``Path.expanduser()`` as user ``$2.115M``, which raises. The turn therefore
    ended with ``Error: Could not determine home directory.`` under a correct,
    fully delivered answer — and the agent, whose row was already written, had
    no record of it to answer for.
    """
    runner = mock_runtime._runners["test_agent"]

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "18 closed won at $3.645M, 13 open at ~$2.115M.")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "pipeline", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [f["type"] for f in frames]
    assert "error" not in kinds, [f for f in frames if f["type"] == "error"]
    assert "done" in kinds
    runner.record_turn_error.assert_not_called()


def test_chat_stream_hands_an_error_to_the_agents_next_turn(api_client, mock_runtime):
    """The error frame is drawn client-side and the assistant row precedes it, so
    without this the agent's own history says the turn went fine."""
    runner = mock_runtime._runners["test_agent"]

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "Working on it")
            raise RuntimeError("Upstream ended the stream.")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "hi", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(f["type"] == "error" for f in frames)

    runner.record_turn_error.assert_called_once()
    args, kwargs = runner.record_turn_error.call_args
    # The classified, user-facing message — never the traceback — and the stage
    # that says the visible reply is truncated rather than absent or complete.
    assert "Upstream ended the stream." in args[1]
    assert kwargs["stage"] == "partial"


def test_chat_stream_emits_one_media_frame_for_files_named_in_the_reply(
    api_client, mock_runtime, tmp_workspace
):
    reports = tmp_workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "chart.png").write_bytes(b"\x89PNG")
    runner = mock_runtime._runners["test_agent"]

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "Done.\n\nSaved: ")
            yield ("text", "reports/chart.png")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "chart", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [f["type"] for f in frames]
    assert "media" in kinds
    # One frame, after the text and before `done` — the path was split across
    # two chunks, so detection must run on the joined reply.
    assert kinds.index("media") > kinds.index("text")
    assert kinds.index("media") < kinds.index("done")
    media = next(f for f in frames if f["type"] == "media")
    assert media["role"] == "assistant"
    assert [m["path"] for m in media["items"]] == ["reports/chart.png"]


def test_chat_stream_never_attaches_a_file_only_a_tool_result_named(
    api_client, mock_runtime, tmp_workspace
):
    """The regression this policy exists for.

    An agent read a skill reference whose prose said "read
    `system/knowledge/USER_GUIDE.md` via `workspace_context`". The path was a
    citation inside a file's contents, not a deliverable — and the 257 KB user
    guide was attached to an answer about something else entirely.
    """
    knowledge = tmp_workspace / "system" / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "USER_GUIDE.md").write_text("# guide\n", encoding="utf-8")
    runner = mock_runtime._runners["test_agent"]

    def fake_events(payload, **kwargs):
        async def gen():
            yield (
                "tool_result",
                {
                    "id": "c1",
                    "name": "read_file",
                    "content": (
                        "     8\tFor anything not covered here, read "
                        "`system/knowledge/USER_GUIDE.md` via `workspace_context`."
                    ),
                },
            )
            yield ("text", "Here is the plan.")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "build it", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert not [f for f in frames if f["type"] == "media"]
    tool_results = [f for f in frames if f["type"] == "tool_result"]
    assert tool_results, "tool_result frame missing"
    assert "media" not in tool_results[0]


def test_chat_stream_attaches_a_file_once_however_often_the_reply_names_it(
    api_client, mock_runtime, tmp_workspace
):
    """One card per file, whatever mix of spellings the final message uses."""
    reports = tmp_workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    chart = reports / "chart.png"
    chart.write_bytes(b"\x89PNG")
    runner = mock_runtime._runners["test_agent"]

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "The chart is `reports/chart.png`, also at ")
            yield ("text", f"{chart} and /workspace/reports/chart.png.\n\n")
            yield ("text", "Saved: reports/chart.png")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post("/api/chat/stream", json={"message": "chart", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    media = [f for f in frames if f["type"] == "media"]
    assert len(media) == 1
    items = media[0]["items"]
    assert [m["path"] for m in items] == ["reports/chart.png"]
    # Every spelling folded into the one descriptor rather than a second card.
    assert len(items[0]["mentions"]) >= 3


def test_files_raw_download_and_hardening_headers(api_client, tmp_workspace):
    reports = tmp_workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "chart.png").write_bytes(b"\x89PNG")
    (reports / "page.html").write_text("<script>1</script>", encoding="utf-8")

    # No `agent` query: the fixture pins the default workspace to tmp_workspace.
    plain = api_client.get("/api/files-raw/reports/chart.png")
    assert plain.status_code == 200
    assert plain.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in plain.headers
    assert "attachment" not in plain.headers.get("content-disposition", "")

    dl = api_client.get("/api/files-raw/reports/chart.png?download=1")
    assert "attachment" in dl.headers["content-disposition"]

    html = api_client.get("/api/files-raw/reports/page.html")
    assert html.headers["content-security-policy"] == "sandbox"


def test_list_threads_includes_gzipped_session(api_client):
    """A compressed-only thread must still appear in the chat rail."""
    import gzip
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "55555555-5555-5555-5555-555555555555"
    lines = (
        json.dumps({"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "archived q"}) + "\n"
    )
    (sessions / f"{tid}.jsonl.gz").write_bytes(gzip.compress(lines.encode("utf-8")))

    resp = api_client.get("/api/agents/test_agent/threads")
    assert resp.status_code == 200
    assert tid in {t["id"] for t in resp.json()}


def test_list_threads_strips_directives_from_the_row_label(api_client):
    """The rail title must be the human's words, not the sender tag that
    ``platforms/session.py`` concatenates onto every inbound platform message.

    ``preview`` stays raw on the wire (back-compat for older clients); only the
    new ``title`` is cleaned.
    """
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "agent:main:telegram:dm:5550001234"
    (sessions / f"{tid}.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-03T00:00:00Z",
                "role": "user",
                "content": "[from @jrivera (Jamie, id=5550001234)]: can you check the deadline",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["title"] == "can you check the deadline"
    assert row["origin"] == "interactive"
    assert row["channel"] == "telegram"
    assert row["sender"] == "Jamie"
    assert row["preview"].startswith("[from @jrivera")


def test_list_threads_names_a_scheduled_run_by_its_task(api_client):
    """Identical autonomous preambles must resolve to distinct task names —
    otherwise every scheduled run is an indistinguishable rail row."""
    import json
    from datetime import datetime, timezone

    from olano.config.home import get_olano_paths
    from olano.scheduler.db import SchedulerDB

    task_id = "1a2b3c4d-1111-4000-8000-000000000000"
    with SchedulerDB()._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, name, prompt, schedule, enabled, "
            "agent_name, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                task_id,
                "Daily SICOP tender sweep",
                "sweep",
                "0 6 * * *",
                1,
                "test_agent",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = f"scheduled_test_agent_{task_id[:8]}@20260603T060000-ab12"
    (sessions / f"{tid}.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-03T06:00:00Z",
                "role": "user",
                "content": "[Autonomous scheduled task — execute directly and completely. "
                "Do NOT ask the user for confirmation or approval before acting.]\n\nsweep",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["title"] == "Daily SICOP tender sweep"
    assert row["origin"] == "scheduled"


def test_list_threads_keeps_a_manual_rename_winning(api_client):
    """A user-set name in <thread>.meta.json must outrank the derived title."""
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "77777777-7777-7777-7777-777777777777"
    (sessions / f"{tid}.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "raw words"}) + "\n",
        encoding="utf-8",
    )
    api_client.patch(f"/api/agents/test_agent/threads/{tid}", json={"name": "Pinned name"})

    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["name"] == "Pinned name"
    assert row["title"] == "raw words"


def test_delete_thread_purges_checkpoint(api_client, monkeypatch):
    """Deleting a thread must purge BOTH the JSONL files and the LangGraph
    checkpoint in threads.db, or the conversation lingers as an orphaned
    checkpoint that threadcost keeps counting."""
    import json

    from olano.config.home import get_olano_paths

    calls: list[tuple[str, str]] = []

    async def fake_adelete(name, thread_id, home=None):
        calls.append((name, thread_id))

    monkeypatch.setattr("olano.core.checkpoints.adelete_thread", fake_adelete)

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "66666666-6666-6666-6666-666666666666"
    (sessions / f"{tid}.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "x"}) + "\n",
        encoding="utf-8",
    )

    resp = api_client.delete(f"/api/agents/test_agent/threads/{tid}")
    assert resp.status_code == 200
    assert not (sessions / f"{tid}.jsonl").exists()
    assert calls == [("test_agent", tid)]


def test_compacted_successor_carries_forward_cost(api_client):
    """An auto-compaction successor's checkpoint has no usage (mined cost ~0), so the
    runner records the predecessor lineage's accumulated cost as an `inherited_*`
    baseline in `<id>.state.json`. The rail (list_threads) and header (get_thread)
    must add that baseline so the conversation isn't shown as free."""
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)

    tid = "33333333-3333-3333-3333-333333333333"
    (sessions / f"{tid}.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-06-03T00:01:00Z",
                        "role": "system",
                        "content": "[Conversation compacted]\n\nPrior thread: old\n\nsummary",
                    }
                ),
                json.dumps({"ts": "2026-06-03T00:02:00Z", "role": "user", "content": "continue"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The runner writes this sidecar at compaction time; the thread's own mined cost
    # is 0 (no usage metadata on its rebuilt checkpoint).
    (sessions / f"{tid}.state.json").write_text(
        json.dumps(
            {
                "inherited_input_tokens": 1000,
                "inherited_output_tokens": 200,
                "inherited_total_tokens": 1200,
                "inherited_usd": 0.05,
            }
        ),
        encoding="utf-8",
    )

    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["total_tokens"] == 1200
    assert row["usd"] == pytest.approx(0.05)
    assert row["input_tokens"] == 1000

    detail = api_client.get(f"/api/agents/test_agent/threads/{tid}").json()
    assert detail["total_tokens"] == 1200
    assert detail["usd"] == pytest.approx(0.05)


def test_delete_subagent_by_archetype_ref(api_client, monkeypatch):
    """Deleting via the archetype ref (the dashboard card's delete key) removes
    the `from_archetype` stub even though it has no `name` field — the bug behind
    'subagent IT Admin not found'."""
    import olano.runtime.api as api_mod

    store = {
        "name": "aurelius",
        "model": "anthropic:claude-haiku-4-5-20251001",
        "subagents": [{"from_archetype": "it-admin"}],
        "builtin_subagents": ["ops"],
    }

    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: store)
    written: dict = {}

    def _fake_write(name, data, create=False):
        written.update(data)
        return None

    monkeypatch.setattr(api_mod, "_write_agent_config", _fake_write)

    resp = api_client.delete("/api/agents/aurelius/subagents/it-admin")

    assert resp.status_code == 200
    assert written["subagents"] == []
    # The built-in subagent is left untouched.
    assert written["builtin_subagents"] == ["ops"]


# ── on-demand thread compaction (/compact slash command) ────────────────────


def test_compact_thread_summarizes(api_client, mock_runtime):
    """POST .../compact asks the agent to run compact_conversation; reports compacted=True."""
    runner = mock_runtime._runners["test_agent"]
    runner.compact_thread = AsyncMock(return_value=True)

    resp = api_client.post("/api/agents/test_agent/threads/thread-1/compact")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "compacted": True}
    runner.compact_thread.assert_awaited_once_with("thread-1")


def test_compact_thread_reports_post_compaction_fill(api_client, mock_runtime, monkeypatch):
    """The chat header's context bar is the last STREAMED turn's fill and a
    compaction streams nothing, so the endpoint reports the estimated
    post-compaction size (static footprint + effective messages) for the
    client to seed the bar with — otherwise /context kept printing the old
    number and /compact read as a no-op."""
    from olano.runtime import context_report

    runner = mock_runtime._runners["test_agent"]
    runner.compact_thread = AsyncMock(return_value=True)
    runner.estimate_thread_context_tokens = AsyncMock(return_value=12_000)
    monkeypatch.setattr(
        context_report,
        "build_context_report",
        lambda name: {"model": "x", "categories": [{"key": "system_prompt", "tokens": 3_000}]},
    )

    resp = api_client.post("/api/agents/test_agent/threads/thread-1/compact")

    assert resp.status_code == 200
    body = resp.json()
    assert body["compacted"] is True
    assert body["messages_tokens"] == 12_000
    assert body["context_tokens"] == 15_000
    runner.estimate_thread_context_tokens.assert_awaited_once_with("thread-1")


def test_compact_thread_estimate_failure_is_not_a_failure(api_client, mock_runtime):
    """The estimate is a nicety: a runner that cannot read its state still
    reports the compaction itself."""
    runner = mock_runtime._runners["test_agent"]
    runner.compact_thread = AsyncMock(return_value=True)
    runner.estimate_thread_context_tokens = AsyncMock(side_effect=RuntimeError("no state"))

    resp = api_client.post("/api/agents/test_agent/threads/thread-1/compact")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "compacted": True}


def test_compact_thread_nothing_to_do(api_client, mock_runtime):
    """When there's nothing to compact, reports compacted=False."""
    runner = mock_runtime._runners["test_agent"]
    runner.compact_thread = AsyncMock(return_value=False)

    resp = api_client.post("/api/agents/test_agent/threads/thread-1/compact")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "compacted": False}


def test_compact_thread_unknown_agent(api_client):
    resp = api_client.post("/api/agents/ghost/threads/thread-1/compact")
    assert resp.status_code == 404


# ── Analytics / Insights ─────────────────────────────────────────────────────


def test_analytics_overview_empty(api_client):
    resp = api_client.get("/api/analytics/overview?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 7
    assert data["usage"]["totals"]["calls"] == 0
    assert len(data["usage"]["daily"]) == 7
    # Stats are fully local by design — the payload never carries a
    # cloud-fetched block, and the combined local figure is always present.
    assert "cloud_credits" not in data
    assert data["credits_summary"] == {"model": 0.0, "services": 0.0, "total": 0.0}


def test_analytics_overview_credits_summary_combines_both_ledgers(api_client):
    """The headline figure = managed model credits (usage ledger) + managed
    service credits (service ledger), computed locally — the box records every
    olano-billed call itself, so no cloud fetch is needed for stats."""
    from olano.analytics import service_ledger, usage_ledger

    usage_ledger.record_usage_event("merli", "olano:pro", 1000, 100, 0.0, credits=5.5)
    usage_ledger.record_usage_event("merli", "anthropic:claude-sonnet-4-6", 500, 50, 0.01)
    service_ledger.record_service_event(
        "vision", provider="olano", managed=True, agent="merli", credits=1.5
    )

    data = api_client.get("/api/analytics/overview?days=7").json()
    assert data["credits_summary"] == {"model": 5.5, "services": 1.5, "total": 7.0}
    assert "cloud_credits" not in data
    # Hourly window carries the same combined shape.
    hourly = api_client.get("/api/analytics/overview?hours=24").json()
    assert hourly["credits_summary"]["total"] == 7.0
    # The managed-tier split rides the same payload ("credits by turbo/pro/max").
    tiers = {r["tier"]: r for r in data["usage"]["by_tier"]}
    assert tiers["pro"]["credits"] == 5.5 and tiers["pro"]["calls"] == 1
    assert "anthropic:claude-sonnet-4-6" not in tiers  # BYOK never becomes a tier


def test_analytics_overview_shape(api_client):
    # Cost/usage is now sourced from the thread checkpoint store (threads.db) so it
    # matches the drill-down; with no checkpoints in the test home the usage rollup
    # is empty but the payload must still be well-formed (usage/tools/a2a present).
    # The cost math itself is covered by tests/test_threadcost.py.
    resp = api_client.get("/api/analytics/overview?days=30")
    assert resp.status_code == 200
    data = resp.json()
    for section in ("usage", "tools", "a2a"):
        assert section in data
    totals = data["usage"]["totals"]
    for key in ("usd", "input_tokens", "output_tokens", "total_tokens", "calls"):
        assert key in totals
    assert isinstance(data["usage"]["daily"], list)


def test_analytics_overview_hourly(api_client):
    # ?hours=24 is the dashboard's "1 day" view: a rolling last-24h window with an
    # hourly usage series sourced from the per-call usage ledger.
    resp = api_client.get("/api/analytics/overview?hours=24")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_hours"] == 24
    assert data["window_days"] == 1
    assert "T" in data["since"]  # full ISO timestamp, not a bare day
    assert data["usage"]["source"] == "usage_ledger"
    assert len(data["usage"]["hourly"]) == 24
    assert data["usage"]["daily"] == []
    for section in ("usage", "tools", "a2a"):
        assert section in data
    # Out-of-range values clamp instead of erroring.
    assert api_client.get("/api/analytics/overview?hours=9999").json()["window_hours"] == 168


def test_analytics_journal_list_and_day(api_client):
    resp = api_client.get("/api/analytics/journal")
    assert resp.status_code == 200
    body = resp.json()
    today = body["today"]
    assert today in body["days"]  # list endpoint builds today

    day_resp = api_client.get(f"/api/analytics/journal/{today}")
    assert day_resp.status_code == 200
    assert day_resp.json()["date"] == today


def test_analytics_journal_bad_date(api_client):
    resp = api_client.get("/api/analytics/journal/not-a-date")
    assert resp.status_code == 400


def test_scope_journal_entry_recomputes_both_billing_books():
    """A member's journal day must recompute BOTH money columns from their own
    rows. `credits` is spread in from the fleet-wide metrics, so leaving it out
    of the recompute would show a member the whole fleet's credit charge next to
    their own dollar figure."""
    from olano.runtime.api import _scope_journal_entry

    entry = {
        "date": "2026-08-13",
        "metrics": {
            "usd": 10.0,
            "credits": 900.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "calls": 30,
            "tool_calls": 9,
            "a2a_messages": 3,
            "active_agents": ["merli", "scout"],
        },
        "by_agent": [
            {
                "agent": "merli",
                "usd": 1.5,
                "credits": 100.0,
                "calls": 10,
                "tool_calls": 4,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "a2a_sent": 1,
                "a2a_received": 0,
            },
            {
                "agent": "scout",
                "usd": 8.5,
                "credits": 800.0,
                "calls": 20,
                "tool_calls": 5,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "a2a_sent": 2,
                "a2a_received": 1,
            },
        ],
        "by_model": [{"model": "olano:pro", "usd": 0.0, "credits": 900.0}],
        "events": {"cognition_actions": [{"action": "x", "summary": "y", "ts": ""}], "cortex": []},
        "narrative": None,
    }

    scoped = _scope_journal_entry(entry, {"merli"})
    assert [r["agent"] for r in scoped["by_agent"]] == ["merli"]
    assert scoped["metrics"]["usd"] == 1.5
    assert scoped["metrics"]["credits"] == 100.0
    assert scoped["metrics"]["calls"] == 10
    assert scoped["by_model"] == []
    # An admin (allowed is None) keeps the fleet-wide figures untouched.
    assert _scope_journal_entry(entry, None)["metrics"]["credits"] == 900.0


def test_analytics_highlights_latest(api_client):
    resp = api_client.get("/api/analytics/highlights/latest")
    assert resp.status_code == 200
    body = resp.json()
    # Always reports today + a (possibly null) narrative for the daily digest.
    assert "today" in body
    assert "narrative" in body
    assert "date" in body


def test_catalog_skills_includes_learned(api_client, tmp_workspace):
    # Learned skills live two levels deep at skills/learned/<slug>/SKILL.md.
    # discover_skills recurses, so scanning skills_dir surfaces them too — guard
    # against a regression where /skills shows "No skills available".
    from olano.config.workspace import Workspace

    ws = Workspace(root=tmp_workspace)
    for base, name in [(ws.skills_dir, "alpha"), (ws.learned_skills_dir, "beta")]:
        d = base / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {name}\n\nDoes {name}.\n", encoding="utf-8")

    resp = api_client.get("/api/catalog/skills")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["skills"]}
    assert "alpha" in names
    assert "beta" in names  # learned skill must be surfaced


def test_catalog_skills_scoped_to_agent_workspace(api_client):
    # An agent's skills live under its own per-agent workspace
    # (<home>/agents/<name>/workspace), NOT the global default workspace. The
    # endpoint must resolve config.workspace when ?agent= is passed; otherwise
    # the chat /skills slash command shows "No skills available" for every agent
    # that uses a per-agent workspace (the common case). Regression guard.
    import json

    from olano.config.home import get_olano_paths

    agents_dir = get_olano_paths().agents_dir
    agent_dir = agents_dir / "skilled-agent"
    agent_ws = agent_dir / "workspace"
    learned = agent_ws / "skills" / "learned" / "gamma"
    learned.mkdir(parents=True)
    (learned / "SKILL.md").write_text("# gamma\n\nDoes gamma.\n", encoding="utf-8")
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "skilled-agent",
                "model": "anthropic:claude-haiku-4-5-20251001",
                "workspace": str(agent_ws),
            }
        ),
        encoding="utf-8",
    )

    # Scoped: the per-agent learned skill is surfaced.
    resp = api_client.get("/api/catalog/skills?agent=skilled-agent")
    assert resp.status_code == 200
    assert "gamma" in {s["name"] for s in resp.json()["skills"]}

    # Unscoped: scans only the global default workspace, which never holds the
    # agent's per-agent skills — so gamma is absent. This is exactly the gap the
    # ?agent= param closes.
    resp = api_client.get("/api/catalog/skills")
    assert "gamma" not in {s["name"] for s in resp.json()["skills"]}

    # Unknown agent falls back to the global listing (no crash, no 4xx).
    resp = api_client.get("/api/catalog/skills?agent=does-not-exist")
    assert resp.status_code == 200


def test_catalog_skills_reports_and_persists_the_per_skill_switch(api_client):
    # The Skills tab's on/off switch: a disabled skill must stay LISTED and be
    # marked, because a listing that hid it would leave nothing to switch back
    # on. The write goes through the ordinary agent PUT — `disabled_skills` is a
    # plain config field, not a bespoke endpoint.
    import json

    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    agent_dir = paths.agents_dir / "switcher"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps({"name": "switcher", "model": "anthropic:claude-haiku-4-5-20251001"}),
        encoding="utf-8",
    )
    for name in ("kept", "dropped"):
        d = paths.agent_workspace_dir("switcher") / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n\nBody.\n", encoding="utf-8"
        )

    body = api_client.get("/api/catalog/skills?agent=switcher").json()
    assert {s["name"]: s["enabled"] for s in body["skills"]} == {"kept": True, "dropped": True}
    assert body["disabled_skills"] == []

    saved = api_client.put("/api/agents/switcher", json={"disabled_skills": ["dropped"]})
    assert saved.status_code == 200, saved.text

    body = api_client.get("/api/catalog/skills?agent=switcher").json()
    assert {s["name"]: s["enabled"] for s in body["skills"]} == {"kept": True, "dropped": False}
    assert body["disabled_skills"] == ["dropped"]

    # The detail view re-derives the same answer rather than trusting the row
    # that opened it.
    location = next(s["location"] for s in body["skills"] if s["name"] == "dropped")
    detail = api_client.get(
        "/api/catalog/skills/detail", params={"location": location, "agent": "switcher"}
    )
    assert detail.json()["skill"]["enabled"] is False


# ── Skill detail / file browsing (dashboard skill viewer) ────────────────────


def _seed_browsable_skills(tmp_workspace):
    """One managed (``<home>/skills``) and one workspace skill, with bundled files."""
    from olano.config.workspace import Workspace

    ws = Workspace(root=tmp_workspace)

    managed = ws.managed_skills_dir / "common-helper"
    managed.mkdir(parents=True, exist_ok=True)
    (managed / "SKILL.md").write_text(
        "---\nname: common-helper\ndescription: Shared by every agent.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (managed / "reference.md").write_text("# Reference\n", encoding="utf-8")

    own = ws.skills_dir / "own-skill"
    (own / "scripts").mkdir(parents=True, exist_ok=True)
    (own / "SKILL.md").write_text(
        "---\nname: own-skill\ndescription: This agent's own.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (own / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return managed, own


def test_catalog_skills_annotates_scope_and_editability(api_client, tmp_workspace):
    # The list view badges each row without a detail request per skill, so scope
    # + editability ride along on the listing itself.
    managed, own = _seed_browsable_skills(tmp_workspace)

    body = api_client.get("/api/catalog/skills").json()
    by_name = {s["name"]: s for s in body["skills"]}

    # The managed pack is shared by every agent, so an agent-scoped page never
    # offers to edit it — regardless of where the home dir sits (this fixture
    # nests it *inside* the workspace, which on a real box it never is).
    assert by_name["common-helper"]["scope"] == "managed"
    assert by_name["common-helper"]["editable"] is False

    assert by_name["own-skill"]["scope"] == "workspace"
    assert by_name["own-skill"]["editable"] is True
    assert by_name["own-skill"]["workspace_path"] == "skills/own-skill"


def test_catalog_skills_annotates_learned_scope(api_client, tmp_workspace):
    # learned/ is nested inside skills/, so a shallowest-wins classification
    # would label every learned skill "workspace".
    from olano.config.workspace import Workspace

    ws = Workspace(root=tmp_workspace)
    learned = ws.learned_skills_dir / "picked-up"
    learned.mkdir(parents=True, exist_ok=True)
    (learned / "SKILL.md").write_text("# picked-up\n\nLearned.\n", encoding="utf-8")

    body = api_client.get("/api/catalog/skills").json()
    row = next(s for s in body["skills"] if s["name"] == "picked-up")
    assert row["scope"] == "learned"
    assert row["editable"] is True


def test_catalog_skill_detail_reads_managed_skill_outside_the_workspace(api_client, tmp_workspace):
    # The reason this endpoint exists: <home>/skills is unreachable through
    # /api/files (it resolves outside the workspace root), so without this the
    # dashboard can list a common skill but never show what it says.
    managed, _own = _seed_browsable_skills(tmp_workspace)

    resp = api_client.get(
        "/api/catalog/skills/detail", params={"location": str(managed / "SKILL.md")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill"]["name"] == "common-helper"
    assert body["skill"]["scope"] == "managed"
    assert body["skill"]["editable"] is False
    assert "Body." in body["content"]["content"]
    # Bundled files travel with the skill so the viewer can offer them.
    assert {f["path"] for f in body["files"]} == {"SKILL.md", "reference.md"}


def test_catalog_skill_detail_lists_nested_files(api_client, tmp_workspace):
    _managed, own = _seed_browsable_skills(tmp_workspace)

    body = api_client.get(
        "/api/catalog/skills/detail", params={"location": str(own / "SKILL.md")}
    ).json()
    paths = {f["path"]: f for f in body["files"]}
    assert paths["scripts"]["type"] == "directory"
    assert paths["scripts/run.py"]["type"] == "file"
    assert paths["scripts/run.py"]["kind"] == "text"
    assert body["skill"]["workspace_path"] == "skills/own-skill"


def test_catalog_skill_detail_refuses_paths_outside_skill_roots(api_client, tmp_workspace):
    # `location` is caller-supplied. Anything not under a directory this agent
    # already loads skills from is refused — otherwise the viewer is a general
    # file reader for the whole box.
    _seed_browsable_skills(tmp_workspace)
    outside = tmp_workspace / "secrets"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "SKILL.md").write_text("# nope\n", encoding="utf-8")

    resp = api_client.get(
        "/api/catalog/skills/detail", params={"location": str(outside / "SKILL.md")}
    )
    assert resp.status_code == 403

    # Only a file literally named SKILL.md identifies a skill; accepting any
    # filename would make every path under a skills dir readable as a "skill".
    from olano.config.workspace import Workspace

    ws = Workspace(root=tmp_workspace)
    (ws.skills_dir / "own-skill" / ".env").write_text("TOKEN=shh\n", encoding="utf-8")
    resp = api_client.get(
        "/api/catalog/skills/detail",
        params={"location": str(ws.skills_dir / "own-skill" / ".env")},
    )
    assert resp.status_code == 403

    # A skills ROOT is not itself a skill.
    resp = api_client.get(
        "/api/catalog/skills/detail", params={"location": str(ws.skills_dir / "SKILL.md")}
    )
    assert resp.status_code == 403


def test_catalog_skill_file_reads_and_refuses_traversal(api_client, tmp_workspace):
    _managed, own = _seed_browsable_skills(tmp_workspace)
    location = str(own / "SKILL.md")

    resp = api_client.get(
        "/api/catalog/skills/file", params={"location": location, "path": "scripts/run.py"}
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "print('hi')\n"

    # `..` must not walk out of the skill folder.
    (tmp_workspace / "MEMORY.md").write_text("secret\n", encoding="utf-8")
    resp = api_client.get(
        "/api/catalog/skills/file",
        params={"location": location, "path": "../../MEMORY.md"},
    )
    assert resp.status_code == 403


def test_catalog_skill_file_raw_serves_bytes(api_client, tmp_workspace):
    _managed, own = _seed_browsable_skills(tmp_workspace)
    # The image previews in the detail view point at this endpoint.
    (own / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    resp = api_client.get(
        "/api/catalog/skills/file-raw",
        params={"location": str(own / "SKILL.md"), "path": "shot.png"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")

    # A binary file is described, never inlined, by the JSON endpoint.
    body = api_client.get(
        "/api/catalog/skills/file", params={"location": str(own / "SKILL.md"), "path": "shot.png"}
    ).json()
    assert body["kind"] == "image"
    assert body["content"] is None


# ── Headless Google OAuth (Connections page) ─────────────────────────────────


class _FakeGoogleCreds:
    def to_json(self):
        return '{"token": "abc", "refresh_token": "r", "scopes": []}'


class _FakeGoogleFlow:
    """Network-free stand-in for google_auth_oauthlib.flow.Flow."""

    def __init__(self):
        self.credentials = _FakeGoogleCreds()

    @classmethod
    def from_client_secrets_file(cls, path, scopes=None, redirect_uri=None):
        return cls()

    @classmethod
    def from_client_config(cls, client_config, scopes=None, redirect_uri=None):
        cls.captured_config = client_config
        return cls()

    def authorization_url(self, **kwargs):
        return ("https://accounts.google.com/o/oauth2/auth?state=xyz", "xyz")

    def fetch_token(self, code=None, **kwargs):
        # Real Flow.fetch_token forwards kwargs (e.g. timeout) to requests.
        pass


def test_google_oauth_start_returns_link_not_browser(api_client, monkeypatch):
    """The dashboard flow returns an authorization URL instead of opening a
    server-side browser (the headless-server fix)."""
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeGoogleFlow)
    from olano.tools import google_tools as gt

    gt._GOOGLE_AUTH_SESSIONS.clear()
    gt._GOOGLE_AUTH_BY_STATE.clear()

    resp = api_client.post(
        "/api/auth/google/start",
        json={
            "creds_path": "/tmp/client_secret.json",
            "redirect_base": "https://host.example",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "awaiting_authorization"
    assert body["authorization_url"].startswith("https://accounts.google.com")
    assert body["redirect_uri"] == "https://host.example/api/auth/google/callback"
    session_id = body["session_id"]

    # Poll returns the pending session.
    poll = api_client.get(f"/api/auth/google/poll/{session_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "awaiting_authorization"

    # The OAuth redirect lands on the callback and completes the flow.
    cb = api_client.get("/api/auth/google/callback", params={"state": "xyz", "code": "code-1"})
    assert cb.status_code == 200
    assert "Authorization received" in cb.text

    poll2 = api_client.get(f"/api/auth/google/poll/{session_id}")
    assert poll2.json()["status"] == "authorized"


def test_google_oauth_start_with_client_pair(api_client, monkeypatch):
    """Google takes the same {"creds": {client_id, client_secret}} start body
    as every other connector — no client_secret.json involved."""
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeGoogleFlow)
    from olano.tools import google_tools as gt

    gt._GOOGLE_AUTH_SESSIONS.clear()
    gt._GOOGLE_AUTH_BY_STATE.clear()

    resp = api_client.post(
        "/api/auth/google/start",
        json={
            "creds": {"client_id": "panel-id", "client_secret": "panel-secret"},
            "redirect_base": "https://host.example",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "awaiting_authorization"
    assert _FakeGoogleFlow.captured_config["web"]["client_id"] == "panel-id"


def test_google_oauth_start_missing_creds_returns_400(api_client, monkeypatch):
    for name in (
        "GOOGLE_OAUTH_CREDENTIALS",
        "GOOGLE_OAUTH_CLIENT",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    from olano.tools import google_tools as gt

    gt._GOOGLE_AUTH_SESSIONS.clear()
    gt._GOOGLE_AUTH_BY_STATE.clear()

    resp = api_client.post("/api/auth/google/start", json={"redirect_base": "https://host.example"})
    assert resp.status_code == 400
    assert "no OAuth client configured" in resp.json()["detail"]


def test_google_oauth_poll_unknown_session_404(api_client):
    resp = api_client.get("/api/auth/google/poll/does-not-exist")
    assert resp.status_code == 404


# ── Global system config (config.yaml) ────────────────────────────────────────


def test_system_config_get_returns_defaults(api_client):
    resp = api_client.get("/api/system/config")
    assert resp.status_code == 200
    data = resp.json()
    # HomeConfig + cognition-model fields are surfaced.
    for key in (
        "default_agent",
        "preferred_model",
        "runtime_api_port",
        "dashboard_port",
        "multi_user_enabled",
        "extra_bind_roots",
        "archetype_dirs",
        "egress_allow",
        "egress_deny",
        "fast_model",
        "deep_model",
        "config_path",
        "restart_required",
    ):
        assert key in data
    assert data["runtime_api_port"] == 8765
    assert "runtime_api_port" in data["restart_required"]


def test_system_config_put_persists_and_preserves_cognition(api_client):
    # Pre-seed a cognition block so we can prove the HomeConfig write preserves it.
    from olano.config.system_settings import load_system_settings, set_cognition_feature

    set_cognition_feature("pulse", False)

    resp = api_client.put(
        "/api/system/config",
        json={
            "default_agent": "test_agent",
            "preferred_model": "anthropic:claude-sonnet-4-6",
            "runtime_api_port": 9100,
            "fast_model": "anthropic:claude-haiku-4-5",
            "deep_model": "",
            "extra_bind_roots": ["/srv/data", "  "],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["preferred_model"] == "anthropic:claude-sonnet-4-6"
    assert data["runtime_api_port"] == 9100
    assert data["deep_model"] is None
    # Blank entries dropped from list fields.
    assert data["extra_bind_roots"] == ["/srv/data"]
    # The cognition toggle set earlier survived the HomeConfig merge-write.
    assert load_system_settings().features.pulse is False


def test_system_config_put_rejects_bad_port(api_client):
    resp = api_client.put("/api/system/config", json={"runtime_api_port": 70000})
    assert resp.status_code == 400


def test_agent_context_endpoint(api_client, tmp_workspace):
    """GET /api/agents/{name}/context returns the estimated usage breakdown."""
    import json

    from olano.config.home import get_olano_paths

    agent_dir = get_olano_paths().agents_dir / "ctxagent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "ctxagent",
                "model": "anthropic:claude-haiku-4-5-20251001",
                "system_prompt": "You are a test assistant for the context breakdown.",
                "workspace": str(tmp_workspace),
            }
        ),
        encoding="utf-8",
    )

    resp = api_client.get("/api/agents/ctxagent/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "anthropic:claude-haiku-4-5-20251001"
    keys = {c["key"] for c in data["categories"]}
    assert keys == {
        "system_prompt",
        "system_tools",
        "subagents",
        "memory_files",
        "skills",
        "mcp_tools",
    }
    # System tools are always present (universal + ops defaults). MCP tools are
    # priced from the live connect summary; this agent is not running, so
    # nothing is bound and the bucket is empty — never "on-demand" (they are
    # bound into every request when a server is connected).
    by_key = {c["key"]: c for c in data["categories"]}
    assert by_key["system_tools"]["tokens"] > 0
    assert by_key["mcp_tools"] == {"key": "mcp_tools", "tokens": 0, "count": 0, "servers": 0}


def test_agent_context_endpoint_unknown_agent(api_client):
    resp = api_client.get("/api/agents/does-not-exist/context")
    assert resp.status_code == 404


# ── Commands catalog + per-agent command config ─────────────────────────────


def test_catalog_commands_endpoint(api_client):
    resp = api_client.get("/api/catalog/commands")
    assert resp.status_code == 200
    data = resp.json()
    groups = {g["id"] for g in data["groups"]}
    assert {"info", "conversation", "sensitive"} <= groups
    by_name = {c["name"]: c for c in data["commands"]}
    # sensitive commands ship default-off; safe ones default-on
    assert by_name["trust"]["sensitive"] is True
    assert by_name["trust"]["default_exposed"] is False
    assert by_name["help"]["default_exposed"] is True
    # aliases are surfaced for the editor's collision check
    assert "models" in by_name["model"]["aliases"]


def test_put_commands_config_round_trips(api_client, monkeypatch):
    """The Commands tab saves via PUT /api/agents/{name}; the `commands` block
    must survive AgentUpdateRequest + AgentConfig validation and reach disk."""
    import olano.runtime.api as api_mod

    store = {"name": "cmd_agent", "model": "anthropic:claude-haiku-4-5-20251001"}
    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: dict(store))
    written: dict = {}
    monkeypatch.setattr(
        api_mod, "_write_agent_config", lambda name, data, create=False: written.update(data)
    )

    resp = api_client.put(
        "/api/agents/cmd_agent",
        json={
            "commands": {
                "enabled": {"trust": True, "memory": False},
                "custom": [
                    {"name": "standup", "prompt": "Summarize today. {args}", "description": "Daily"}
                ],
            }
        },
    )
    assert resp.status_code == 200, resp.text
    assert written["commands"]["enabled"] == {"trust": True, "memory": False}
    assert written["commands"]["custom"][0]["name"] == "standup"


def test_put_cognition_config_round_trips(api_client, monkeypatch):
    """The Overview Cortex toggle + Autonomy per-agent scope save via
    PUT /api/agents/{name}; the `cognition` block must survive AgentUpdateRequest
    + AgentConfig validation, reach disk, and be honored by the resolvers."""
    import olano.runtime.api as api_mod
    from olano.config.agent_config import AgentConfig
    from olano.config.system_settings import cortex_enabled, feature_enabled, set_cortex_master

    # The per-feature half needs a running engine, which is off by default. Set
    # here, not by the cortex_on fixture: api_client repoints LIONCLAW_HOME.
    set_cortex_master(True)

    store = {"name": "cog_agent", "model": "anthropic:claude-haiku-4-5-20251001"}
    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: dict(store))
    written: dict = {}
    monkeypatch.setattr(
        api_mod, "_write_agent_config", lambda name, data, create=False: written.update(data)
    )

    # Master off → Cortex disabled entirely for this agent.
    resp = api_client.put("/api/agents/cog_agent", json={"cognition": {"enabled": False}})
    assert resp.status_code == 200, resp.text
    assert written["cognition"]["enabled"] is False
    assert cortex_enabled(AgentConfig(**written)) is False

    # Per-feature override in isolation: engine stays on, only brainstorming off.
    written.clear()
    resp = api_client.put("/api/agents/cog_agent", json={"cognition": {"brainstorming": False}})
    assert resp.status_code == 200, resp.text
    merged = AgentConfig(**written)
    assert cortex_enabled(merged) is True
    assert feature_enabled("brainstorming", merged) is False
    assert feature_enabled("pulse", merged) is True


def test_cognition_status_exposes_overrides(api_client):
    """GET /api/cognition/status returns the raw per-agent overrides the UI needs
    to render the three-state Inherit/On/Off controls."""
    from olano.config.system_settings import FEATURES

    resp = api_client.get("/api/cognition/status?agent=test_agent")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    ov = payload["overrides"]
    # A fresh agent inherits EVERYTHING, the master switch included — every
    # override is None. `enabled` used to report True here, which is the raw
    # value of a field that had no way to say "inherit".
    assert ov["enabled"] is None
    assert ov["autonomous"] is None
    assert ov["auto_approve"] is None
    assert ov["intensity"] is None
    assert set(ov["features"]) == set(FEATURES)
    assert all(v is None for v in ov["features"].values())

    # The resolved column says what the agent actually runs with, and the
    # defaults column is what it inherits from: on a fresh box the engine is off
    # and the cadence is relaxed, so an inheriting agent resolves to off.
    assert payload["defaults"]["enabled"] is False
    assert payload["resolved"]["enabled"] is False
    assert payload["defaults"]["intensity"] == "relaxed"
    assert payload["resolved"]["intensity"] == "relaxed"
    assert set(payload["defaults"]["features"]) == set(FEATURES)
    assert payload["resolved"]["features"] == payload["features"]


def test_cognition_status_exposes_the_worker_budget_in_all_three_tiers(api_client):
    """The step budget renders as a two-tier row like every other knob, so the
    payload has to carry the agent's override, the fleet default, the resolved
    answer AND the legal range the UI validates against."""
    payload = api_client.get("/api/cognition/status?agent=test_agent").json()

    # A fresh agent inherits — the field that used to be a plain int, and so
    # could never say "inherit" at all.
    assert payload["overrides"]["worker_max_iterations"] is None
    assert payload["overrides"]["worker_timeout_seconds"] is None
    assert payload["defaults"]["worker_max_iterations"] == 100
    assert payload["defaults"]["worker_timeout_seconds"] == 1200
    assert payload["resolved"]["worker_max_iterations"] == 100
    assert payload["resolved"]["worker_timeout_seconds"] == 1200

    # Bounds come from the backend so the UI never keeps its own copy to drift.
    bounds = payload["worker_budget_bounds"]
    assert bounds["max_iterations_min"] == 10
    assert bounds["max_iterations_max"] == 400
    assert bounds["timeout_seconds_min"] == 30
    assert bounds["timeout_seconds_max"] == 7200


def test_setting_the_fleet_wide_budget_reaches_an_inheriting_agent(api_client):
    """The whole point of the new tier: raise it once, every agent without an
    opinion of its own picks it up. A plain int made this impossible."""
    resp = api_client.post(
        "/api/cognition/autonomy",
        json={"worker_max_iterations": 250, "worker_timeout_seconds": 3000},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["worker_max_iterations"] == 250

    payload = api_client.get("/api/cognition/status?agent=test_agent").json()
    assert payload["overrides"]["worker_max_iterations"] is None  # still no opinion
    assert payload["resolved"]["worker_max_iterations"] == 250
    assert payload["resolved"]["worker_timeout_seconds"] == 3000


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"worker_max_iterations": 5}, "between 10 and 400"),
        ({"worker_max_iterations": 5000}, "between 10 and 400"),
        ({"worker_timeout_seconds": 1}, "between 30 and 7200"),
        ({"worker_max_iterations": "lots"}, "whole number"),
    ],
)
def test_the_fleet_wide_budget_rejects_rather_than_silently_clamps(api_client, body, expected):
    """This is a person typing into a form: a number that quietly becomes a
    different number is how a knob loses its user's trust. (The loader still
    clamps a hand-edited config.yaml — there is nobody there to tell.)"""
    resp = api_client.post("/api/cognition/autonomy", json=body)
    assert resp.status_code == 400
    assert expected in resp.json()["detail"]


def _patch_cognition_store(monkeypatch, initial: dict | None = None) -> dict:
    """Back PATCH/PUT with an in-memory agent.json5 the test can inspect."""
    import olano.runtime.api as api_mod

    store = {"name": "test_agent", "model": "anthropic:claude-haiku-4-5-20251001"}
    store.update(initial or {})
    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: dict(store))

    def _write(name, data, create=False):
        store.clear()
        store.update(data)

    monkeypatch.setattr(api_mod, "_write_agent_config", _write)
    return store


def test_patch_cognition_merges_one_field(api_client, monkeypatch):
    """The knob endpoint takes a delta, not the whole block."""
    from olano.config.system_settings import set_cortex_master

    set_cortex_master(True)  # a feature resolves on only with the engine on
    store = _patch_cognition_store(monkeypatch, {"cognition": {"intensity": "relaxed"}})

    resp = api_client.patch("/api/agents/test_agent/cognition", json={"idea_generation": True})
    assert resp.status_code == 200, resp.text
    # The field it was told about changed; the one it wasn't told about survived.
    assert store["cognition"] == {"intensity": "relaxed", "idea_generation": True}
    body = resp.json()
    assert body["overrides"]["features"]["idea_generation"] is True
    assert body["overrides"]["intensity"] == "relaxed"
    assert body["resolved"]["features"]["idea_generation"] is True


def test_patch_cognition_null_clears_the_override(api_client, monkeypatch):
    """Inherit is expressed by removing the key, not by storing null — a field
    that reads `null` in agent.json5 looks set when it isn't."""
    store = _patch_cognition_store(monkeypatch, {"cognition": {"pulse": False, "autonomous": True}})

    resp = api_client.patch("/api/agents/test_agent/cognition", json={"pulse": None})
    assert resp.status_code == 200, resp.text
    assert "pulse" not in store["cognition"]
    assert store["cognition"]["autonomous"] is True
    assert resp.json()["overrides"]["features"]["pulse"] is None


def test_patch_cognition_rejects_unknown_fields(api_client, monkeypatch):
    store = _patch_cognition_store(monkeypatch)
    resp = api_client.patch("/api/agents/test_agent/cognition", json={"ideation": True})
    assert resp.status_code == 400
    assert "ideation" in resp.json()["detail"]
    assert "cognition" not in store, "a rejected patch must not touch the file"


def test_patch_cognition_rejects_empty_body(api_client, monkeypatch):
    _patch_cognition_store(monkeypatch)
    assert api_client.patch("/api/agents/test_agent/cognition", json={}).status_code == 400


async def test_patch_cognition_concurrent_toggles_both_survive(
    mock_runtime, tmp_workspace, monkeypatch
):
    """The lost-update bug, driven concurrently — the only way it reproduces.

    The dashboard used to read the agent's whole config, merge one field
    client-side and PUT the block back. Two toggles clicked in quick succession
    both read the pre-write state, and the second PUT reverted the first one's
    field (verified: clicking idea_generation and brainstorming left only
    idea_generation on disk). Merging server-side under the agent's
    read-modify-write gate makes patches of different keys commute, so both land
    whatever the interleaving.
    """
    import asyncio

    import httpx

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    store = _patch_cognition_store(monkeypatch)

    from olano.runtime.api import create_api

    transport = httpx.ASGITransport(app=create_api(mock_runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        results = await asyncio.gather(
            client.patch("/api/agents/test_agent/cognition", json={"idea_generation": True}),
            client.patch("/api/agents/test_agent/cognition", json={"brainstorming": True}),
            client.patch("/api/agents/test_agent/cognition", json={"intensity": "relaxed"}),
        )
    assert [r.status_code for r in results] == [200, 200, 200]

    cog = store["cognition"]
    assert cog["idea_generation"] is True
    assert cog["brainstorming"] is True, "a concurrent patch reverted another's field"
    assert cog["intensity"] == "relaxed"


def test_patch_cognition_enables_one_agent_against_a_global_off(api_client, monkeypatch):
    """End to end, the arrangement that was unreachable: Cortex off for every
    agent, then on for this one."""
    from olano.config.system_settings import set_autonomy

    _patch_cognition_store(monkeypatch)
    set_autonomy(False, master=False)  # fleet-wide off

    before = api_client.get("/api/cognition/status?agent=test_agent").json()
    assert before["defaults"]["enabled"] is False
    assert before["resolved"]["enabled"] is False

    resp = api_client.patch("/api/agents/test_agent/cognition", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"]["enabled"] is True

    after = api_client.get("/api/cognition/status?agent=test_agent").json()
    assert after["defaults"]["enabled"] is False, "the fleet default is untouched"
    assert after["overrides"]["enabled"] is True
    assert after["resolved"]["enabled"] is True


def test_patch_cognition_does_not_broadcast_a_roster_change(api_client, monkeypatch):
    """A knob click is not a roster change. `agents_update` makes every open
    dashboard tab refetch the whole agent list; `cognition_updated` is scoped to
    the panel that cares."""
    import olano.runtime.api as api_mod

    _patch_cognition_store(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(api_mod, "_push_event", lambda kind, data=None: seen.append(kind))

    assert (
        api_client.patch("/api/agents/test_agent/cognition", json={"pulse": False}).status_code
        == 200
    )
    assert "cognition_updated" in seen
    assert "agents_update" not in seen


def test_patch_cognition_nudges_prompt_refresh_only_when_enabled_flips(
    api_client, mock_runtime, monkeypatch
):
    """`enabled` gates the cortex_* tools, so it moves the prompt. Nothing else
    in the block does, and rebuilding a graph per feature toggle is waste."""
    _patch_cognition_store(monkeypatch)
    nudged: list[list[str]] = []
    mock_runtime.request_prompt_refresh = lambda names=None: nudged.append(list(names or []))

    api_client.patch("/api/agents/test_agent/cognition", json={"pulse": False})
    assert nudged == []

    api_client.patch("/api/agents/test_agent/cognition", json={"enabled": False})
    assert nudged == [["test_agent"]]


def test_cognition_status_reflects_running_agent_toggle(api_client, mock_runtime, monkeypatch):
    """Regression: a per-agent Cortex override saved via PUT /api/agents/{name}
    must be reflected by the live GET /api/cognition/status read.

    The status endpoint resolves a *running* agent from
    ``runtime._runners[name].config`` — the in-memory config captured at boot —
    so a disk-only write left the dashboard reading a stale config. Every
    "This agent" toggle in AutonomyPanel (writeAgentCog → PUT → reload status)
    then snapped back to its previous value and looked frozen. The PUT must
    refresh the running runner's cached cognition so the live read agrees with
    what was just written.
    """
    import olano.runtime.api as api_mod

    store = {"name": "test_agent", "model": "anthropic:claude-haiku-4-5-20251001"}
    monkeypatch.setattr(api_mod, "_read_agent_config", lambda name: dict(store))
    monkeypatch.setattr(api_mod, "_write_agent_config", lambda *a, **k: None)

    # Baseline: the running agent has no per-agent engine override yet, so it
    # inherits (None) the fleet default, which is off.
    base = api_client.get("/api/cognition/status?agent=test_agent")
    assert base.status_code == 200, base.text
    assert base.json()["overrides"]["enabled"] is None
    assert base.json()["resolved"]["enabled"] is False

    # Toggle the per-agent "Cortex engine" ON then OFF, exactly as the
    # AutonomyPanel master switch does. Both writes must be visible on the very
    # next status read for the *running* agent, resolved included: On is the
    # override that runs this one agent through the fleet-wide off.
    for want in (True, False):
        put = api_client.put("/api/agents/test_agent", json={"cognition": {"enabled": want}})
        assert put.status_code == 200, put.text
        status = api_client.get("/api/cognition/status?agent=test_agent").json()
        assert status["overrides"]["enabled"] is want
        assert status["resolved"]["enabled"] is want

    # A three-state override (Autonomous → force On) must land live too.
    put = api_client.put("/api/agents/test_agent", json={"cognition": {"autonomous": True}})
    assert put.status_code == 200, put.text
    ov = api_client.get("/api/cognition/status?agent=test_agent").json()["overrides"]
    assert ov["autonomous"] is True


def test_raw_config_put_cognition_applies_live(api_client, tmp_path, monkeypatch):
    """Same regression as above via the OTHER write path: the raw JSON5 Config
    editor (PUT /api/agents/{name}/config/raw) must also refresh the running
    agent's cached cognition — it used to write the file verbatim and sync
    nothing, so a cognition edit there showed stale until Restart."""
    import olano.config.loader as loader_mod

    cfg_path = tmp_path / "test_agent" / "agent.json5"
    cfg_path.parent.mkdir(parents=True)
    base_cfg = '{"name": "test_agent", "model": "anthropic:claude-haiku-4-5-20251001"'
    cfg_path.write_text(base_cfg + "}", encoding="utf-8")
    monkeypatch.setattr(
        loader_mod,
        "find_agent_config_path",
        lambda name, **kw: cfg_path if name == "test_agent" else None,
    )

    assert (
        api_client.get("/api/cognition/status?agent=test_agent").json()["overrides"]["enabled"]
        is None
    )
    resp = api_client.put(
        "/api/agents/test_agent/config/raw",
        json={"content": base_cfg + ', "cognition": {"enabled": false}}'},
    )
    assert resp.status_code == 200, resp.text
    ov = api_client.get("/api/cognition/status?agent=test_agent").json()["overrides"]
    assert ov["enabled"] is False


def test_put_rejects_custom_command_colliding_with_builtin(api_client, monkeypatch):
    import olano.runtime.api as api_mod

    monkeypatch.setattr(
        api_mod,
        "_read_agent_config",
        lambda name: {"name": "cmd_agent2", "model": "anthropic:claude-haiku-4-5-20251001"},
    )
    monkeypatch.setattr(api_mod, "_write_agent_config", lambda *a, **k: None)
    # "help" is a built-in → must be rejected at the API boundary (422).
    resp = api_client.put(
        "/api/agents/cmd_agent2",
        json={"commands": {"custom": [{"name": "help", "prompt": "x"}]}},
    )
    assert resp.status_code == 422


# ── Generic OAuth connector broker (Connections page) ────────────────────────


def _clear_connector_sessions():
    """Reset the broker's module-global flow registries (mirrors the Google
    tests clearing gt._GOOGLE_AUTH_SESSIONS / _GOOGLE_AUTH_BY_STATE)."""
    from olano.core.connectors import base

    base._SESSIONS.clear()
    base._BY_STATE.clear()
    return base


def test_connector_oauth_start_poll_callback_flow(api_client, monkeypatch):
    """The parameterized /api/auth/{service}/* family drives a broker service
    end-to-end, mirroring the headless Google flow test above."""
    base = _clear_connector_sessions()

    resp = api_client.post(
        "/api/auth/microsoft365/start",
        json={
            "creds": {"client_id": "cid", "client_secret": "cs"},
            "redirect_base": "https://host.example",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_authorization"
    assert body["redirect_uri"] == "https://host.example/api/auth/microsoft365/callback"
    assert body["authorization_url"].startswith("https://login.microsoftonline.com")
    session_id = body["session_id"]

    # Poll returns the pending session.
    poll = api_client.get(f"/api/auth/microsoft365/poll/{session_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "awaiting_authorization"

    # The OAuth redirect lands on the callback and completes the exchange.
    state = base._SESSIONS[session_id].state
    monkeypatch.setattr(
        base,
        "_post_token",
        lambda url, data, *, headers=None, auth=None: {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        },
    )
    cb = api_client.get("/api/auth/microsoft365/callback", params={"state": state, "code": "abc"})
    assert cb.status_code == 200
    assert "Authorization received" in cb.text

    poll2 = api_client.get(f"/api/auth/microsoft365/poll/{session_id}")
    assert poll2.json()["status"] == "authorized"

    from olano.config.home import get_olano_paths

    assert get_olano_paths().connector_token_path("microsoft365").exists()


def test_auth_status_lists_google_and_every_connector_service(api_client):
    from olano.catalog import connectors_registry

    connectors_registry.invalidate_cache()  # in-process cache outlives tmp homes
    resp = api_client.get("/api/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    expected = {
        "google",
        "buffer",
        "docusign",
        "fitbit",
        "github",
        "linkedin",
        "meta",
        "microsoft365",
        "quickbooks",
        "reddit",
        "salesforce",
        "shopify",
        "x_twitter",
    }
    assert expected.issubset(body.keys())
    for service in expected:
        assert "connected" in body[service]
    # No connectors catalog + no relay on a bare box → nothing official, but
    # the key is present on every generic connector so the UI can branch.
    for service in expected - {"google"}:
        assert body[service]["official_available"] is False
        # Every broker card says how it can be connected.
        assert "oauth" in body[service]["methods"]
    # google (BYO-only in v1) never carries the key; it is OAuth-only.
    assert "official_available" not in body["google"]
    assert body["google"]["methods"] == ["oauth"]
    # The GitHub CLI card is gone: GitHub is one card with two sign-in methods.
    assert "github-cli" not in body
    assert "device" in body["github"]["methods"]


def test_auth_status_official_available_gates_on_catalog_relay_and_shape(api_client, monkeypatch):
    from olano.core.mcp_relay import RelayConfig

    monkeypatch.setattr(
        "olano.catalog.connectors_registry.official_ids",
        lambda: frozenset({"microsoft365", "meta"}),
    )
    monkeypatch.setattr(
        "olano.core.connect_relay.resolve_connect_relay_config",
        lambda: RelayConfig(url="https://relay.example", secret="s", instance_id="i"),
    )
    body = api_client.get("/api/auth/status").json()
    assert body["microsoft365"]["official_available"] is True
    # meta has a catalog row here but a GET token method — not relay-shaped.
    assert body["meta"]["official_available"] is False
    # salesforce is relay-shaped but has no catalog row.
    assert body["salesforce"]["official_available"] is False

    # Relay creds missing (URL set, nothing to sign with) → nothing official.
    def broken():
        raise RuntimeError("no relay credential")

    monkeypatch.setattr("olano.core.connect_relay.resolve_connect_relay_config", broken)
    body = api_client.get("/api/auth/status").json()
    assert body["microsoft365"]["official_available"] is False


def test_connector_official_start_runs_relay_mode(api_client, monkeypatch):
    """Empty creds + official client → relay-mode session: the relay's
    callback replaces the box redirect (redirect_base is ignored)."""
    from olano.core.connectors import base
    from olano.core.mcp_relay import RelayConfig

    _clear_connector_sessions()
    monkeypatch.setattr(
        "olano.catalog.connectors_registry.official_client",
        lambda pid: "off-cid" if pid == "microsoft365" else None,
    )
    monkeypatch.setattr(
        "olano.core.connect_relay.resolve_connect_relay_config",
        lambda: RelayConfig(url="https://relay.example", secret="s", instance_id="i"),
    )
    registered: list[str] = []
    monkeypatch.setattr(
        "olano.core.connect_relay.register_connect_state",
        lambda cfg, state, **kw: registered.append(state),
    )
    monkeypatch.setattr(base, "_relay_pump", lambda provider, session: None)

    resp = api_client.post(
        "/api/auth/microsoft365/start",
        json={"creds": {}, "redirect_base": "https://host.example"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auth_mode"] == "relay"
    assert body["redirect_uri"] == "https://relay.example/connect/callback"
    assert "client_id=off-cid" in body["authorization_url"]
    assert registered  # the state was announced before the URL was handed out


def test_auth_status_kicks_connectors_freshness(api_client, monkeypatch):
    """Opening Connections nudges a stale connectors catalog to refresh.

    The kick itself is a no-op stub under the hermetic conftest pin; this
    asserts the endpoint calls the seam (fire-and-forget, response unaffected).
    """
    from olano.catalog import connectors_registry

    calls: list[bool] = []
    monkeypatch.setattr(
        connectors_registry, "kick_refresh_if_stale", lambda *a, **k: calls.append(True) or False
    )
    resp = api_client.get("/api/auth/status")
    assert resp.status_code == 200
    assert calls == [True]


def test_catalog_status_reports_connectors(api_client):
    from olano.catalog import connectors_registry

    connectors_registry.invalidate_cache()  # in-process cache outlives tmp homes
    resp = api_client.get("/api/catalog/status")
    assert resp.status_code == 200
    connectors = resp.json()["catalogs"]["connectors"]
    # Bare box: bundled-empty catalog — the diagnosable signature.
    assert connectors["official_count"] == 0
    assert "revision" in connectors and "source" in connectors


def test_connector_disconnect_and_share_endpoints(api_client):
    from olano.config.home import get_olano_paths
    from olano.core.connectors import base, is_connector_shared

    token_path = get_olano_paths().connector_token_path("microsoft365")
    base._save_record({"access_token": "at", "expires_at": None}, token_path)

    resp = api_client.delete("/api/auth/microsoft365")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "removed": True, "agent": None}
    assert not token_path.exists()

    share = api_client.patch("/api/auth/microsoft365/share", json={"agent": "a1", "shared": True})
    assert share.status_code == 200
    assert share.json() == {"ok": True, "agent": "a1", "shared": True}
    assert is_connector_shared("microsoft365", "a1") is True


def test_connector_unknown_service_routes_404(api_client):
    assert api_client.post("/api/auth/nope/start", json={}).status_code == 404
    assert api_client.get("/api/auth/nope/poll/x").status_code == 404
    assert api_client.delete("/api/auth/nope").status_code == 404
    # The fixed /api/auth/me route family is not shadowed by {service} routes:
    # "me" is not a registered provider, so its /start is a plain 404 too.
    assert api_client.post("/api/auth/me/start", json={}).status_code == 404


def test_device_routes_serve_only_providers_declaring_a_code(api_client, monkeypatch):
    """`/api/auth/{service}/device/*` is generic: GitHub declares a device
    recipe, Microsoft 365 does not, and an unknown service is a plain 404."""
    from olano.core.connectors import device as dev

    monkeypatch.setattr(
        dev,
        "_request_device_code",
        lambda provider: {
            "user_code": "ABCD-1234",
            "device_code": "dc-1",
            "verification_url": "https://github.com/login/device",
            "interval": 5.0,
            "expires_in": 900.0,
        },
    )
    monkeypatch.setattr(dev, "_poll_for_approval", lambda session, provider: None)

    start = api_client.post("/api/auth/github/device/start", json={})
    assert start.status_code == 200, start.text
    session = start.json()
    assert session["user_code"] == "ABCD-1234"
    assert session["service"] == "github"
    assert session["status"] == "pending"

    poll = api_client.get(f"/api/auth/github/device/poll/{session['session_id']}")
    assert poll.status_code == 200
    assert poll.json()["verification_url"].endswith("/login/device")
    # A session id is scoped to its service.
    assert (
        api_client.get(f"/api/auth/microsoft365/device/poll/{session['session_id']}").status_code
        == 400
    )
    assert api_client.get("/api/auth/github/device/poll/unknown").status_code == 404

    cancel = api_client.post(f"/api/auth/github/device/cancel/{session['session_id']}")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    # No device recipe → actionable 400, not a silent OAuth redirect.
    no_code = api_client.post("/api/auth/microsoft365/device/start", json={})
    assert no_code.status_code == 400
    assert "sign-in code" in no_code.json()["detail"]
    assert api_client.post("/api/auth/nope/device/start", json={}).status_code == 404


# ── chat cost surfaces: rail + header show MINED cost (not just inherited) ─────


def _seed_costed_thread(tid: str, agent: str = "test_agent") -> None:
    """Write a real checkpoint (usage-bearing AIMessage) + JSONL for one thread."""
    import json
    import sqlite3

    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver

    from olano.config.home import get_olano_paths
    from olano.core.checkpoints import scope_thread

    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{tid}.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(get_olano_paths().thread_db_path))
    saver = SqliteSaver(conn)
    saver.setup()
    chk = empty_checkpoint()
    chk["channel_values"]["messages"] = [
        AIMessage(
            content="hello",
            usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
            response_metadata={"model_name": "anthropic:claude-sonnet-4-6"},
        )
    ]
    chk["channel_versions"]["messages"] = "1"
    saver.put(
        {"configurable": {"thread_id": scope_thread(agent, tid), "checkpoint_ns": ""}},
        chk,
        {},
        {"messages": "1"},
    )
    conn.close()


def test_thread_rail_and_header_show_mined_cost(api_client):
    """The threads-history panel (cache-fed) and the conversation header restore
    (live single-thread mine) must both carry the checkpoint-mined usd/tokens."""
    tid = "44444444-4444-4444-4444-444444444444"
    _seed_costed_thread(tid)

    # Header restore first: GET thread detail mines the one thread live — it must
    # show cost even before the threadcost cache has ever been warmed.
    detail = api_client.get(f"/api/agents/test_agent/threads/{tid}").json()
    assert detail["total_tokens"] == 1100
    assert detail["input_tokens"] == 1000
    assert detail["usd"] > 0

    # Rail: cache-only. Warm it the way the dashboard does (an Insights visit),
    # then the thread row must carry the same mined numbers.
    status = api_client.get("/api/analytics/threadcost/agents").json()["index_status"]
    assert status["errors"] == 0
    rows = api_client.get("/api/agents/test_agent/threads").json()
    row = next(t for t in rows if t["id"] == tid)
    assert row["total_tokens"] == 1100
    assert row["usd"] == pytest.approx(detail["usd"])


def test_threadcost_endpoints_accept_a_days_window(api_client):
    """The drill-down's 1/7/30/90-day selector: every ranking endpoint takes
    `?days=`, a freshly-seeded thread is inside every window, and omitting the
    param keeps the pre-window "all history" behaviour."""
    tid = "66666666-6666-6666-6666-666666666666"
    _seed_costed_thread(tid)

    def _agent_usd(query: str) -> float:
        rows = api_client.get(f"/api/analytics/threadcost/agents{query}").json()["agents"]
        return next((a["usd"] for a in rows if a["agent"] == "test_agent"), 0.0)

    all_usd = _agent_usd("")
    assert all_usd > 0
    # The thread was just written, so it sits inside every trailing window.
    for days in (1, 7, 30, 90):
        assert _agent_usd(f"?days={days}") == pytest.approx(all_usd)
    # days=0 / garbage → all history, not an empty window.
    assert _agent_usd("?days=0") == pytest.approx(all_usd)

    threads = api_client.get("/api/analytics/threadcost/agents/test_agent/threads?days=30").json()[
        "threads"
    ]
    assert any(t["thread_id"].endswith(tid) for t in threads)

    for path in ("/api/analytics/threadcost/origins", "/api/analytics/threadcost/tools"):
        assert api_client.get(f"{path}?days=7").status_code == 200


def test_threadcost_days_param_reaches_the_service(api_client, monkeypatch):
    """Every trailing window contains "now", so the behavioural test above can't
    tell a plumbed `days` from a dropped one — assert the value the routes
    actually hand the service (0/garbage → None = all history)."""
    from olano.analytics import threadcost

    seen: dict[str, object] = {}

    def _spy(name, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return {"agents": [], "totals": {}, "index_status": {}, "threads": [], "total": 0}

    monkeypatch.setattr(
        threadcost, "agents_ranked", lambda **kw: _spy("agents_ranked", **kw), raising=True
    )
    monkeypatch.setattr(threadcost, "threads_ranked", lambda name, **kw: _spy(name, **kw))
    monkeypatch.setattr(threadcost, "origins", lambda **kw: _spy("origins", **kw))
    monkeypatch.setattr(threadcost, "tools_rollup", lambda a, t, **kw: _spy("tools", **kw))

    api_client.get("/api/analytics/threadcost/agents?days=7")
    assert seen["days"] == 7
    api_client.get("/api/analytics/threadcost/agents")  # omitted → all history
    assert seen["days"] is None
    api_client.get("/api/analytics/threadcost/agents?days=0")
    assert seen["days"] is None
    api_client.get("/api/analytics/threadcost/agents?days=99999")  # clamped to the max
    assert seen["days"] == 366

    api_client.get("/api/analytics/threadcost/agents/test_agent/threads?days=90")
    assert seen["days"] == 90
    api_client.get("/api/analytics/threadcost/origins?days=30")
    assert seen["days"] == 30
    api_client.get("/api/analytics/threadcost/tools?days=1")
    assert seen["days"] == 1


def test_threadcost_diagnostics_endpoint(api_client):
    """The diagnostics endpoint reports backend/store health and a live read probe
    that confirms checkpointed AI messages carry minable usage_metadata."""
    tid = "55555555-5555-5555-5555-555555555555"
    _seed_costed_thread(tid)

    diag = api_client.get("/api/analytics/threadcost/diagnostics").json()
    assert diag["backend"] == "sqlite"
    assert diag["store_available"] is True
    assert diag["checkpoint_threads"] >= 1
    probe = diag["probe"]
    assert probe["ok"] is True and probe["error"] is None
    assert probe["ai_messages"] >= 1
    assert probe["ai_with_usage"] >= 1


# ── A2A conversations: /api/a2a/threads + /api/a2a/activity filters ──────────


def _write_a2a(events: list[dict], *, base="2026-07-16T10:00:0"):
    """Seed the A2A log. Each event may carry ``_ts``; otherwise events are
    stamped a second apart from ``base`` so ordering is deterministic."""
    import json as _json

    from olano.analytics.a2a_log import a2a_log_path

    path = a2a_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, data in enumerate(events):
        data = dict(data)
        ts = data.pop("_ts", f"{base}{i}+00:00")
        lines.append(_json.dumps({"type": "agent_message", "data": data, "ts": ts}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a2a_threads_folds_both_directions_into_one_peer_conversation(api_client):
    """The whole point of the pair rail: marcus→valentina and valentina→marcus
    are one conversation, not two rows — and casing never forks them."""
    _write_a2a(
        [
            {"kind": "peer", "phase": "send", "from": "marcus", "to": "valentina", "message": "a"},
            {"kind": "peer", "phase": "send", "from": "Valentina", "to": "Marcus", "message": "b"},
            {"kind": "peer", "phase": "send", "from": "marcus", "to": "valentina", "message": "c"},
        ]
    )
    threads = api_client.get("/api/a2a/threads").json()
    assert len(threads) == 1
    row = threads[0]
    assert row["key"] == "peer:marcus::valentina"
    assert row["kind"] == "peer"
    assert {row["a"], row["b"]} == {"marcus", "valentina"}
    assert row["messages"] == 3
    assert row["last_preview"] == "c"


def test_a2a_threads_keep_subagent_delegations_directional(api_client):
    """``to`` is a subagent *type*, not an agent — two parents both delegating
    to ``researcher`` must stay separate rows, and must not fold with a peer
    conversation between the same names."""
    _write_a2a(
        [
            {
                "kind": "subagent",
                "phase": "send",
                "from": "marcus",
                "to": "researcher",
                "message": "dig",
                "tool_call_id": "t1",
            },
            {
                "kind": "subagent",
                "phase": "send",
                "from": "valentina",
                "to": "researcher",
                "message": "dig",
                "tool_call_id": "t2",
            },
            {"kind": "peer", "phase": "send", "from": "researcher", "to": "marcus", "message": "x"},
        ]
    )
    threads = api_client.get("/api/a2a/threads").json()
    keys = {t["key"] for t in threads}
    assert keys == {
        "subagent:marcus::researcher",
        "subagent:valentina::researcher",
        "peer:marcus::researcher",
    }


def test_a2a_threads_report_in_flight_and_failed_sends(api_client):
    """A failed talk_to_agent publishes no reply frame at all, so an unanswered
    send must age out of "thinking" into "no reply" instead of pulsing forever.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=30)).isoformat()
    ancient = (now - timedelta(hours=3)).isoformat()
    _write_a2a(
        [
            # answered
            {
                "kind": "peer",
                "phase": "send",
                "from": "marcus",
                "to": "ana",
                "message": "q1",
                "_ts": ancient,
            },
            {
                "kind": "peer",
                "phase": "reply",
                "from": "marcus",
                "to": "ana",
                "message": "q1",
                "reply": "a1",
                "ts_sent": "2026-07-16T10:00:00+00:00",
                "ts_reply": "2026-07-16T10:00:02+00:00",
                "_ts": ancient,
            },
            # never answered, long ago → failed
            {
                "kind": "peer",
                "phase": "send",
                "from": "marcus",
                "to": "ana",
                "message": "q2",
                "_ts": ancient,
            },
            # just sent → genuinely in flight
            {
                "kind": "peer",
                "phase": "send",
                "from": "marcus",
                "to": "ana",
                "message": "q3",
                "_ts": recent,
            },
        ]
    )
    row = api_client.get("/api/a2a/threads").json()[0]
    assert row["messages"] == 3
    assert row["in_flight"] == 1
    assert row["no_reply"] == 1
    assert row["avg_ms"] == 2000


def test_a2a_reply_matching_does_not_cross_concurrent_exchanges(api_client):
    """Two callers with open sends to the same recipient: the reply carries the
    original ``message`` verbatim, so it must close *its own* send. Matching on
    "most recent open send to this recipient" would close kevin's."""
    _write_a2a(
        [
            {"kind": "peer", "phase": "send", "from": "marcus", "to": "ana", "message": "from-m"},
            {"kind": "peer", "phase": "send", "from": "kevin", "to": "ana", "message": "from-k"},
            {
                "kind": "peer",
                "phase": "reply",
                "from": "marcus",
                "to": "ana",
                "message": "from-m",
                "reply": "answered-m",
            },
        ]
    )
    rows = {t["key"]: t for t in api_client.get("/api/a2a/threads").json()}

    def still_open(row):
        return row["in_flight"] + row["no_reply"]

    # marcus's exchange closed; kevin's is still open.
    assert still_open(rows["peer:ana::marcus"]) == 0
    assert still_open(rows["peer:ana::kevin"]) == 1


def test_a2a_activity_filters(api_client):
    _write_a2a(
        [
            {"kind": "peer", "phase": "send", "from": "marcus", "to": "ana", "message": "alpha"},
            {"kind": "peer", "phase": "send", "from": "kevin", "to": "ana", "message": "beta"},
            {
                "kind": "subagent",
                "phase": "send",
                "from": "marcus",
                "to": "researcher",
                "message": "gamma",
                "tool_call_id": "t1",
            },
        ]
    )

    def msgs(url):
        return [e["data"]["message"] for e in api_client.get(url).json()]

    assert msgs("/api/a2a/activity") == ["alpha", "beta", "gamma"]
    assert msgs("/api/a2a/activity?kind=peer") == ["alpha", "beta"]
    assert msgs("/api/a2a/activity?kind=subagent") == ["gamma"]
    # a single participant → every exchange touching them
    assert msgs("/api/a2a/activity?a=marcus") == ["alpha", "gamma"]
    # both participants → only exchanges between the two
    assert msgs("/api/a2a/activity?a=marcus&b=ana") == ["alpha"]
    # participants are matched case-insensitively, like the pair keys
    assert msgs("/api/a2a/activity?a=MARCUS&b=Ana") == ["alpha"]
    assert msgs("/api/a2a/activity?q=BETA") == ["beta"]
    assert msgs("/api/a2a/activity?since=2026-07-16T10:00:01+00:00") == ["beta", "gamma"]
    assert msgs("/api/a2a/activity?before=2026-07-16T10:00:01+00:00") == ["alpha"]
    assert msgs("/api/a2a/activity?limit=1") == ["gamma"]


def test_a2a_activity_limit_applies_after_filtering(api_client):
    """Regression: the old handler tail-sliced raw lines and *then* filtered, so
    a narrow filter returned a fraction of the page the caller asked for."""
    events = []
    for i in range(60):
        events.append(
            {"kind": "peer", "phase": "send", "from": "marcus", "to": "ana", "message": f"m{i}"}
        )
        events.append(
            {"kind": "peer", "phase": "send", "from": "kevin", "to": "zoe", "message": f"k{i}"}
        )
    _write_a2a(events)
    page = api_client.get("/api/a2a/activity?a=marcus&limit=50").json()
    assert len(page) == 50
    assert all(e["data"]["from"] == "marcus" for e in page)


def test_a2a_threads_empty_when_no_log(api_client):
    assert api_client.get("/api/a2a/threads").json() == []


# ── Avatar image backends ────────────────────────────────────────────────────
# The picker behind agent avatar / character generation. Olano credits are the
# product default, so they must lead the list and be the marked default on a
# managed box; gpt-image-1 is retired and gpt-image-2 superseded.


def test_avatar_models_lists_olano_first(api_client, monkeypatch):
    from olano.tools import image_tools as it

    monkeypatch.setattr(it, "_olano_image_available", lambda: True)
    models = api_client.get("/api/avatar/models").json()["models"]

    assert models[0]["id"] == "olano:olano-image"
    assert models[0]["available"] is True
    assert models[0]["default"] is True
    # Exactly one default, or the UI has to guess.
    assert [m["id"] for m in models if m.get("default")] == ["olano:olano-image"]


def test_avatar_models_offer_the_openai_default_and_never_a_retired_one(api_client):
    """The picker offers the SAME OpenAI model an unpinned agent call would
    use, so the dashboard and the tools cannot disagree about what "OpenAI"
    means on this box. Superseded models stay in the model catalog (spend rows
    must still price them) but are not offered here."""
    from olano.tools.image_tools import DEFAULT_OPENAI_IMAGE_MODEL

    ids = [m["id"] for m in api_client.get("/api/avatar/models").json()["models"]]

    assert f"openai:{DEFAULT_OPENAI_IMAGE_MODEL}" in ids
    assert not any("gpt-image-1" in i for i in ids)
    assert not any(i == "openai:gpt-image-2" for i in ids)


def test_openai_image_default_is_flare(api_client):
    """The one place the literal is written down, so moving the default is a
    deliberate edit here rather than something the derived assertions absorb."""
    from olano.tools.image_tools import DEFAULT_OPENAI_IMAGE_MODEL

    assert DEFAULT_OPENAI_IMAGE_MODEL == "gpt-image-2.5-flare"


def test_avatar_models_fall_back_to_openai_when_unmanaged(api_client, monkeypatch):
    from olano.tools import image_tools as it

    monkeypatch.setattr(it, "_olano_image_available", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    models = api_client.get("/api/avatar/models").json()["models"]

    olano = next(m for m in models if m["provider"] == "olano")
    assert olano["available"] is False
    assert "cloud.olano.ai" in olano["reason"]
    # With no gateway to bill through, the customer's own account is the default.
    assert [m["id"] for m in models if m.get("default")] == [
        f"openai:{it.DEFAULT_OPENAI_IMAGE_MODEL}"
    ]


def test_status_reports_image_gen_from_credits_alone(api_client, monkeypatch):
    """The 'Generate cartoon' button is gated on this flag. Before the Olano
    image service it meant 'has an OpenAI key', which greyed the button out on
    exactly the managed boxes where generation is free."""
    from olano.tools import image_tools as it

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: True)
    caps = api_client.get("/api/status").json()["capabilities"]

    assert caps["image_gen"] is True
    assert caps["openai_image_gen"] is True  # deprecated alias, same value


def test_status_image_gen_false_on_a_bare_box(api_client, monkeypatch):
    from olano.tools import image_tools as it

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(it, "_olano_image_available", lambda: False)
    assert api_client.get("/api/status").json()["capabilities"]["image_gen"] is False


# ── Tool catalog: brokered credentials count as satisfied ─────────────────────


def test_catalog_tools_marks_connector_supplied_credentials(api_client, monkeypatch):
    """A credential a connected OAuth connector supplies must not read as missing.

    `get_credential` resolves the broker *above* the vault and the environment,
    so a brokered-only name (MS365_ACCESS_TOKEN once Microsoft 365 is connected)
    appears in neither store — the catalog used to paint every such tool "missing
    keys" while the tool worked fine.
    """
    from olano.core import connectors

    monkeypatch.setattr(
        connectors, "exposed_credential_names", lambda: frozenset({"MS365_ACCESS_TOKEN"})
    )
    monkeypatch.setattr(
        connectors,
        "resolve_exposed_credential",
        lambda name: "live-graph-token" if name == "MS365_ACCESS_TOKEN" else None,
    )

    tools = api_client.get("/api/catalog/tools").json()["tools"]
    ms365 = next(t for t in tools if t["name"] == "ms365_send_mail")
    specs = {s["name"]: s for s in ms365["requirements"]["env_vars"]}

    assert specs["MS365_ACCESS_TOKEN"]["in_connector"] is True
    # The broker is a separate source from the vault and the environment.
    assert specs["MS365_ACCESS_TOKEN"]["in_env"] is False
    assert specs["MS365_ACCESS_TOKEN"]["in_vault"] is False
    # A name no connector exposes still reports absent, so the flag is meaningful.
    other = next(
        (s for s in specs.values() if s["name"] != "MS365_ACCESS_TOKEN"),
        None,
    )
    if other is not None:
        assert other["in_connector"] is False


# ── thread provenance ────────────────────────────────────────────────────────


def _write_thread(tid: str, rows: list[dict]) -> None:
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{tid}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def test_list_threads_uses_the_stamped_channel(api_client):
    """A platform conversation is persisted under the bare platform chat id, so
    nothing in the file name says "WhatsApp". The channel the runner stamped
    from the live request context is the only thing that does."""
    tid = "5215512345678"
    _write_thread(
        tid,
        [
            {
                "ts": "2026-06-03T00:00:00Z",
                "role": "user",
                "content": "yooo",
                "channel": "whatsapp",
                "sender": "Carlos",
            }
        ],
    )
    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["channel"] == "whatsapp"
    assert row["sender"] == "Carlos"


def test_list_threads_backfills_the_channel_from_the_chat_directory(api_client):
    """Transcripts written before the stamp existed still have to label
    correctly — that is what the user actually sees on an existing box."""
    from olano.platforms.chat_directory import get_chat_directory

    tid = "5550001234"
    _write_thread(
        tid,
        [{"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "what's the signboard"}],
    )
    get_chat_directory().record_chat(
        agent="test_agent", platform="telegram", chat_id=tid, chat_name="Carlos"
    )

    row = next(t for t in api_client.get("/api/agents/test_agent/threads").json() if t["id"] == tid)
    assert row["channel"] == "telegram"
    assert row["title"] == "what's the signboard"


def test_list_threads_child_of_a_new_inherits_its_parents_channel(api_client):
    """`/new` on a platform swaps the streaming thread to a bare uuid4 recorded
    as `active_thread` on the parent's state file. Without inheritance, a second
    conversation in the same Telegram chat falls back to "Dashboard"."""
    import json

    from olano.config.home import get_olano_paths
    from olano.platforms.chat_directory import get_chat_directory

    parent = "1345757599"
    child = "9f6a1b2c-0000-4000-8000-000000000000"
    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    _write_thread(parent, [{"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "first"}])
    _write_thread(child, [{"ts": "2026-06-03T01:00:00Z", "role": "user", "content": "second"}])
    (sessions / f"{parent}.state.json").write_text(
        json.dumps({"active_thread": child}), encoding="utf-8"
    )
    get_chat_directory().record_chat(
        agent="test_agent", platform="telegram", chat_id=parent, chat_name="Ana"
    )

    rows = {t["id"]: t for t in api_client.get("/api/agents/test_agent/threads").json()}
    assert rows[parent]["channel"] == "telegram"
    assert rows[child]["channel"] == "telegram"


def test_get_thread_peels_the_sender_tag_and_reports_provenance(api_client):
    """The chat window rendered the runtime's own identity tag verbatim, so
    every platform message opened with `[from @jrivera (…)]: ` before its words.
    The body must survive the peel — this is why it does NOT use the title
    builder, which would replace an email body with its subject line."""
    tid = "7788990011"
    _write_thread(
        tid,
        [
            {
                "ts": "2026-06-03T00:00:00Z",
                "role": "user",
                "content": "[from @jrivera (Jamie, id=[PHONE_US_REDACTED])]: "
                "[Subject: Q3]\n\nthe body survives",
                "channel": "telegram",
            },
            {"ts": "2026-06-03T00:00:01Z", "role": "assistant", "content": "on it"},
        ],
    )
    detail = api_client.get(f"/api/agents/test_agent/threads/{tid}").json()
    assert detail["channel"] == "telegram"
    first = detail["messages"][0]
    assert first["content"] == "[Subject: Q3]\n\nthe body survives"
    assert first["sender"] == "Jamie"
    assert first["channel"] == "telegram"


def test_get_thread_recovers_the_channel_for_a_new_child(api_client):
    """The header must agree with the rail on a `/new` fork, which has no chat
    id of its own — the link lives on the parent's state file."""
    import json

    from olano.config.home import get_olano_paths
    from olano.platforms.chat_directory import get_chat_directory

    parent = "1345900011"
    child = "af6a1b2c-0000-4000-8000-000000000000"
    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    _write_thread(parent, [{"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "first"}])
    _write_thread(child, [{"ts": "2026-06-03T01:00:00Z", "role": "user", "content": "second"}])
    (sessions / f"{parent}.state.json").write_text(
        json.dumps({"active_thread": child}), encoding="utf-8"
    )
    get_chat_directory().record_chat(
        agent="test_agent", platform="telegram", chat_id=parent, chat_name="Ana"
    )

    assert api_client.get(f"/api/agents/test_agent/threads/{child}").json()["channel"] == "telegram"


def test_get_thread_leaves_a_real_dashboard_chat_alone(api_client):
    tid = "bf6a1b2c-0000-4000-8000-000000000000"
    _write_thread(tid, [{"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "hello"}])
    detail = api_client.get(f"/api/agents/test_agent/threads/{tid}").json()
    assert detail["channel"] == "dashboard"
    assert detail["messages"][0]["content"] == "hello"


# ── Avatar serving: size and caching ─────────────────────────────────────────
#
# The endpoint used to hand the dashboard the raw 1024×1024 image-model output
# (a measured median of 888 KB) to draw a 40 px circle, under
# `Cache-Control: no-cache` — and Starlette's `FileResponse` sends an `ETag`
# then ignores `If-None-Match`, so every revalidation came back 200 with the
# whole body. Eight agents cost ~7 MB on every mount of every view showing the
# roster. Both halves are pinned here at the HTTP surface, because that is the
# only place the two interact.


@pytest.fixture
def avatar_agent(api_client, tmp_workspace, monkeypatch):
    """An on-disk agent carrying a realistically heavy 1024px PNG avatar."""

    from PIL import Image, ImageDraw

    agent_dir = tmp_workspace / "home" / "agents" / "sunny"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text('{ name: "sunny", model: "fake:instant" }')

    im = Image.new("RGB", (1024, 1024))
    draw = ImageDraw.Draw(im)
    for y in range(1024):
        draw.line([(0, y), (1024, y)], fill=(y // 4, 200 - y // 8, 120))
    draw.ellipse([200, 150, 800, 750], fill=(240, 190, 150))
    im.save(agent_dir / "avatar.png", "PNG")
    return agent_dir


def test_avatar_is_served_as_the_small_rendition(api_client, avatar_agent):
    """A 40 px circle must not cost a megabyte."""
    source_bytes = (avatar_agent / "avatar.png").stat().st_size

    resp = api_client.get("/api/agents/sunny/avatar")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert len(resp.content) < source_bytes
    assert len(resp.content) < 16 * 1024


def test_full_still_serves_the_original(api_client, avatar_agent):
    """The 1024px source stays the source of truth, and stays reachable."""
    resp = api_client.get("/api/agents/sunny/avatar?full=1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) == (avatar_agent / "avatar.png").stat().st_size


def test_revalidation_answers_304_instead_of_the_whole_image(api_client, avatar_agent):
    """The defect, stated exactly: this used to be a 200 with the full body."""
    first = api_client.get("/api/agents/sunny/avatar")
    etag = first.headers["etag"]

    second = api_client.get("/api/agents/sunny/avatar", headers={"If-None-Match": etag})

    assert second.status_code == 304
    assert second.content == b""


def test_a_stamped_url_is_cached_outright(api_client, avatar_agent):
    """With `?v=`, the browser should not come back at all for a year."""
    stamped = api_client.get("/api/agents/sunny").json()["avatar_url"]
    assert "?v=" in stamped

    resp = api_client.get(stamped)

    assert "immutable" in resp.headers["cache-control"]
    assert "max-age=31536000" in resp.headers["cache-control"]


def test_a_bare_url_is_never_pinned(api_client, avatar_agent):
    """An unstamped URL is stable, so a far-future entry would strand an old
    dashboard bundle on a stale avatar with nothing able to bust it."""
    resp = api_client.get("/api/agents/sunny/avatar")

    assert "immutable" not in resp.headers["cache-control"]
    assert "must-revalidate" in resp.headers["cache-control"]
    assert resp.headers["etag"], "no validator, so it cannot even 304"


def test_avatar_caching_is_private(api_client, avatar_agent):
    """`/api/agents/*` sits behind the session gate on a multi-user install, so
    a shared proxy must not hold one tenant's avatar."""
    resp = api_client.get("/api/agents/sunny/avatar")

    assert resp.headers["cache-control"].startswith("private")


def test_regenerating_the_avatar_changes_its_url(api_client, avatar_agent):
    """This is the entire cache-invalidation mechanism. If the stamp stops
    moving, a new portrait stays invisible behind a year-long cache entry."""
    from io import BytesIO

    from PIL import Image

    before = api_client.get("/api/agents/sunny").json()["avatar_url"]

    buf = BytesIO()
    Image.new("RGB", (1024, 1024), (10, 90, 200)).save(buf, "PNG")
    upload = api_client.post(
        "/api/agents/sunny/avatar",
        files={"file": ("new.png", buf.getvalue(), "image/png")},
    )
    assert upload.status_code == 200

    after = api_client.get("/api/agents/sunny").json()["avatar_url"]
    assert after != before
    assert upload.json()["url"] == after


def test_a_replaced_avatar_serves_the_new_face(api_client, avatar_agent):
    """The rendition is derived, so a stale one is a real hazard: the endpoint
    would keep serving the previous agent's portrait at the new URL."""
    from io import BytesIO

    from PIL import Image

    first = api_client.get("/api/agents/sunny/avatar").content

    buf = BytesIO()
    Image.new("RGB", (1024, 1024), (10, 90, 200)).save(buf, "PNG")
    api_client.post(
        "/api/agents/sunny/avatar",
        files={"file": ("new.png", buf.getvalue(), "image/png")},
    )

    assert api_client.get("/api/agents/sunny/avatar").content != first


def test_deleting_the_avatar_removes_the_rendition_too(api_client, avatar_agent):
    """An orphaned rendition next to a deleted source is disk nobody reclaims."""
    api_client.get("/api/agents/sunny/avatar")
    assert (avatar_agent / "avatar-256.webp").exists()

    api_client.delete("/api/agents/sunny/avatar")

    assert not (avatar_agent / "avatar-256.webp").exists()
    assert api_client.get("/api/agents/sunny/avatar").status_code == 404


def test_agent_json5_never_stores_a_stamped_url(api_client, avatar_agent):
    """A stamp describes one moment of one file. Persisting one bakes a value
    that goes stale into config every reader re-derives from disk anyway."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (512, 512), (30, 30, 30)).save(buf, "PNG")
    api_client.post(
        "/api/agents/sunny/avatar",
        files={"file": ("new.png", buf.getvalue(), "image/png")},
    )

    raw = (avatar_agent / "agent.json5").read_text()
    assert "?v=" not in raw


def test_a_malformed_full_flag_serves_the_rendition_rather_than_erroring(api_client, avatar_agent):
    """`full` is read off the query string, not declared as a typed parameter:
    FastAPI would 422 `?full=`, and failing a picture over an unparseable flag
    is worse than ignoring it."""
    resp = api_client.get("/api/agents/sunny/avatar?full=")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


# ── magic-link outcome rows in a reloaded thread ─────────────────────────────


def test_get_thread_returns_a_magic_link_row_but_hides_other_system_rows(api_client):
    """`system` rows are hidden as a class — prompt-update notices and context
    notes are written for the model's cold-start rehydration, not for a human
    reading the thread. A magic-link outcome is the one exception, because it
    is written FOR this view: it is how a connection that completed on the
    user's phone is still on screen after a reload, and in a second tab.
    """
    import json

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "55555555-5555-5555-5555-555555555555"
    (sessions / f"{tid}.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "connect wechat"},
                # Written by runner._notify_prompt_update — must stay invisible.
                {"ts": "2026-06-03T00:00:01Z", "role": "system", "content": "prompt updated"},
                {
                    "ts": "2026-06-03T00:00:02Z",
                    "role": "system",
                    "event": "magic_link",
                    "content": "✅ curtis is connected — weixin: x.",
                    "link_id": "hs_abc",
                    "status": "done",
                    "flow": "channel-connect",
                    "label": "Connect channels",
                    "target_agent": "curtis",
                    "apply": "restarted",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    messages = api_client.get(f"/api/agents/test_agent/threads/{tid}").json()["messages"]

    assert [m["content"] for m in messages] == [
        "connect wechat",
        "✅ curtis is connected — weixin: x.",
    ]
    # The structured half rides along, so the chat rebuilds the same card the
    # live SSE frame draws — Restart button and all — rather than bare text.
    outcome = messages[-1]
    assert outcome["event"] == "magic_link"
    assert outcome["link_id"] == "hs_abc"
    assert outcome["target_agent"] == "curtis"
    assert outcome["apply"] == "restarted"


def test_chat_stream_binds_the_thread_id_as_the_turn_session(api_client, mock_runtime):
    """`session_id` IS the thread id on a turn, and the dashboard was the one
    surface that left it empty.

    The platform path binds it (`runtime/server.py`), and everything that has
    to answer "which conversation was this?" long after the turn ended reads it
    from there — the magic-link and MCP-OAuth completion watchers above all.
    With it unbound, a link minted from the dashboard chat froze
    `target: "owner"` and its ✅ notice had nowhere to go.
    """
    from olano.security.request_context import get_request_context

    runner = mock_runtime._runners["test_agent"]
    seen: dict = {}

    def fake_events(payload, **kwargs):
        ctx = get_request_context()
        seen["session_id"] = ctx.session_id
        seen["channel"] = ctx.channel
        seen["thread_id"] = kwargs.get("thread_id")

        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "Hi", "agent": "test_agent", "thread_id": "thread-42"},
    )
    assert resp.status_code == 200
    assert seen["channel"] == "api"
    # The very id the turn runs against — not a client session, not empty.
    assert seen["session_id"] == seen["thread_id"] == "thread-42"


def test_chat_stream_binds_a_minted_thread_id_when_none_is_given(api_client, mock_runtime):
    """A first message has no thread id yet; the runtime mints one. The turn
    context must carry *that*, or a magic link minted in the very first turn of
    a conversation would still be unaddressable."""
    from olano.security.request_context import get_request_context

    runner = mock_runtime._runners["test_agent"]
    seen: dict = {}

    def fake_events(payload, **kwargs):
        seen["session_id"] = get_request_context().session_id
        seen["thread_id"] = kwargs.get("thread_id")

        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events

    api_client.post("/api/chat/stream", json={"message": "Hi", "agent": "test_agent"})
    assert seen["session_id"]
    assert seen["session_id"] == seen["thread_id"]


# ── PUT /api/agents/{name} → commands live-sync ───────────────────────────────


def test_commands_edit_applies_without_a_restart(api_client, mock_runtime, monkeypatch):
    """A Commands-tab save reaches the running agent and its connectors.

    The inbound gate reads ``runner.config`` — the object captured at boot — so
    a disk-only write left every chat surface on the pre-edit exposure map until
    someone restarted the agent, and the connectors kept advertising the old
    menu. Both halves have to move on the PUT.
    """
    from olano.runtime import api as api_module

    published: list[str] = []

    class _Adapter:
        async def publish_command_menu(self, *, force: bool = False) -> bool:
            published.append("telegram")
            return True

    from olano.platforms.registry import get_platform_registry

    registry = get_platform_registry()
    registry.register("test_agent", "telegram", _Adapter())
    try:
        stored: dict = {
            "name": "test_agent",
            "model": "anthropic:claude-haiku-4-5-20251001",
        }
        monkeypatch.setattr(api_module, "_read_agent_config", lambda name: dict(stored))

        async def _read_for_update(name):
            return dict(stored)

        async def _write(name, data):
            stored.clear()
            stored.update(data)

        monkeypatch.setattr(api_module, "_read_agent_config_for_update", _read_for_update)
        monkeypatch.setattr(api_module, "_write_agent_config_async", _write)

        resp = api_client.put(
            "/api/agents/test_agent",
            json={
                "commands": {
                    "enabled": {"model": True, "context": False},
                    "custom": [{"name": "standup", "prompt": "Summarize. {args}"}],
                }
            },
        )
        assert resp.status_code == 200

        # (a) the live config the gate reads was swapped, not just the file
        live = mock_runtime._runners["test_agent"].config.commands
        assert live.is_exposed("model") is True
        assert live.is_exposed("context") is False
        assert live.find_custom("standup") is not None

        # (b) every connected adapter was asked to re-publish its menu
        assert published == ["telegram"]
    finally:
        registry.unregister("test_agent", "telegram")


# ── the handle is minted from the display name ───────────────────────────────


def _raw_config(api_client, name: str) -> dict:
    import json

    return json.loads(api_client.get(f"/api/agents/{name}/config/raw").json()["content"])


def test_create_agent_mints_the_handle_from_the_display_name(api_client):
    """Both dashboards send only a display name now: the person names the
    agent, the server derives its permanent key — and suffixes a collision
    instead of refusing on a field the person cannot edit."""
    first = api_client.post("/api/agents", json={"display_name": "Hillary"})
    assert first.status_code == 200, first.text
    assert first.json()["name"] == "hillary"
    second = api_client.post("/api/agents", json={"display_name": "Hillary"})
    assert second.status_code == 200, second.text
    assert second.json()["name"] == "hillary-2"
    rows = {a["name"]: a for a in api_client.get("/api/agents").json()}
    assert rows["hillary"]["display_name"] == "Hillary"
    assert rows["hillary-2"]["display_name"] == "Hillary"


def test_create_agent_needs_a_display_name_when_no_handle_is_given(api_client):
    resp = api_client.post("/api/agents", json={})
    assert resp.status_code == 422
    assert "display_name_required" in resp.text


def test_create_agent_an_explicit_handle_is_still_honoured(api_client):
    """API clients and the AgentFather tool pass a handle; it is used verbatim
    and still validated."""
    ok = api_client.post("/api/agents", json={"name": "fresh_agent", "display_name": "Fresh"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "fresh_agent"
    bad = api_client.post("/api/agents", json={"name": "Not A Slug"})
    assert bad.status_code == 422
    assert "invalid_agent_name" in bad.text


def test_create_agent_writes_the_title_into_org_title(api_client):
    resp = api_client.post(
        "/api/agents", json={"display_name": "Hillary", "title": "Recruitment Specialist"}
    )
    assert resp.status_code == 200, resp.text
    raw = _raw_config(api_client, "hillary")
    assert raw["org"]["title"] == "Recruitment Specialist"
    assert "title" not in raw
    detail = api_client.get("/api/agents/hillary").json()
    assert detail["title"] == "Recruitment Specialist"
    row = next(a for a in api_client.get("/api/agents").json() if a["name"] == "hillary")
    assert row["title"] == "Recruitment Specialist"


def test_archetype_create_keeps_the_persons_name_and_defaults_the_title(api_client):
    """The bug this whole change exists for: the simple shell's New bot sent
    `{name: "hillary", archetype}`, the installer resolved the display name
    to the archetype's label, and the prompt told Hillary she was called
    "Recruitment Specialist". Now the person's name is the name, and the
    archetype's label becomes her title."""
    resp = api_client.post(
        "/api/agents", json={"display_name": "Hillary", "archetype": "customer-service"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "hillary"
    detail = api_client.get("/api/agents/hillary").json()
    assert detail["display_name"] == "Hillary"
    assert detail["title"] == "Customer Service Agent"
    assert detail["archetype"] == "customer-service"


def test_archetype_create_with_no_display_name_lands_on_the_archetype_slug(api_client):
    resp = api_client.post("/api/agents", json={"archetype": "customer-service"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "customer-service"
    detail = api_client.get("/api/agents/customer-service").json()
    # No name typed ⇒ the archetype's own label, exactly as before.
    assert detail["display_name"] == "Customer Service Agent"
    assert detail["title"] == "Customer Service Agent"


def test_archetype_install_endpoint_mints_the_handle_too(api_client):
    resp = api_client.post(
        "/api/archetypes/install",
        json={"archetype": "customer-service", "display_name": "Ana", "title": "Support Lead"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "ana"
    detail = api_client.get("/api/agents/ana").json()
    assert detail["display_name"] == "Ana"
    assert detail["title"] == "Support Lead"
    again = api_client.post(
        "/api/archetypes/install", json={"archetype": "customer-service", "display_name": "Ana"}
    )
    assert again.status_code == 200, again.text
    assert again.json()["name"] == "ana-2"


def test_title_is_a_read_only_mirror_on_the_agent_editor(api_client):
    """The detail payload injects a resolved `title`; a PUT echoing it back must
    not write a stray top-level key — the title is edited through the org
    endpoint, where the central store can stay canonical."""
    assert api_client.post("/api/agents", json={"display_name": "Hillary"}).status_code == 200
    put = api_client.put("/api/agents/hillary", json={"title": "Recruiter", "description": "x"})
    assert put.status_code == 200, put.text
    raw = _raw_config(api_client, "hillary")
    assert "title" not in raw
    assert raw["description"] == "x"
    org = api_client.put(
        "/api/agents/hillary/org",
        json={
            "title": "Recruiter",
            "manager": None,
            "reports": [],
            "coworkers": [],
            "input": {"mode": "everyone", "agents": []},
            "output": {"mode": "everyone", "agents": []},
            "autogenerate": False,
            "mutating_from_manager_only": False,
        },
    )
    assert org.status_code == 200, org.text
    assert api_client.get("/api/agents/hillary").json()["title"] == "Recruiter"


# ── Files listing cap + read cap ─────────────────────────────────────────────
#
# The dashboard's multi-agent tree asks `GET /api/files` for the first N entries
# and `GET /api/files/{path}` for the first M bytes: a 50,000-entry directory
# or a 2 GB log must cost a bounded amount of server memory and browser DOM.
# Both are opt-in shapes — without `limit` the listing is the bare list every
# other consumer reads; `truncated`/`total_size` are additive fields.


def test_list_files_without_limit_keeps_the_bare_list(api_client, tmp_workspace):
    for i in range(5):
        (tmp_workspace / f"f{i}.md").write_text("x", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_files_limit_returns_envelope_and_counts_the_rest(api_client, tmp_workspace):
    for i in range(7):
        (tmp_workspace / f"f{i}.md").write_text("x", encoding="utf-8")
    (tmp_workspace / ".hidden").write_text("x", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files", params={"limit": 3})
        full = api_client.get("/api/files", params={"limit": 500})

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert len(body["entries"]) == 3
    assert body["truncated"] is True
    # Hidden entries are neither listed nor counted (parity with the bare list).
    assert body["total"] == len([p for p in tmp_workspace.iterdir() if not p.name.startswith(".")])
    assert all("name" in e and "path" in e and "type" in e for e in body["entries"])

    assert full.json()["truncated"] is False
    assert len(full.json()["entries"]) == full.json()["total"]


def test_list_files_rejects_non_positive_limit(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files", params={"limit": 0})
    assert resp.status_code == 400


def test_list_files_abs_path_follows_symlinks_only(api_client, tmp_workspace):
    """`abs_path` is derived from one resolve() of the directory for plain
    entries and still fully resolved for symlinks — both must equal what
    `Path.resolve()` says per entry."""
    real = tmp_workspace / "real.md"
    real.write_text("x", encoding="utf-8")
    outside = tmp_workspace.parent / "outside-target.md"
    outside.write_text("y", encoding="utf-8")
    (tmp_workspace / "link.md").symlink_to(outside)

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    by_name = {e["name"]: e for e in resp.json()}
    assert by_name["real.md"]["abs_path"] == str(real.resolve())
    assert by_name["link.md"]["abs_path"] == str(outside.resolve())


def test_read_file_small_file_is_not_truncated(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Hello", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/notes.md")

    data = resp.json()
    assert data["content"] == "# Hello"
    assert data["truncated"] is False
    assert data["total_size"] == len(b"# Hello")


def test_read_file_honours_client_max_bytes(api_client, tmp_workspace):
    (tmp_workspace / "big.log").write_text("a" * 10_000, encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/big.log", params={"max_bytes": 100})

    data = resp.json()
    assert len(data["content"]) == 100
    assert data["truncated"] is True
    assert data["total_size"] == 10_000


def test_read_file_server_cap_bounds_every_read(api_client, tmp_workspace, monkeypatch):
    """The env cap is a ceiling: a client asking for more than it gets the cap."""
    (tmp_workspace / "big.log").write_text("b" * 5_000, encoding="utf-8")
    monkeypatch.setenv("LIONCLAW_FILES_READ_MAX_BYTES", "1000")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        capped = api_client.get("/api/files/big.log")
        asked_more = api_client.get("/api/files/big.log", params={"max_bytes": 4_000})

    assert len(capped.json()["content"]) == 1000 and capped.json()["truncated"] is True
    assert len(asked_more.json()["content"]) == 1000


def test_read_file_head_cut_inside_multibyte_char_does_not_500(api_client, tmp_workspace):
    (tmp_workspace / "u.txt").write_bytes("héllo wörld".encode())

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        # 2 bytes lands in the middle of "é" (0xC3 0xA9).
        resp = api_client.get("/api/files/u.txt", params={"max_bytes": 2})

    assert resp.status_code == 200
    assert resp.json()["truncated"] is True


def test_read_file_rejects_non_positive_max_bytes(api_client, tmp_workspace):
    (tmp_workspace / "a.md").write_text("x", encoding="utf-8")
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/a.md", params={"max_bytes": 0})
    assert resp.status_code == 400


def test_read_file_binary_head_comes_back_flagged_and_empty(api_client, tmp_workspace):
    """A zip renders as a screen of mojibake when decoded with ``replace``; the
    read says it is binary and ships none of it."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("skills/demo/SKILL.md", "# demo\n" * 200)
    (tmp_workspace / "export.zip").write_bytes(buf.getvalue())
    (tmp_workspace / "notes.txt").write_text("plain text\twith a tab\n", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        zipped = api_client.get("/api/files/export.zip").json()
        text = api_client.get("/api/files/notes.txt").json()

    assert zipped["binary"] is True
    assert zipped["content"] == "" and zipped["size"] == 0
    assert zipped["total_size"] == len(buf.getvalue())
    assert text["binary"] is False and text["content"].startswith("plain text")


def test_looks_binary_tolerates_legacy_encodings_and_cut_heads():
    from olano.runtime.routes.files import looks_binary

    assert looks_binary(b"") is False
    assert looks_binary(b"\x7fELF\x02\x01\x01\x00") is True
    # NUL-free noise is still not text.
    assert looks_binary(bytes(range(0x80, 0x100)) * 4) is True
    # Latin-1 prose: a few undecodable bytes in a mostly ASCII line.
    prose = "Le caf\xe9 du coin sert une cr\xe8me br\xfbl\xe9e excellente tous les jours de la semaine.\n"
    assert looks_binary((prose * 20).encode("latin-1")) is False
    # A head cut inside a multibyte character is the read's seam, not binary.
    assert looks_binary("é".encode()[:1]) is False
    assert looks_binary("ok é".encode()[:-1]) is False


# ── GET /api/files/search ─────────────────────────────────────────────────


def _seed_search_workspace(root):
    """A workspace with matches at several depths plus the trees a name search
    must never descend into (VCS, dependencies, an embedded repo, dot-entries)."""
    # The fixture workspace already carries MEMORY.md + memories/ — the same
    # shape every real agent workspace has, which is what the search is for.
    (root / "MEMORY.md").write_text("# memory", encoding="utf-8")
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / "2026-09-01.md").write_text("day", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "MEMORANDUM.txt").write_text("memo", encoding="utf-8")
    (root / "docs" / "other.txt").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "MEMORY-in-git.md").write_text("no", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "memory-lib.js").write_text("no", encoding="utf-8")
    (root / "vendored").mkdir()
    (root / "vendored" / ".git").mkdir()
    (root / "vendored" / "MEMORY.md").write_text("embedded repo", encoding="utf-8")
    (root / ".memoranda").write_text("hidden", encoding="utf-8")


def test_search_files_matches_by_name_and_path_across_depths(api_client, tmp_workspace):
    _seed_search_workspace(tmp_workspace)
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/search", params={"q": "MEMOR"})
    assert resp.status_code == 200
    body = resp.json()
    paths = [r["path"] for r in body["results"]]
    # Case-insensitive, at any depth, matching the name OR the relative path
    # (`memories/2026-09-01.md` matches through its folder).
    assert paths == ["MEMORY.md", "docs/MEMORANDUM.txt", "memories", "memories/2026-09-01.md"]
    assert body["truncated"] is False
    assert body["agents_searched"] and all(
        r["agent"] == body["agents_searched"][0] for r in body["results"]
    )
    kinds = {r["path"]: r["type"] for r in body["results"]}
    assert kinds["memories"] == "directory" and kinds["MEMORY.md"] == "file"


def test_search_files_prunes_vcs_dependency_and_embedded_repo_trees(api_client, tmp_workspace):
    _seed_search_workspace(tmp_workspace)
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/search", params={"q": "memor"})
    paths = {r["path"] for r in resp.json()["results"]}
    assert not any(p.startswith(".git/") for p in paths)
    assert not any(p.startswith("node_modules/") for p in paths)
    assert "vendored/MEMORY.md" not in paths, "an embedded repository's tree is not the workspace's"
    assert ".memoranda" not in paths, "dot-entries stay hidden unless asked for"


def test_search_files_hidden_flag_admits_dot_entries(api_client, tmp_workspace):
    _seed_search_workspace(tmp_workspace)
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/search", params={"q": "memor", "hidden": "true"})
    paths = {r["path"] for r in resp.json()["results"]}
    assert ".memoranda" in paths
    # Hidden shows dot-FILES; pruned trees stay pruned — they are not "hidden",
    # they are not part of the workspace a person browses.
    assert not any(p.startswith(".git/") for p in paths)


def test_search_files_limit_truncates_and_says_so(api_client, tmp_workspace):
    _seed_search_workspace(tmp_workspace)
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/search", params={"q": "memor", "limit": 1})
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["truncated"] is True


def test_search_files_rejects_a_query_too_short_to_mean_anything(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        assert api_client.get("/api/files/search", params={"q": "m"}).status_code == 400
        assert api_client.get("/api/files/search", params={"q": "  "}).status_code == 400


# ── mid-turn steering (core/steering.py) ─────────────────────────────────────


def _first_frame(resp_text: str) -> dict:
    import json as _json

    for line in resp_text.splitlines():
        if line.startswith("data: "):
            return _json.loads(line[len("data: ") :])
    raise AssertionError("no SSE frame in response")


def test_chat_stream_start_frame_carries_the_turn_id(api_client, mock_runtime):
    """The dashboard steers against the turn the `start` frame named, and the
    runner files the turn under that same id."""
    runner = mock_runtime._runners["test_agent"]
    captured: dict = {}
    _stub_stream(runner, captured)

    resp = api_client.post("/api/chat/stream", json={"message": "Hi", "agent": "test_agent"})
    assert resp.status_code == 200
    start = _first_frame(resp.text)
    assert start["type"] == "start"
    assert start["turn_id"] and start["turn_id"] == captured.get("turn_id")


def test_chat_steer_refused_when_no_turn_is_running(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    runner.steer = MagicMock(return_value=None)
    resp = api_client.post(
        "/api/chat/steer",
        json={"agent": "test_agent", "thread_id": "t1", "message": "use blue"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "turn_ended"


def test_chat_steer_accepts_and_forwards_to_the_runner(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    runner.steer = MagicMock(return_value=SimpleNamespace(id="abcdefgh", turn_id="turn-1"))
    resp = api_client.post(
        "/api/chat/steer",
        json={
            "agent": "test_agent",
            "thread_id": "t1",
            "message": "  use blue  ",
            "turn_id": "turn-1",
            "steer_id": "abcdefgh",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "steer_id": "abcdefgh", "turn_id": "turn-1"}
    runner.steer.assert_called_once()
    args, kwargs = runner.steer.call_args
    assert args[1] == "use blue"  # stripped
    assert kwargs["turn_id"] == "turn-1"
    assert kwargs["steer_id"] == "abcdefgh"


def test_chat_steer_validation(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    runner.steer = MagicMock(return_value=SimpleNamespace(id="x", turn_id="y"))
    blank = api_client.post(
        "/api/chat/steer", json={"agent": "test_agent", "thread_id": "t1", "message": "   "}
    )
    assert blank.status_code == 400
    bad_id = api_client.post(
        "/api/chat/steer",
        json={"agent": "test_agent", "thread_id": "t1", "message": "x", "steer_id": "no spaces!"},
    )
    assert bad_id.status_code == 400
    missing = api_client.post(
        "/api/chat/steer", json={"agent": "nonexistent_agent", "thread_id": "t1", "message": "x"}
    )
    assert missing.status_code == 404
    runner.steer.assert_not_called()
    # A message the input scan blocks, or a full turn, is a 400 with the reason.
    runner.steer = MagicMock(side_effect=ValueError("this turn already holds 10 steers"))
    full = api_client.post(
        "/api/chat/steer", json={"agent": "test_agent", "thread_id": "t1", "message": "x"}
    )
    assert full.status_code == 400
    assert "10 steers" in full.json()["detail"]


def test_chat_steer_cancel_outcomes(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    runner.cancel_steer = MagicMock(side_effect=["cancelled", "delivered", "unknown"])
    body = {"agent": "test_agent", "thread_id": "t1", "steer_id": "abcdefgh"}
    assert api_client.post("/api/chat/steer/cancel", json=body).json() == {"cancelled": True}
    delivered = api_client.post("/api/chat/steer/cancel", json=body)
    assert delivered.status_code == 409
    assert delivered.json()["detail"]["code"] == "already_delivered"
    unknown = api_client.post("/api/chat/steer/cancel", json=body)
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "unknown_steer"


def test_transcript_message_projects_the_steer_stamp():
    from olano.runtime.api import _transcript_message

    row = _transcript_message(
        "bot", {"role": "user", "content": "use blue", "steer": True, "steer_id": "abcdefgh"}
    )
    assert row is not None
    assert row["steer"] is True
    assert row["steer_id"] == "abcdefgh"
    plain = _transcript_message("bot", {"role": "user", "content": "hello"})
    assert plain is not None
    assert plain["steer"] is False
    assert plain["steer_id"] == ""


# ── detached runs: a turn outlives the socket that started it ────────────────
# The defect these cover: `/api/chat/stream` used to run the turn inside its own
# SSE response generator, so a phone locking or a network handover cancelled the
# agent mid-tool-call. See olano/core/runs.py.


@pytest.fixture(autouse=True)
def _clean_run_registry():
    from olano.core.runs import REGISTRY

    REGISTRY.reset()
    yield
    REGISTRY.reset()


def _frames(resp) -> list[dict]:
    """Parse an SSE body into its JSON frames."""
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                out.append(json.loads(payload))
    return out


def test_chat_stream_frames_are_numbered_and_name_their_run(api_client, mock_runtime):
    """`seq` and `run_id` are what a returning client attaches with."""
    runner = mock_runtime._runners["test_agent"]
    _stub_stream(runner, {})

    resp = api_client.post("/api/chat/stream", json={"message": "Hello", "agent": "test_agent"})
    assert resp.status_code == 200
    frames = _frames(resp)
    start = frames[0]
    assert start["type"] == "start"
    assert start["run_id"]
    # Monotonic from 1, with no gaps — an attach cursor is meaningless otherwise.
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    assert frames[-1]["type"] == "done"


def test_a_finished_run_can_still_be_attached_and_replayed(api_client, mock_runtime):
    """A phone that comes back a moment after the answer landed gets the reply,
    not a 404 that reads as 'your turn vanished'."""
    runner = mock_runtime._runners["test_agent"]
    _stub_stream(runner, {})

    started = api_client.post("/api/chat/stream", json={"message": "Hello", "agent": "test_agent"})
    frames = _frames(started)
    run_id = frames[0]["run_id"]
    thread_id = frames[0]["thread_id"]

    attached = api_client.get(
        "/api/chat/attach",
        params={"agent": "test_agent", "thread_id": thread_id, "run_id": run_id},
    )
    assert attached.status_code == 200
    assert [f["type"] for f in _frames(attached)] == [f["type"] for f in frames]


def test_attach_from_a_cursor_returns_only_what_was_missed(api_client, mock_runtime):
    runner = mock_runtime._runners["test_agent"]
    _stub_stream(runner, {})

    frames = _frames(
        api_client.post("/api/chat/stream", json={"message": "Hello", "agent": "test_agent"})
    )
    run_id, thread_id = frames[0]["run_id"], frames[0]["thread_id"]

    tail = _frames(
        api_client.get(
            "/api/chat/attach",
            params={
                "agent": "test_agent",
                "thread_id": thread_id,
                "run_id": run_id,
                "cursor": 1,  # saw the `start` frame and nothing else
            },
        )
    )
    assert [f["seq"] for f in tail] == [f["seq"] for f in frames[1:]]


def test_attach_404s_for_a_run_that_never_existed(api_client, mock_runtime):
    """The client falls back to refetching the transcript, which is what it
    already does when a turn is genuinely gone."""
    resp = api_client.get(
        "/api/chat/attach",
        params={"agent": "test_agent", "thread_id": "t1", "run_id": "nope"},
    )
    assert resp.status_code == 404


def test_attach_refuses_a_run_belonging_to_another_thread(api_client, mock_runtime):
    """A run is addressable only through the thread it belongs to — the id
    alone must never be enough, or a guessed run id would leak a conversation."""
    runner = mock_runtime._runners["test_agent"]
    _stub_stream(runner, {})
    frames = _frames(
        api_client.post("/api/chat/stream", json={"message": "Hello", "agent": "test_agent"})
    )
    resp = api_client.get(
        "/api/chat/attach",
        params={
            "agent": "test_agent",
            "thread_id": "some-other-thread",
            "run_id": frames[0]["run_id"],
        },
    )
    assert resp.status_code == 404


def test_attach_by_thread_alone_finds_the_live_run(api_client, mock_runtime):
    """What a freshly reloaded page can ask: it knows the thread was busy, not
    which run was working on it."""
    from olano.core.runs import REGISTRY

    run = REGISTRY.begin("chat", "test_agent", "t-live", "turn-1")
    assert run is not None
    run.emit({"type": "text", "text": "mid-turn"})
    # Finish it so the view terminates instead of tailing forever. A run that
    # ended a moment ago is still attachable — that is the point of the test.
    REGISTRY.finish(run)

    frames = _frames(
        api_client.get("/api/chat/attach", params={"agent": "test_agent", "thread_id": "t-live"})
    )
    assert [f["text"] for f in frames] == ["mid-turn"]


def test_a_second_send_while_a_run_is_live_is_refused_with_the_live_turn(api_client, mock_runtime):
    """Two concurrent runs would braid two replies into one transcript. The 409
    names the live turn so the client steers into it instead."""
    from olano.core.runs import REGISTRY

    run = REGISTRY.begin("chat", "test_agent", "t-busy", "turn-1")
    assert run is not None

    resp = api_client.post(
        "/api/chat/stream",
        json={"message": "keep going", "agent": "test_agent", "thread_id": "t-busy"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "run_in_progress"
    assert detail["run_id"] == run.run_id
    assert detail["turn_id"] == "turn-1"


def test_stop_cancels_the_live_run(api_client, mock_runtime):
    """Closing the socket no longer stops a turn, so Stop needs a way to say so."""
    from olano.core.runs import REGISTRY

    run = REGISTRY.begin("chat", "test_agent", "t-stop", "turn-1")
    assert run is not None

    resp = api_client.post("/api/chat/stop", json={"agent": "test_agent", "thread_id": "t-stop"})
    assert resp.status_code == 200
    assert resp.json()["stopped"] is True
    assert run.stopped is True

    # Idempotent: stopping a thread with nothing running is not an error.
    again = api_client.post("/api/chat/stop", json={"agent": "test_agent", "thread_id": "t-stop"})
    assert again.json()["stopped"] is False


def test_stop_requires_an_agent(api_client, mock_runtime):
    assert api_client.post("/api/chat/stop", json={"thread_id": "t1"}).status_code == 400


def test_thread_detail_advertises_a_live_run_so_a_reload_can_attach(api_client, mock_runtime):
    """The transcript holds only persisted rows, so on its own it cannot say a
    turn is still running — which is how a reload used to lose the turn."""
    from olano.config.home import get_olano_paths
    from olano.core.runs import REGISTRY

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    thread_id = "66666666-6666-6666-6666-666666666666"
    (sessions / f"{thread_id}.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "hello"}) + "\n",
        encoding="utf-8",
    )

    # Nothing running on it yet.
    detail = api_client.get(f"/api/agents/test_agent/threads/{thread_id}")
    assert detail.status_code == 200
    assert detail.json()["run"] is None

    run = REGISTRY.begin("chat", "test_agent", thread_id, "turn-2")
    assert run is not None
    run.emit({"type": "start"})
    payload = api_client.get(f"/api/agents/test_agent/threads/{thread_id}").json()["run"]
    assert payload["run_id"] == run.run_id
    assert payload["running"] is True
    assert payload["cursor"] == 1  # attach from here and miss nothing


def test_baseline_security_headers_on_api_responses(api_client):
    resp = api_client.get("/api/status")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # Deliberately absent: a CSP has not been audited against the SPA, and HSTS
    # would lock a self-signed self-hosted box out.
    assert "content-security-policy" not in resp.headers
    assert "strict-transport-security" not in resp.headers


def test_agent_published_pages_keep_their_own_headers(api_client):
    """/pages/* is the owner's content and may be embedded elsewhere."""
    resp = api_client.get("/pages/some-slug/")
    assert "x-frame-options" not in resp.headers
    assert "x-content-type-options" not in resp.headers


def test_baseline_security_headers_on_auth_refusals(mock_runtime, tmp_workspace, monkeypatch):
    """The headers middleware is outermost, so a 401 the auth middleware
    returns without calling the route still carries them."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "sekrit-token")
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    with TestClient(create_api(mock_runtime), raise_server_exceptions=False) as c:
        resp = c.get("/api/agents", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_gmail_watch_card_endpoints(api_client, monkeypatch):
    """The Overview card reads GET …/gmail-watch (settings, the watch's last
    check, the Google connection) and saves through PUT, which re-registers
    the watch job live. The Advanced panel points at the card."""
    from olano.scheduler import cron, gmail_watch

    monkeypatch.setattr(
        gmail_watch, "google_connection", lambda agent: {"connected": False, "scope": "none"}
    )
    assert api_client.post("/api/agents", json={"name": "gw_probe"}).status_code == 200

    body = api_client.get("/api/agents/gw_probe/gmail-watch").json()
    assert body["enabled"] is False
    assert body["status"] == "off"
    assert body["google"] == {"connected": False, "scope": "none"}

    resynced: list[bool] = []

    class _Sched:
        def resync_agent_gmail_watch(self, cfg):
            resynced.append(cfg.gmail_watch.enabled)
            return True, ""

    monkeypatch.setattr(cron, "get_active_scheduler", lambda: _Sched())
    put = api_client.put(
        "/api/agents/gw_probe",
        json={"gmail_watch": {"enabled": True, "query": "label:trading", "poll_minutes": 5}},
    )
    assert put.status_code == 200, put.text
    assert resynced == [True]
    body = api_client.get("/api/agents/gw_probe/gmail-watch").json()
    assert body["enabled"] is True and body["query"] == "label:trading"

    schema = api_client.get("/api/agents/gw_probe/config/schema").json()
    assert schema["managed"]["gmail_watch"] == "overview"
    assert api_client.get("/api/agents/nope_probe/gmail-watch").status_code == 404


def test_get_thread_reports_the_model_it_last_ran_on(api_client):
    """The runner stamps every assistant row with its model; the thread
    payload surfaces the newest stamp so a reopened chat continues on it.
    Rows from before the stamp carry nothing and the key is simply absent."""
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "test_agent"
    sessions.mkdir(parents=True, exist_ok=True)
    tid = "55555555-5555-5555-5555-555555555555"
    rows = [
        {"ts": "2026-06-03T00:00:00Z", "role": "user", "content": "hi"},
        {"ts": "2026-06-03T00:00:01Z", "role": "assistant", "content": "a", "model": "olano:turbo"},
        {"ts": "2026-06-03T00:00:02Z", "role": "user", "content": "more"},
        {
            "ts": "2026-06-03T00:00:03Z",
            "role": "assistant",
            "content": "b",
            "model": "anthropic:claude-sonnet-4-6",
            "reasoning_effort": "high",
        },
        {"ts": "2026-06-03T00:00:04Z", "role": "user", "content": "unanswered yet"},
    ]
    (sessions / f"{tid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    legacy = "66666666-6666-6666-6666-666666666666"
    (sessions / f"{legacy}.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T00:00:01Z", "role": "assistant", "content": "old"}) + "\n",
        encoding="utf-8",
    )

    resp = api_client.get(f"/api/agents/test_agent/threads/{tid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "anthropic:claude-sonnet-4-6"
    assert body["reasoning_effort"] == "high"
    # Per-message provenance rides the projection too.
    models = [m.get("model") for m in body["messages"] if m["role"] == "assistant"]
    assert models == ["olano:turbo", "anthropic:claude-sonnet-4-6"]

    resp = api_client.get(f"/api/agents/test_agent/threads/{legacy}")
    assert resp.status_code == 200
    assert "model" not in resp.json()

    rail = {t["id"]: t for t in api_client.get("/api/agents/test_agent/threads").json()}
    assert rail[tid]["model"] == "anthropic:claude-sonnet-4-6"
    assert rail[legacy]["model"] == ""


def test_create_scheduler_task_with_a_model(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    with (
        patch("olano.runtime.api.SchedulerDB", return_value=db),
        patch("olano.admin.models.model_provider_connected", return_value=(True, "ok")),
    ):
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={
                "name": "Deep dive",
                "prompt": "Analyse the week",
                "schedule": "0 9 * * 1",
                "model": " olano:pro ",
            },
        )
        assert resp.status_code == 200
        task_id = resp.json()["id"]
        assert db.get_task(task_id)["model"] == "olano:pro"

        # "" on update returns the task to the agent's configured model.
        resp = api_client.put(f"/api/scheduler/tasks/{task_id}", json={"model": ""})
        assert resp.status_code == 200
        assert db.get_task(task_id)["model"] == ""


def test_create_scheduler_task_refuses_an_unusable_model(api_client, tmp_path):
    """A task on a model this box cannot run would fail every fire — refused
    at save time with the reason, so the form can show it next to the field."""
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    with (
        patch("olano.runtime.api.SchedulerDB", return_value=db),
        patch(
            "olano.admin.models.model_provider_connected",
            return_value=(False, "OPENAI_API_KEY is not set"),
        ),
    ):
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={"name": "x", "prompt": "y", "schedule": "0 9 * * *", "model": "openai:gpt-5"},
        )
    assert resp.status_code == 422
    assert "openai:gpt-5" in resp.json()["detail"]
    assert "OPENAI_API_KEY" in resp.json()["detail"]
    assert db.list_tasks() == []


def test_config_schedule_job_model_round_trips_through_the_api(api_client):
    """A `model` on an agent.json5 schedule entry is listed, editable and
    removed from the file again when cleared (never written as "")."""
    from olano.config.home import get_olano_paths

    adir = get_olano_paths().agents_dir / "sicoper"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "sicoper",
                "model": "fake:instant",
                "schedule": [{"cron": "0 10 * * *", "message": "sweep", "model": "olano:pro"}],
            }
        ),
        encoding="utf-8",
    )
    rows = {t["id"]: t for t in api_client.get("/api/scheduler/tasks").json()}
    assert rows["cfg:sicoper:0"]["model"] == "olano:pro"

    with patch("olano.admin.models.model_provider_connected", return_value=(True, "ok")):
        resp = api_client.put("/api/scheduler/tasks/cfg:sicoper:0", json={"model": "olano:max"})
    assert resp.status_code == 200
    data = json.loads((adir / "agent.json5").read_text(encoding="utf-8"))
    assert data["schedule"][0]["model"] == "olano:max"

    resp = api_client.put("/api/scheduler/tasks/cfg:sicoper:0", json={"model": ""})
    assert resp.status_code == 200
    data = json.loads((adir / "agent.json5").read_text(encoding="utf-8"))
    # The config writer dumps the full entry, so a cleared model reads as null.
    assert not data["schedule"][0].get("model")
    rows = {t["id"]: t for t in api_client.get("/api/scheduler/tasks").json()}
    assert rows["cfg:sicoper:0"]["model"] == ""
