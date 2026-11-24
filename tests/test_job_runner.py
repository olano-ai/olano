"""`run_connected_job` — one credentialed command in a throwaway container.

Pins the posture (ADR-002) at the seam: in the sandboxed lane a secret rides
as a name-only ``-e NAME`` flag with its value only in the docker CLI child's
env, never ``NAME=value`` on any argv; in the host lane it is in the child env
only, on top of a scrubbed environment; a blocked posture runs nothing; every
secret value is redacted from the captured output; a timeout is exit 124 and
removes the container the SIGKILLed CLI could not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import olano.core.job_runner as jr
import olano.core.mcp_setup as mcp_setup

SECRET = "s3cret-job-value"
WORKSPACE_MARK = "ws"


class _Proc:
    def __init__(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, rc


@pytest.fixture
def fake_run(monkeypatch):
    """Record every subprocess.run call; return the queued results in order."""
    calls: list[dict] = []
    queue: list[object] = []

    def run(cmd, **kw):
        calls.append({"cmd": list(cmd), **kw})
        result = queue.pop(0) if queue else _Proc()
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(jr.subprocess, "run", run)
    return {"calls": calls, "queue": queue}


def _force(monkeypatch, mode: str, reason: str | None = None) -> None:
    monkeypatch.setattr(mcp_setup, "ephemeral_launch_mode", lambda: (mode, reason))


@pytest.fixture
def sandboxed(monkeypatch):
    _force(monkeypatch, "sandboxed")
    monkeypatch.setattr(jr.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image", lambda: "olano-sandbox:test")
    monkeypatch.setattr(mcp_setup, "_prepare_stdio_sandbox_paths", lambda agent, ws: None)


def _job(tmp_path: Path, **kw) -> jr.ExecResult:
    params = dict(
        agent="alpha",
        workspace_root=tmp_path,
        secret_env={"GIT_JOB_TOKEN": SECRET},
        extra_env={"GIT_TERMINAL_PROMPT": "0"},
        timeout=42,
        label="git",
    )
    params.update(kw)
    return jr.run_connected_job(["git", "clone", "--", "https://x/y", "/w s/repo"], **params)


# ── sandboxed lane ─────────────────────────────────────────────────────────


def test_sandboxed_secret_is_name_only_on_argv_and_value_in_cli_env(
    monkeypatch, tmp_path, fake_run, sandboxed
):
    monkeypatch.setenv("GATE_CANARY", "must-not-leak")
    res = _job(tmp_path)
    assert res.returncode == 0
    (call,) = fake_run["calls"]
    cmd = call["cmd"]
    assert cmd[0] == "/usr/local/bin/docker" and cmd[1] == "run"
    # Name-only forwarding: the name is its own argv element after -e, the
    # value appears nowhere on the argv.
    assert cmd[cmd.index("GIT_JOB_TOKEN") - 1] == "-e"
    assert cmd[cmd.index("GIT_TERMINAL_PROMPT") - 1] == "-e"
    assert not any(SECRET in a for a in cmd)
    assert not any(a.startswith("GIT_JOB_TOKEN=") for a in cmd)
    # …and only in the docker CLI child's env, with the runtime env kept out.
    env = call["env"]
    assert env["GIT_JOB_TOKEN"] == SECRET
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GATE_CANARY" not in env
    assert call["stdin"] is subprocess.DEVNULL
    assert call["timeout"] == 42


def test_sandboxed_container_shape(tmp_path, fake_run, sandboxed):
    _job(tmp_path)
    cmd = fake_run["calls"][0]["cmd"]
    for flag in ("--rm", "--init"):
        assert flag in cmd
    assert "-i" not in cmd  # a job has no stdio pipe to keep open
    assert "--label=olano.job=git" in cmd
    assert "--label=olano.job.agent=alpha" in cmd
    assert "--label=olano.mcp=1" not in cmd
    name = cmd[cmd.index("--name") + 1]
    assert name.startswith("olano-job-git-alpha-")
    joined = " ".join(cmd)
    ws = str(tmp_path.resolve())
    assert f"{ws}:/workspace:rw" in joined
    assert f"{ws}:{ws}:rw" in joined  # one path namespace
    assert ":/opt/agent-tools:ro" in joined  # tool layer read-only for a job
    assert cmd[-4] == "olano-sandbox:test"
    assert cmd[-3:-1] == ["sh", "-c"]


def test_sandboxed_launch_line_keeps_the_agent_writable_tool_layer_off_path(
    tmp_path, fake_run, sandboxed
):
    """The tool layer is written by the agent; prepending it would let a
    planted `git` run inside the job with the credential in its env."""
    _job(tmp_path)
    script = fake_run["calls"][0]["cmd"][-1]
    assert script.startswith("export PATH=$PATH:/opt/agent-tools/shims; exec ")
    assert "/opt/agent-tools/python/bin" not in script
    assert "exec git clone -- https://x/y '/w s/repo'" in script  # shlex-joined


def test_sandboxed_without_docker_cli_is_unavailable_not_host(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "sandboxed")
    monkeypatch.setattr(jr.shutil, "which", lambda name: None)
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_UNAVAILABLE
    assert "docker CLI" in res.stderr
    assert fake_run["calls"] == []


# ── host lane ──────────────────────────────────────────────────────────────


def test_host_lane_runs_argv_in_workspace_with_scrubbed_env(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "host")
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "vault-master")
    monkeypatch.setenv("PATH", "/usr/bin")
    res = _job(tmp_path)
    assert res.returncode == 0
    (call,) = fake_run["calls"]
    assert call["cmd"] == ["git", "clone", "--", "https://x/y", "/w s/repo"]
    assert call["cwd"] == str(tmp_path)
    env = call["env"]
    assert env["GIT_JOB_TOKEN"] == SECRET
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "LIONCLAW_VAULT_KEY" not in env
    assert env["PATH"].startswith("/usr/bin")


# ── blocked lane ───────────────────────────────────────────────────────────


def test_blocked_runs_nothing_and_names_the_reason(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "blocked", "sandboxed execution is not enabled")
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_BLOCKED
    assert res.stderr == "[job blocked] sandboxed execution is not enabled"
    assert fake_run["calls"] == []


def test_launch_mode_failure_blocks_fail_closed(monkeypatch, tmp_path, fake_run):
    def boom():
        raise RuntimeError("posture store down")

    monkeypatch.setattr(mcp_setup, "ephemeral_launch_mode", boom)
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_BLOCKED
    assert "posture could not be resolved" in res.stderr
    assert fake_run["calls"] == []


# ── output scrubbing / timeout / never raises ──────────────────────────────


def test_secret_values_are_redacted_from_output(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "host")
    fake_run["queue"].append(
        _Proc(stdout=f"url https://x:{SECRET}@h", stderr=f"bad {SECRET}", rc=1)
    )
    res = _job(tmp_path)
    assert res.returncode == 1
    assert SECRET not in res.stdout and SECRET not in res.stderr
    assert res.stdout == "url https://x:[redacted]@h"
    assert res.stderr == "bad [redacted]"


def test_scrub_secrets_longest_first():
    assert jr.scrub_secrets("abc abcdef", ["abc", "abcdef"]) == "[redacted] [redacted]"
    assert jr.scrub_secrets("nothing", ["", "x"]) == "nothing"


def test_timeout_is_exit_124_and_removes_the_container(tmp_path, fake_run, sandboxed):
    fake_run["queue"].append(
        subprocess.TimeoutExpired(["docker"], 42, output="partial out", stderr=b"partial err")
    )
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_TIMEOUT
    assert res.stdout == "partial out"
    assert res.stderr.startswith("partial err\n[job timeout] git exceeded 42s")
    run_call, rm_call = fake_run["calls"]
    name = run_call["cmd"][run_call["cmd"].index("--name") + 1]
    assert rm_call["cmd"] == ["/usr/local/bin/docker", "rm", "-f", name]
    assert "GIT_JOB_TOKEN" not in rm_call["env"]  # cleanup never carries the secret


def test_host_timeout_has_nothing_to_remove(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "host")
    fake_run["queue"].append(subprocess.TimeoutExpired(["git"], 42))
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_TIMEOUT
    assert len(fake_run["calls"]) == 1


def test_missing_executable_is_exit_127_not_an_exception(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "host")
    fake_run["queue"].append(FileNotFoundError("git"))
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_UNAVAILABLE
    assert "[job error] git" in res.stderr


def test_unexpected_launch_error_never_raises(monkeypatch, tmp_path, fake_run):
    _force(monkeypatch, "host")
    fake_run["queue"].append(OSError(f"spawn failed for {SECRET}"))
    res = _job(tmp_path)
    assert res.returncode == jr.EXIT_UNAVAILABLE
    assert SECRET not in res.stderr and "[redacted]" in res.stderr


def test_empty_secret_values_are_not_forwarded(tmp_path, fake_run, sandboxed):
    _job(tmp_path, secret_env={"GIT_JOB_TOKEN": ""}, extra_env=None)
    cmd = fake_run["calls"][0]["cmd"]
    assert "GIT_JOB_TOKEN" not in cmd
    assert "GIT_JOB_TOKEN" not in fake_run["calls"][0]["env"]


# ── the MCP wrapper is untouched by the refactor ──────────────────────────


def test_mcp_stdio_run_args_is_the_interactive_mcp_family_of_ephemeral_run_args(tmp_path):
    from olano.backends.lionclaw_docker import ephemeral_run_args, mcp_stdio_run_args

    via_wrapper = mcp_stdio_run_args(
        agent_name="Agent One", workspace_root=tmp_path, secret_names=["B", "A"], image="img:t"
    )
    direct = ephemeral_run_args(
        label="mcp",
        agent_name="Agent One",
        workspace_root=tmp_path,
        secret_names=["B", "A"],
        image="img:t",
        interactive=True,
    )
    assert via_wrapper == direct
    assert via_wrapper[-5:] == ["-e", "A", "-e", "B", "img:t"]  # sorted, name-only
    tail = via_wrapper[via_wrapper.index("--rm") :]
    assert tail[:3] == ["--rm", "-i", "--init"]
    assert tail[3:5] == ["--label=olano.mcp=1", "--label=olano.mcp.agent=agent-one"]
    assert "--name" not in via_wrapper
    assert any(m.endswith(":/opt/agent-tools:rw") for m in via_wrapper)
