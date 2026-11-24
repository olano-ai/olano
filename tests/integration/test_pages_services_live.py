"""Deployed pages against a real sandbox container (the docker exec lane).

The unit suite runs the keeper as a host subprocess; this runs it where a
managed box runs it: inside the agent's sandbox, through ``docker exec -i``,
over a real ``OlanoDockerBackend``. It pins the three things only a real
container can show:

* the server runs as the sandbox user, inside the container, and is reached
  through the keeper with nothing published on the host;
* killing the ``docker exec`` client (what a SIGKILLed runtime looks like) does
  not leave the server running in the container, whether the daemon closes
  the keeper's stdin, or the next keeper reaps the old one from its pidfile;
* a build timeout kills the build's whole process group *inside* the
  container (killing the client alone would not stop it).

Needs a docker daemon and a base image with ``python3``, ``node`` and
coreutils ``timeout`` whose uid 1000 exists: ``LIONCLAW_PAGES_TEST_IMAGE``,
default ``node:22-bookworm``, from which a ``USER 1000`` variant is built
locally (``docker pull mirror.gcr.io/library/node:22-bookworm &&
docker tag mirror.gcr.io/library/node:22-bookworm node:22-bookworm`` where
Docker Hub's CDN is blocked). Skips with the reason otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import olano.core.builder as builder
from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR, OlanoDockerBackend
from olano.config.backend_config import OlanoDockerBackendConfig
from olano.pages import policy, services
from olano.pages.registry import deploy_page, set_page_auth
from olano.pages.spec import build_spec
from olano.runtime import pages_routes

BASE_IMAGE = os.environ.get("LIONCLAW_PAGES_TEST_IMAGE", "node:22-bookworm")
#: The base image with the sandbox's user: the real sandbox image runs as uid
#: 1000, and a root user with every capability dropped (what a stock image
#: becomes under the hardened run flags) cannot write a uid-1000 workspace.
IMAGE = "olano-pages-test:latest"
AGENT = "pages-live"
PASSWORD = "long-enough-password"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"owner:{PASSWORD}".encode()).decode()}


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
pytestmark = pytest.mark.skipif(not _READY, reason=f"live pages lane unavailable: {_WHY}")

SERVER_JS = textwrap.dedent(
    """
    const http = require("http");
    http.createServer((req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ path: req.url, uid: process.getuid(), pid: process.pid,
                               viewer: req.headers["x-olano-viewer"] || null }));
    }).listen(Number(process.env.PORT), process.env.HOST, () => console.log("up", process.env.PORT));
    """
)

BUILD_JS = textwrap.dedent(
    """
    const fs = require("fs");
    fs.mkdirSync("dist", { recursive: true });
    fs.writeFileSync("dist/index.html", "<h1>built in " + process.getuid() + "</h1>");
    console.log("built");
    """
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("pages-live")
    ws, tools = root / "workspace", root / "tools"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "server.js").write_text(SERVER_JS)
    (ws / "web").mkdir()
    (ws / "web" / "build.js").write_text(BUILD_JS)
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    builder._ensure_sandbox_shared_dir(tools)
    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=IMAGE)
    )
    backend.start(
        agent_name=AGENT, workspace_root=ws, shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")]
    )
    yield backend, ws
    subprocess.run(
        ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
    )


@pytest.fixture
def lane(sandbox, monkeypatch):
    backend, ws = sandbox
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, AGENT, backend)
    monkeypatch.setattr(policy, "command_denial", lambda agent, config=None: None)
    monkeypatch.setattr(services, "agent_workspace_root", lambda agent: ws)
    monkeypatch.setattr(pages_routes, "_agent_workspace_root", lambda agent: ws)
    monkeypatch.setattr(services, "RECONCILE_INTERVAL", 0.3)
    monkeypatch.setattr(services, "LOST_RESTART_DELAY", 0.2)
    settings = SimpleNamespace(
        services_enabled=True,
        max_services_per_agent=5,
        health_interval_seconds=2,
        request_timeout_seconds=20,
        max_request_body_mb=5,
        max_concurrent_requests=16,
    )
    monkeypatch.setattr(services.PageServiceManager, "settings", lambda self: settings)
    set_page_auth(AGENT, "owner", PASSWORD)
    pages_routes._AUTH_CACHE.clear()
    return backend, ws


def _in_container(backend: OlanoDockerBackend, command: str) -> str:
    out = subprocess.run(
        ["docker", "exec", backend.container_ref() or "", "sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout


def _count(backend: OlanoDockerBackend, prefix: str) -> int:
    """Processes in the container whose command line STARTS with ``prefix``.

    Anchored on purpose: the keeper's own command line embeds its spec (which
    contains the server command), so a substring match would count it too.
    pgrep is not in every image, so /proc is scanned directly.
    """
    script = "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $f 2>/dev/null; echo; done"
    lines = _in_container(backend, script).splitlines()
    return sum(1 for line in lines if line.startswith(prefix))


async def _wait(predicate, timeout=60.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {services.page_status('live-crm')}")


def _client():
    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_api(runtime)), base_url="https://box.example"
    )


async def test_service_runs_inside_the_sandbox_and_survives_a_lost_client(lane):
    backend, ws = lane
    await services.start_manager()
    try:
        deploy_page(AGENT, "live-crm", "app", ws, build_spec(start="exec node server.js"))
        services.notify_page_changed("live-crm")
        await _wait(
            lambda: (services.page_status("live-crm") or {}).get("state") == "running",
            90,
            "running",
        )
        state = services.page_status("live-crm")
        assert state["lane"] == "sandbox" and state["listen_scope"] == "loopback"

        async with _client() as client:
            body = (await client.get("/pages/live-crm/x?y=1", headers=AUTH)).json()
        assert body["path"] == "/x?y=1" and body["uid"] == 1000 and body["viewer"] == "owner"
        assert _count(backend, "node server.js") == 1
        # Nothing is published on the host for it.
        ports = subprocess.run(
            ["docker", "port", backend.container_ref() or ""],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        assert ports.strip() == ""

        # Simulate a runtime that died without cleaning up: SIGKILL the docker
        # exec client holding the keeper.
        runner = services.get_manager().runner("live-crm")
        old_pid = body["pid"]
        old_session = runner.session
        old_session.process.kill()
        await _wait(
            lambda: runner.session is not None
            and runner.session is not old_session
            and runner.ready.is_set(),
            90,
            "the restart after a lost client",
        )
        # A lost keeper is infrastructure, not a crash: no restart counted.
        assert services.page_status("live-crm")["restarts"] == 0
        async with _client() as client:
            body = (await client.get("/pages/live-crm/", headers=AUTH)).json()
        assert body["pid"] != old_pid
        # Exactly one server: the old one did not outlive its keeper.
        await _wait(lambda: _count(backend, "node server.js") == 1, 30, "a single server")
        assert _count(backend, "python3 -c") == 1
    finally:
        await services.stop_manager()
    await _wait(lambda: _count(backend, "node server.js") == 0, 30, "no server after shutdown")
    assert _count(backend, "python3 -c") == 0


async def test_static_build_runs_in_the_sandbox(lane):
    backend, ws = lane
    await services.start_manager()
    try:
        deploy_page(AGENT, "live-web", "web", ws, build_spec(build="node build.js", output="dist"))
        services.notify_page_changed("live-web")
        await _wait(
            lambda: (services.page_status("live-web") or {}).get("state") == "live", 90, "live"
        )
        async with _client() as client:
            r = await client.get("/pages/live-web/", headers=AUTH)
        assert r.text == "<h1>built in 1000</h1>"
        assert "built" in (ws / ".olano" / "pages" / "live-web" / "build.log").read_text()
    finally:
        await services.stop_manager()


async def test_build_timeout_kills_the_group_inside_the_container(lane):
    backend, ws = lane
    from olano.pages.lanes import SandboxLane

    marker = "sleep 4242"  # each of the two sleeps' own cmdline starts with this
    result = await SandboxLane(backend).run_build(
        f"sh -c '{marker} & {marker}'",
        cwd=ws / "web",
        env={},
        timeout=2,
        log_path=ws / ".olano" / "pages" / "t" / "build.log",
    )
    assert result.timed_out and not result.ok
    await _wait(lambda: _count(backend, marker) == 0, 30, "the build's processes to be killed")
