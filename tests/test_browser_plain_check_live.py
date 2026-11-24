"""Live: the plain browser test, against a real Xvfb and Chromium on this machine.

``browserd.py`` runs here as it runs in the ``olano-browser`` container (a
virtual display and Chromium on it, DevTools on a local port); only the
``docker exec`` hop is replaced by running the same helper and pipe locally.
Everything else is the real path: the routes, the in-container helper speaking
X11 (GetImage for the screen, XTEST for input), the attached Playwright client
and the bare page the person drives it from.

The page under test reports how the click it received looked. Through the
display, ``screenY`` sits below ``clientY`` by the height of the browser's own
toolbar, as for a person at a real screen. Injected through DevTools, some
Chromium builds report the two as equal (no click on a real window can) and
others place it on the screen properly, so that variant only checks the click
lands where it was aimed.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BROWSERD = Path(__file__).resolve().parent.parent / "docker" / "olano-browser" / "browserd.py"


def _chrome_for_browserd() -> bool:
    if not shutil.which("Xvfb"):
        return False
    sys.path.insert(0, str(BROWSERD.parent))
    try:
        import browserd  # type: ignore[import-not-found]

        browserd.find_chrome()
        return True
    except (ImportError, SystemExit):
        return False
    finally:
        sys.path.pop(0)


pytestmark = pytest.mark.skipif(
    not _chrome_for_browserd(), reason="needs Xvfb and a Chromium browserd.py can find"
)

_PAGE = b"""<!doctype html><title>Clicks</title><body style="margin:0">
<button id=b style="position:absolute;left:100px;top:100px;width:1000px;height:500px">tick</button>
<script>
b.onclick = (e) => { document.title = "clicked sy=" + e.screenY + " cy=" + e.clientY; };
</script>"""


def _free_display() -> str:
    """A display number no X server holds (socket or lock), so a test never
    lands on one an earlier test's server is still letting go of."""
    for num in range(90 + os.getpid() % 50, 400):
        held = (Path(f"/tmp/.X11-unix/X{num}"), Path(f"/tmp/.X{num}-lock"))
        if not any(path.exists() for path in held):
            return str(num)
    raise RuntimeError("no free X display number")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture
def display(tmp_path, monkeypatch):
    """browserd.py on a display and port of its own, standing in for the container."""
    from olano.browser import plain_check, sidecar

    num = _free_display()
    port = _free_port()
    env = dict(
        os.environ,
        OLANO_BROWSER_DISPLAY_NUM=num,
        OLANO_BROWSER_CDP_PORT=str(port),
        OLANO_BROWSER_PROFILE=str(tmp_path / "profile"),
        # A loaded CI runner can take a while to bring Xvfb and Chromium up.
        OLANO_BROWSER_VERSION_WAIT="60",
    )
    proc = subprocess.Popen(
        [sys.executable, str(BROWSERD)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for key in ("OLANO_BROWSER_DISPLAY_NUM", "OLANO_BROWSER_CDP_PORT"):
        monkeypatch.setenv(key, env[key])
    ready = subprocess.run(
        [sys.executable, str(BROWSERD), "version"], env=env, capture_output=True, timeout=90
    )
    assert ready.returncode == 0, ready.stderr

    def version(_name: str) -> dict:
        import json

        out = subprocess.run(
            [sys.executable, str(BROWSERD), "version"], env=env, capture_output=True, text=True
        )
        return dict(json.loads(out.stdout.strip().splitlines()[-1]))

    monkeypatch.setattr(
        plain_check, "helper_argv", lambda _c, *a: [sys.executable, "-c", plain_check.HELPER, *a]
    )
    monkeypatch.setattr(plain_check, "_start_container", lambda agent: "local")
    monkeypatch.setattr(sidecar, "_remove", lambda name: None)
    monkeypatch.setattr(sidecar, "devtools_version", version)
    monkeypatch.setattr(sidecar, "pipe_argv", lambda _c: [sys.executable, str(BROWSERD), "pipe"])
    from olano.browser import egress_proxy

    monkeypatch.setattr(
        egress_proxy,
        "container_argv",
        lambda _c: [sys.executable, "-c", egress_proxy.core_source()],
    )
    yield
    os.killpg(proc.pid, 15)
    proc.wait(10)
    # Xvfb and Chromium are browserd's children in the same group: wait until
    # the display is really gone, and make sure nothing of it outlives the test.
    deadline = time.monotonic() + 10
    while Path(f"/tmp/.X11-unix/X{num}").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        pass


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from olano.config.agent_config import AgentConfig
    from olano.config.loader import save_agent_config
    from olano.runtime.api import create_api
    from olano.runtime.routes import browser as browser_routes

    monkeypatch.setattr(
        browser_routes, "_availability", lambda: {"mode": "live", "provider": "local"}
    )
    save_agent_config(AgentConfig(name="scout"))
    with TestClient(create_api(MagicMock())) as c:
        yield c
    c.post("/api/agents/scout/browser/plain-check", json={"action": "stop"})


def _title(client, deadline_s: float = 15, *, contains: str) -> str:
    deadline = time.monotonic() + deadline_s
    titles: list[str] = []
    while time.monotonic() < deadline:
        status = client.get("/api/agents/scout/browser/plain-check").json()
        titles = [t["title"] for t in status.get("tabs", [])]
        hit = next((t for t in titles if contains in t), None)
        if hit:
            return hit
        time.sleep(0.3)
    raise AssertionError(f"no tab title containing {contains!r}: {titles}")


def _click(client, x: float, y: float) -> None:
    """One click through the screen socket, at display pixels (x, y)."""
    with client.websocket_connect("/api/agents/scout/browser/plain-check/screen") as ws:
        frame = ws.receive_bytes()
        assert frame[:2] == b"\xff\xd8", "a JPEG of the display"
        point = {"x": x / 1279, "y": y / 799}
        ws.send_json({"t": "move", **point})
        ws.send_json({"t": "down", "button": "left", **point})
        ws.send_json({"t": "up", "button": "left", **point})
        time.sleep(0.5)


# The button covers page (100..1100, 100..600); its middle is (600, 300). The
# browser's own toolbar sits above the page, about 87 px on the builds this
# runs on: the target is large so a build whose toolbar differs still lands.
_AIM = (600, 300 + 87)


def _click_until_clicked(client) -> str:
    """Click the button through the screen until the page reports it: the
    first click can land while a fresh window is still taking focus."""
    for _ in range(3):
        _click(client, *_AIM)
        try:
            return _title(client, 4, contains="clicked")
        except AssertionError:
            continue
    return _title(client, 1, contains="clicked")


def test_plain_click_comes_from_the_screen(display, client, site):
    started = client.post(
        "/api/agents/scout/browser/plain-check",
        json={"action": "start", "url": site, "variant": "plain"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["attached"] is False
    _title(client, contains="Clicks")
    opened = client.get("/api/agents/scout/browser/plain-check").json()["page"]
    assert opened["url"] == site and opened["id"]
    title = _click_until_clicked(client)
    sy, cy = (int(part.split("=")[1]) for part in title.split()[1:])
    # A click through the display sits below the browser's toolbar on screen.
    assert sy - cy > 40, title


def test_devtools_input_lands_where_it_was_aimed(display, client, site):
    started = client.post(
        "/api/agents/scout/browser/plain-check",
        json={"action": "start", "url": site, "variant": "devtools-input"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["attached"] is True
    _title(client, contains="Clicks")
    title = _click_until_clicked(client)
    cy = int(title.rsplit("cy=", 1)[1])
    # Mapped onto the page through the measured toolbar height.
    assert abs(cy - 300) < 60, title


def test_an_agent_like_context_carries_the_agents_setup(display, client, site, monkeypatch):
    from olano.browser import plain_check

    # The harness page is on 127.0.0.1, which the request guard refuses by default.
    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    started = client.post(
        "/api/agents/scout/browser/plain-check",
        json={
            "action": "start",
            "url": site,
            "variant": "context",
            "parts": ["scripts", "guards", "emulation", "state"],
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["parts"] == list(plain_check.PART_GROUPS["all"])
    _title(client, contains="Clicks")
    opened = client.get("/api/agents/scout/browser/plain-check").json()["page"]
    assert opened["url"] == site
    halves = [
        "route_requests",
        "route_websockets",
        "copy_binding",
        "copy_script",
        "permission_binding",
        "permission_script",
        "probe_binding",
    ]
    started = client.post(
        "/api/agents/scout/browser/plain-check",
        json={"action": "start", "url": site, "variant": "context", "parts": halves},
    )
    assert started.status_code == 200, started.text
    assert started.json()["parts"] == halves
    _title(client, contains="Clicks")
    bad = client.post(
        "/api/agents/scout/browser/plain-check",
        json={"action": "start", "url": site, "variant": "context", "parts": ["nope"]},
    )
    assert bad.status_code == 400


def test_a_second_page_loads_in_the_same_browser(display, client, site):
    started = client.post(
        "/api/agents/scout/browser/plain-check",
        json={
            "action": "start",
            "url": site,
            "variant": "plain",
            "then_url": site + "?second",
            "then_after_s": 1,
        },
    )
    assert started.status_code == 200, started.text
    deadline = time.monotonic() + 30
    status: dict = {}
    while time.monotonic() < deadline:
        status = client.get("/api/agents/scout/browser/plain-check").json()
        if status.get("then_state") == "done" and (status.get("page") or {}).get("title"):
            break
        time.sleep(0.3)
    assert status["then_state"] == "done", status
    assert status["first_page"]["url"] == site
    assert status["page"]["url"] == site + "?second"


def test_the_page_drives_it(display, client, site):
    page = client.get("/api/agents/scout/browser/plain-check/view")
    assert page.status_code == 200
    assert "Plain browser test: scout" in page.text
    assert 'const AGENT = "scout";' in page.text
