"""Live: another sandbox cannot open a connection to a server in this one.

Two real sandboxes from ``OlanoDockerBackend``. Sandbox A runs an HTTP server
bound to ALL interfaces (what Express's ``app.listen(PORT)`` and Python's
``http.server`` do by default); sandbox B tries to connect to A's address. On
the default lane (the isolated network, ICC off) the connection must fail
while A still reaches its own server and both still reach their gateway. The
same setup on the shared default bridge (isolation off) must SUCCEED, which is
what proves the first half measures docker's isolation and not a broken probe.

Needs a docker daemon and a base image with ``python3`` whose uid 1000 exists:
``LIONCLAW_PAGES_TEST_IMAGE``, default ``node:22-bookworm`` (``docker pull
mirror.gcr.io/library/node:22-bookworm && docker tag
mirror.gcr.io/library/node:22-bookworm node:22-bookworm`` where Docker Hub's CDN
is blocked), from which a ``USER 1000`` variant is built locally. Skips with the
reason otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

import olano.core.builder as builder
from olano.backends import lionclaw_docker as ld
from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR, OlanoDockerBackend
from olano.config.backend_config import OlanoDockerBackendConfig

BASE_IMAGE = os.environ.get("LIONCLAW_PAGES_TEST_IMAGE", "node:22-bookworm")
IMAGE = "olano-pages-test:latest"
PORT = 8099


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not installed"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return False, "docker daemon not reachable"
        if (
            subprocess.run(
                ["docker", "image", "inspect", BASE_IMAGE], capture_output=True, timeout=20
            ).returncode
            != 0
        ):
            return False, f"test image {BASE_IMAGE!r} not present locally"
        built = subprocess.run(
            ["docker", "build", "-q", "-t", IMAGE, "-"],
            input=f"FROM {BASE_IMAGE}\nUSER 1000:1000\n".encode(),
            capture_output=True,
            timeout=120,
        )
        if built.returncode != 0:
            return False, f"could not derive {IMAGE}: {built.stderr.decode()[-300:]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return True, ""


_READY, _WHY = _docker_ready()
pytestmark = pytest.mark.skipif(not _READY, reason=f"live sandbox isolation unavailable: {_WHY}")

CONNECT = (
    'python3 -c "import socket,sys; s=socket.socket(); s.settimeout(3);'
    "\ntry:\n s.connect((sys.argv[1], {port})); print('OPEN')"
    "\nexcept Exception as e: print('BLOCKED', type(e).__name__)\" {host}"
)


def _sandbox(root, name: str) -> OlanoDockerBackend:
    ws, tools = root / name / "workspace", root / name / "tools"
    ws.mkdir(parents=True)
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    builder._ensure_sandbox_shared_dir(tools)
    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=IMAGE)
    )
    backend.start(
        agent_name=name, workspace_root=ws, shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")]
    )
    return backend


def _ip(backend: OlanoDockerBackend) -> str:
    ip = backend._container_ip()
    assert ip, "the sandbox has no address"
    return ip


def _serve_on_all_interfaces(backend: OlanoDockerBackend) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            backend.container_ref() or "",
            "python3",
            "-m",
            "http.server",
            str(PORT),
            "--bind",
            "0.0.0.0",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    for _ in range(50):
        if "OPEN" in backend.run(CONNECT.format(port=PORT, host="127.0.0.1")).stdout:
            return
        time.sleep(0.1)
    raise AssertionError("the server in sandbox A never came up")


def _reset_lane_memo(monkeypatch) -> None:
    monkeypatch.setattr(ld, "_ISOLATION_STATE", {})
    monkeypatch.setattr(ld, "_NETWORK_FAILED_AT", {})
    monkeypatch.setattr(ld, "_RESCUE_NETWORK_READY", False)
    monkeypatch.delenv(ld.SANDBOX_NETWORK_ENV, raising=False)


def _remove(*backends: OlanoDockerBackend) -> None:
    for backend in backends:
        subprocess.run(
            ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
        )


def test_default_lane_blocks_sandbox_to_sandbox_connections(tmp_path, monkeypatch):
    _reset_lane_memo(monkeypatch)
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, "1")
    a = _sandbox(tmp_path, "iso-a")
    b = _sandbox(tmp_path, "iso-b")
    try:
        assert a._effective_network() == ld._ISOLATED_NETWORK
        assert b._effective_network() == ld._ISOLATED_NETWORK
        assert a.isolated and a.network_report()["isolated"] is True
        assert a.network_report()["health"] in ("ok", "unchecked")
        _serve_on_all_interfaces(a)
        out = b.run(CONNECT.format(port=PORT, host=_ip(a))).stdout
        assert out.startswith("BLOCKED"), f"sandbox B reached A's server: {out!r}"
        # Each sandbox still reaches its own gateway (egress proxy, DNS lane).
        state, detail = ld.probe_container_network(b.container_ref() or "")
        assert state == "ok", detail
    finally:
        _remove(a, b)


def test_shared_bridge_control_proves_the_probe(tmp_path, monkeypatch):
    """Isolation off: the very same connection succeeds on docker0."""
    _reset_lane_memo(monkeypatch)
    monkeypatch.setenv(ld.SANDBOX_ISOLATION_ENV, "0")
    a = _sandbox(tmp_path, "shared-a")
    b = _sandbox(tmp_path, "shared-b")
    try:
        assert a._effective_network() == "bridge" and a.isolated is False
        _serve_on_all_interfaces(a)
        out = b.run(CONNECT.format(port=PORT, host=_ip(a))).stdout
        assert out.startswith("OPEN"), f"expected the shared bridge to connect: {out!r}"
    finally:
        _remove(a, b)
