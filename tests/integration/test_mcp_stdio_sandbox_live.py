"""Live stdio-MCP-in-sandbox tests — real `docker run` containers, no mocks.

**Opt-in.** Skipped unless a Docker daemon is reachable AND the sandbox image
is present (same gate as ``test_sandbox_live.py``).

Why this exists: the sandboxed stdio-MCP launch crosses the container boundary
three ways the unit suite fakes — name-only ``-e`` env forwarding through the
docker CLI (the load-bearing secret path: if this breaks, secrets must move to
a stdin preamble, never argv), the MCP JSON-RPC handshake over the CLI's
attached stdio, and container lifecycle (stdin-EOF exit, SIGTERM sig-proxy,
``--rm`` cleanup — an orphaned container here is a leak on every tool call).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
import uuid

import pytest

import olano.core.builder as builder
import olano.core.mcp_setup as mcp_setup
from olano.backends.lionclaw_docker import _agent_slug, mcp_stdio_run_args

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

SECRET = "s3cret-live-42"  # gitleaks:allow


def _unique_agent() -> str:
    return f"pytest-mcp-{uuid.uuid4().hex[:8]}"


def _containers_for(agent: str) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=olano.mcp.agent={_agent_slug(agent)}"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return [c for c in out.stdout.split() if c]


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def _run_argv(agent: str, ws, secret_names: list[str]) -> list[str]:
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    return mcp_stdio_run_args(
        agent_name=agent, workspace_root=ws, secret_names=secret_names, image=SANDBOX_IMAGE
    )


def test_name_only_env_forwarding_lands_in_container(tmp_path):
    """`-e NAME` (no value) + the value in the CLI child's env → in-container env."""
    agent = _unique_agent()
    argv = [
        *_run_argv(agent, tmp_path, ["LIVE_MCP_SECRET"]),
        "sh",
        "-c",
        "printenv LIVE_MCP_SECRET",
    ]
    assert not any(SECRET in a for a in argv)  # never on argv
    proc = subprocess.run(
        argv,
        env={**mcp_setup._minimal_cli_env(), "LIVE_MCP_SECRET": SECRET},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == SECRET


def test_stdin_eof_ends_container_and_rm_cleans_up(tmp_path):
    """Session close (stdin EOF) is the normal exit: server ends, --rm removes."""
    agent = _unique_agent()
    argv = [*_run_argv(agent, tmp_path, []), "sh", "-c", "exec cat >/dev/null"]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=mcp_setup._minimal_cli_env(),
    )
    try:
        assert _wait_for(lambda: _containers_for(agent), timeout=60), "container never appeared"
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=60) == 0
        assert _wait_for(lambda: not _containers_for(agent)), "container survived stdin EOF"
    finally:
        proc.kill()


def test_sigterm_on_cli_child_kills_container(tmp_path):
    """Non-tty docker run sig-proxies SIGTERM — the SDK terminating the stdio
    child must terminate the in-container server, not orphan it."""
    agent = _unique_agent()
    argv = [*_run_argv(agent, tmp_path, []), "sh", "-c", "exec sleep 300"]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=mcp_setup._minimal_cli_env(),
    )
    try:
        assert _wait_for(lambda: _containers_for(agent), timeout=60), "container never appeared"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        assert _wait_for(lambda: not _containers_for(agent)), "container survived SIGTERM"
    finally:
        proc.kill()


def _write_mini_mcp_server(ws) -> str:
    """A dependency-free stdio MCP server: newline-delimited JSON-RPC, three
    methods. Lives in the workspace, which the container mounts at the same
    host path — so the host path in argv resolves in-container (the one-path-
    namespace property the agent sandbox also relies on)."""
    server = ws / "mini_mcp_server.py"
    server.write_text(
        """
import json, os, sys

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "pytest-live-mcp", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
            "name": "echo_env",
            "description": "Echo LIVE_MCP_SECRET from the server environment.",
            "inputSchema": {"type": "object", "properties": {}}}]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": os.environ.get("LIVE_MCP_SECRET", "<unset>")}],
            "isError": False}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "unsupported"}})
""".lstrip(),
        encoding="utf-8",
    )
    return str(server)


async def test_full_mcp_handshake_through_planned_launch(tmp_path, monkeypatch):
    """plan_stdio_launch (forced sandboxed) → stdio_client → initialize →
    tools/list → tools/call, with the secret readable only inside the server."""
    agent = _unique_agent()
    server_path = _write_mini_mcp_server(tmp_path)
    builder._ensure_sandbox_shared_dir(tmp_path, recursive=True)

    monkeypatch.setattr(mcp_setup, "_stdio_launch_decision", lambda: ("sandboxed", None))
    monkeypatch.setattr(mcp_setup, "_stdio_sandbox_image", lambda: SANDBOX_IMAGE)
    monkeypatch.setattr(mcp_setup, "_agent_workspace_root", lambda a: tmp_path)
    monkeypatch.setattr(mcp_setup, "_prepare_stdio_sandbox_paths", lambda a, ws: None)
    # The sandboxed lane forwards only the server's DECLARED env (#437), never
    # the agent's whole secret set, so that is the seam to stand in for.
    monkeypatch.setattr(mcp_setup, "agent_declared_env", lambda env, a: {"LIVE_MCP_SECRET": SECRET})

    plan = mcp_setup.plan_stdio_launch("python3", ["-u", server_path], {}, agent)
    assert plan.invocation is not None and plan.invocation.sandboxed
    assert not any(SECRET in a for a in plan.invocation.args)

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=plan.invocation.command,
        args=plan.invocation.args,
        env=plan.invocation.env,
    )

    async def _handshake() -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert [t.name for t in tools] == ["echo_env"]
                result = await session.call_tool("echo_env", {})
                return json.loads(result.content[0].model_dump_json())["text"]

    echoed = await asyncio.wait_for(_handshake(), timeout=180)
    assert echoed == SECRET  # the secret crossed via -e, never via argv

    # Session closed → stdin EOF → server exits → --rm removes the container.
    assert await asyncio.to_thread(_wait_for, lambda: not _containers_for(agent)), (
        "sandbox container survived the MCP session"
    )
