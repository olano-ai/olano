"""Runtime-owned vault entries never reach a person or an agent (``vault/reserved.py``).

The property under test: the rows the runtime writes about its own standing
with the control plane (the model-gateway key, the relay signing key, the
deployment token) are invisible to every listing, refused by every reveal,
edit, delete and override, and unresolvable from every path an agent has into
the vault, while the runtime's own direct reads are untouched. One test per
surface, so a new resolver cannot quietly leak them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.vault.reserved import (
    RUNTIME_SECRET_NAMES,
    RUNTIME_SECRET_REFUSAL,
    is_runtime_secret,
    without_runtime_secrets,
)
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore

GATEWAY_KEY = "OLANO_GATEWAY_KEY"
LEGACY_GATEWAY_KEY = "LIONCLAW_OLANO_GATEWAY_KEY"
USER_KEY = "OPENAI_API_KEY"


@pytest.fixture
def vault(lionclaw_home):
    """A real unlocked vault holding one user secret and the runtime's rows."""
    store = VaultStore.open(
        passphrase="test-passphrase",
        db_path=lionclaw_home / "vault-reserved.db",
        use_keyring=False,
    )
    store.set(USER_KEY, "sk-user", description="the user's key", category="api-keys")
    store.set(GATEWAY_KEY, "sk-gateway", description="gateway", category="system")
    store.set("OLANO_GATEWAY_URL", "https://gw.test", category="system")
    store.set(LEGACY_GATEWAY_KEY, "sk-legacy-gateway", category="system")
    store.set("LIONCLAW_MCP_RELAY_PRIVATE_KEY", "relay-pem", category="system")
    # An override under a runtime name must be just as invisible as the row.
    store.set(GATEWAY_KEY, "sk-gateway-override", agent="merli")
    set_vault(store)
    yield store
    set_vault(None)


# ── the set itself ────────────────────────────────────────────────────────────


def test_every_writer_constant_is_reserved():
    """The modules that file ``category="system"`` rows name them by constant;
    each constant, in both spellings, must be in the reserved set."""
    from olano.core import mcp_relay, mcp_relay_selfhost
    from olano.deployment import gateway_key, sources
    from olano.env_aliases import spellings
    from olano.observability import credentials as langfuse_credentials
    from olano.revisions import remote

    constants = (
        gateway_key.GATEWAY_KEY_ENV,
        gateway_key.GATEWAY_URL_ENV,
        gateway_key.GATEWAY_KEY_DEAD_MARKER,
        gateway_key.ACCOUNT_KEY_ENV,
        gateway_key.LEGACY_GATEWAY_KEY_ENV,
        gateway_key.LEGACY_GATEWAY_URL_ENV,
        gateway_key.LEGACY_GATEWAY_KEY_DEAD_MARKER,
        mcp_relay._RELAY_PRIVATE_KEY_ENV,
        mcp_relay._RELAY_SECRET_ENV,
        mcp_relay_selfhost.ENROLLMENT_VAULT_NAME,
        sources.DEPLOYMENT_TOKEN_ENV,
        remote.TOKEN_VAULT_NAME,
        langfuse_credentials.PUBLIC_KEY_NAME,
        langfuse_credentials.SECRET_KEY_NAME,
        langfuse_credentials.HOST_NAME,
        langfuse_credentials.USER_SALT_NAME,
        langfuse_credentials.PROJECT_ID_NAME,
    )
    for name in constants:
        for spelling in spellings(name):
            assert is_runtime_secret(spelling), spelling


def test_only_known_writers_file_system_rows():
    """A new ``category="system"`` write anywhere else is a new runtime-owned
    row that the reserved set does not know about."""
    root = Path(__file__).resolve().parents[1] / "olano"
    writers = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'category=(?:"system"|VAULT_ROW_CATEGORY)', text):
            writers.add(path.relative_to(root).as_posix())
    assert writers == {
        "deployment/gateway_key.py",
        "core/mcp_relay_enroll.py",
        "core/mcp_relay_selfhost.py",
        "revisions/remote.py",
        "observability/credentials.py",
    }, writers


def test_user_names_are_not_reserved():
    assert not is_runtime_secret(USER_KEY)
    assert not is_runtime_secret("TELEGRAM_BOT_TOKEN")
    assert not is_runtime_secret("")
    assert not is_runtime_secret(None)
    # The alias layer pairs OLANO_GATEWAY_KEY with LIONCLAW_GATEWAY_KEY; a
    # mirror is a second copy of the same secret, so it is reserved too.
    assert "LIONCLAW_GATEWAY_KEY" in RUNTIME_SECRET_NAMES
    assert "OLANO_OLANO_GATEWAY_KEY" in RUNTIME_SECRET_NAMES


def test_without_runtime_secrets_filters_by_name():
    rows = [{"name": USER_KEY}, {"name": GATEWAY_KEY}, {"name": LEGACY_GATEWAY_KEY}, {}]
    assert without_runtime_secrets(rows) == [{"name": USER_KEY}, {}]


# ── agent-side resolution ─────────────────────────────────────────────────────


def test_get_credential_never_resolves_a_runtime_secret(vault, monkeypatch):
    """Not from the vault, and not from the environment either: the env is
    where provisioning puts the key, and ``get_credential`` is what every
    toolkit calls."""
    from olano.tools._credentials import agent_runtime_context, get_credential

    monkeypatch.setenv(GATEWAY_KEY, "sk-from-env")
    monkeypatch.setenv(LEGACY_GATEWAY_KEY, "sk-from-legacy-env")
    with agent_runtime_context("merli", None):
        assert get_credential(GATEWAY_KEY, "absent") == "absent"
        assert get_credential(LEGACY_GATEWAY_KEY, "absent") == "absent"
        assert get_credential(USER_KEY) == "sk-user"  # the user's own rows still resolve
    assert get_credential(GATEWAY_KEY) == ""


def test_vault_refs_in_config_never_resolve_a_runtime_secret(vault, monkeypatch):
    """``${vault:NAME}`` and bare ``${NAME}`` in an agent.json5 go through
    ``resolver.lookup_credential``; a runtime name stays a literal."""
    from olano.vault.resolver import lookup_credential, resolve_vault_refs

    monkeypatch.setenv(GATEWAY_KEY, "sk-from-env")
    assert lookup_credential(GATEWAY_KEY) is None
    assert lookup_credential(GATEWAY_KEY, agent="merli") is None
    assert lookup_credential(USER_KEY) == "sk-user"
    assert resolve_vault_refs("Bearer ${vault:OLANO_GATEWAY_KEY}") == (
        "Bearer ${vault:OLANO_GATEWAY_KEY}"
    )
    assert resolve_vault_refs("${OLANO_GATEWAY_KEY}", agent="merli") == "${OLANO_GATEWAY_KEY}"
    assert resolve_vault_refs("${vault:OPENAI_API_KEY}") == "sk-user"


def test_mcp_header_and_env_refs_never_resolve_a_runtime_secret(vault):
    from olano.core.mcp_setup import _resolve_agent_ref, agent_secret_headers

    assert (
        _resolve_agent_ref("${vault:OLANO_GATEWAY_KEY}", "merli", vault)
        == "${vault:OLANO_GATEWAY_KEY}"
    )
    assert _resolve_agent_ref("${vault:OPENAI_API_KEY}", "merli", vault) == "sk-user"
    headers = agent_secret_headers({"Authorization": "Bearer ${vault:OLANO_GATEWAY_KEY}"}, "merli")
    assert headers == {"Authorization": "Bearer ${vault:OLANO_GATEWAY_KEY}"}


def test_stdio_mcp_hand_off_leaves_runtime_secrets_out(vault):
    """The whole-vault env a host-launched stdio MCP server receives."""
    from olano.vault.runtime import resolve_agent_credentials

    resolved = resolve_agent_credentials("merli", vault)
    assert resolved == {USER_KEY: "sk-user"}
    assert not any(is_runtime_secret(name) for name in resolved)


def test_legacy_env_hydration_leaves_runtime_secrets_out(vault, monkeypatch):
    from olano.vault.runtime import hydrate_env_from_vault

    for name in (USER_KEY, GATEWAY_KEY, LEGACY_GATEWAY_KEY, "OLANO_GATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)

    injected = hydrate_env_from_vault(vault, overwrite=True)

    assert injected == [USER_KEY]
    assert GATEWAY_KEY not in os.environ
    assert LEGACY_GATEWAY_KEY not in os.environ


def test_secret_tools_refuse_and_omit_runtime_secrets(vault):
    from olano.tools.secret_tools import secret_get, secret_list

    got = secret_get.invoke({"name": GATEWAY_KEY, "reveal": True})
    assert "sk-gateway" not in got
    assert RUNTIME_SECRET_REFUSAL in got

    listing = secret_list.invoke({})
    assert USER_KEY in listing
    assert GATEWAY_KEY not in listing
    assert LEGACY_GATEWAY_KEY not in listing
    assert "RELAY_PRIVATE_KEY" not in listing

    overrides = secret_list.invoke({"agent": "merli"})
    assert GATEWAY_KEY not in overrides


# ── the runtime's own reads are untouched ─────────────────────────────────────


def test_runtime_direct_reads_still_work(vault, monkeypatch):
    from olano.deployment import gateway_key as gk

    monkeypatch.delenv(GATEWAY_KEY, raising=False)
    monkeypatch.delenv(LEGACY_GATEWAY_KEY, raising=False)
    monkeypatch.setattr(gk, "_memory_key", None)
    monkeypatch.setattr(gk, "_dead_env_digest", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    assert gk.gateway_key() == "sk-gateway"
    assert gk.gateway_url() == "https://gw.test"


# ── the dashboard API ─────────────────────────────────────────────────────────


def _runner():
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value="ok")
    return r


@pytest.fixture
def admin(lionclaw_home, vault):
    from olano.config.home import get_olano_paths
    from olano.runtime.api import create_api

    agent_dir = get_olano_paths().agents_dir / "merli"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        '{"name": "merli", "model": "anthropic:claude-haiku-4-5-20251001"}\n', encoding="utf-8"
    )
    runtime = MagicMock()
    runtime._runners = {"merli": _runner()}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    client = TestClient(create_api(runtime))
    client.post(
        "/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"}
    )
    return client


def test_vault_listing_omits_runtime_rows(admin):
    rows = admin.get("/api/vault/secrets", params={"include_overrides": "true"}).json()
    names = {row["name"] for row in rows}
    assert USER_KEY in names
    assert not names & RUNTIME_SECRET_NAMES, names & RUNTIME_SECRET_NAMES
    assert admin.get("/api/vault/secrets", params={"agent": "merli"}).json() == []
    # The category filter is not a side door either.
    assert admin.get("/api/vault/secrets", params={"category": "system"}).json() == []


@pytest.mark.parametrize("name", [GATEWAY_KEY, LEGACY_GATEWAY_KEY, "OLANO_GATEWAY_URL"])
def test_vault_reveal_edit_delete_and_override_are_refused(admin, vault, name):
    assert admin.get(f"/api/vault/secrets/{name}/reveal").status_code == 403
    assert (
        admin.get(f"/api/vault/secrets/{name}/reveal", params={"agent": "merli"}).status_code == 403
    )
    r = admin.post("/api/vault/secrets", json={"name": name, "value": "x"})
    assert r.status_code == 403
    assert r.json()["detail"] == RUNTIME_SECRET_REFUSAL
    assert (
        admin.post(
            "/api/vault/secrets", json={"name": name, "value": "x", "agent": "merli"}
        ).status_code
        == 403
    )
    assert admin.delete(f"/api/vault/secrets/{name}").status_code == 403
    assert admin.delete(f"/api/vault/secrets/{name}", params={"agent": "merli"}).status_code == 403
    # Nothing moved.
    assert vault.get(GATEWAY_KEY, touch=False) == "sk-gateway"
    assert vault.get(GATEWAY_KEY, agent="merli", touch=False) == "sk-gateway-override"


def test_vault_user_rows_still_work(admin):
    assert admin.get(f"/api/vault/secrets/{USER_KEY}/reveal").json()["value"] == "sk-user"
    assert (
        admin.post("/api/vault/secrets", json={"name": "NEW_KEY", "value": "v"}).status_code == 200
    )


def test_agent_variables_tab_omits_and_refuses_runtime_rows(admin, vault):
    body = admin.get("/api/agents/merli/vault/secrets").json()
    assert body["unlocked"] is True
    assert {row["name"] for row in body["common"]} == {USER_KEY}
    assert body["secrets"] == []  # the override under OLANO_GATEWAY_KEY is not shown
    assert admin.get(f"/api/agents/merli/vault/secrets/{GATEWAY_KEY}/reveal").status_code == 403
    assert (
        admin.post(
            "/api/agents/merli/vault/secrets", json={"name": GATEWAY_KEY, "value": "x"}
        ).status_code
        == 403
    )
    assert admin.delete(f"/api/agents/merli/vault/secrets/{GATEWAY_KEY}").status_code == 403
    assert vault.get(GATEWAY_KEY, agent="merli", touch=False) == "sk-gateway-override"
