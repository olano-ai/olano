"""The shell-family tool bodies never run a host process on a managed box.

`safe_shell`, `node_run` and `npm_install` used to call `subprocess.run` in the
runtime container directly (the container that holds `/app` and the vault),
gated only on the `shell` feature flag, which failed open on a resolver error.
In an agent turn the security middleware's shell chokepoint
(`deepagents_middleware._run_shell_command`) intercepts those calls before the
body runs and already fails closed; these tests pin the bodies themselves and
the `sandboxed_run` seam they now share with every in-process CLI (ffmpeg):

* managed posture + no live sandbox ⇒ a refusal, and no subprocess is spawned;
* a posture that cannot be resolved ⇒ the same refusal (fail closed);
* a live sandbox ⇒ the command runs there, with `cwd` honoured;
* open posture ⇒ the host lane, byte-for-byte what it did before.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

import olano.core.builder as builder
import olano.deployment.enforcement as enf
import olano.security.deepagents_middleware as mw
from olano.backends.lionclaw_docker import OlanoDockerBackend, SandboxRun
from olano.config.backend_config import OlanoDockerBackendConfig
from olano.tools import node as node_tools
from olano.tools import shell as shell_tools
from olano.tools._sandbox_exec import (
    EXIT_REFUSED,
    HOST_EXECUTION_REFUSED,
    host_execution_refused,
    sandbox_active_for_current_agent,
    sandboxed_run,
)
from olano.tools.node import node_run_tool, npm_install_tool
from olano.tools.shell import safe_shell_tool
from olano.tools.talk_to import current_agent

AGENT = "posture-test-agent"


def _posture(monkeypatch, *, managed: bool) -> None:
    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: managed)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)


def _fake_sandbox(monkeypatch, recorder: dict) -> OlanoDockerBackend:
    sb = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    sb._container_name = "olano-test-container"  # liveness needs a bound container

    def fake_run(command, **kw):
        recorder["cmd"] = command
        recorder["timeout"] = kw.get("timeout")
        return SandboxRun("SANDBOX_OUT", "", 0)

    monkeypatch.setattr(sb, "run", fake_run)
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, AGENT, sb)
    return sb


@pytest.fixture
def as_agent():
    """Bind the turn's agent (no sandbox registered under it unless a test adds one)."""
    token = current_agent.set(AGENT)
    try:
        yield
    finally:
        current_agent.reset(token)


@pytest.fixture
def no_host_process(monkeypatch):
    """Any host subprocess is the bug these tests exist to catch."""

    def boom(*args, **kwargs):
        raise AssertionError(f"a host process was spawned: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


# ── sandboxed_run: the one seam ──────────────────────────────────────────────


def test_sandboxed_run_refuses_the_host_on_a_managed_box_without_a_sandbox(
    monkeypatch, as_agent, no_host_process
):
    _posture(monkeypatch, managed=True)
    assert sandbox_active_for_current_agent() is False

    result = sandboxed_run(["cat", "/app/olano/core/builder.py"], timeout=5)

    assert host_execution_refused(result)
    assert result.returncode == EXIT_REFUSED
    assert result.stdout == ""
    assert "isolated sandbox" in result.stderr


def test_sandboxed_run_fails_closed_when_posture_cannot_be_resolved(
    monkeypatch, as_agent, no_host_process
):
    """`require_sandboxed_exec` already fails closed on its own; a failure one
    level up (the enforcement module itself erroring) must read the same way."""

    def broken():
        raise RuntimeError("entitlements layer down")

    monkeypatch.setattr(enf, "require_sandboxed_exec", broken)

    result = sandboxed_run(["printenv"], timeout=5)

    assert host_execution_refused(result)


def test_sandboxed_run_open_posture_runs_on_the_host_and_honours_cwd(
    monkeypatch, as_agent, tmp_path
):
    _posture(monkeypatch, managed=False)

    result = sandboxed_run(["pwd"], timeout=10, cwd=str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()
    assert not host_execution_refused(result)


def test_sandboxed_run_sandbox_lane_wraps_cwd_and_joins_argv(monkeypatch, as_agent):
    _posture(monkeypatch, managed=True)
    rec: dict = {}
    _fake_sandbox(monkeypatch, rec)
    assert sandbox_active_for_current_agent() is True

    plain = sandboxed_run(["ffmpeg", "-i", "in file.mp4"], timeout=7)
    assert plain.stdout == "SANDBOX_OUT" and plain.returncode == 0
    assert rec["cmd"] == "ffmpeg -i 'in file.mp4'" and rec["timeout"] == 7

    sandboxed_run(["npm", "install", "left-pad"], timeout=7, cwd="/srv/my project")
    assert rec["cmd"] == f"cd {shlex.quote('/srv/my project')} && npm install left-pad"


def test_a_real_command_failure_is_not_mistaken_for_the_refusal(monkeypatch, as_agent):
    """Exit 126 from a command that genuinely could not execute keeps its own
    stderr, so `host_execution_refused` must key on both fields."""
    _posture(monkeypatch, managed=True)
    rec: dict = {}
    sb = _fake_sandbox(monkeypatch, rec)
    monkeypatch.setattr(
        sb, "run", lambda command, **kw: SandboxRun("", "sh: 1: ./x: Permission denied", 126)
    )

    result = sandboxed_run(["./x"], timeout=5)

    assert result.returncode == EXIT_REFUSED
    assert not host_execution_refused(result)
    assert result.stderr != HOST_EXECUTION_REFUSED


# ── safe_shell ───────────────────────────────────────────────────────────────


def test_safe_shell_body_refuses_on_a_managed_box_without_a_sandbox(
    monkeypatch, as_agent, no_host_process
):
    _posture(monkeypatch, managed=True)

    out = safe_shell_tool.invoke({"command": "cat /app/olano/core/builder.py"})

    assert out.startswith("Error: ")
    assert "isolated sandbox" in out
    assert "builder" not in out.split("Error: ", 1)[1]  # nothing of the file leaked


def test_safe_shell_body_fails_closed_when_the_feature_resolver_errors_on_a_managed_box(
    monkeypatch, as_agent, no_host_process
):
    """The old body did `except Exception: shell_allowed = True`: a resolver
    hiccup left this path open while the backend build failed closed on the
    same question. Now a managed box with a broken resolver gets no shell."""

    def broken(*a, **k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(enf, "feature_enabled", broken)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", broken)
    monkeypatch.setattr(enf, "require_sandboxed_exec", broken)
    monkeypatch.setattr(builder, "_managed_install_fallback", lambda: True)

    out = safe_shell_tool.invoke({"command": "ls"})

    assert out.startswith("Error: ")


def test_safe_shell_body_runs_inside_a_live_sandbox(monkeypatch, as_agent, no_host_process):
    _posture(monkeypatch, managed=True)
    rec: dict = {}
    _fake_sandbox(monkeypatch, rec)

    out = safe_shell_tool.invoke({"command": "echo hi there", "timeout": 9})

    assert out == "SANDBOX_OUT"
    assert rec["cmd"] == "echo hi there" and rec["timeout"] == 9


def test_safe_shell_body_still_runs_on_the_host_under_open_posture(monkeypatch, as_agent):
    """The operator's own box keeps its shell, with the same scrubbed env the
    host lane always had: platform secrets gone, SHELL_ENV_DEFAULTS layered on."""
    _posture(monkeypatch, managed=False)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "vault-master-key-must-not-leak")
    monkeypatch.setenv("SAFE_SHELL_CANARY", "still-here")

    assert safe_shell_tool.invoke({"command": "echo open-host-ok"}).strip() == "open-host-ok"

    env_out = safe_shell_tool.invoke({"command": "printenv"})
    assert "vault-master-key-must-not-leak" not in env_out
    assert "LIONCLAW_VAULT_KEY" not in env_out
    assert "SAFE_SHELL_CANARY=still-here" in env_out
    assert "NO_COLOR=1" in env_out


def test_safe_shell_allowlist_and_feature_flag_are_checked_before_execution(
    monkeypatch, as_agent, no_host_process
):
    _posture(monkeypatch, managed=False)
    assert "not in the allowlist" in safe_shell_tool.invoke({"command": "perl -e 1"})
    assert "not allowed for safety" in safe_shell_tool.invoke({"command": "rm -rf /"})

    monkeypatch.setattr(enf, "feature_enabled", lambda name: name != "shell")
    assert "disabled for this deployment" in safe_shell_tool.invoke({"command": "ls"})


# ── node_run / npm_install ───────────────────────────────────────────────────


def test_node_tool_bodies_refuse_on_a_managed_box_without_a_sandbox(
    monkeypatch, as_agent, no_host_process
):
    _posture(monkeypatch, managed=True)
    # `which` on the host must find them for the body to get as far as running.
    monkeypatch.setattr(node_tools.shutil, "which", lambda name: f"/usr/bin/{name}")

    run_out = node_run_tool.func("cowsay hi")
    install_out = npm_install_tool.func("cowsay", global_install=True)

    assert run_out.startswith("Error: ") and "isolated sandbox" in run_out
    assert install_out.startswith("Error: ") and "isolated sandbox" in install_out


def test_node_tool_bodies_use_bare_names_and_cwd_inside_a_live_sandbox(
    monkeypatch, as_agent, no_host_process
):
    """A host `which` would hand the container a runtime-image path; inside the
    sandbox the bare name resolves through the shared tool layer on its PATH."""
    _posture(monkeypatch, managed=True)
    rec: dict = {}
    _fake_sandbox(monkeypatch, rec)
    monkeypatch.setattr(
        node_tools.shutil, "which", lambda name: (_ for _ in ()).throw(AssertionError(name))
    )

    # `.func`, not `.invoke`: langchain's schema generation renames a parameter
    # called `args` (pydantic reserves it), so the body is reached directly.
    out = node_run_tool.func("cowsay hi", args=["--", "moo"], cwd="/ws/proj", timeout=33)
    assert out == "SANDBOX_OUT"
    assert rec["cmd"] == "cd /ws/proj && npx --yes cowsay hi -- moo" and rec["timeout"] == 33

    assert npm_install_tool.func("cowsay", global_install=True) == "SANDBOX_OUT"
    assert rec["cmd"] == "npm install -g cowsay"

    npm_install_tool.func("left-pad", cwd="/ws/proj")
    assert rec["cmd"] == "cd /ws/proj && npm install left-pad"


def test_node_run_body_runs_a_script_on_the_host_under_open_posture(
    monkeypatch, as_agent, tmp_path
):
    _posture(monkeypatch, managed=False)
    if node_tools._find_node() is None:
        pytest.skip("node is not installed on this host")
    script = tmp_path / "hello.js"
    script.write_text("console.log('node-host-ok', process.cwd())\n", encoding="utf-8")

    out = node_run_tool.func(str(script), cwd=str(tmp_path))

    assert "node-host-ok" in out
    assert str(tmp_path.resolve()) in out


# ── the two gates name the same tools ────────────────────────────────────────


def test_middleware_chokepoint_covers_every_shell_family_tool_name():
    """The security middleware intercepts shell-family calls by tool NAME before
    the body runs. A tool renamed in shell.py / node.py without this set
    following would fall through to the handler; the body still refuses on a
    managed box, but the summariser, the validate_command pass and the
    workspace log would be gone with it."""
    names = {t.name for t in shell_tools.get_tools()} | {t.name for t in node_tools.get_tools()}
    assert names, "no shell-family tools discovered"
    assert names <= mw._SHELL_TOOL_NAMES, names - mw._SHELL_TOOL_NAMES
