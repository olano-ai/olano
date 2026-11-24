"""Deployed pages end to end: supervisor, keeper, proxy route, on the host lane.

Every test here deploys through the registry, lets the real supervisor build
and start real processes, and reads the page through the real ``/pages/``
route of the real API app, so the whole chain is exercised: registry →
supervisor → lane → keeper → mux → httpcore pool → proxy rules → viewer. Only
the posture questions (is there a sandbox, is host execution allowed) are
pinned, to the open-posture answers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from olano.pages import policy, releases, services
from olano.pages.registry import (
    delete_page,
    deploy_page,
    restart_page,
    set_page_auth,
    start_page,
    stop_page,
)
from olano.pages.spec import build_spec
from olano.runtime import pages_routes

AGENT = "alice"
PASSWORD = "long-enough-password"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"owner:{PASSWORD}".encode()).decode()}

SERVER = textwrap.dedent(
    """
    import http.server, json, os, sys
    from pathlib import Path

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            print("req", self.command, self.path, flush=True)

        def _send(self, code, body, ctype="text/plain", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                bad = Path("unhealthy.flag").exists()
                return self._send(500 if bad else 200, b"health")
            if self.path == "/headers":
                return self._send(200, json.dumps(dict(self.headers)).encode(), "application/json")
            if self.path == "/redirect":
                return self._send(302, b"", extra=[("Location", "/login")])
            if self.path == "/cookie":
                return self._send(200, b"c", extra=[
                    ("Set-Cookie", "sid=1; Path=/; HttpOnly"),
                    ("Set-Cookie", "lc_session=evil; Path=/"),
                    ("Clear-Site-Data", '"cookies"'),
                ])
            self._send(200, ("ok %s pid=%d" % (self.path, os.getpid())).encode())

        def _read_body(self):
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                data = b""
                while True:
                    size = int(self.rfile.readline().strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        return data
                    data += self.rfile.read(size)
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):
            data = self._read_body()
            self._send(200, b"got:" + data[:64], "application/json")

    print("listening on", os.environ["PORT"], flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
    """
)

BUILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    if Path("fail.flag").exists():
        print("build broke on purpose", flush=True)
        sys.exit(2)
    n = int(Path("count").read_text()) + 1 if Path("count").exists() else 1
    Path("count").write_text(str(n))
    Path("dist/assets").mkdir(parents=True, exist_ok=True)
    Path("dist/index.html").write_text("<h1>build %d</h1>" % n)
    Path("dist/assets/app.js").write_text("console.log(%d)" % n)
    print("built", n, flush=True)
    """
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "alice-ws"
    (root / "app").mkdir(parents=True)
    (root / "app" / "server.py").write_text(SERVER)
    (root / "site").mkdir()
    (root / "site" / "build.py").write_text(BUILD)
    monkeypatch.setattr(policy, "command_denial", lambda agent, config=None: None)
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "host_execution_forbidden", lambda: False)
    monkeypatch.setattr(services, "agent_workspace_root", lambda agent: root)
    monkeypatch.setattr(pages_routes, "_agent_workspace_root", lambda agent: root)
    # Fast clocks: the logic is the same, the waits are not.
    monkeypatch.setattr(services, "RECONCILE_INTERVAL", 0.2)
    monkeypatch.setattr(services, "WAIT_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(services, "LOST_RESTART_DELAY", 0.1)
    monkeypatch.setattr(services, "BACKOFF_CAP", 0.3)
    monkeypatch.setattr(services, "POLICY_RECHECK_SECONDS", 0.5)
    settings = SimpleNamespace(
        services_enabled=True,
        max_services_per_agent=5,
        health_interval_seconds=1,
        request_timeout_seconds=10,
        max_request_body_mb=1,
        max_concurrent_requests=16,
    )
    monkeypatch.setattr(services.PageServiceManager, "settings", lambda self: settings)
    set_page_auth(AGENT, "owner", PASSWORD)
    pages_routes._AUTH_CACHE.clear()
    yield root
    pages_routes._AUTH_CACHE.clear()


@pytest.fixture
async def manager(ws):
    mgr = await services.start_manager()
    yield mgr
    await services.stop_manager()


@pytest.fixture
def client(ws):
    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    app = create_api(runtime)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.7", 5555)),
        base_url="https://box.example",
    )


async def _until(predicate, timeout: float = 30.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for {what}; last state: {services.page_status('crm') or services.page_status('site')}"
    )


def _state(slug: str) -> dict:
    return services.page_status(slug) or {}


async def _wait_state(slug: str, *states: str, timeout: float = 30.0) -> dict:
    return await _until(
        lambda: _state(slug) if _state(slug).get("state") in states else None,
        timeout,
        f"{slug} in {states}",
    )


def _deploy_service(ws: Path, command: str | None = None, **spec_kwargs) -> None:
    spec = build_spec(start=command or f"exec {sys.executable} server.py", **spec_kwargs)
    deploy_page(AGENT, "crm", "app", ws, spec)
    services.notify_page_changed("crm")


def _service_pid(ws: Path, slug: str = "crm") -> int:
    record = json.loads((ws / ".olano" / "pages" / slug / "keeper.pid").read_text())
    return int(record["service_pid"])


def _new_service_pid(ws: Path, previous: int, slug: str = "crm") -> int | None:
    """The pid of a server other than ``previous`` once its keeper has written
    it and it is alive, else None (for ``_until``)."""
    try:
        pid = _service_pid(ws, slug)
    except (OSError, ValueError, KeyError):
        return None  # the new keeper has not written its pid file yet
    return pid if pid != previous and _alive(pid) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


# ── services ─────────────────────────────────────────────────────────────────


async def test_service_page_serves_through_the_proxy(ws, manager, client):
    _deploy_service(ws, healthcheck="/health")
    state = await _wait_state("crm", "running")
    assert state["lane"] == "host" and state["port"] > 0
    assert state["listen_scope"] in ("loopback", "")

    async with client:
        assert (await client.get("/pages/crm/")).status_code == 401
        r = await client.get("/pages/crm/hello?x=1", headers=AUTH)
        assert r.status_code == 200 and r.text.startswith("ok /hello?x=1")
        assert r.headers["cache-control"] == "private"

        seen = (
            await client.get("/pages/crm/headers", headers={**AUTH, "Cookie": "lc_session=s; a=b"})
        ).json()
        lowered = {k.lower(): v for k, v in seen.items()}
        assert "authorization" not in lowered
        assert lowered["cookie"] == "a=b"
        assert lowered["x-olano-viewer"] == "owner"
        assert lowered["x-forwarded-prefix"] == "/pages/crm"
        assert lowered["x-forwarded-for"] == "198.51.100.7"

        r = await client.post(
            "/pages/crm/api/items",
            headers={**AUTH, "Sec-Fetch-Site": "same-origin"},
            json={"name": "lead"},
        )
        assert r.status_code == 200 and r.text == 'got:{"name":"lead"}'

        r = await client.post(
            "/pages/crm/api/items", headers={**AUTH, "Sec-Fetch-Site": "cross-site"}, json={}
        )
        assert r.status_code == 403

        r = await client.post("/pages/crm/api/x", headers=AUTH, content=b"x" * (2 * 1024 * 1024))
        assert r.status_code == 413

        r = await client.get("/pages/crm/redirect", headers=AUTH, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/pages/crm/login"

        r = await client.get("/pages/crm/cookie", headers=AUTH)
        cookies = r.headers.get_list("set-cookie")
        assert cookies == ["sid=1; Path=/pages/crm/; HttpOnly"]
        assert "clear-site-data" not in r.headers

        # The bare URL redirects with the method kept (307).
        r = await client.post("/pages/crm", headers=AUTH, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/pages/crm/"

    log = (ws / ".olano" / "pages" / "crm" / "service.log").read_text()
    assert "listening on" in log and "req GET /hello?x=1" in log


async def test_killed_server_is_restarted(ws, manager, client):
    _deploy_service(ws)
    await _wait_state("crm", "running")
    pid = _service_pid(ws)
    os.kill(pid, 9)
    await _until(lambda: _state("crm").get("restarts", 0) >= 1, 20, "a restart")
    await _wait_state("crm", "running")
    assert _service_pid(ws) != pid
    async with client:
        r = await client.get("/pages/crm/", headers=AUTH)
    assert r.status_code == 200


async def test_crash_loop_settles_in_failed_and_restart_recovers(ws, manager, client, monkeypatch):
    monkeypatch.setattr(services, "CRASH_LIMIT", 3)
    _deploy_service(ws, command="echo 'boom: missing DATABASE_URL'; exit 4")
    state = await _wait_state("crm", "failed", timeout=40)
    assert state["restarts"] == 3 and state["last_exit"] == 4
    assert "boom: missing DATABASE_URL" in state["last_error"]
    async with client:
        r = await client.get("/pages/crm/", headers={**AUTH, "Accept": "text/html"})
    assert r.status_code == 503 and "not running" in r.text and "Retry-After" not in r.headers

    # Fix the command (a new deploy) and it comes up.
    _deploy_service(ws)
    await _wait_state("crm", "running")


async def test_unhealthy_server_is_restarted(ws, manager, monkeypatch):
    monkeypatch.setattr(services, "HEALTH_FAILURES_TO_RESTART", 2)
    _deploy_service(ws, healthcheck="/health")
    await _wait_state("crm", "running")
    (ws / "app" / "unhealthy.flag").write_text("1")
    await _until(lambda: _state("crm").get("restarts", 0) >= 1, 30, "a health restart")
    (ws / "app" / "unhealthy.flag").unlink()
    await _wait_state("crm", "running")


async def test_server_that_never_listens_times_out(ws, manager, monkeypatch):
    monkeypatch.setattr(services, "CRASH_LIMIT", 1)
    _deploy_service(ws, command="exec sleep 60", startup_timeout=5)
    state = await _wait_state("crm", "failed", timeout=40)
    assert "did not answer" in state["last_error"]


async def test_stop_and_delete_stop_the_server(ws, manager):
    _deploy_service(ws)
    await _wait_state("crm", "running")
    pid = _service_pid(ws)
    stop_page(AGENT, "crm")
    services.notify_page_changed("crm")
    await _until(lambda: not _alive(pid), 20, "the server to stop")
    restart_page(AGENT, "crm", ws)
    services.notify_page_changed("crm")
    # Not `_wait_state("running")` alone: the old server dies before the
    # manager records "stopped", so that wait can match the stale "running"
    # from before the stop and read keeper.pid before the new keeper wrote it.
    old = pid
    pid = await _until(lambda: _new_service_pid(ws, old), 30, "the restarted server")
    await _wait_state("crm", "running")
    delete_page(AGENT, "crm")
    services.notify_page_changed("crm")
    await _until(lambda: not _alive(pid), 20, "the server to stop after delete")


async def test_republishing_as_plain_page_stops_the_server(ws, manager):
    _deploy_service(ws)
    await _wait_state("crm", "running")
    pid = _service_pid(ws)
    (ws / "app" / "index.html").write_text("static now")
    start_page(AGENT, "crm", "app", ws)
    services.notify_page_changed("crm")
    await _until(lambda: not _alive(pid), 20, "the server to stop")


async def test_runtime_shutdown_stops_every_server(ws):
    await services.start_manager()
    _deploy_service(ws)
    await _wait_state("crm", "running")
    pid = _service_pid(ws)
    await services.stop_manager()
    assert not _alive(pid)


async def test_blocked_page_waits_with_the_reason(ws, manager, client, monkeypatch):
    monkeypatch.setattr(policy, "command_denial", lambda agent, config=None: policy.SANDBOX_DOWN)
    _deploy_service(ws)
    state = await _wait_state("crm", "waiting")
    assert state["detail"] == policy.SANDBOX_DOWN
    async with client:
        r = await client.get("/pages/crm/", headers={**AUTH, "Accept": "text/html"})
    assert r.status_code == 503 and "sandbox is not running" in r.text
    assert r.headers["retry-after"] == "3"


async def test_service_page_without_supervisor_answers_starting(ws, client):
    _deploy_service(ws)
    async with client:
        r = await client.get("/pages/crm/", headers=AUTH)
    assert r.status_code == 503


# ── static builds ────────────────────────────────────────────────────────────


def _deploy_static(ws: Path, spa: bool = True) -> None:
    spec = build_spec(build=f"{sys.executable} build.py", output="dist")
    deploy_page(AGENT, "site", "site", ws, spec, spa=spa)
    services.notify_page_changed("site")


async def test_static_build_release_spa_and_failed_build_keeps_previous(ws, manager, client):
    _deploy_static(ws)
    state = await _wait_state("site", "live")
    first = state["release"]
    assert state["build"]["state"] == "succeeded"
    async with client:
        r = await client.get("/pages/site/", headers=AUTH)
        assert r.text == "<h1>build 1</h1>"
        r = await client.get("/pages/site/deep/route", headers={**AUTH, "Accept": "text/html"})
        assert r.status_code == 200 and r.text == "<h1>build 1</h1>"
        r = await client.get("/pages/site/assets/missing.js", headers=AUTH)
        assert r.status_code == 404
        r = await client.post("/pages/site/", headers=AUTH)
        assert r.status_code == 405

        # A broken build: the page keeps serving the previous release.
        (ws / "site" / "fail.flag").write_text("1")
        restart_page(AGENT, "site", ws)
        services.notify_page_changed("site")
        await _until(
            lambda: _state("site").get("build", {}).get("state") == "failed", 30, "build failure"
        )
        state = _state("site")
        assert state["state"] == "live" and state["release"] == first
        assert "build broke on purpose" in state["last_error"]
        assert (await client.get("/pages/site/", headers=AUTH)).text == "<h1>build 1</h1>"

        # Fixed: the next build becomes the new release.
        (ws / "site" / "fail.flag").unlink()
        restart_page(AGENT, "site", ws)
        services.notify_page_changed("site")
        await _until(
            lambda: _state("site").get("release") not in (None, "", first), 30, "a new release"
        )
        assert (await client.get("/pages/site/", headers=AUTH)).text == "<h1>build 2</h1>"
    build_log = (ws / ".olano" / "pages" / "site" / "build.log").read_text()
    assert "built 2" in build_log


async def test_static_page_is_not_rebuilt_on_runtime_restart(ws, client):
    await services.start_manager()
    _deploy_static(ws)
    await _wait_state("site", "live")
    await services.stop_manager()
    await services.start_manager()
    try:
        await asyncio.sleep(1.0)
        assert (ws / "site" / "count").read_text() == "1"
        assert _state("site")["state"] == "live"
    finally:
        await services.stop_manager()


async def test_static_page_rebuilds_when_its_release_vanished(ws, manager):
    _deploy_static(ws)
    await _wait_state("site", "live")
    releases.delete_releases("site")
    await _until(lambda: (ws / "site" / "count").read_text() == "2", 30, "a rebuild")


async def test_build_timeout_is_enforced(ws, manager):
    spec = build_spec(build="sleep 120", output="dist", build_timeout=30)
    deploy_page(AGENT, "site", "site", ws, spec)
    # Shrink the deadline below the spec floor for the test.
    original = services.page_lanes.HostLane.run_build

    async def fast(self, command, *, cwd, env, timeout, log_path):
        return await original(self, command, cwd=cwd, env=env, timeout=1, log_path=log_path)

    services.page_lanes.HostLane.run_build = fast  # type: ignore[method-assign]
    try:
        services.notify_page_changed("site")
        state = await _wait_state("site", "failed", timeout=30)
        assert state["build"]["timed_out"] is True
    finally:
        services.page_lanes.HostLane.run_build = original  # type: ignore[method-assign]


async def test_runtime_shutdown_kills_a_running_build(ws):
    await services.start_manager()
    spec = build_spec(build="echo $$ > build.pid; exec sleep 120", output="dist")
    deploy_page(AGENT, "site", "site", ws, spec)
    services.notify_page_changed("site")
    pid_file = ws / "site" / "build.pid"
    await _until(
        lambda: pid_file.exists() and pid_file.read_text().strip(), 20, "the build to start"
    )
    pid = int(pid_file.read_text())
    assert _alive(pid)
    await services.stop_manager()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        await asyncio.sleep(0.1)
    assert not _alive(pid), "a build outlived the runtime that started it"


async def test_streamed_upload_over_the_cap_is_refused(ws, manager, client):
    _deploy_service(ws)
    await _wait_state("crm", "running")

    async def body():
        for _ in range(40):
            yield b"x" * 64 * 1024  # 2.5 MB in chunks, no Content-Length

    async with client:
        r = await client.post("/pages/crm/upload", headers=AUTH, content=body())
        assert r.status_code == 413
        # The page keeps serving afterwards.
        assert (await client.get("/pages/crm/", headers=AUTH)).status_code == 200
