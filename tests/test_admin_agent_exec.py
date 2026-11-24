"""Cross-agent command execution — AgentFather running a command in another
agent's workspace/sandbox.

The file tools cover inspect/seed/fix; this adds the missing *run* verb. The
executor is never weaker than what the target itself gets: a live sandbox runs
via the target's container, a managed box with no live sandbox refuses (never a
runtime-host fallback), and an open/self-host box runs on the host with the cwd
pinned inside the target workspace and the target's own environment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.admin import run_agent_command


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    return home


@pytest.fixture
def target(home) -> Path:
    """A deployed agent with a booted workspace."""
    agent_dir = home / "agents" / "subs-mate"
    ws = agent_dir / "workspace" / "skills" / "bill-watch" / "scripts"
    ws.mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": "subs-mate"}), encoding="utf-8")
    (agent_dir / "workspace" / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    return agent_dir / "workspace"


def _force_open_posture(monkeypatch) -> None:
    """Pretend this is an open/self-host box: no managed posture, no sandbox."""
    import olano.admin.agent_exec as ax

    monkeypatch.setattr(ax, "_live_sandbox", lambda _agent: None)
    monkeypatch.setattr(ax, "_managed_posture", lambda: False)


# ── open / self-host posture (tc-manual-host installs) ───────────────────────


def test_host_run_executes_in_target_workspace(home, target, monkeypatch):
    _force_open_posture(monkeypatch)
    (target / "hello.txt").write_text("hi\n", encoding="utf-8")

    receipt = run_agent_command("subs-mate", "cat hello.txt")

    assert receipt["executor"] == "host"
    assert receipt["exit_code"] == 0
    assert receipt["stdout"] == "hi\n"
    assert Path(receipt["cwd"]) == target.resolve()


def test_host_run_honors_cwd_inside_workspace(home, target, monkeypatch):
    _force_open_posture(monkeypatch)

    receipt = run_agent_command("subs-mate", "pwd", cwd="skills/bill-watch/scripts")

    assert receipt["exit_code"] == 0
    assert Path(receipt["stdout"].strip()) == (target / "skills/bill-watch/scripts").resolve()


def test_host_run_reports_nonzero_exit(home, target, monkeypatch):
    _force_open_posture(monkeypatch)

    receipt = run_agent_command("subs-mate", "sh -c 'exit 7'")

    assert receipt["exit_code"] == 7


def test_host_run_uses_target_environment(home, target, monkeypatch):
    """Runs as the target: config.env + vault, via agent_secret_env."""
    _force_open_posture(monkeypatch)
    # agent_secret_env is imported lazily inside _target_host_env; patch the source.
    monkeypatch.setattr(
        "olano.core.mcp_setup.agent_secret_env",
        lambda _env, agent: {"SUBS_MATE_TOKEN": f"for-{agent}"},
    )

    receipt = run_agent_command("subs-mate", "printenv SUBS_MATE_TOKEN")

    assert receipt["stdout"].strip() == "for-subs-mate"


# ── managed posture (OlanoSandbox) ────────────────────────────────────────


def test_managed_without_live_sandbox_refuses(home, target, monkeypatch):
    """A managed box never falls back to the runtime host."""
    import olano.admin.agent_exec as ax

    monkeypatch.setattr(ax, "_live_sandbox", lambda _agent: None)
    monkeypatch.setattr(ax, "_managed_posture", lambda: True)

    with pytest.raises(RuntimeError, match="isolated sandbox"):
        run_agent_command("subs-mate", "echo hi")


def test_live_sandbox_runs_via_target_container(home, target, monkeypatch):
    """With a live sandbox, the command is routed through the target's backend."""
    import olano.admin.agent_exec as ax

    class FakeRun:
        stdout = "sandboxed\n"
        stderr = ""
        returncode = 0

    calls: list[str] = []

    class FakeBackend:
        def run(self, command, *, timeout=None):
            calls.append(command)
            return FakeRun()

    monkeypatch.setattr(ax, "_live_sandbox", lambda _agent: FakeBackend())
    # If the code ever consulted the host path here, that would be the bug.
    monkeypatch.setattr(ax, "_managed_posture", lambda: True)

    receipt = run_agent_command("subs-mate", "python3 verify.py", cwd="skills/bill-watch/scripts")

    assert receipt["executor"] == "sandbox"
    assert receipt["exit_code"] == 0
    assert receipt["stdout"] == "sandboxed\n"
    # cwd is applied inside the container via the target's host-path mount.
    assert "cd " in calls[0] and "python3 verify.py" in calls[0]
    assert str((target / "skills/bill-watch/scripts").resolve()) in calls[0]


def test_sandbox_unavailable_note_on_dead_container(home, target, monkeypatch):
    import olano.admin.agent_exec as ax

    class DeadRun:
        stdout = ""
        stderr = "Error: No such container: agent-subs-mate"
        returncode = 1

    class DeadBackend:
        def run(self, command, *, timeout=None):
            return DeadRun()

    monkeypatch.setattr(ax, "_live_sandbox", lambda _agent: DeadBackend())
    monkeypatch.setattr(ax, "_managed_posture", lambda: True)

    receipt = run_agent_command("subs-mate", "echo hi")

    assert "note" in receipt and "start the agent" in receipt["note"].lower()


# ── containment + guards ─────────────────────────────────────────────────────


def test_cwd_traversal_is_refused(home, target, monkeypatch):
    _force_open_posture(monkeypatch)

    with pytest.raises(PermissionError, match="outside"):
        run_agent_command("subs-mate", "ls", cwd="../../../etc")


def test_destructive_command_is_refused_by_shell_guard(home, target, monkeypatch):
    _force_open_posture(monkeypatch)

    with pytest.raises(PermissionError):
        run_agent_command("subs-mate", "rm -rf /")


def test_unknown_agent_is_a_clear_error(home):
    with pytest.raises(ValueError, match="does not exist"):
        run_agent_command("ghost", "echo hi")


def test_empty_command_is_rejected(home, target):
    with pytest.raises(ValueError, match="empty"):
        run_agent_command("subs-mate", "   ")


def test_output_is_capped(home, target, monkeypatch):
    import olano.admin.agent_exec as ax

    _force_open_posture(monkeypatch)
    monkeypatch.setattr(ax, "_OUTPUT_CAP", 20)

    receipt = run_agent_command("subs-mate", "python3 -c \"print('x' * 100)\"")

    assert receipt["stdout_truncated"] is True
    assert "truncated" in receipt["stdout"]


# ── HITL policy wiring ───────────────────────────────────────────────────────


def test_tool_is_in_cross_agent_exec_category_in_every_profile():
    from olano.security.hitl_policy import CATEGORY_TOOLS, PROFILE_CATEGORIES

    assert "olano_run_agent_command" in CATEGORY_TOOLS["cross_agent_exec"]
    for profile, cats in PROFILE_CATEGORIES.items():
        assert "cross_agent_exec" in cats, f"{profile} must gate cross-agent exec"


def test_gated_in_developer_profile_but_not_by_write_toggle():
    """Even the most permissive profile (developer, no toggles) gates it."""
    from types import SimpleNamespace

    from olano.security.hitl_policy import resolve_approval_set

    sec = SimpleNamespace(
        profile="developer",
        hitl_enabled=True,
        hitl_write_tools=False,
        hitl_shell=False,
        tools={},
    )
    approval = resolve_approval_set(SimpleNamespace(security=sec))
    assert "olano_run_agent_command" in approval


def test_master_switch_still_disables_everything():
    from types import SimpleNamespace

    from olano.security.hitl_policy import resolve_approval_set

    sec = SimpleNamespace(
        profile="production",
        hitl_enabled=False,
        hitl_write_tools=True,
        hitl_shell=True,
        tools={},
    )
    approval = resolve_approval_set(SimpleNamespace(security=sec))
    assert approval == set()


def test_runner_never_auto_approves_cross_agent_exec():
    """Remembered 'always' rules and the low-risk switch must not bypass it."""
    from types import SimpleNamespace

    from olano.core import runner

    action = {"name": "olano_run_agent_command", "args": {"command": "ls"}}
    cfg = SimpleNamespace(
        name="agentfather",
        security=SimpleNamespace(hitl_auto_approve_low_risk=True),
    )
    assert runner._is_action_auto_approved(action, cfg) is False
    # A read-only command is still not "low" — it runs in another workspace.
    assert runner._classify_interrupt_action_risk(action) in {"medium", "high"}
