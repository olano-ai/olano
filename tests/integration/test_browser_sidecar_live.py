"""The live browser end to end: a real olano-browser container, driven through
the docker-exec DevTools pipe, streamed over the live-view socket and taken over.

What only a real container can show: that Chromium's DevTools endpoint is
reachable through ``docker exec`` and nothing else, that the relay carries a
whole CDP session, that a screencast frame arrives, that a viewer's input is
refused until they take control and lands on the page once they do, that the
agent is held off while they hold it, and that closing removes the container.

Needs Docker and the ``olano-browser:latest`` image (CI builds it from
docker/olano-browser in sandbox-live.yml; locally: ``docker build -t
olano-browser:latest docker/olano-browser``). Skips with "live browser
unavailable" otherwise, which the CI job turns into a failure.
"""

from __future__ import annotations

import contextvars
import http.server
import json
import shutil
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest


def _unavailable() -> str | None:
    if not shutil.which("docker"):
        return "no docker CLI"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "no docker daemon"
    if (
        subprocess.run(
            ["docker", "image", "inspect", "olano-browser:latest"], capture_output=True
        ).returncode
        != 0
    ):
        return "olano-browser:latest not built"
    return None


_REASON = _unavailable()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=f"live browser unavailable: {_REASON}")

_PAGE = b"""<html><head><title>Live Shop</title></head><body style="margin:0">
<button id=plain style="position:absolute;left:0;top:0;width:1280px;height:400px"
  onclick="document.title='human clicked'">Plain</button></body></html>"""

# An intranet app that talks to its own server by fetch (served at /app).
_APP = b"""<html><head><title>Intranet app</title></head><body><p id=out>pending</p><script>
fetch('/api', {method: 'POST', body: 'q'}).then(r => r.text())
  .then(t => { document.getElementById('out').textContent = 'ok:' + t; })
  .catch(e => { document.getElementById('out').textContent = 'error:' + e; });
</script></body></html>"""


@pytest.fixture(scope="module")
def page_url():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_APP if self.path == "/app" else _PAGE)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"pong")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gateway = subprocess.run(
        ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    yield f"http://{gateway}:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture
def live(monkeypatch):
    from olano.browser import loop as browser_loop
    from olano.browser import policy, sessions, sidecar
    from olano.config.system_settings import set_browser_settings

    # The harness page sits on the docker bridge gateway, a private address.
    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setattr(policy, "_host", lambda: (4, 8 * policy.GIB))
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: None)
    set_browser_settings({"mode": "live"})
    yield sessions
    browser_loop.run_sync(sessions.close_all(save=False), timeout_s=60)
    sidecar.reap_orphans()


def _as_agent(agent: str, fn, *args):
    from olano.tools.talk_to import current_agent

    ctx = contextvars.copy_context()

    def run():
        current_agent.set(agent)
        return fn(*args)

    return ctx.run(run)


def test_live_browser_end_to_end(live, page_url):
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api
    from olano.tools.browser import browser_click, browser_close, browser_navigate, browser_snapshot

    nav = _as_agent("alice", browser_navigate.invoke, {"url": page_url})
    assert nav.startswith("# Live Shop"), nav
    containers = subprocess.run(
        ["docker", "ps", "--filter", "label=olano.browser=1", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert containers == ["olano-browser-alice"]
    ports = subprocess.run(
        ["docker", "port", "olano-browser-alice"], capture_output=True, text=True
    )
    assert ports.stdout.strip() == ""  # nothing published: DevTools only via docker exec

    with TestClient(create_api(MagicMock())) as client:
        with client.websocket_connect("/api/agents/alice/browser/live") as ws:
            frame = None
            deadline = time.time() + 15
            while time.time() < deadline and frame is None:
                msg = ws.receive()
                if msg.get("bytes"):
                    frame = msg["bytes"]
            assert frame is not None and frame[:3] == b"\xff\xd8\xff"  # a JPEG

            # The agent's click while we watch arrives as pointer frames: the
            # cursor glides there, then the click (a frame has no pointer).
            clicker = threading.Thread(
                target=_as_agent, args=("alice", browser_click.invoke, {"selector": "#plain"})
            )
            clicker.start()
            kinds: list[str] = []
            deadline = time.time() + 20
            while time.time() < deadline and "click" not in kinds:
                msg = ws.receive()
                if msg.get("text"):
                    data = json.loads(msg["text"])
                    if data.get("type") == "pointer":
                        kinds.append(data["kind"])
                        assert 0.0 <= data["x"] <= 1.0 and 0.0 <= data["y"] <= 1.0
            clicker.join(timeout=30)
            assert kinds[-2:] == ["move", "click"]

            ws.send_text(json.dumps({"t": "down", "x": 0.5, "y": 0.2}))
            reply = None
            while reply is None:
                msg = ws.receive()
                if msg.get("text"):
                    data = json.loads(msg["text"])
                    if data.get("type") == "error":
                        reply = data
            assert "Take control" in reply["message"]

            ws.send_text(json.dumps({"t": "control", "who": "user"}))
            time.sleep(1.0)
            held = _as_agent("alice", browser_click.invoke, {"selector": "#plain"})
            assert "taken control" in held
            ws.send_text(json.dumps({"t": "down", "x": 0.5, "y": 0.2}))
            ws.send_text(json.dumps({"t": "up", "x": 0.5, "y": 0.2}))
            time.sleep(1.5)

        handed = client.post("/api/agents/alice/browser/control", json={"who": "agent"})
        assert handed.json()["session"]["controller"] == "agent"

    assert _as_agent("alice", browser_snapshot.invoke, {}).startswith("# human clicked")
    assert _as_agent("alice", browser_close.invoke, {}) == "Browser closed."
    left = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=olano.browser=1"], capture_output=True, text=True
    ).stdout.strip()
    assert left == ""
    assert live.state_path("alice").is_file()


def test_live_browser_lan_page_fetches_its_own_origin(live, page_url):
    """The page sits on the docker bridge gateway, a private address: its own
    fetch POST must reach its server. A document the guard fulfilled was filed
    under the public address space, and Local Network Access refused it."""
    from olano.tools.browser import browser_close, browser_extract, browser_navigate

    nav = _as_agent("lan-app", browser_navigate.invoke, {"url": f"{page_url}app"})
    assert nav.startswith("# Intranet app"), nav
    out = ""
    deadline = time.time() + 15
    while time.time() < deadline:
        out = _as_agent("lan-app", browser_extract.invoke, {"selector": "#out"})
        if "pending" not in out:
            break
        time.sleep(0.5)
    assert "ok:pong" in out, out
    assert _as_agent("lan-app", browser_close.invoke, {}) == "Browser closed."


def test_live_browser_websockets_go_through_its_egress_proxy(live, page_url):
    """The live browser's connections go through the egress proxy running in
    its own container (browser/egress_proxy.py): a WebSocket the guard allows
    opens and talks, one it refuses never reaches the server, and the page
    carries nothing of ours (Playwright's WebSocket routing put a binding on
    every page, which bot checks pick up)."""
    import asyncio
    from urllib.parse import urlsplit

    from websockets.asyncio.server import serve

    from olano.browser import egress_proxy
    from olano.browser import loop as browser_loop
    from olano.browser.guard import GuardPolicy
    from olano.tools.browser import browser_navigate

    loop = asyncio.new_event_loop()
    box: dict = {}
    ready = threading.Event()
    reached: list[str] = []

    async def handler(ws):
        reached.append("connected")
        async for message in ws:
            await ws.send(f"echo:{message}")

    async def main():
        box["server"] = await serve(handler, "0.0.0.0", 0)
        box["port"] = next(iter(box["server"].sockets)).getsockname()[1]
        ready.set()
        await box["server"].serve_forever()

    threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True).start()
    assert ready.wait(10)
    gateway = urlsplit(page_url).hostname
    socket_url = f"ws://{gateway}:{box['port']}/"
    try:
        nav = _as_agent("alice", browser_navigate.invoke, {"url": page_url})
        assert nav.startswith("# Live Shop"), nav
        sess = live.get("alice")
        assert isinstance(sess.egress, egress_proxy.ContainerProxy)

        async def talk() -> str:
            return await sess.page.evaluate(
                """(u) => new Promise(r => {
                    const w = new WebSocket(u);
                    w.onopen = () => w.send('ping');
                    w.onmessage = e => r(e.data);
                    w.onerror = () => r('error');
                    setTimeout(() => r('timeout'), 8000);
                })""",
                socket_url,
            )

        assert browser_loop.run_sync(talk(), timeout_s=30) == "echo:ping"
        assert reached == ["connected"]

        sess.guard.policy = GuardPolicy(deny=[gateway], block_private=False)
        assert browser_loop.run_sync(talk(), timeout_s=30) == "error"
        assert reached == ["connected"]  # the refused one never got there
        assert any(gateway in entry["url"] for entry in sess.guard.blocked)

        async def page_globals() -> list:
            return await sess.page.evaluate(
                "Object.getOwnPropertyNames(window).filter(n => /^__(olano|playwright|pw)/i.test(n))"
            )

        assert browser_loop.run_sync(page_globals(), timeout_s=30) == []
    finally:
        loop.call_soon_threadsafe(box["server"].close)
