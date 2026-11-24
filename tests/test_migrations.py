"""Tests for olano/migrations/__init__.py migration runner."""

from __future__ import annotations

import importlib
import json
import types
from pathlib import Path
from unittest.mock import patch

from olano.migrations import run_pending_migrations


def _make_migration(name: str, call_log: list) -> types.ModuleType:
    mod = types.ModuleType(f"olano.migrations.{name}")
    mod.ID = name

    def apply(home: Path) -> None:
        call_log.append((name, str(home)))

    mod.apply = apply
    return mod


def _patch_migrations(*mods):
    """Context manager that patches _load_migrations to return `mods`."""
    return patch("olano.migrations._load_migrations", return_value=list(mods))


def test_no_migrations_runs_clean(tmp_path: Path):
    with _patch_migrations():
        result = run_pending_migrations(tmp_path, [])
    assert result == []


def test_migration_applied_once(tmp_path: Path):
    log: list = []
    m = _make_migration("0001_test", log)
    with _patch_migrations(m):
        first = run_pending_migrations(tmp_path, [])
        second = run_pending_migrations(tmp_path, first)
    assert first == ["0001_test"]
    assert second == []
    assert len(log) == 1  # only called once


def test_migration_order(tmp_path: Path):
    log: list = []
    m1 = _make_migration("0001_alpha", log)
    m2 = _make_migration("0002_beta", log)
    # Pass them in reverse order — should still run 0001 first (sorted by ID)
    with _patch_migrations(m2, m1):
        # Re-sort happens inside, but our mock returns in given order.
        # The actual loader sorts; we test sorted mods here.
        pass
    # Sort the mods by ID ourselves to simulate what _load_migrations would do
    sorted_mods = sorted([m1, m2], key=lambda m: m.ID)
    with patch("olano.migrations._load_migrations", return_value=sorted_mods):
        result = run_pending_migrations(tmp_path, [])
    assert result == ["0001_alpha", "0002_beta"]
    assert [entry[0] for entry in log] == ["0001_alpha", "0002_beta"]


def test_already_applied_skipped(tmp_path: Path):
    log: list = []
    m1 = _make_migration("0001_done", log)
    m2 = _make_migration("0002_new", log)
    with _patch_migrations(m1, m2):
        result = run_pending_migrations(tmp_path, ["0001_done"])
    assert result == ["0002_new"]
    assert len(log) == 1
    assert log[0][0] == "0002_new"


# ── 0002_agent_visibility_default ────────────────────────────────────────────


def _seed_agent(home: Path, name: str, cfg: dict) -> Path:
    d = home / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "agent.json5"
    p.write_text(json.dumps({"name": name, **cfg}), encoding="utf-8")
    return p


def test_visibility_migration_sets_default_and_is_idempotent(tmp_path: Path):
    import importlib

    from olano.config.loader import _load_json5

    mig = importlib.import_module("olano.migrations.0002_agent_visibility_default")

    home = tmp_path / "home"
    p_missing = _seed_agent(home, "worker", {"model": "anthropic:claude-haiku-4-5-20251001"})
    # An agent that deliberately chose a non-default mode must be left untouched.
    p_explicit = _seed_agent(home, "hub", {"agent_visibility": {"mode": "all", "agents": []}})

    mig.apply(home)

    got_missing = _load_json5(p_missing.read_text())
    assert got_missing["agent_visibility"] == {"mode": "none", "agents": []}
    got_explicit = _load_json5(p_explicit.read_text())
    assert got_explicit["agent_visibility"] == {"mode": "all", "agents": []}

    # Idempotent: a second run rewrites nothing (bytes stable).
    before = p_missing.read_bytes()
    mig.apply(home)
    assert p_missing.read_bytes() == before


def test_visibility_migration_no_agents_dir_is_noop(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0002_agent_visibility_default")

    # No agents/ directory — must not raise.
    mig.apply(tmp_path / "empty-home")


# ── 0003: grant olano_install_skill to existing AgentFathers ───────────────


def _agent_json(home: Path, name: str, data: dict) -> Path:
    import json

    agent_dir = home / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_0003_grants_install_skill_next_to_create_skill(tmp_path: Path):
    import importlib
    import json

    mig = importlib.import_module("olano.migrations.0003_agentfather_install_skill")

    path = _agent_json(
        tmp_path,
        "agentfather",
        {"name": "agentfather", "tools": ["olano_list_agents", "olano_create_skill"]},
    )
    bystander = _agent_json(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == [
        "olano_list_agents",
        "olano_create_skill",
        "olano_install_skill",
        "olano_list_agent_workspace",
        "olano_read_agent_file",
        "olano_write_agent_file",
    ]
    assert "olano_install_skill" not in json.loads(bystander.read_text(encoding="utf-8"))["tools"]

    before = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)  # idempotent
    assert path.read_text(encoding="utf-8") == before


def test_0003_respects_an_explicit_opt_out(tmp_path: Path):
    import importlib
    import json

    mig = importlib.import_module("olano.migrations.0003_agentfather_install_skill")

    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_create_skill"],
            "disabled_tools": ["olano_install_skill"],
        },
    )

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert "olano_install_skill" not in tools  # opted out — respected
    assert "olano_write_agent_file" in tools  # the rest still granted


# ── 0004: grant the lifecycle tools to existing AgentFathers ─────────────────


def test_0004_grants_lifecycle_tools_next_to_agent_crud(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0004_agentfather_lifecycle_tools")

    path = _agent_json(
        tmp_path,
        "agentfather",
        {"name": "agentfather", "tools": ["olano_delete_agent", "olano_list_skills"]},
    )

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == [
        "olano_delete_agent",
        "olano_start_agent",
        "olano_stop_agent",
        "olano_restart_agent",
        "olano_list_skills",
    ]

    before = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)  # idempotent
    assert path.read_text(encoding="utf-8") == before


def test_0004_respects_an_explicit_opt_out(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0004_agentfather_lifecycle_tools")

    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_delete_agent"],
            "disabled_tools": ["olano_stop_agent"],
        },
    )

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert "olano_stop_agent" not in tools  # opted out — respected
    assert "olano_start_agent" in tools
    assert "olano_restart_agent" in tools


# ── 0005: grant the catalog-read tools to existing AgentFathers ──────────────


def test_0005_grants_catalog_read_tools_next_to_their_anchors(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0005_agentfather_catalog_read_tools")

    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": [
                "olano_list_archetypes",
                "olano_scaffold_archetype",
                "olano_describe_tool",
            ],
        },
    )

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == [
        "olano_list_archetypes",
        "olano_read_archetype",
        "olano_scaffold_archetype",
        "olano_describe_tool",
        "olano_find_tools",
    ]

    before = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)  # idempotent
    assert path.read_text(encoding="utf-8") == before


def test_0005_appends_when_the_anchor_is_absent(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0005_agentfather_catalog_read_tools")

    path = _agent_json(tmp_path, "agentfather", {"name": "agentfather", "tools": ["whoami"]})
    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == ["whoami", "olano_read_archetype", "olano_find_tools"]


def test_0005_respects_an_explicit_opt_out_and_skips_other_agents(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0005_agentfather_catalog_read_tools")

    father = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_describe_tool"],
            "disabled_tools": ["olano_find_tools"],
        },
    )
    other = _agent_json(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})

    mig.apply(tmp_path)

    father_tools = json.loads(father.read_text(encoding="utf-8"))["tools"]
    assert "olano_find_tools" not in father_tools  # opted out — respected
    assert "olano_read_archetype" in father_tools
    assert json.loads(other.read_text(encoding="utf-8"))["tools"] == ["web_search"]


# ── 0007: move channel credentials out of every config and into the vault ────


class _FakeVault:
    def __init__(self) -> None:
        self.stored: dict[tuple[str, str | None], str] = {}

    def set(self, name: str, value: str, agent: str | None = None, **kw) -> None:
        self.stored[(name, agent)] = value


def _patch_vault(monkeypatch) -> _FakeVault:
    vault = _FakeVault()
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: vault)
    return vault


def test_0007_moves_a_literal_token_and_deletes_the_field(tmp_path: Path, monkeypatch):
    import importlib

    vault = _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(
        tmp_path,
        "marisol",
        {
            "name": "marisol",
            "channels": [{"type": "telegram", "token": "8182:AAHsecret", "allowed_users": ["1"]}],
        },
    )

    mig.apply(tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["channels"][0] == {"type": "telegram", "allowed_users": ["1"]}
    assert vault.stored == {("TELEGRAM_BOT_TOKEN", "marisol"): "8182:AAHsecret"}
    assert "8182:AAHsecret" not in path.read_text(encoding="utf-8")


def test_0007_drops_a_now_redundant_reference(tmp_path: Path, monkeypatch):
    """Already vaulted — the reference says only what the convention says."""
    import importlib

    vault = _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(
        tmp_path,
        "marisol",
        {
            "name": "marisol",
            "channels": [{"type": "telegram", "token": "${vault:TELEGRAM_BOT_TOKEN}"}],
        },
    )

    mig.apply(tmp_path)

    assert json.loads(path.read_text(encoding="utf-8"))["channels"][0] == {"type": "telegram"}
    assert vault.stored == {}  # nothing to re-store


def test_0019_moves_an_mcp_client_secret_into_the_vault(tmp_path: Path, monkeypatch):
    """The MCP sibling of 0007: the secret leaves the file, a reference stays."""
    import importlib

    vault = _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0019_mcp_client_secrets_to_vault")
    path = _agent_json(
        tmp_path,
        "merli",
        {
            "name": "merli",
            "mcp_servers": [
                {
                    "url": "https://mcp.hubspot.com",
                    "transport": "http",
                    "auth": "oauth",
                    "oauth_client_id": "cid",
                    "oauth_client_secret": "hs-secret",
                },
                {"url": "https://mcp.stripe.com", "auth": "oauth"},
            ],
        },
    )
    untouched = _agent_json(tmp_path, "kev", {"name": "kev", "tools": ["web_search"]})

    mig.apply(tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcp_servers"][0]["oauth_client_secret"] == (
        "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}"
    )
    assert data["mcp_servers"][0]["oauth_client_id"] == "cid"
    assert data["mcp_servers"][1] == {"url": "https://mcp.stripe.com", "auth": "oauth"}
    assert vault.stored == {("MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM", "merli"): "hs-secret"}
    assert "hs-secret" not in path.read_text(encoding="utf-8")
    assert json.loads(untouched.read_text(encoding="utf-8")) == {
        "name": "kev",
        "tools": ["web_search"],
    }

    # Idempotent: a second run finds only references.
    mig.apply(tmp_path)
    assert len(vault.stored) == 1


def test_0007_keeps_a_reference_to_a_different_key(tmp_path: Path, monkeypatch):
    """A non-canonical key is a deliberate override — never touch it."""
    import importlib

    _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(
        tmp_path,
        "marisol",
        {
            "name": "marisol",
            "channels": [{"type": "telegram", "token": "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"}],
        },
    )

    mig.apply(tmp_path)

    channel = json.loads(path.read_text(encoding="utf-8"))["channels"][0]
    assert channel["token"] == "${vault:MARISOL_TELEGRAM_BOT_TOKEN}"


def test_0007_is_idempotent(tmp_path: Path, monkeypatch):
    import importlib

    _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(
        tmp_path,
        "marisol",
        {"name": "marisol", "channels": [{"type": "telegram", "token": "tok"}]},
    )

    mig.apply(tmp_path)
    once = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)

    assert path.read_text(encoding="utf-8") == once


def test_0007_leaves_the_token_in_place_when_the_vault_is_locked(tmp_path: Path, monkeypatch):
    """A locked vault must never lose a credential."""
    import importlib

    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: None)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(
        tmp_path,
        "marisol",
        {"name": "marisol", "channels": [{"type": "telegram", "token": "still-here"}]},
    )

    mig.apply(tmp_path)

    assert json.loads(path.read_text(encoding="utf-8"))["channels"][0]["token"] == "still-here"


def test_0007_skips_agents_with_no_channels(tmp_path: Path, monkeypatch):
    import importlib

    _patch_vault(monkeypatch)
    mig = importlib.import_module("olano.migrations.0007_channel_credentials_to_vault")
    path = _agent_json(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})
    before = path.read_text(encoding="utf-8")

    mig.apply(tmp_path)

    assert path.read_text(encoding="utf-8") == before


# ── 0008: grant the model catalog tool to existing AgentFathers ──────────────


def test_0008_grants_the_model_catalog_tool(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0008_agentfather_model_catalog_tool")
    path = _agent_json(
        tmp_path,
        "agentfather",
        {"name": "agentfather", "tools": ["olano_list_agents", "olano_list_tools"]},
    )
    bystander = _agent_json(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == ["olano_list_agents", "olano_list_models", "olano_list_tools"]
    assert "olano_list_models" not in json.loads(bystander.read_text(encoding="utf-8"))["tools"]

    before = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)  # idempotent
    assert path.read_text(encoding="utf-8") == before


def test_0008_respects_an_explicit_opt_out(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0008_agentfather_model_catalog_tool")
    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_list_tools"],
            "disabled_tools": ["olano_list_models"],
        },
    )

    mig.apply(tmp_path)

    assert "olano_list_models" not in json.loads(path.read_text(encoding="utf-8"))["tools"]


# ── 0009: grant the cross-agent run tool to existing AgentFathers ────────────


def test_0009_grants_the_run_tool_next_to_write(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0009_agentfather_run_agent_command")
    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_read_agent_file", "olano_write_agent_file"],
        },
    )
    bystander = _agent_json(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == [
        "olano_read_agent_file",
        "olano_write_agent_file",
        "olano_run_agent_command",
    ]
    assert (
        "olano_run_agent_command" not in json.loads(bystander.read_text(encoding="utf-8"))["tools"]
    )

    before = path.read_text(encoding="utf-8")
    mig.apply(tmp_path)  # idempotent
    assert path.read_text(encoding="utf-8") == before


def test_0009_appends_when_the_anchor_is_absent(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0009_agentfather_run_agent_command")
    path = _agent_json(
        tmp_path, "agentfather", {"name": "agentfather", "tools": ["olano_list_agents"]}
    )

    mig.apply(tmp_path)

    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert tools == ["olano_list_agents", "olano_run_agent_command"]


def test_0009_respects_an_explicit_opt_out(tmp_path: Path):
    import importlib

    mig = importlib.import_module("olano.migrations.0009_agentfather_run_agent_command")
    path = _agent_json(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_write_agent_file"],
            "disabled_tools": ["olano_run_agent_command"],
        },
    )

    mig.apply(tmp_path)

    assert "olano_run_agent_command" not in json.loads(path.read_text(encoding="utf-8"))["tools"]


# ── the optional applicable() gate ────────────────────────────────────────────
#
# A migration may declare applicable() -> bool: False means "nothing to do on
# this box YET" — skip without recording, so the migration still runs on the
# boot where the condition flips. Recording the skip is how 0011 stranded
# posture-raced boxes on the BYOK default forever.


def _make_gated_migration(name: str, call_log: list, gate) -> types.ModuleType:
    mod = _make_migration(name, call_log)
    mod.applicable = gate
    return mod


def test_inapplicable_migration_is_neither_run_nor_recorded(tmp_path: Path):
    log: list = []
    m = _make_gated_migration("0001_gated", log, lambda: False)
    with _patch_migrations(m):
        assert run_pending_migrations(tmp_path, []) == []
    assert log == []


def test_migration_runs_on_the_pass_where_it_becomes_applicable(tmp_path: Path):
    log: list = []
    gate = {"on": False}
    m = _make_gated_migration("0001_gated", log, lambda: gate["on"])
    with _patch_migrations(m):
        assert run_pending_migrations(tmp_path, []) == []
        gate["on"] = True
        assert run_pending_migrations(tmp_path, []) == ["0001_gated"]
    assert len(log) == 1


def test_pending_ids_exclude_inapplicable_migrations(tmp_path: Path):
    """pending_migration_ids feeds the boot fast path: a perpetually-deferred
    migration must not read as pending, or every boot on such a box re-runs
    the whole upgrade pass for nothing."""
    from olano.migrations import pending_migration_ids

    m1 = _make_gated_migration("0001_gated", [], lambda: False)
    m2 = _make_migration("0002_plain", [])
    with _patch_migrations(m1, m2):
        assert pending_migration_ids([]) == ["0002_plain"]


# ── 0013: handshake → magic_links ────────────────────────────────────────────


def _write_agent(home: Path, name: str, payload: dict) -> Path:
    path = home / "agents" / name / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_0013_moves_the_config_key_and_renames_the_tool(tmp_path: Path):
    import yaml

    mig = importlib.import_module("olano.migrations.0013_magic_links_rename")

    config = tmp_path / "config.yaml"
    config.write_text(
        "cognition:\n  enabled: false\nhandshake:\n  ttl_minutes: 42\n", encoding="utf-8"
    )
    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["handshake_status", "connect_service"],
            "system_prompt": "Check with handshake_status when asked.",
        },
    )

    mig.apply(tmp_path)

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert "handshake" not in raw
    assert raw["magic_links"] == {"ttl_minutes": 42}
    assert raw["cognition"] == {"enabled": False}

    data = json.loads(agent.read_text(encoding="utf-8"))
    assert data["tools"] == ["magic_link_status", "connect_service"]
    assert "magic_link_status" in data["system_prompt"]
    assert "handshake_status" not in data["system_prompt"]


def test_0013_is_idempotent_and_leaves_a_clean_home_alone(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0013_magic_links_rename")

    config = tmp_path / "config.yaml"
    config.write_text("magic_links:\n  ttl_minutes: 7\n", encoding="utf-8")
    agent = _write_agent(tmp_path, "merli", {"name": "merli", "tools": ["magic_link_status"]})
    before = (config.read_text(encoding="utf-8"), agent.read_text(encoding="utf-8"))

    mig.apply(tmp_path)
    mig.apply(tmp_path)

    assert (config.read_text(encoding="utf-8"), agent.read_text(encoding="utf-8")) == before


# ── 0015: AgentFather MCP directory tools ────────────────────────────────────


def test_0015_grants_the_directory_tools_next_to_their_anchors(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0015_agentfather_mcp_directory_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_list_agents", "olano_list_mcp_servers", "olano_test_mcp_server"],
        },
    )
    bystander = _write_agent(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})
    before_bystander = bystander.read_text(encoding="utf-8")

    mig.apply(tmp_path)

    data = json.loads(agent.read_text(encoding="utf-8"))
    assert data["tools"] == [
        "olano_list_agents",
        "olano_list_mcp_servers",
        "olano_search_mcp_directory",
        "olano_get_mcp_directory_entry",
        "olano_test_mcp_server",
    ]
    # Non-AgentFather agents are untouched.
    assert bystander.read_text(encoding="utf-8") == before_bystander


def test_0015_is_idempotent_and_respects_a_disabled_tools_opt_out(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0015_agentfather_mcp_directory_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_list_mcp_servers"],
            "disabled_tools": ["olano_search_mcp_directory"],
        },
    )

    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))
    # The opted-out tool stays out; its sibling lands (appended — its anchor,
    # the opted-out tool, never materialized).
    assert "olano_search_mcp_directory" not in data["tools"]
    assert "olano_get_mcp_directory_entry" in data["tools"]

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0015_survives_escaped_emoji_in_a_json5_fallback_config(tmp_path: Path):
    """The regression that killed 0010 on a real config, pinned for the first
    time: a file that takes the json5 fallback parse (unquoted key) with an
    escaped emoji yields lone surrogates, and an unrepaired
    json.dumps(ensure_ascii=False) payload dies in write_text. The migration
    must complete and write valid UTF-8."""
    mig = importlib.import_module("olano.migrations.0015_agentfather_mcp_directory_tools")

    path = tmp_path / "agents" / "agentfather" / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unquoted key forces the json5 package (surrogate-yielding) parse path.
    path.write_text(
        '{name: "agentfather", "emoji": "\\ud83e\\udd81", "tools": ["olano_list_mcp_servers"]}',
        encoding="utf-8",
    )

    mig.apply(tmp_path)

    text = path.read_text(encoding="utf-8")  # raises if the write produced garbage
    data = json.loads(text)
    assert data["emoji"] == "🦁"
    assert "olano_search_mcp_directory" in data["tools"]


def test_0015_skips_a_top_level_array_file_without_aborting_the_pass(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0015_agentfather_mcp_directory_tools")

    weird = tmp_path / "agents" / "broken" / "agent.json5"
    weird.parent.mkdir(parents=True, exist_ok=True)
    weird.write_text('["not", "a", "config"]', encoding="utf-8")
    agent = _write_agent(
        tmp_path, "agentfather", {"name": "agentfather", "tools": ["olano_list_mcp_servers"]}
    )

    mig.apply(tmp_path)  # must not raise

    data = json.loads(agent.read_text(encoding="utf-8"))
    assert "olano_search_mcp_directory" in data["tools"]


# ── 0016: magic_link_* tool prefix ───────────────────────────────────────────


def test_0016_renames_tools_disabled_tools_and_the_prompt(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["connect_service", "open_dashboard", "olano_list_agents"],
            "disabled_tools": ["request_file_edit"],
            "system_prompt": "Use connect_service to connect, then open_dashboard.",
            "subagents": [{"name": "helper", "tools": ["request_secret"]}],
        },
    )

    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))

    assert data["tools"] == [
        "magic_link_connect_service",
        "magic_link_open_dashboard",
        "olano_list_agents",
    ]
    # An opt-out must survive as an opt-out, or the rename silently re-enables it.
    assert data["disabled_tools"] == ["magic_link_request_file_edit"]
    assert "magic_link_connect_service" in data["system_prompt"]
    assert "magic_link_open_dashboard" in data["system_prompt"]
    # Nested subagent tool lists are rewritten too.
    assert data["subagents"][0]["tools"] == ["magic_link_request_secret"]


def test_0016_rewrites_tool_names_used_as_dict_keys(tmp_path: Path):
    """security.tools and rate_limits.per_tool are keyed BY tool name. Left
    stale, an operator's deliberate 'always ask' becomes dead config while the
    tool keeps running — a security regression that looks like nothing."""
    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["connect_service"],
            "security": {
                "tools": {"open_dashboard": {"approval": "always"}},
                "rate_limits": {"per_tool": {"request_secret": 2}},
            },
        },
    )

    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))

    assert data["security"]["tools"] == {"magic_link_open_dashboard": {"approval": "always"}}
    assert data["security"]["rate_limits"]["per_tool"] == {"magic_link_request_secret": 2}


def test_0016_leaves_mcp_server_disabled_tools_alone(tmp_path: Path):
    """Those are a third-party server's tool names, not ours."""
    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["connect_service"],
            "mcp_servers": [{"url": "https://x.test", "disabled_tools": ["open_dashboard"]}],
        },
    )

    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))
    assert data["mcp_servers"][0]["disabled_tools"] == ["open_dashboard"]
    assert data["tools"] == ["magic_link_connect_service"]


def test_0016_is_idempotent_and_leaves_a_clean_home_alone(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")

    agent = _write_agent(
        tmp_path, "merli", {"name": "merli", "tools": ["magic_link_connect_service"]}
    )
    before = agent.read_text(encoding="utf-8")

    mig.apply(tmp_path)
    mig.apply(tmp_path)

    assert agent.read_text(encoding="utf-8") == before


def test_0016_survives_escaped_emoji_in_a_json5_fallback_config(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")

    path = tmp_path / "agents" / "agentfather" / "agent.json5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{name: "agentfather", "emoji": "\\ud83e\\udd81", "tools": ["connect_service"]}',
        encoding="utf-8",
    )

    mig.apply(tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["emoji"] == "🦁"
    assert data["tools"] == ["magic_link_connect_service"]


def test_every_renamed_magic_link_name_resolves_after_the_migration():
    """Both spellings must resolve: the new one directly, the old one through
    registry._ALIASES for configs nobody migrated."""
    from olano.tools.registry import resolve_tools

    mig = importlib.import_module("olano.migrations.0016_magic_link_tool_prefix")
    for old, new in mig.RENAMES.items():
        assert [t.name for t in resolve_tools([new])] == [new]
        assert [t.name for t in resolve_tools([old])] == [new], f"alias for {old} is missing"


def test_renamed_magic_link_tools_are_still_hitl_gated():
    """The category lists match raw names with no alias resolution, so a rename
    that missed hitl_policy would silently un-gate six owner-only tools that
    mint dashboard sessions and vault-writing forms."""
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"] | CATEGORY_TOOLS["secret_write"]
    for new in (
        "magic_link_connect_service",
        "magic_link_request_secret",
        "magic_link_edit_instructions",
        "magic_link_request_file_edit",
        "magic_link_connect_channels",
        "magic_link_open_dashboard",
    ):
        assert new in gated, f"{new} lost its human-approval gate"


# ── 0017: AgentFather user-admin + disk + skill-repair tools ─────────────────


def test_0017_grants_the_admin_tools_next_to_their_anchors(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0017_agentfather_admin_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": [
                "olano_update_home_config",
                "olano_install_skill",
                "olano_memory_monitor",
                "olano_tail_log",
            ],
        },
    )
    bystander = _write_agent(tmp_path, "merli", {"name": "merli", "tools": ["web_search"]})
    before_bystander = bystander.read_text(encoding="utf-8")

    mig.apply(tmp_path)

    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]
    # The user block lands as one run right after its anchor.
    start = tools.index("olano_update_home_config") + 1
    assert tools[start : start + 10] == [
        "olano_list_users",
        "olano_get_user",
        "olano_create_user",
        "olano_reset_user_password",
        "olano_set_user_role",
        "olano_update_user",
        "olano_delete_user",
        "olano_grant_agent_access",
        "olano_revoke_agent_access",
        "olano_access_overview",
    ]
    assert tools[tools.index("olano_install_skill") + 1] == "olano_repair_skill_names"
    assert tools[tools.index("olano_memory_monitor") + 1] == "olano_disk_report"
    assert tools[tools.index("olano_disk_report") + 1] == "olano_disk_monitor"
    # Non-AgentFather agents are untouched.
    assert bystander.read_text(encoding="utf-8") == before_bystander


def test_0017_is_idempotent_and_respects_a_disabled_tools_opt_out(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0017_agentfather_admin_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_update_home_config"],
            "disabled_tools": ["olano_delete_user"],
        },
    )

    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))
    # The opted-out tool stays out; its siblings land.
    assert "olano_delete_user" not in data["tools"]
    assert "olano_create_user" in data["tools"]
    assert "olano_disk_monitor" in data["tools"]

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0017_user_admin_tools_are_hitl_gated_and_owner_only():
    """The category lists match raw names, and the pipeline's owner gate reads
    @tool_metadata — a tool that missed either would let a non-admin member (or
    an unapproved turn) mint login links or change who holds admin."""
    from olano.security.hitl_policy import CATEGORY_TOOLS
    from olano.security.tool_metadata import get_tool_metadata
    from olano.tools import olano_admin

    mutating = (
        "olano_create_user",
        "olano_reset_user_password",
        "olano_set_user_role",
        "olano_update_user",
        "olano_delete_user",
        "olano_grant_agent_access",
        "olano_revoke_agent_access",
    )
    for name in mutating + ("olano_disk_monitor", "olano_repair_skill_names"):
        assert name in CATEGORY_TOOLS["write_external"], f"{name} lost its approval gate"
    for name in mutating + ("olano_list_users", "olano_get_user", "olano_access_overview"):
        meta = get_tool_metadata(olano_admin.get_tool(name))
        assert meta.owner_only is True, f"{name} must be owner-only"
    for name in (
        "olano_list_users",
        "olano_get_user",
        "olano_access_overview",
        "olano_disk_report",
    ):
        assert name not in CATEGORY_TOOLS["write_external"]


def test_0017_patches_a_stock_prompt(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0017_agentfather_admin_tools")

    prompt = (
        "You are AgentFather.\n"
        "  • Credentials live in the vault. For non-secret runtime settings\n"
        "    use `olano_update_home_config`.\n"
        "  • Lifecycle control: stopping is non-destructive.\n"
        "  • Diagnose before guessing. Start with `olano_runtime_status`.\n"
    )
    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_update_home_config"],
            "system_prompt": prompt,
        },
    )

    mig.apply(tmp_path)

    out = json.loads(agent.read_text(encoding="utf-8"))["system_prompt"]
    # The config sentence is extended in place…
    assert "reports honestly: check `ignored`" in out
    assert "use `olano_update_home_config`.\n" not in out
    # …and the team-accounts rule lands before the Diagnose bullet, teaching
    # the password-less flow.
    assert out.index("• Team accounts:") < out.index("• Diagnose before guessing.")
    assert "never set, ask for, or accept a password" in out

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0017_appends_to_a_customized_prompt_and_skips_an_updated_one(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0017_agentfather_admin_tools")

    # An operator-rewritten prompt: no stock anchors → the bullet appends at
    # the end and nothing else is rewritten.
    custom = "You are a fully custom operator prompt with no stock bullets."
    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_update_home_config"],
            "system_prompt": custom,
        },
    )
    mig.apply(tmp_path)
    out = json.loads(agent.read_text(encoding="utf-8"))["system_prompt"]
    assert out.startswith(custom)
    assert "• Team accounts:" in out
    assert "check `ignored`" not in out  # no old sentence → nothing extended

    # A prompt that already teaches the flow, with every tool granted, is not
    # touched at all — the file stays byte-identical.
    home2 = tmp_path / "h2"
    all_tools = [name for name, _ in mig._TOOLS]
    agent2 = _write_agent(
        home2,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_update_home_config", *all_tools],
            "system_prompt": "Custom prompt that already covers olano_create_user invites.",
        },
    )
    before = agent2.read_text(encoding="utf-8")
    mig.apply(home2)
    assert agent2.read_text(encoding="utf-8") == before


def test_0017_prompt_only_change_still_writes(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0017_agentfather_admin_tools")

    all_tools = [name for name, _ in mig._TOOLS]
    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_update_home_config", *all_tools],
            "system_prompt": "Custom.\n",
        },
    )
    mig.apply(tmp_path)
    out = json.loads(agent.read_text(encoding="utf-8"))["system_prompt"]
    assert "• Team accounts:" in out


# ── 0018: move the API-server key onto the OLANO_ name ───────────────────────


class _RenameVault:
    """Enough vault to exercise the rename: named rows, per-agent overrides."""

    def __init__(self, rows: dict[tuple[str, str | None], str], *, locked: bool = False) -> None:
        self.rows = dict(rows)
        self.locked = locked

    def list_names(self) -> list[dict]:
        return [{"name": n} for (n, a) in self.rows if a is None]

    def list_overrides(self, name: str | None = None, agent: str | None = None) -> list[dict]:
        return [
            {"name": n, "agent": a}
            for (n, a) in self.rows
            if a is not None and (name is None or n == name)
        ]

    def get(self, name: str, agent: str | None = None, touch: bool = True) -> str:
        if self.locked:
            raise RuntimeError("vault is locked")
        try:
            return self.rows[(name, agent)]
        except KeyError:
            raise RuntimeError(f"Credential '{name}' not found in vault") from None

    def set(self, name: str, value: str, agent: str | None = None, **kw) -> None:
        self.rows[(name, agent)] = value

    def delete(self, name: str, agent: str | None = None) -> bool:
        return self.rows.pop((name, agent), None) is not None


def _mig_0018():
    return importlib.import_module("olano.migrations.0018_api_server_vault_key_rename")


def _patch_rename_vault(monkeypatch, vault) -> None:
    monkeypatch.setattr("olano.vault.resolver._vault_instance", vault, raising=False)


def test_0018_moves_overrides_and_the_shared_row(tmp_path: Path, monkeypatch):
    vault = _RenameVault(
        {
            ("LIONCLAW_API_KEY", "marisol"): "marisols-key",
            ("LIONCLAW_API_KEY", None): "shared-key",
        }
    )
    _patch_rename_vault(monkeypatch, vault)

    _mig_0018().apply(tmp_path)

    assert vault.rows == {
        ("OLANO_API_KEY", "marisol"): "marisols-key",
        ("OLANO_API_KEY", None): "shared-key",
    }


def test_0018_is_idempotent(tmp_path: Path, monkeypatch):
    vault = _RenameVault({("LIONCLAW_API_KEY", "marisol"): "k"})
    _patch_rename_vault(monkeypatch, vault)
    mig = _mig_0018()

    mig.apply(tmp_path)
    mig.apply(tmp_path)

    assert vault.rows == {("OLANO_API_KEY", "marisol"): "k"}


def test_0018_never_clobbers_a_value_already_under_the_new_name(tmp_path: Path, monkeypatch):
    """The canonical row is the newer intent — the stale legacy row goes."""
    vault = _RenameVault(
        {
            ("OLANO_API_KEY", "marisol"): "current",
            ("LIONCLAW_API_KEY", "marisol"): "stale",
        }
    )
    _patch_rename_vault(monkeypatch, vault)

    _mig_0018().apply(tmp_path)

    assert vault.rows == {("OLANO_API_KEY", "marisol"): "current"}


def test_0018_leaves_everything_alone_when_the_vault_is_locked(tmp_path: Path, monkeypatch):
    """A key that cannot be read must not be deleted — the runtime fallback
    still resolves the old name, so the connector keeps working."""
    vault = _RenameVault({("LIONCLAW_API_KEY", "marisol"): "unreadable"}, locked=True)
    _patch_rename_vault(monkeypatch, vault)

    _mig_0018().apply(tmp_path)

    assert vault.rows == {("LIONCLAW_API_KEY", "marisol"): "unreadable"}


def test_0018_is_a_noop_with_nothing_to_move(tmp_path: Path, monkeypatch):
    vault = _RenameVault({("TELEGRAM_BOT_TOKEN", "marisol"): "unrelated"})
    _patch_rename_vault(monkeypatch, vault)

    _mig_0018().apply(tmp_path)

    assert vault.rows == {("TELEGRAM_BOT_TOKEN", "marisol"): "unrelated"}


def test_0018_survives_a_box_with_no_vault(tmp_path: Path, monkeypatch):
    _patch_rename_vault(monkeypatch, None)
    _mig_0018().apply(tmp_path)  # must not raise


# ── 0025: move the gateway rows onto the OLANO_GATEWAY_* names ───────────────


def _mig_0025():
    return importlib.import_module("olano.migrations.0025_gateway_vault_key_rename")


def test_0025_moves_the_three_gateway_rows(tmp_path: Path, monkeypatch):
    vault = _RenameVault(
        {
            ("LIONCLAW_OLANO_GATEWAY_KEY", None): "sk-key",
            ("LIONCLAW_OLANO_GATEWAY_URL", None): "https://gw.test",
            ("LIONCLAW_OLANO_GATEWAY_KEY_REVOKED_DIGEST", None): "abcd",
            ("TELEGRAM_BOT_TOKEN", None): "unrelated",
        }
    )
    _patch_rename_vault(monkeypatch, vault)

    _mig_0025().apply(tmp_path)

    assert vault.rows == {
        ("OLANO_GATEWAY_KEY", None): "sk-key",
        ("OLANO_GATEWAY_URL", None): "https://gw.test",
        ("OLANO_GATEWAY_KEY_REVOKED_DIGEST", None): "abcd",
        ("TELEGRAM_BOT_TOKEN", None): "unrelated",
    }


def test_0025_rows_match_what_enrollment_writes(tmp_path: Path, monkeypatch):
    """The migrated row must be indistinguishable from one the runtime filed
    itself: same description, same category, so the vault stays one story."""
    from olano.deployment import gateway_key as gk

    mig = _mig_0025()
    assert mig.RENAMES == gk.LEGACY_NAMES.__class__(
        (legacy, new) for new, legacy in gk.LEGACY_NAMES.items()
    )
    # The migration describes exactly the rows it renames; the runtime's table
    # also carries rows it never touches (the account API key).
    assert mig.DESCRIPTIONS == {new: gk.VAULT_ROW_DESCRIPTIONS[new] for new in mig.RENAMES.values()}
    assert mig.CATEGORY == gk.VAULT_ROW_CATEGORY

    seen: list[dict] = []

    class _Recording(_RenameVault):
        def set(self, name, value, agent=None, **kw):
            seen.append({"name": name, **kw})
            super().set(name, value, agent=agent, **kw)

    vault = _Recording({("LIONCLAW_OLANO_GATEWAY_KEY", None): "sk-key"})
    _patch_rename_vault(monkeypatch, vault)
    mig.apply(tmp_path)
    assert seen == [
        {
            "name": "OLANO_GATEWAY_KEY",
            "description": gk.VAULT_ROW_DESCRIPTIONS["OLANO_GATEWAY_KEY"],
            "category": "system",
        }
    ]


def test_0025_is_idempotent(tmp_path: Path, monkeypatch):
    vault = _RenameVault({("LIONCLAW_OLANO_GATEWAY_KEY", None): "sk-key"})
    _patch_rename_vault(monkeypatch, vault)
    mig = _mig_0025()

    mig.apply(tmp_path)
    mig.apply(tmp_path)

    assert vault.rows == {("OLANO_GATEWAY_KEY", None): "sk-key"}


def test_0025_never_clobbers_a_newer_enrollment(tmp_path: Path, monkeypatch):
    """A box that enrolled after the upgrade already holds the canonical row,
    and that key is the live one; the legacy row is a revoked predecessor."""
    vault = _RenameVault(
        {
            ("LIONCLAW_OLANO_GATEWAY_KEY", None): "sk-revoked",
            ("OLANO_GATEWAY_KEY", None): "sk-current",
        }
    )
    _patch_rename_vault(monkeypatch, vault)

    _mig_0025().apply(tmp_path)

    assert vault.rows == {("OLANO_GATEWAY_KEY", None): "sk-current"}


def test_0025_leaves_everything_alone_when_the_vault_is_locked(tmp_path: Path, monkeypatch):
    vault = _RenameVault({("LIONCLAW_OLANO_GATEWAY_KEY", None): "unreadable"}, locked=True)
    _patch_rename_vault(monkeypatch, vault)

    _mig_0025().apply(tmp_path)

    assert vault.rows == {("LIONCLAW_OLANO_GATEWAY_KEY", None): "unreadable"}


def test_0025_ignores_per_agent_overrides(tmp_path: Path, monkeypatch):
    """The runtime never writes overrides for these names; one that exists was
    typed by a person, and the migration does not guess at its intent."""
    vault = _RenameVault({("LIONCLAW_OLANO_GATEWAY_KEY", "merli"): "typed"})
    _patch_rename_vault(monkeypatch, vault)

    _mig_0025().apply(tmp_path)

    assert vault.rows == {("LIONCLAW_OLANO_GATEWAY_KEY", "merli"): "typed"}


def test_0025_survives_a_box_with_no_vault(tmp_path: Path, monkeypatch):
    _patch_rename_vault(monkeypatch, None)
    _mig_0025().apply(tmp_path)  # must not raise


# ── 0021: AgentFather fleet scheduler tools ──────────────────────────────────


def test_0021_grants_the_six_fleet_scheduler_tools_contiguously(tmp_path: Path):
    """Chained anchors put them in call order after olano_disk_monitor, so an
    AgentFather created before this release reads the same as a fresh one."""
    mig = importlib.import_module("olano.migrations.0021_agentfather_scheduler_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_list_agents", "olano_disk_monitor", "olano_update"],
        },
    )
    bystander = _write_agent(tmp_path, "merli", {"name": "merli", "tools": ["scheduler_list"]})
    before_bystander = bystander.read_text(encoding="utf-8")

    mig.apply(tmp_path)

    data = json.loads(agent.read_text(encoding="utf-8"))
    assert data["tools"] == [
        "olano_list_agents",
        "olano_disk_monitor",
        "olano_scheduler_list",
        "olano_scheduler_get",
        "olano_scheduler_create",
        "olano_scheduler_update",
        "olano_scheduler_delete",
        "olano_scheduler_run_now",
        "olano_update",
    ]
    # A fleet scheduler writer must never reach an ordinary agent.
    assert bystander.read_text(encoding="utf-8") == before_bystander


def test_0021_appends_when_the_anchor_is_absent(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0021_agentfather_scheduler_tools")

    agent = _write_agent(
        tmp_path, "agentfather", {"name": "agentfather", "tools": ["olano_list_agents"]}
    )
    mig.apply(tmp_path)

    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]
    assert tools[0] == "olano_list_agents"
    assert tools[1:] == [
        "olano_scheduler_list",
        "olano_scheduler_get",
        "olano_scheduler_create",
        "olano_scheduler_update",
        "olano_scheduler_delete",
        "olano_scheduler_run_now",
    ]


def test_0021_is_idempotent_and_respects_a_disabled_tools_opt_out(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0021_agentfather_scheduler_tools")

    agent = _write_agent(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["olano_disk_monitor"],
            # An operator who deliberately took away the destructive ones.
            "disabled_tools": ["olano_scheduler_delete", "olano_scheduler_run_now"],
        },
    )

    mig.apply(tmp_path)
    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]
    assert "olano_scheduler_delete" not in tools
    assert "olano_scheduler_run_now" not in tools
    assert "olano_scheduler_get" in tools

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0021_skips_an_unparseable_config(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0021_agentfather_scheduler_tools")

    broken = tmp_path / "agents" / "agentfather" / "agent.json5"
    broken.parent.mkdir(parents=True)
    broken.write_text("{ this is not json", encoding="utf-8")
    before = broken.read_text(encoding="utf-8")

    mig.apply(tmp_path)  # must not raise
    assert broken.read_text(encoding="utf-8") == before


# ── 0022: the expanded Microsoft 365 toolkit ────────────────────────────────

_MS365_ORIGINAL = [
    "ms365_list_messages",
    "ms365_send_mail",
    "ms365_list_events",
    "ms365_create_event",
    "ms365_delete_event",
    "ms365_list_drive_items",
    "ms365_search_drive",
    "ms365_upload_drive_file",
    "ms365_list_teams",
    "ms365_list_channels",
    "ms365_send_channel_message",
    "ms365_decode_file_content",
]


def test_0022_grants_the_new_tools_after_the_last_ms365_entry(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0022_microsoft365_toolkit_tools")

    agent = _write_agent(
        tmp_path,
        "office",
        {"name": "office", "tools": ["web_search", *_MS365_ORIGINAL, "time_now"]},
    )
    mig.apply(tmp_path)
    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]

    start = tools.index("ms365_decode_file_content") + 1
    assert tools[start : start + len(mig._NEW_TOOLS)] == list(mig._NEW_TOOLS)
    assert tools[-1] == "time_now"


def test_0022_frozen_list_matches_the_toolkit_it_shipped_with():
    """A regenerated list would be a bug in a historical record, but on the
    release that adds it the two must agree exactly."""
    from olano.tools import microsoft365_tools

    mig = importlib.import_module("olano.migrations.0022_microsoft365_toolkit_tools")
    live = {t.name for t in microsoft365_tools.get_tools()}
    assert set(mig._NEW_TOOLS) | set(_MS365_ORIGINAL) == live


def test_0022_leaves_a_hand_picked_subset_alone(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0022_microsoft365_toolkit_tools")

    agent = _write_agent(
        tmp_path,
        "reader",
        {"name": "reader", "tools": ["ms365_list_messages", "ms365_list_events"]},
    )
    before = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == before


def test_0022_is_idempotent_and_respects_a_disabled_tools_opt_out(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0022_microsoft365_toolkit_tools")

    agent = _write_agent(
        tmp_path,
        "office",
        {
            "name": "office",
            "tools": list(_MS365_ORIGINAL),
            "disabled_tools": ["ms365_forward", "ms365_share_drive_item"],
        },
    )
    mig.apply(tmp_path)
    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]
    assert "ms365_forward" not in tools
    assert "ms365_share_drive_item" not in tools
    assert "ms365_create_draft" in tools

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0022_skips_an_unparseable_config(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0022_microsoft365_toolkit_tools")

    broken = tmp_path / "agents" / "office" / "agent.json5"
    broken.parent.mkdir(parents=True)
    broken.write_text("{ this is not json", encoding="utf-8")
    before = broken.read_text(encoding="utf-8")

    mig.apply(tmp_path)  # must not raise
    assert broken.read_text(encoding="utf-8") == before


# ── 0023: the retired ask_user tool ─────────────────────────────────────────


def test_0023_strips_ask_user_everywhere_we_name_tools(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0023_retire_ask_user_tool")

    agent = _write_agent(
        tmp_path,
        "merli",
        {
            "name": "merli",
            "tools": ["ask_user", "safe_shell"],
            "disabled_tools": ["ask_user", "wait_n_seconds"],
            "subagents": [{"name": "helper", "tools": ["ask_user", "web_search"]}],
            "builtin_subagents": [{"name": "AsanaAgent", "extra_tools": ["ask_user"]}],
            "security": {
                "tools": {"ask_user": "never", "safe_shell": "always"},
                "rate_limits": {"per_tool": {"ask_user": 5}},
            },
        },
    )
    mig.apply(tmp_path)
    data = json.loads(agent.read_text(encoding="utf-8"))

    assert data["tools"] == ["safe_shell"]
    assert data["disabled_tools"] == ["wait_n_seconds"]
    assert data["subagents"][0]["tools"] == ["web_search"]
    assert data["builtin_subagents"][0]["extra_tools"] == []
    assert data["security"]["tools"] == {"safe_shell": "always"}
    assert data["security"]["rate_limits"]["per_tool"] == {}


def test_0023_leaves_mcp_server_tool_names_and_clean_configs_alone(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0023_retire_ask_user_tool")

    # An MCP server may well expose its own tool called ask_user.
    mcp = _write_agent(
        tmp_path,
        "mcp",
        {"name": "mcp", "mcp_servers": [{"url": "https://x", "disabled_tools": ["ask_user"]}]},
    )
    clean = _write_agent(tmp_path, "clean", {"name": "clean", "tools": ["web_search"]})
    before = {p: p.read_text(encoding="utf-8") for p in (mcp, clean)}

    mig.apply(tmp_path)
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text


def test_0023_is_idempotent_and_skips_an_unparseable_config(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0023_retire_ask_user_tool")

    agent = _write_agent(tmp_path, "a", {"name": "a", "tools": ["ask_user"]})
    broken = tmp_path / "agents" / "b" / "agent.json5"
    broken.parent.mkdir(parents=True)
    broken.write_text("{ this is not json", encoding="utf-8")

    mig.apply(tmp_path)
    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first
    assert json.loads(after_first)["tools"] == []
    assert broken.read_text(encoding="utf-8") == "{ this is not json"


def test_retired_tool_names_resolve_quietly(capsys):
    from olano.tools.registry import resolve_tools

    assert resolve_tools(["ask_user"]) == []
    assert "unknown tool" not in capsys.readouterr().out


# ── 0024: Gmail attachment download ─────────────────────────────────────────


def test_0024_grants_the_download_tool_after_gmail_get_message(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0024_gmail_download_attachments")

    agent = _write_agent(
        tmp_path,
        "desk",
        {"name": "desk", "tools": ["gmail_read", "gmail_get_message", "gmail_reply"]},
    )
    mig.apply(tmp_path)
    tools = json.loads(agent.read_text(encoding="utf-8"))["tools"]
    assert tools == ["gmail_read", "gmail_get_message", "gmail_download_attachments", "gmail_reply"]

    after_first = agent.read_text(encoding="utf-8")
    mig.apply(tmp_path)
    assert agent.read_text(encoding="utf-8") == after_first


def test_0024_leaves_agents_without_gmail_or_with_an_opt_out_alone(tmp_path: Path):
    mig = importlib.import_module("olano.migrations.0024_gmail_download_attachments")

    no_gmail = _write_agent(tmp_path, "plain", {"name": "plain", "tools": ["web_search"]})
    opted_out = _write_agent(
        tmp_path,
        "strict",
        {
            "name": "strict",
            "tools": ["gmail_get_message"],
            "disabled_tools": ["gmail_download_attachments"],
        },
    )
    before = (no_gmail.read_text(encoding="utf-8"), opted_out.read_text(encoding="utf-8"))
    mig.apply(tmp_path)
    assert (no_gmail.read_text(encoding="utf-8"), opted_out.read_text(encoding="utf-8")) == before
