"""Agent-scoped vault override endpoints (/api/agents/{name}/vault/…).

These power the agent edit "Variables" tab: a global admin OR the agent's
owner manages that one agent's per-agent overrides; plain members (chat-only
grant) are rejected; the scope is always forced to the agent in the path so
the surface can never read or write a common (shared) secret.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths
from olano.runtime.api import create_api
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore


def _runner(model="anthropic:claude-haiku-4-5-20251001", reply="ok"):
    r = MagicMock()
    r.config = MagicMock(model=model)
    r.ainvoke = AsyncMock(return_value=reply)
    return r


def _write_agent_config(name: str) -> None:
    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        f'{{"name": "{name}", "model": "anthropic:claude-haiku-4-5-20251001"}}\n',
        encoding="utf-8",
    )


@pytest.fixture
def app(lionclaw_home):
    runtime = MagicMock()
    runtime._runners = {"merli": _runner(), "scout": _runner()}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    # The endpoints 404 on agents with no config on disk.
    _write_agent_config("merli")
    _write_agent_config("scout")
    return create_api(runtime)


@pytest.fixture
def vault(lionclaw_home):
    """A real unlocked VaultStore registered with the resolver, reset after."""
    store = VaultStore.open(
        passphrase="test-passphrase",
        db_path=lionclaw_home / "vault-test.db",
        use_keyring=False,
    )
    store.set("OPENAI_API_KEY", "sk-common", description="shared key", category="api-keys")
    set_vault(store)
    yield store
    set_vault(None)


@pytest.fixture
def locked_vault(monkeypatch):
    """No vault registered and no way to auto-unlock → locked."""
    monkeypatch.delenv("LIONCLAW_VAULT_KEY", raising=False)
    monkeypatch.delenv("OLANO_VAULT_KEY", raising=False)
    set_vault(None)
    yield
    set_vault(None)


def _admin(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"})
    return c


def _make_member(app, admin: TestClient, email="bob@x.com") -> tuple[TestClient, str]:
    uid = admin.post(
        "/api/admin/users", json={"email": email, "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": email, "password": "pw"})
    return member, uid


# ── admin ────────────────────────────────────────────────────────────────────


def test_admin_full_crud(app, vault):
    admin = _admin(app)

    # Empty to start; the common key is listed as overridable metadata.
    listing = admin.get("/api/agents/merli/vault/secrets").json()
    assert listing["unlocked"] is True
    assert listing["secrets"] == []
    assert [c["name"] for c in listing["common"]] == ["OPENAI_API_KEY"]
    assert "value" not in listing["common"][0]

    # Create an override, then read it back.
    r = admin.post(
        "/api/agents/merli/vault/secrets",
        json={"name": "OPENAI_API_KEY", "value": "sk-merli", "description": "merli key"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "OPENAI_API_KEY", "agent": "merli"}

    rows = admin.get("/api/agents/merli/vault/secrets").json()["secrets"]
    assert [(x["name"], x["agent"]) for x in rows] == [("OPENAI_API_KEY", "merli")]

    reveal = admin.get("/api/agents/merli/vault/secrets/OPENAI_API_KEY/reveal")
    assert reveal.status_code == 200
    assert reveal.json() == {"name": "OPENAI_API_KEY", "value": "sk-merli", "agent": "merli"}

    # Delete removes the override but never the common value.
    assert admin.delete("/api/agents/merli/vault/secrets/OPENAI_API_KEY").status_code == 200
    assert admin.get("/api/agents/merli/vault/secrets").json()["secrets"] == []
    assert vault.get("OPENAI_API_KEY") == "sk-common"


def test_unknown_agent_404(app, vault):
    admin = _admin(app)
    assert admin.get("/api/agents/nope/vault/secrets").status_code == 404
    assert (
        admin.post("/api/agents/nope/vault/secrets", json={"name": "X", "value": "y"}).status_code
        == 404
    )


def test_validation_errors(app, vault):
    admin = _admin(app)
    assert (
        admin.post("/api/agents/merli/vault/secrets", json={"name": "", "value": "y"}).status_code
        == 400
    )
    assert (
        admin.post("/api/agents/merli/vault/secrets", json={"name": "X", "value": ""}).status_code
        == 400
    )


# ── scope containment ────────────────────────────────────────────────────────


def test_scope_forced_from_path(app, vault):
    """The path agent always wins — a body cannot target another agent or the
    common table, and reveal/delete never touch the common row."""
    admin = _admin(app)
    r = admin.post(
        "/api/agents/merli/vault/secrets",
        # A hostile/buggy client passing agent/category must not escape scope.
        json={"name": "TOKEN", "value": "v1", "agent": "scout", "category": "api-keys"},
    )
    assert r.json()["agent"] == "merli"
    assert vault.list_overrides(agent="scout") == []
    assert [x["name"] for x in vault.list_overrides(agent="merli")] == ["TOKEN"]
    # No common TOKEN row was created.
    assert "TOKEN" not in {x["name"] for x in vault.list_names()}


def test_reveal_never_falls_back_to_common(app, vault):
    """An owner/admin revealing through the agent surface must only ever see
    the override row — never the shared common value."""
    admin = _admin(app)
    # No override exists for merli, but the common OPENAI_API_KEY does.
    r = admin.get("/api/agents/merli/vault/secrets/OPENAI_API_KEY/reveal")
    assert r.status_code == 404

    # Delete without an override row → 404, and the common row survives.
    assert admin.delete("/api/agents/merli/vault/secrets/OPENAI_API_KEY").status_code == 404
    assert vault.get("OPENAI_API_KEY") == "sk-common"


# ── roles ────────────────────────────────────────────────────────────────────


def test_owner_can_manage_their_agents_variables(app, vault):
    admin = _admin(app)
    carol, carol_id = _make_member(app, admin, email="carol@x.com")
    admin.post("/api/agents/merli/access", json={"user_id": carol_id, "role": "owner"})

    r = carol.post(
        "/api/agents/merli/vault/secrets", json={"name": "SLACK_TOKEN", "value": "xoxb-1"}
    )
    assert r.status_code == 200
    assert [x["name"] for x in carol.get("/api/agents/merli/vault/secrets").json()["secrets"]] == [
        "SLACK_TOKEN"
    ]
    assert (
        carol.get("/api/agents/merli/vault/secrets/SLACK_TOKEN/reveal").json()["value"] == "xoxb-1"
    )
    assert carol.delete("/api/agents/merli/vault/secrets/SLACK_TOKEN").status_code == 200

    # Owner of merli is not owner of scout.
    assert carol.get("/api/agents/scout/vault/secrets").status_code == 403
    assert (
        carol.post("/api/agents/scout/vault/secrets", json={"name": "X", "value": "y"}).status_code
        == 403
    )

    # The org-wide vault stays admin-only for the non-admin owner.
    assert carol.get("/api/vault/secrets").status_code == 403
    assert carol.get("/api/vault/status").status_code == 403


def test_member_cannot_touch_agent_vault(app, vault):
    admin = _admin(app)
    member, uid = _make_member(app, admin)
    admin.post("/api/agents/merli/access", json={"user_id": uid})  # chat-only grant

    assert member.get("/api/agents/merli/vault/secrets").status_code == 403
    assert (
        member.post("/api/agents/merli/vault/secrets", json={"name": "X", "value": "y"}).status_code
        == 403
    )
    assert member.get("/api/agents/merli/vault/secrets/X/reveal").status_code == 403
    assert member.delete("/api/agents/merli/vault/secrets/X").status_code == 403
    # The blanket member block on agent mutations (e.g. reload-vault) still holds.
    assert member.post("/api/agents/merli/reload-vault").status_code == 403


# ── locked vault ─────────────────────────────────────────────────────────────


def test_locked_vault_list_is_soft_mutations_503(app, locked_vault):
    admin = _admin(app)
    listing = admin.get("/api/agents/merli/vault/secrets")
    assert listing.status_code == 200
    assert listing.json() == {"unlocked": False, "secrets": [], "common": []}

    assert (
        admin.post("/api/agents/merli/vault/secrets", json={"name": "X", "value": "y"}).status_code
        == 503
    )
    assert admin.get("/api/agents/merli/vault/secrets/X/reveal").status_code == 503
    assert admin.delete("/api/agents/merli/vault/secrets/X").status_code == 503
