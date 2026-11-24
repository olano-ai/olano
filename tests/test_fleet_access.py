"""Fleet filesystem access: AgentFather's shell and file tools reach the deployment.

``agent.json5 → fleet_access`` opens ``<home>/agents`` and ``<home>/skills``
read-write and ``<home>/logs`` read-only, for that agent only. Pinned here at
each place it is enforced: the sandbox mount plan, the security middleware's
file boundary, deepagents' permission rules, the ownership hand-off, the prompt,
the admin-only gate on granting it, and the migration that turns it on for
existing AgentFathers.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import olano.core.builder as builder
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.security import fleet_access
from olano.security.deepagents_middleware import (
    OlanoSecurityMiddleware,
    _file_read_block_reason,
    _file_write_block_reason,
)
from olano.security.request_context import push_request_context
from olano.tools import olano_admin


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(root))
    for sub in ("agents", "skills", "logs", "vault"):
        (root / sub).mkdir(parents=True)
    return root


def _god(**kw) -> AgentConfig:
    return AgentConfig(name="agentfather", system_prompt="x", fleet_access=True, **kw)


def _own_workspace(home: Path, name: str = "agentfather") -> Path:
    ws = home / "agents" / name / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# ── the grant ─────────────────────────────────────────────────────────────────


def test_off_by_default_and_no_roots_without_it(home):
    cfg = AgentConfig(name="plain", system_prompt="x")
    assert cfg.fleet_access is False
    assert fleet_access.fleet_roots(cfg) == []


def test_roots_are_agents_and_skills_rw_and_logs_ro(home):
    roots = {r.label: r for r in fleet_access.fleet_roots(_god())}
    assert roots["agents"].path == (home / "agents").resolve()
    assert roots["skills"].path == (home / "skills").resolve()
    assert roots["logs"].path == (home / "logs").resolve()
    assert (roots["agents"].mode, roots["skills"].mode, roots["logs"].mode) == ("rw", "rw", "ro")
    # The vault and the home root are never a fleet root.
    paths = {r.path for r in roots.values()}
    assert (home / "vault").resolve() not in paths
    assert home.resolve() not in paths


def test_a_worker_inherits_the_grant_with_its_parents_config(home):
    """A cognition worker runs in its parent's container; the same mounts keep
    the container signature stable instead of recreating it per run."""
    from olano.cognition.worker import _build_worker_config

    parent = _god()
    child = _build_worker_config(
        parent,
        system_prompt="w",
        tools_allow=["read_file"],
        extra_blocked=None,
        model=None,
        label="t",
    )
    assert fleet_access.fleet_roots(child) == fleet_access.fleet_roots(parent)


# ── sandbox mount plan ────────────────────────────────────────────────────────


def _targets(mounts):
    return {container: (host, mode) for host, container, mode in mounts}


def test_mounts_the_whole_agents_tree_and_skills_rw_and_logs_ro(home):
    ws = _own_workspace(home)
    by_target = _targets(builder._resolve_sandbox_shared_mounts(_god(), ws))

    agents = str((home / "agents").resolve())
    skills = str((home / "skills").resolve())
    logs = str((home / "logs").resolve())
    assert by_target[agents] == (agents, "rw")
    # rw wins over the read-only skill-root mount every other agent gets.
    assert by_target[skills] == (skills, "rw")
    assert by_target[logs] == (logs, "ro")


def test_never_mounts_the_vault_or_the_home_root(home):
    ws = _own_workspace(home)
    hosts = {host for host, _c, _m in builder._resolve_sandbox_shared_mounts(_god(), ws)}
    assert str((home / "vault").resolve()) not in hosts
    assert str(home.resolve()) not in hosts


def test_an_ordinary_agent_gets_none_of_it(home):
    ws = _own_workspace(home, "plain")
    by_target = _targets(
        builder._resolve_sandbox_shared_mounts(AgentConfig(name="plain", system_prompt="x"), ws)
    )
    assert str((home / "agents").resolve()) not in by_target
    assert str((home / "logs").resolve()) not in by_target
    skills = str((home / "skills").resolve())
    assert by_target[skills] == (skills, "ro")


def test_turning_the_grant_on_changes_the_container_signature(home):
    from olano.backends.lionclaw_docker import _container_signature

    ws = _own_workspace(home)

    def sig(cfg):
        return _container_signature(
            image="olano-sandbox:latest",
            network="bridge",
            workspace_root=ws,
            extra_mounts=builder._resolve_sandbox_shared_mounts(cfg, ws),
            memory="1g",
            cpus=1.0,
        )

    assert sig(_god()) != sig(AgentConfig(name="agentfather", system_prompt="x"))


# ── security middleware boundary ──────────────────────────────────────────────


def _boundary(home: Path, cfg: AgentConfig):
    ws = _own_workspace(home)
    roots = fleet_access.fleet_roots(cfg)
    return ws, roots


def test_reads_reach_other_workspaces_skills_and_logs(home):
    ws, roots = _boundary(home, _god())
    other = home / "agents" / "sales" / "workspace" / "notes.md"
    other.parent.mkdir(parents=True)
    other.write_text("hi")
    for path in (other, home / "skills", home / "logs" / "olano.log", home / "agents"):
        assert (
            _file_read_block_reason(
                tool_name="read_file",
                args={"path": str(path)},
                workspace_root=ws,
                mounts=[],
                fleet_roots=roots,
            )
            == ""
        ), path


def test_writes_reach_agents_and_skills_but_not_logs(home):
    cfg = _god()
    ws, roots = _boundary(home, cfg)

    def write(path: Path) -> str:
        return _file_write_block_reason(
            tool_name="write_file",
            args={"path": str(path)},
            config=cfg,
            workspace_root=ws,
            mounts=[],
            skill_roots=[(home / "skills").resolve()],
            fleet_roots=roots,
        )

    assert write(home / "agents" / "sales" / "workspace" / "x.md") == ""
    assert write(home / "agents" / "sales" / "agent.json5") == ""
    # The skill-root refusal every other agent gets does not apply.
    assert write(home / "skills" / "demo" / "SKILL.md") == ""
    assert "read-only" in write(home / "logs" / "olano.log")


def test_the_vault_and_home_files_stay_out_of_reach(home):
    cfg = _god()
    ws, roots = _boundary(home, cfg)
    for path in (home / "vault" / "vault.db", home / "config.yaml"):
        assert "outside the workspace" in _file_read_block_reason(
            tool_name="read_file",
            args={"path": str(path)},
            workspace_root=ws,
            mounts=[],
            fleet_roots=roots,
        )


def test_escaping_a_root_by_symlink_is_judged_by_where_it_lands(home):
    cfg = _god()
    ws, roots = _boundary(home, cfg)
    link = home / "agents" / "sales"
    link.mkdir(parents=True)
    (link / "vault-link").symlink_to(home / "vault")
    assert "outside the workspace" in _file_read_block_reason(
        tool_name="read_file",
        args={"path": str(link / "vault-link" / "vault.db")},
        workspace_root=ws,
        mounts=[],
        fleet_roots=roots,
    )


def test_trust_levels_still_cap_writes(home):
    cfg = _god()
    cfg.security.trust_level = 1
    ws, roots = _boundary(home, cfg)
    reason = _file_write_block_reason(
        tool_name="write_file",
        args={"path": str(home / "agents" / "sales" / "workspace" / "x.md")},
        config=cfg,
        workspace_root=ws,
        mounts=[],
        fleet_roots=roots,
    )
    assert "Trust level 1" in reason


def test_an_ordinary_agent_is_still_confined(home):
    cfg = AgentConfig(name="plain", system_prompt="x")
    ws = _own_workspace(home, "plain")
    mw = OlanoSecurityMiddleware(config=cfg, workspace_root=ws)
    assert mw._fleet_roots == []
    assert "outside the workspace" in _file_read_block_reason(
        tool_name="read_file",
        args={"path": str(home / "agents" / "agentfather" / "workspace" / "SOUL.md")},
        workspace_root=ws,
        mounts=[],
        fleet_roots=mw._fleet_roots,
    )


def test_the_middleware_carries_the_roots_for_a_fleet_agent(home):
    ws = _own_workspace(home)
    mw = OlanoSecurityMiddleware(config=_god(), workspace_root=ws)
    assert {r.label for r in mw._fleet_roots} == {"agents", "skills", "logs"}


# ── deepagents permissions (plain filesystem backend) ─────────────────────────


def test_filesystem_backend_permissions_allow_the_fleet_trees(home):
    ws = _own_workspace(home)
    cfg = _god(backend=FilesystemBackendConfig(root_dir=str(ws)))
    rules = builder._build_permissions(cfg, ws, [])
    logs = str((home / "logs").resolve())
    agents = str((home / "agents").resolve())
    # Logs: write denied before read allowed; both before the catch-all deny.
    log_rules = [(r.operations, r.mode) for r in rules if logs in r.paths]
    assert log_rules == [(["write"], "deny"), (["read"], "allow")]
    assert any(agents in r.paths and r.mode == "allow" for r in rules)
    assert rules[-1].paths == ["/**"] and rules[-1].mode == "deny"


# ── ownership hand-off ────────────────────────────────────────────────────────


def test_hand_off_is_a_no_op_when_not_root(home, monkeypatch):
    monkeypatch.setattr(fleet_access.os, "geteuid", lambda: 1000)
    fleet_access._reset_handoff_throttle()
    assert fleet_access.handoff_fleet_trees(fleet_access.fleet_roots(_god()), force=True) == 0


def test_hand_off_skips_workspaces_and_is_throttled(home, monkeypatch):
    (home / "agents" / "sales" / "workspace" / "deep").mkdir(parents=True)
    (home / "agents" / "sales" / "agent.json5").write_text("{}")
    (home / "skills" / "demo").mkdir()
    chowned: list[Path] = []
    monkeypatch.setattr(fleet_access.os, "geteuid", lambda: 0)
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: chowned.append(Path(p)) or True)
    fleet_access._reset_handoff_throttle()
    roots = fleet_access.fleet_roots(_god())

    assert fleet_access.handoff_fleet_trees(roots) > 0
    names = {p.name for p in chowned}
    assert {"agent.json5", "sales", "demo"} <= names
    assert "deep" not in names  # a workspace's contents are its own lifecycle's
    # Inside the throttle window nothing is walked again.
    chowned.clear()
    assert fleet_access.handoff_fleet_trees(roots) == 0
    assert chowned == []


# ── prompt ────────────────────────────────────────────────────────────────────


def test_prompt_lists_the_reachable_trees_only_for_a_fleet_agent(home):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    ws = _own_workspace(home)
    block = PromptAssembler(_god(), Workspace(root=ws))._build_workspace_environment()
    assert "Fleet filesystem access is ON" in block
    assert f"`{(home / 'agents').resolve()}` (read-write)" in block
    assert f"`{(home / 'logs').resolve()}` (read-only)" in block

    plain = PromptAssembler(
        AgentConfig(name="plain", system_prompt="x"), Workspace(root=ws)
    )._build_workspace_environment()
    assert "Fleet filesystem access" not in plain


# ── granting it is an administrator's decision ────────────────────────────────


def test_an_agent_driven_grant_is_refused_without_an_admin(home):
    from olano import admin

    admin.create_agent("sales", model="fake:instant", system_prompt="x")
    with push_request_context(source="peer", channel="peer"):
        denial = olano_admin._fleet_access_denial("sales", {"fleet_access": True})
    assert denial is not None
    assert json.loads(denial)["refused_fields"] == ["fleet_access"]


def test_update_and_create_tools_apply_the_gate(home):
    from olano import admin

    admin.create_agent("sales", model="fake:instant", system_prompt="x")
    with push_request_context(source="peer", channel="peer"):
        out = olano_admin.olano_update_agent.invoke(
            {"name": "sales", "updates": {"fleet_access": True}}
        )
        assert "refused_fields" in out
        out = olano_admin.olano_create_agent.invoke(
            {"name": "newbie", "model": "fake:instant", "extra": {"fleet_access": True}}
        )
        assert "refused_fields" in out
    assert admin.get_agent("sales")["fleet_access"] is False


def test_an_admin_may_grant_and_anyone_may_resave_or_revoke(home):
    from olano import admin

    admin.create_agent("sales", model="fake:instant", system_prompt="x")
    with push_request_context(source="api", channel="dashboard", is_owner=True, is_admin=True):
        assert olano_admin._fleet_access_denial("sales", {"fleet_access": True}) is None
    admin.update_agent("sales", {"fleet_access": True})
    with push_request_context(source="peer", channel="peer"):
        assert olano_admin._fleet_access_denial("sales", {"fleet_access": True}) is None
        assert olano_admin._fleet_access_denial("sales", {"fleet_access": False}) is None


def test_doctor_names_the_fleet_access_holders(home):
    from olano import admin
    from olano.admin.system import fleet_access_holders

    admin.create_agent("plain", model="fake:instant", system_prompt="x")
    admin.create_agent("god", model="fake:instant", system_prompt="x", extra={"fleet_access": True})
    assert fleet_access_holders() == ["god"]
    assert admin.doctor()["fleet_access_holders"] == ["god"]


# ── template + migration ──────────────────────────────────────────────────────


def test_the_agentfather_template_ships_the_grant():
    from olano.config.json5_fast import loads

    path = Path(builder.__file__).parents[1] / "archetypes" / "templates" / "agentfather.json5"
    data = loads(path.read_text(encoding="utf-8"))
    assert data["fleet_access"] is True
    assert "Never try to reach another agent's" not in data["system_prompt"]
    assert "With fleet access on" in data["system_prompt"]


def _mig():
    return importlib.import_module("olano.migrations.0027_agentfather_fleet_access")


def test_migration_turns_it_on_and_rewrites_the_old_paragraph(tmp_path):
    mig = _mig()
    cfg = tmp_path / "agents" / "agentfather" / "agent.json5"
    cfg.parent.mkdir(parents=True)
    prompt = "Intro\n" + mig._OLD_PARAGRAPH + "Outro"
    cfg.write_text(json.dumps({"name": "agentfather", "system_prompt": prompt}))
    other = tmp_path / "agents" / "sales" / "agent.json5"
    other.parent.mkdir(parents=True)
    other.write_text('{"name": "sales", "system_prompt": "x"}')
    before_other = other.read_text()

    mig.apply(tmp_path)

    data = json.loads(cfg.read_text())
    assert data["fleet_access"] is True
    assert data["system_prompt"] == "Intro\n" + mig._NEW_PARAGRAPH + "Outro"
    assert other.read_text() == before_other
    # Idempotent: a second run leaves the file byte-for-byte alone.
    after = cfg.read_text()
    mig.apply(tmp_path)
    assert cfg.read_text() == after


def test_migration_respects_an_explicit_opt_out_and_a_rewritten_prompt(tmp_path):
    mig = _mig()
    cfg = tmp_path / "agents" / "agentfather" / "agent.json5"
    cfg.parent.mkdir(parents=True)
    original = '{"name": "agentfather", "fleet_access": false, "system_prompt": "mine"}'
    cfg.write_text(original)
    mig.apply(tmp_path)
    assert cfg.read_text() == original


# ── the real tool-call path ───────────────────────────────────────────────────


async def test_awrap_tool_call_lets_a_fleet_read_through_and_blocks_others(home):
    """End to end through the middleware: the same read is handed to the tool
    for AgentFather and refused for an ordinary agent."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest

    target = home / "agents" / "sales" / "workspace" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("hi")

    def request():
        return ToolCallRequest(
            tool_call={"name": "read_file", "args": {"file_path": str(target)}, "id": "r1"},
            tool=None,
            state={},
            runtime=SimpleNamespace(),
        )

    handler = AsyncMock(return_value=ToolMessage(content="hi", tool_call_id="r1"))
    god = OlanoSecurityMiddleware(config=_god(), workspace_root=_own_workspace(home))
    out = await god.awrap_tool_call(request(), handler)
    handler.assert_awaited_once()
    assert out.content == "hi"

    handler.reset_mock()
    plain = OlanoSecurityMiddleware(
        config=AgentConfig(name="plain", system_prompt="x"),
        workspace_root=_own_workspace(home, "plain"),
    )
    out = await plain.awrap_tool_call(request(), handler)
    handler.assert_not_awaited()
    assert "outside the workspace" in str(out.content)
