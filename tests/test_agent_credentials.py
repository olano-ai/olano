"""Per-agent credential resolution (tools/_credentials.py).

Covers the headline goal — two agents running in parallel each resolve their
own account for the same service — plus the backward-compat fallbacks and the
runner wiring that binds the overlay on every execution path.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from olano.tools._credentials import (
    agent_runtime_context,
    get_credential,
    require_credentials,
)


@pytest.fixture(autouse=True)
def _clear_vault():
    """Keep the process-global vault singleton out of unrelated tests."""
    from olano.vault import resolver

    resolver.set_vault(None)
    yield
    resolver.set_vault(None)


# ── Core resolution ─────────────────────────────────────────────────────────


def test_overlay_switches_by_agent(monkeypatch):
    """The headline: same key, different value per agent context."""
    monkeypatch.delenv("X_API_KEY", raising=False)
    with agent_runtime_context("alice", {"X_API_KEY": "key-A"}):
        assert get_credential("X_API_KEY") == "key-A"
    with agent_runtime_context("bob", {"X_API_KEY": "key-B"}):
        assert get_credential("X_API_KEY") == "key-B"
    # Cleared once the context exits.
    assert get_credential("X_API_KEY") == ""


def test_overlay_beats_global_env_but_global_is_fallback(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "global")
    with agent_runtime_context("a", {"STRIPE_API_KEY": "per-agent"}):
        assert get_credential("STRIPE_API_KEY") == "per-agent"
    # Outside any agent context, fall straight through to os.environ.
    assert get_credential("STRIPE_API_KEY") == "global"


def test_no_overlay_reads_os_environ(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    assert get_credential("SLACK_BOT_TOKEN") == "xoxb-from-env"


def test_get_credential_default():
    assert get_credential("DEFINITELY_MISSING_KEY", "fallback") == "fallback"
    assert get_credential("DEFINITELY_MISSING_KEY") == ""


def test_empty_overlay_value_falls_through_to_env(monkeypatch):
    """A blank per-agent value should not shadow a real global one."""
    monkeypatch.setenv("NOTION_API_KEY", "real-global")
    with agent_runtime_context("a", {"NOTION_API_KEY": ""}):
        assert get_credential("NOTION_API_KEY") == "real-global"


def test_require_credentials_message_and_success(monkeypatch):
    monkeypatch.delenv("B_KEY", raising=False)
    with agent_runtime_context("a", {"A_KEY": "v"}):
        vals, err = require_credentials(["A_KEY", "B_KEY"], message="[missing-credentials] need B")
        assert err == "[missing-credentials] need B"
        assert vals == {}

        vals2, err2 = require_credentials(["A_KEY"], message="unused")
        assert err2 is None
        assert vals2 == {"A_KEY": "v"}


# ── Vault-ref resolution in the overlay ─────────────────────────────────────


def test_overlay_resolves_vault_refs():
    from olano.vault import resolver

    class FakeVault:
        def get(self, name: str) -> str:
            return {"HS_TOKEN": "secret-xyz"}[name]

    resolver.set_vault(FakeVault())
    try:
        with agent_runtime_context("a", {"HUBSPOT_ACCESS_TOKEN": "${vault:HS_TOKEN}"}):
            assert get_credential("HUBSPOT_ACCESS_TOKEN") == "secret-xyz"
    finally:
        resolver.set_vault(None)


# ── Concurrency isolation (the actual parallel-agents guarantee) ────────────


async def test_concurrent_agents_stay_isolated():
    results: dict[str, str] = {}

    async def run(agent: str, value: str) -> None:
        with agent_runtime_context(agent, {"X_API_KEY": value}):
            # Yield control between set and read so the two tasks interleave;
            # contextvars are per-task so they must not bleed across.
            await asyncio.sleep(0)
            results[agent] = get_credential("X_API_KEY")

    await asyncio.gather(run("alice", "key-A"), run("bob", "key-B"))
    assert results == {"alice": "key-A", "bob": "key-B"}


# ── Runner wiring: overlay is bound on both execution paths ─────────────────


async def test_ainvoke_binds_overlay(base_config, mock_agent, monkeypatch):
    """ainvoke covers scheduler / non-streaming /api/chat / webhooks."""
    monkeypatch.delenv("PROBE_CRED", raising=False)
    base_config.env = {"PROBE_CRED": "from-ainvoke"}
    seen: dict[str, str] = {}

    # `ainvoke` is a wrapper over the stream path, so the graph is driven
    # through `astream`; the overlay must be visible from inside it.
    async def _astream(payload, *args, **kwargs):
        seen["probe"] = get_credential("PROBE_CRED")
        yield (MagicMock(type="ai", content="ok"), {})

    mock_agent.astream = _astream

    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    await runner.ainvoke("hi", thread_id="t-ainvoke")
    assert seen["probe"] == "from-ainvoke"
    # And nothing leaked into the shared process env.
    import os

    assert "PROBE_CRED" not in os.environ


async def test_astream_events_binds_overlay(base_config, mock_agent, monkeypatch):
    monkeypatch.delenv("PROBE_CRED", raising=False)
    base_config.env = {"PROBE_CRED": "from-stream"}
    seen: dict[str, str] = {}

    async def _astream(*args, **kwargs):
        seen["probe"] = get_credential("PROBE_CRED")
        msg = MagicMock()
        msg.content = "ok"
        msg.type = "ai"
        yield (msg, {})

    mock_agent.astream = _astream

    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    async for _ in runner.astream_events("hi", thread_id="t-stream"):
        pass
    assert seen["probe"] == "from-stream"


# ── End-to-end through a real toolkit helper ────────────────────────────────


def test_hubspot_helper_uses_per_agent_token(monkeypatch):
    """A migrated toolkit helper resolves the per-agent token, not global env."""
    import olano.tools.hubspot_tools as h

    monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)
    captured: dict[str, str] = {}

    class FakeHubSpot:
        def __init__(self, access_token: str | None = None):
            captured["token"] = access_token or ""

    monkeypatch.setattr(
        h, "optional_import", lambda *a, **k: (MagicMock(HubSpot=FakeHubSpot), None)
    )

    # No overlay and no env → actionable missing-credentials error.
    client, err = h._hs_client()
    assert client is None
    assert err is not None and "missing-credentials" in err

    # Per-agent overlay supplies the token even though os.environ has none.
    with agent_runtime_context("hub-agent", {"HUBSPOT_ACCESS_TOKEN": "tok-A"}):
        client, err = h._hs_client()
    assert err is None
    assert captured["token"] == "tok-A"


# ── Vault-backed per-agent resolution (override → common → env) ──────────────


def _open_vault(tmp_path):
    from olano.vault.store import VaultStore

    return VaultStore.open(passphrase="pw", db_path=tmp_path / "v.db", use_keyring=False)


def test_get_credential_reads_vault_override_then_common(tmp_path, monkeypatch):
    from olano.vault import resolver

    monkeypatch.delenv("SVC_TOKEN", raising=False)
    v = _open_vault(tmp_path)
    v.set("SVC_TOKEN", "common")
    v.set("SVC_TOKEN", "alice-tok", agent="alice")
    resolver.set_vault(v)

    with agent_runtime_context("alice", {}):
        assert get_credential("SVC_TOKEN") == "alice-tok"
    with agent_runtime_context("bob", {}):
        assert get_credential("SVC_TOKEN") == "common"
    # No agent bound → common value.
    assert get_credential("SVC_TOKEN") == "common"


def test_overlay_beats_vault(tmp_path):
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("SVC_TOKEN", "vault-common")
    resolver.set_vault(v)

    with agent_runtime_context("alice", {"SVC_TOKEN": "overlay-wins"}):
        assert get_credential("SVC_TOKEN") == "overlay-wins"


def test_vault_beats_os_environ(tmp_path, monkeypatch):
    from olano.vault import resolver

    monkeypatch.setenv("SVC_TOKEN", "from-env")
    v = _open_vault(tmp_path)
    v.set("SVC_TOKEN", "from-vault")
    resolver.set_vault(v)

    with agent_runtime_context("alice", {}):
        assert get_credential("SVC_TOKEN") == "from-vault"


def test_vault_absent_key_falls_through_to_env(tmp_path, monkeypatch):
    from olano.vault import resolver

    monkeypatch.setenv("SVC_TOKEN", "from-env")
    resolver.set_vault(_open_vault(tmp_path))  # empty vault

    with agent_runtime_context("alice", {}):
        assert get_credential("SVC_TOKEN") == "from-env"


def test_vault_memo_is_stable_within_turn(tmp_path, monkeypatch):
    """Repeated reads in one turn return a stable value (memoised)."""
    from olano.vault import resolver

    monkeypatch.delenv("SVC_TOKEN", raising=False)
    v = _open_vault(tmp_path)
    v.set("SVC_TOKEN", "v1")
    resolver.set_vault(v)

    with agent_runtime_context("alice", {}):
        assert get_credential("SVC_TOKEN") == "v1"
        v.set("SVC_TOKEN", "v2")  # changed mid-turn
        assert get_credential("SVC_TOKEN") == "v1"  # memo holds for the turn
    # A fresh turn picks up the new value.
    with agent_runtime_context("alice", {}):
        assert get_credential("SVC_TOKEN") == "v2"


# ── Env hardening: no global hydration; per-agent MCP child env ─────────────


def test_resolve_agent_credentials_merges_common_and_override(tmp_path):
    from olano.vault.runtime import resolve_agent_credentials

    v = _open_vault(tmp_path)
    v.set("COMMON_ONLY", "c1")
    v.set("SHARED", "common")
    v.set("SHARED", "alice-override", agent="alice")
    v.set("ALICE_ONLY", "a-only", agent="alice")

    res_a = resolve_agent_credentials("alice", vault=v)
    assert res_a["COMMON_ONLY"] == "c1"
    assert res_a["SHARED"] == "alice-override"
    assert res_a["ALICE_ONLY"] == "a-only"

    res_b = resolve_agent_credentials("bob", vault=v)
    assert res_b["SHARED"] == "common"
    assert "ALICE_ONLY" not in res_b


def test_maybe_hydrate_env_is_gated(tmp_path, monkeypatch):
    from olano.vault import runtime as vrt

    v = _open_vault(tmp_path)
    v.set("HYDRATE_ME", "secret")
    monkeypatch.delenv("HYDRATE_ME", raising=False)
    try:
        # Hardened default: nothing leaks into the process env.
        monkeypatch.delenv("LIONCLAW_LEGACY_ENV_HYDRATE", raising=False)
        assert vrt.maybe_hydrate_env_from_vault(v) == []
        assert "HYDRATE_ME" not in os.environ

        # Opt-in restores the legacy global hydration.
        monkeypatch.setenv("LIONCLAW_LEGACY_ENV_HYDRATE", "1")
        injected = vrt.maybe_hydrate_env_from_vault(v)
        assert "HYDRATE_ME" in injected
        assert os.environ.get("HYDRATE_ME") == "secret"
    finally:
        os.environ.pop("HYDRATE_ME", None)


def test_agent_secret_env_injects_per_agent(tmp_path):
    from olano.core.mcp_setup import agent_secret_env
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("GITHUB_TOKEN", "common-gh")
    v.set("GITHUB_TOKEN", "alice-gh", agent="alice")
    resolver.set_vault(v)

    # The agent's resolved creds plus a server env that references the vault.
    env_a = agent_secret_env({"SERVER_SPECIFIC": "${vault:GITHUB_TOKEN}"}, "alice")
    assert env_a["GITHUB_TOKEN"] == "alice-gh"
    assert env_a["SERVER_SPECIFIC"] == "alice-gh"

    env_b = agent_secret_env({}, "bob")
    assert env_b["GITHUB_TOKEN"] == "common-gh"


def _sandboxed_stdio(monkeypatch, tmp_path, mode="sandboxed"):
    """Plan a stdio launch as a managed box would, without docker."""
    from olano.core import mcp_setup

    monkeypatch.setattr(mcp_setup, "_stdio_launch_decision", lambda: (mode, None))
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image", lambda: "olano-sandbox:test")
    monkeypatch.setattr(mcp_setup, "_prepare_stdio_sandbox_paths", lambda agent, ws: None)
    monkeypatch.setattr(mcp_setup, "_agent_workspace_root", lambda agent: tmp_path)
    return mcp_setup


def test_sandboxed_stdio_env_is_the_declared_block_only(tmp_path, monkeypatch):
    """A directory package in an ephemeral container gets what the server
    declares — never every secret the agent can see."""
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("GITHUB_TOKEN", "common-gh")
    v.set("GITHUB_TOKEN", "alice-gh", agent="alice")
    v.set("STRIPE_API_KEY", "sk_live_never_forwarded")
    resolver.set_vault(v)
    mcp_setup = _sandboxed_stdio(monkeypatch, tmp_path)

    plan = mcp_setup.plan_stdio_launch(
        "npx", ["-y", "server"], {"SERVER_SPECIFIC": "${vault:GITHUB_TOKEN}"}, "alice"
    )
    inv = plan.invocation
    assert inv is not None and inv.sandboxed
    assert inv.env["SERVER_SPECIFIC"] == "alice-gh"
    assert "GITHUB_TOKEN" not in inv.env
    assert "STRIPE_API_KEY" not in inv.env
    flags = [inv.args[i + 1] for i, a in enumerate(inv.args) if a == "-e"]
    assert "SERVER_SPECIFIC" in flags
    assert "GITHUB_TOKEN" not in flags and "STRIPE_API_KEY" not in flags
    # Name-only: no value ever rides the argv.
    assert not any("alice-gh" in a or "sk_live" in a for a in inv.args)


def test_sandboxed_stdio_env_resolves_a_connected_broker_token(tmp_path, monkeypatch):
    """ "Connected in the dashboard" reaches a stdio MCP server too: the ref
    resolves through get_credential, so a broker record beats the vault."""
    from olano.core.connectors import get_provider
    from olano.core.connectors.base import _save_record, _token_path_for
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("GITHUB_TOKEN", "vault-gh")
    resolver.set_vault(v)
    provider = get_provider("github")
    assert provider is not None
    _save_record(
        {"service": "github", "access_token": "broker-gh", "expires_at": None},
        _token_path_for("github", "alice"),
    )
    mcp_setup = _sandboxed_stdio(monkeypatch, tmp_path)
    plan = mcp_setup.plan_stdio_launch(
        "npx", ["server"], {"GITHUB_PERSONAL_ACCESS_TOKEN": "${vault:GITHUB_TOKEN}"}, "alice"
    )
    assert plan.invocation is not None
    assert plan.invocation.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "broker-gh"


def test_host_stdio_env_still_merges_the_vault(tmp_path, monkeypatch):
    """Open posture is byte-identical to before: the operator's own box."""
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("GITHUB_TOKEN", "common-gh")
    resolver.set_vault(v)
    mcp_setup = _sandboxed_stdio(monkeypatch, tmp_path, mode="host")
    plan = mcp_setup.plan_stdio_launch("npx", ["server"], {}, "bob")
    assert plan.invocation is not None and not plan.invocation.sandboxed
    assert plan.invocation.env["GITHUB_TOKEN"] == "common-gh"


def test_child_scope_resolves_parent_override(tmp_path):
    """A sub-agent under a fresh name resolves as the parent it acts for."""
    from olano.tools._credentials import agent_runtime_context, get_credential
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("X_API_KEY", "common")
    v.set("X_API_KEY", "alice-own", agent="alice")
    resolver.set_vault(v)
    with agent_runtime_context("alice__cog__task__ab12cd", None, credential_scope="alice"):
        assert get_credential("X_API_KEY") == "alice-own"
    with agent_runtime_context("alice__cog__task__ab12cd", None):
        assert get_credential("X_API_KEY") == "common"


# ── Per-agent model-provider key via vault override ─────────────────────────


def test_provider_key_override_merged_into_build_env(tmp_path):
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _merge_provider_override
    from olano.vault import resolver

    v = _open_vault(tmp_path)
    v.set("ANTHROPIC_API_KEY", "common-key")
    v.set("ANTHROPIC_API_KEY", "alice-key", agent="alice")
    resolver.set_vault(v)

    cfg_alice = AgentConfig(name="alice", model="anthropic:claude-sonnet-4-6")
    env_a: dict[str, str] = {}
    _merge_provider_override(env_a, cfg_alice)
    assert env_a.get("ANTHROPIC_API_KEY") == "alice-key"

    # An agent with no override is left to the common-key path (not added here).
    cfg_bob = AgentConfig(name="bob", model="anthropic:claude-sonnet-4-6")
    env_b: dict[str, str] = {}
    _merge_provider_override(env_b, cfg_bob)
    assert "ANTHROPIC_API_KEY" not in env_b

    # config.env wins — an already-present key is never clobbered.
    env_c = {"ANTHROPIC_API_KEY": "from-config-env"}
    _merge_provider_override(env_c, cfg_alice)
    assert env_c["ANTHROPIC_API_KEY"] == "from-config-env"
