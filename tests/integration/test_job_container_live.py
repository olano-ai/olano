"""Live connected-job tests — real `docker run` containers, no mocks.

**Opt-in.** Skipped unless a Docker daemon is reachable AND the sandbox image
is present (same gate as ``test_mcp_stdio_sandbox_live.py``).

Why this exists: the connected-job lane (``core/job_runner.py``) crosses the
container boundary the unit suite fakes — name-only ``-e`` env forwarding
(the load-bearing secret path), a clone landing at the host path with the
sandbox uid owning it, and a failed credentialed clone leaving neither the
token (stdout, stderr, ``.git/config``) nor a container behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest

import olano.core.builder as builder
import olano.core.mcp_setup as mcp_setup
import olano.tools.git_tools as gt
from olano.backends.lionclaw_docker import _agent_slug
from olano.core.job_runner import run_connected_job

SANDBOX_IMAGE = os.environ.get("LIONCLAW_SANDBOX_IMAGE", "olano-sandbox:latest")


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return False, "no Docker daemon"
        probe = subprocess.run(
            ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker unavailable: {exc}"
    if probe.returncode != 0:
        return False, f"sandbox image {SANDBOX_IMAGE!r} not present locally"
    return True, ""


_READY, _WHY = _docker_ready()
pytestmark = pytest.mark.skipif(not _READY, reason=f"live sandbox unavailable — {_WHY}")

SECRET = "s3cret-job-live-42"
BOGUS_TOKEN = "ghp_bogus_live_token_0123456789abcdef"
PUBLIC_REPO = "https://github.com/octocat/Hello-World.git"
PRIVATE_REPO = "https://github.com/olano-ai/definitely-private-nonexistent-repo-xyz.git"


def _unique_agent() -> str:
    return f"pytest-job-{uuid.uuid4().hex[:8]}"


def _job_containers(agent: str, *, all_states: bool = False) -> list[str]:
    cmd = ["docker", "ps", "-q", "--filter", f"label=olano.job.agent={_agent_slug(agent)}"]
    if all_states:
        cmd.insert(2, "-a")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return [c for c in out.stdout.split() if c]


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


@pytest.fixture
def sandboxed(monkeypatch, tmp_path):
    """Force the sandboxed lane against the real daemon and a tmp workspace."""
    builder._ensure_sandbox_shared_dir(tmp_path, recursive=True)
    monkeypatch.setattr(mcp_setup, "ephemeral_launch_mode", lambda: ("sandboxed", None))
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image", lambda: SANDBOX_IMAGE)
    return tmp_path


def test_name_only_env_forwarding_never_puts_the_value_on_container_args(sandboxed):
    """`-e NAME` + the value in the CLI child's env → in-container env; the
    container's own Args (what `docker inspect` shows) never carry it."""
    agent = _unique_agent()
    inspected: dict = {}

    def run():
        inspected["result"] = run_connected_job(
            ["sh", "-c", "printenv LIVE_JOB_SECRET; sleep 4"],
            agent=agent,
            workspace_root=sandboxed,
            secret_env={"LIVE_JOB_SECRET": SECRET},
            timeout=120,
            label="live-test",
        )

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert _wait_for(lambda: _job_containers(agent), timeout=90), "job container never appeared"
        (cid,) = _job_containers(agent)
        args = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Args}}", cid],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        labels = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Config.Labels}}", cid],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    finally:
        worker.join(timeout=150)
    assert SECRET not in args and "LIVE_JOB_SECRET=" not in args
    assert '"olano.job":"live-test"' in labels
    result = inspected["result"]
    assert result.returncode == 0, result.stderr
    # The value crossed via the env — and the runner redacts it on the way back.
    assert result.stdout.strip() == "[redacted]"
    assert _wait_for(lambda: not _job_containers(agent, all_states=True)), "job container leaked"


def test_public_clone_lands_at_the_host_path_owned_by_the_sandbox_uid(sandboxed, monkeypatch):
    agent = _unique_agent()
    monkeypatch.setattr(gt, "_workspace_root", lambda: sandboxed)
    monkeypatch.setattr(gt, "_agent_name", lambda: agent)
    monkeypatch.setattr(gt, "get_credential", lambda name, default="": "")

    out = gt.git_clone.invoke({"url": PUBLIC_REPO, "depth": 1})
    assert out.startswith("Cloned"), out
    dest = sandboxed / "Hello-World"
    assert (dest / ".git").is_dir()
    assert (dest / "README").is_file()
    assert dest.stat().st_uid == 1000
    assert (dest / "README").stat().st_uid == 1000
    assert _wait_for(lambda: not _job_containers(agent, all_states=True)), "job container leaked"


def test_bogus_token_private_clone_leaves_no_token_and_no_container(sandboxed, monkeypatch):
    agent = _unique_agent()
    monkeypatch.setattr(gt, "_workspace_root", lambda: sandboxed)
    monkeypatch.setattr(gt, "_agent_name", lambda: agent)
    monkeypatch.setattr(gt, "get_credential", lambda name, default="": BOGUS_TOKEN)

    out = gt.git_clone.invoke({"url": PRIVATE_REPO, "dest": "private"})
    assert not out.startswith("Cloned"), out
    assert BOGUS_TOKEN not in out
    assert "[tool-error]" in out
    dest = sandboxed / "private"
    for cfg in dest.rglob("config") if dest.exists() else []:
        assert BOGUS_TOKEN not in cfg.read_text(errors="replace")
    assert _wait_for(lambda: not _job_containers(agent, all_states=True)), "job container leaked"
