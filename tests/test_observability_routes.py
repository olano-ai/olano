"""Who can switch tracing, and the admin routes that manage it.

Turning tracing on (or to ``full`` capture) sends an agent's conversations to
an outside service, and switching it off hides what the agent does from
whoever reviews its traces, so both directions are an administrator's call:
the agent-config tools refuse a change without an admin human on the turn,
and every ``/api/observability`` route is admin-only. The keys go in through
one route and never come back out.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.config.loader import load_agent_config, save_agent_config
from olano.config.system_settings import load_observability_settings
from olano.observability import credentials
from olano.security.request_context import ORIGIN_KEY, make_origin, push_request_context
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore

PK = "pk-lf-routes-0001"
SK = "sk-lf-routes-0002"


@pytest.fixture
def agent(lionclaw_home: Path) -> str:
    save_agent_config(AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001"))
    return "merli"


def _dashboard_admin(**over):
    kw = {
        "source": "dashboard",
        "channel": "dashboard",
        "user_id": "u-admin",
        "authenticated": True,
        "is_owner": True,
        "is_admin": True,
        "metadata": {ORIGIN_KEY: make_origin("human", True, root_user_id="u-admin")},
    }
    kw.update(over)
    return push_request_context(**kw)


# ── the agent-config tools ───────────────────────────────────────────────────


def _update(name, updates):
    from olano.tools.olano_admin import olano_update_agent

    return json.loads(olano_update_agent.invoke({"name": name, "updates": updates}))


@pytest.mark.parametrize("source", ["peer", "scheduled", "cognition", "mcp", "a2a"])
def test_a_change_without_an_admin_human_is_refused(agent, source):
    with push_request_context(source=source, is_admin=True):
        out = _update(agent, {"observability": {"langfuse": {"capture": "full"}}})
    assert out.get("refused_fields") == ["observability"]
    assert load_agent_config(agent).observability.langfuse.capture is None
    with push_request_context(source=source, is_admin=True):
        out = _update(agent, {"observability": {"langfuse": {"enabled": False}}})
    assert out.get("refused_fields") == ["observability"]


def test_resending_the_unchanged_block_is_not_gated(agent):
    with push_request_context(source="peer", is_admin=True):
        out = _update(
            agent,
            {
                "description": "new words",
                "observability": {
                    "langfuse": {"enabled": None, "capture": None, "sample_rate": None}
                },
            },
        )
    assert "refused_fields" not in out, out
    assert load_agent_config(agent).description == "new words"


def test_an_admin_human_may_change_it(agent):
    with _dashboard_admin():
        out = _update(agent, {"observability": {"langfuse": {"enabled": True, "capture": "full"}}})
    assert "refused_fields" not in out, out
    cfg = load_agent_config(agent)
    assert cfg.observability.langfuse.enabled is True
    assert cfg.observability.langfuse.capture == "full"


def test_create_agent_with_tracing_needs_an_admin(lionclaw_home):
    from olano.tools.olano_admin import olano_create_agent

    with push_request_context(source="peer", is_admin=True):
        out = json.loads(
            olano_create_agent.invoke(
                {"name": "newbie", "extra": {"observability": {"langfuse": {"enabled": True}}}}
            )
        )
    assert out.get("refused_fields") == ["observability"]


# ── the routes ────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(lionclaw_home):
    store = VaultStore.open(
        passphrase="pw", db_path=lionclaw_home / "vault-obs-routes.db", use_keyring=False
    )
    set_vault(store)
    credentials._reset_for_tests()
    yield store
    set_vault(None)
    credentials._reset_for_tests()


def _runtime():
    runtime = MagicMock()
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value="ok")
    runtime._runners = {"merli": r}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return runtime


@pytest.fixture
def admin(agent, vault):
    from olano.runtime.api import create_api

    client = TestClient(create_api(_runtime()))
    client.post(
        "/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"}
    )
    return client


def test_routes_are_admin_only(agent):
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES

    assert any("/api/observability/langfuse/status".startswith(p) for p in _ADMIN_ONLY_PREFIXES)


def test_save_status_and_clear_credentials(admin, vault):
    r = admin.put(
        "/api/observability/langfuse/credentials",
        json={"public_key": PK, "secret_key": SK, "host": "https://jp.cloud.langfuse.com/"},
    )
    assert r.status_code == 200, r.text
    assert vault.get(credentials.SECRET_KEY_NAME, touch=False) == SK
    assert vault.get(credentials.HOST_NAME, touch=False) == "https://jp.cloud.langfuse.com"
    status = admin.get("/api/observability/langfuse/status").json()
    assert status["credential_source"] == "vault"
    assert status["effective_host"] == "https://jp.cloud.langfuse.com"
    assert SK not in json.dumps(status)
    assert PK not in json.dumps(status)  # only a prefix is shown
    # The Vault page never lists them.
    names = {row["name"] for row in admin.get("/api/vault/secrets").json()}
    assert credentials.SECRET_KEY_NAME not in names
    assert admin.delete("/api/observability/langfuse/credentials").status_code == 200
    assert admin.get("/api/observability/langfuse/status").json()["credential_source"] == "none"


def test_per_agent_credentials(admin, vault):
    r = admin.put(
        "/api/observability/langfuse/credentials?agent=merli",
        json={"public_key": PK, "secret_key": SK},
    )
    assert r.status_code == 200
    assert vault.get_override(credentials.SECRET_KEY_NAME, "merli") == SK
    assert (
        admin.put(
            "/api/observability/langfuse/credentials?agent=ghost",
            json={"public_key": PK, "secret_key": SK},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"public_key": "pk-x", "secret_key": SK}, "pk-lf-"),
        ({"public_key": PK, "secret_key": "secret"}, "sk-lf-"),
        ({"public_key": PK, "secret_key": SK, "host": "http://lf.example.com"}, "https://"),
    ],
)
def test_bad_credentials_are_refused(admin, body, fragment):
    r = admin.put("/api/observability/langfuse/credentials", json=body)
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_system_config_round_trip_and_validation(admin):
    r = admin.put(
        "/api/system/config",
        json={
            "observability": {"langfuse": {"enabled": True, "capture": "full", "sample_rate": 0.5}}
        },
    )
    assert r.status_code == 200, r.text
    got = admin.get("/api/system/config").json()["observability"]["langfuse"]
    assert got["enabled"] is True and got["capture"] == "full" and got["sample_rate"] == 0.5
    for bad in (
        {"capture": "everything"},
        {"sample_rate": 3},
        {"host": "http://lf.example.com"},
        {"enabled": "yes"},
    ):
        r = admin.put("/api/system/config", json={"observability": {"langfuse": bad}})
        assert r.status_code == 400, bad


def _fake_get(status: int, payload: dict | None = None):
    def _get(url, auth=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=payload or {}, request=request)

    return _get


def test_connection_test_stores_the_project_id(admin, monkeypatch):
    monkeypatch.setattr(
        httpx, "get", _fake_get(200, {"data": [{"id": "proj-9", "name": "My Project"}]})
    )
    r = admin.post(
        "/api/observability/langfuse/test",
        json={"public_key": PK, "secret_key": SK, "host": "https://jp.cloud.langfuse.com"},
    )
    body = r.json()
    assert body["ok"] is True and body["project_name"] == "My Project"
    assert load_observability_settings().langfuse.project_id == "proj-9"


def test_connection_test_names_the_region_on_401(admin, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(401))
    body = admin.post(
        "/api/observability/langfuse/test",
        json={"public_key": PK, "secret_key": SK, "host": "https://cloud.langfuse.com"},
    ).json()
    assert body["ok"] is False and body["reason"] == "unauthorized"
    assert "https://jp.cloud.langfuse.com" in body["message"]


def test_connection_test_unreachable(admin, monkeypatch):
    def _down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _down)
    body = admin.post(
        "/api/observability/langfuse/test",
        json={"public_key": PK, "secret_key": SK, "host": "https://lf.example.com"},
    ).json()
    assert body["ok"] is False and body["reason"] == "unreachable"


def test_connection_test_without_keys(admin):
    r = admin.post("/api/observability/langfuse/test", json={})
    assert r.status_code == 400 and "No Langfuse keys" in r.json()["detail"]


def test_agentfather_cannot_patch_the_home_block(lionclaw_home):
    """``olano_update_home_config`` patches every other config.yaml block; this
    one is the dashboard's alone, so it is reported as ignored, never written."""
    from olano.admin.config import update_home_config

    out = update_home_config({"observability": {"langfuse": {"enabled": True, "capture": "full"}}})
    assert "observability" not in out["applied"]
    assert any(item["key"] == "observability" for item in out["ignored"])
    assert load_observability_settings().langfuse.enabled is False
