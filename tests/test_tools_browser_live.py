"""Live end-to-end tests for the browser_* tools against a real headless Chromium.

These are the tests that actually exercise Playwright — the mock-based suite in
``test_tools_browser.py`` can't catch session-management bugs like the
loop-per-call regression (navigate succeeded, every following browser tool call
hung), nor the isolation and guard properties below, which only a real browser
can demonstrate: two agents' cookie jars, a click whose navigation leaves the
allowlist, a form submit stopped for approval. Skipped automatically when
playwright or its Chromium build isn't installed, so CI without browsers stays
green.

Run just these:  uv run pytest tests/test_tools_browser_live.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import http.server
import re
import threading
from functools import lru_cache

import pytest

from olano.security.egress import ALLOW_KEY
from olano.security.request_context import push_request_context


def _invoke(tool, args, deadline_s: float = 60):
    """Invoke a tool with a hard deadline so a dispatcher regression FAILS the
    test instead of hanging it."""
    out: dict = {}
    # A bare Thread starts with an EMPTY context; the runtime runs tools with the
    # caller's context copied (which agent, which egress policy), so do the same.
    ctx = contextvars.copy_context()

    def run():
        out["r"] = ctx.run(tool.invoke, args)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(deadline_s)
    assert "r" in out, f"{tool.name} did not return within {deadline_s}s"
    return out["r"]


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    """Probe by actually launching: headless launches use the separate
    chromium_headless_shell build, so checking ``chromium.executable_path``
    alone passes on partial installs that then fail red at launch time."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)

_PAGE = """<!doctype html>
<html><head><title>Live Test</title></head><body>
  <h1 id="headline">live-harness</h1>
  <div id="counter">clicks:0</div>
  <button id="btn" onclick="
      var c=document.getElementById('counter');
      c.textContent='clicks:'+(parseInt(c.textContent.split(':')[1])+1);">Add</button>
  <input id="field" type="text">
  <div id="mirror">mirror:</div>
  <form method="post" action="/buy"><button id="buy" type="submit">Place order</button></form>
  <form method="get" action="/search" role="search"><input id="q" type="search" name="q"></form>
  <a id="away" href="__AWAY__">Elsewhere</a>
  <div id="cookie">cookie:</div>
  <script>
    document.getElementById('field').addEventListener('input', function(e){
      document.getElementById('mirror').textContent = 'mirror:' + e.target.value;
    });
    document.getElementById('cookie').textContent = 'cookie:' + document.cookie;
  </script>
</body></html>"""


@pytest.fixture(scope="module")
def live_server():
    port_holder: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            away = f"http://localhost:{port_holder['port']}/other"
            body = _PAGE.replace("__AWAY__", away).encode()
            self.send_response(200)
            if self.path.startswith("/login"):
                self.send_header("Set-Cookie", "sid=signed-in; Path=/; Max-Age=3600")
                # What a site behind Cloudflare also sets: its pass and bot score.
                self.send_header("Set-Cookie", "cf_clearance=pass; Path=/; Max-Age=3600")
                self.send_header("Set-Cookie", "__cf_bm=score; Path=/; Max-Age=3600")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # the approval gate must keep this from ever running
            self.server.posts += 1  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.posts = 0  # type: ignore[attr-defined]
    port_holder["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def _url(server, path: str = "/") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


@pytest.fixture()
def browser_on(monkeypatch):
    """Headless mode on; the harness page is on 127.0.0.1, so allow private."""
    from olano.browser import loop as browser_loop
    from olano.browser import policy, sessions
    from olano.browser.guard import clear_dns_cache
    from olano.config.system_settings import set_browser_settings

    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setattr(policy, "_host", lambda: (4, 8 * policy.GIB))
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: None)
    set_browser_settings({"mode": "headless", "max_sessions": 4})
    clear_dns_cache()
    yield sessions
    browser_loop.run_sync(sessions.close_all(save=False), timeout_s=30)


def _as(agent: str):
    from olano.tools.talk_to import current_agent

    return current_agent.set(agent)


def _ref(snapshot: str, role: str, name: str) -> str:
    match = re.search(rf'{role} "{re.escape(name)}" \[ref=(e\d+)\]', snapshot)
    assert match, f"{role} {name!r} not in snapshot:\n{snapshot[:2000]}"
    return match.group(1)


def test_multi_step_flow_across_separate_tool_calls(live_server, browser_on, tmp_path):
    """navigate → extract → click → type → screenshot as SEPARATE tool calls."""
    from olano.tools.browser import (
        browser_click,
        browser_extract,
        browser_navigate,
        browser_screenshot,
        browser_type,
    )

    result = _invoke(browser_navigate, {"url": _url(live_server)})
    assert result.startswith("# Live Test")
    assert "live-harness" in result
    assert "<untrusted_web_page" in result  # page text is fenced as untrusted

    assert "live-harness" in _invoke(browser_extract, {"selector": "#headline"})

    # Click by snapshot ref, the way the model is told to.
    assert _invoke(browser_click, {"selector": _ref(result, "button", "Add")}).startswith("Clicked")
    assert "clicks:1" in _invoke(browser_extract, {"selector": "#counter"})

    assert _invoke(browser_type, {"selector": "#field", "text": "hello"}).startswith("Typed")
    assert "mirror:hello" in _invoke(browser_extract, {"selector": "#mirror"})

    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        result = browser_screenshot.invoke({"filename": "live-flow"})
    assert "Screenshot saved" in result
    shot = tmp_path / "screenshots" / "live-flow.png"
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_post_submit_is_stopped_for_approval_but_a_search_is_not(live_server, browser_on):
    from olano.tools.browser import browser_click, browser_navigate, browser_type

    page = _invoke(browser_navigate, {"url": _url(live_server)})
    result = _invoke(browser_click, {"selector": _ref(page, "button", "Place order")})
    assert result.startswith("Not done:")
    assert "approval" in result
    assert live_server.posts == 0  # nothing reached the server

    # A GET search form is a lookup: Enter submits it without asking.
    typed = _invoke(browser_type, {"selector": "#q", "text": "shoes", "press_enter": True})
    assert typed.startswith("Typed"), typed


def test_agents_never_share_cookies_and_sign_ins_persist(live_server, browser_on):
    """The fix for one module-global browser: agent A signing in must not sign
    agent B in, and A's sign-in must survive closing and reopening. A bot
    check's pass is not a sign-in: it stays in the open browser and is never
    carried into the next one (sessions._CHECK_COOKIES)."""
    import json

    from olano.tools.browser import browser_close, browser_extract, browser_navigate

    tok = _as("alice")
    try:
        _invoke(browser_navigate, {"url": _url(live_server, "/login")})
        _invoke(browser_navigate, {"url": _url(live_server)})
        cookies = _invoke(browser_extract, {"selector": "#cookie"})
        assert "sid=signed-in" in cookies and "cf_clearance=pass" in cookies
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(tok)

    tok = _as("bob")
    try:
        _invoke(browser_navigate, {"url": _url(live_server)})
        assert "sid=" not in _invoke(browser_extract, {"selector": "#cookie"})
    finally:
        current_agent.reset(tok)

    tok = _as("alice")
    try:
        assert _invoke(browser_close, {}) == "Browser closed."
        saved = json.loads(browser_on.state_path("alice").read_text())
        names = {c["name"] for c in saved["cookies"]}
        assert "sid" in names and not names & {"cf_clearance", "__cf_bm"}
        _invoke(browser_navigate, {"url": _url(live_server)})
        cookies = _invoke(browser_extract, {"selector": "#cookie"})
        assert "sid=signed-in" in cookies and "cf_" not in cookies
    finally:
        current_agent.reset(tok)


def test_click_that_leaves_the_allowlist_is_blocked(live_server, browser_on):
    """Only the URL handed to browser_navigate used to be checked; a click,
    redirect or script navigation went anywhere. Every request is checked now."""
    from olano.tools.browser import browser_click, browser_extract, browser_navigate

    security = {"security": {ALLOW_KEY: ["127.0.0.1"]}}
    with push_request_context(metadata=security):
        page = _invoke(browser_navigate, {"url": _url(live_server)})
        clicked = _invoke(browser_click, {"selector": _ref(page, "link", "Elsewhere")})
        assert "was blocked" in clicked and "localhost" in clicked
        # Still on the allowed page: the navigation to localhost was refused.
        assert "live-harness" in _invoke(browser_extract, {"selector": "#headline"})
        refused = _invoke(
            browser_navigate, {"url": f"http://localhost:{live_server.server_address[1]}/"}
        )
    assert "not in the allowlist" in refused
    from olano.browser import loop as browser_loop

    async def _blocked():
        sess = browser_on.get("_default")
        return list(sess.guard.blocked) if sess else []

    blocked = browser_loop.run_sync(_blocked(), timeout_s=10)
    assert any("localhost" in b["url"] for b in blocked)


def test_error_paths_return_strings_fast(live_server, browser_on):
    """Bad selector / bad host produce error strings (no exception, no hang)."""
    from olano.tools.browser import browser_extract, browser_navigate

    result = browser_navigate.invoke({"url": "https://no-such-host-3befy8.invalid/"})
    assert "Error navigating" in result or "Browser error" in result

    browser_navigate.invoke({"url": _url(live_server)})
    assert browser_extract.invoke({"selector": "#nope"}).startswith("No elements found")


def test_isolated_navigate_and_screenshot(live_server, browser_on, tmp_path):
    """One-shot isolated capture: custom path, no shared state left."""
    import olano.tools.browser as b

    dest = tmp_path / "iso.png"
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        result = b.browser_navigate_and_screenshot.invoke(
            {"url": _url(live_server), "save_path": str(dest)}
        )
    assert "Screenshot saved" in result
    assert "Live Test" in result
    assert dest.exists()
    assert not b._active_isolated


def test_a_watched_click_shows_the_pointer_travel(live_server, browser_on):
    """While someone watches, the agent's click reports where it aims (the live
    view draws the cursor from it) and really moves the mouse there first."""
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_click, browser_navigate

    page = _invoke(browser_navigate, {"url": _url(live_server)})
    events: list[dict] = []

    async def _watch(on: bool):
        sess = browser_on.get("_default")
        sess.viewers += 1 if on else -1
        if on:
            sess.pointer_listeners.add(events.append)
        else:
            sess.pointer_listeners.discard(events.append)

    browser_loop.run_sync(_watch(True), timeout_s=10)
    try:
        assert _invoke(browser_click, {"selector": _ref(page, "button", "Add")}).startswith(
            "Clicked"
        )
    finally:
        browser_loop.run_sync(_watch(False), timeout_s=10)
    assert [e["kind"] for e in events] == ["move", "click"]
    assert 0.0 < events[-1]["x"] < 1.0 and 0.0 < events[-1]["y"] < 1.0


# ── redirects, subresources, sockets and local files, against real Chromium ──


_SITE_HITS: list[tuple[str, str]] = []


@pytest.fixture(scope="module")
def site():
    """A tiny site: sign-in by cookie, and a link / image / redirect / socket
    pointing at ``localhost`` (the same server under a hostname the allowlist
    omits)."""
    port_box: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: str, status: int = 200, headers: dict | None = None):
            data = body.encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            _SITE_HITS.append((self.headers.get("Host", ""), self.path))
            n = int(self.headers.get("Content-Length", 0))
            user = self.rfile.read(n).decode().split("user=", 1)[-1] or "nobody"
            self._send(
                "", status=302, headers={"Set-Cookie": f"sid={user}; Path=/", "Location": "/whoami"}
            )

        def do_GET(self):
            _SITE_HITS.append((self.headers.get("Host", ""), self.path))
            other = f"http://localhost:{port_box['port']}"
            if self.path == "/login-form":
                self._send(
                    '<title>form</title><form method="post" action="/login">'
                    '<input name="user" value="alice"><button id="submit">Sign in</button></form>'
                )
            elif self.path == "/whoami":
                cookie = self.headers.get("Cookie", "")
                user = cookie.split("sid=", 1)[1].split(";")[0] if "sid=" in cookie else "nobody"
                self._send(f"<title>who</title><p id=who>user:{user}</p>")
            elif self.path == "/redir":
                self._send("", status=302, headers={"Location": f"{other}/landed"})
            elif self.path == "/ws":
                self._send(
                    "<title>ws</title><p id=ws>pending</p><script>"
                    f"const w = new WebSocket('ws://localhost:{port_box['port']}/sock');"
                    "w.onclose = e => document.getElementById('ws').textContent = 'closed ' + e.code;"
                    "</script>"
                )
            elif self.path.startswith("/ws-echo?port="):
                echo = int(self.path.split("=", 1)[1])
                self._send(
                    "<title>ws</title><p id=ws>pending</p><script>"
                    f"const w = new WebSocket('ws://127.0.0.1:{echo}/');"
                    "w.onopen = () => w.send('ping');"
                    "w.onmessage = e => document.getElementById('ws').textContent = e.data;"
                    "w.onclose = e => document.getElementById('ws').textContent += ' closed ' + e.code;"
                    "</script>"
                )
            elif self.path == "/img":
                self._send(f'<title>img</title><img src="{other}/pixel.gif">hello')
            else:
                self._send("<title>Home</title><h1>home</h1>")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_box["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port_box['port']}"
    server.shutdown()


@contextlib.contextmanager
def as_agent(name: str, workspace, allow=None):
    from olano.tools.talk_to import current_agent

    metadata: dict = {"workspace_root": str(workspace)}
    if allow is not None:
        metadata["security"] = {ALLOW_KEY: allow}
    token = current_agent.set(name)
    try:
        with push_request_context(metadata=metadata):
            yield
    finally:
        current_agent.reset(token)


def test_redirect_to_a_disallowed_host_is_never_followed(site, browser_on, tmp_path):
    """Playwright never routes a redirect hop: the navigation is fetched with
    redirects off and its Location checked before the browser may follow it."""
    from olano.tools.browser import browser_extract, browser_navigate

    _SITE_HITS.clear()
    with as_agent("alice", tmp_path, allow=["127.0.0.1"]):
        result = _invoke(browser_navigate, {"url": f"{site}/redir"})
        assert result.startswith("Error: the page redirected to http://localhost:"), result
        assert "not followed" in result
        assert _invoke(browser_extract, {"selector": "body"}).count("landed") == 0
    # The refused host never received the hop: it was stopped at the Location header.
    assert not [h for h in _SITE_HITS if h[0].startswith("localhost")], _SITE_HITS


def test_login_flows_survive_the_document_route(site, browser_on, tmp_path):
    """Navigations are fetched by the route: a POST that answers 302 +
    Set-Cookie must still sign the browser in. (The submit is approved: this
    pins the redirect check, not the approval gate.)"""
    from olano.browser import consequence
    from olano.tools.browser import browser_click, browser_extract, browser_navigate

    with (
        as_agent("alice", tmp_path, allow=["127.0.0.1"]),
        _approve_everything(consequence),
    ):
        assert "# form" in _invoke(browser_navigate, {"url": f"{site}/login-form"})
        clicked = _invoke(browser_click, {"selector": "#submit"})
        assert clicked.startswith("Clicked"), clicked
        assert "user:alice" in _invoke(browser_extract, {"selector": "#who"})


@contextlib.contextmanager
def _approve_everything(consequence):
    original = consequence.ask_approval
    consequence.ask_approval = lambda *a, **k: None
    try:
        yield
    finally:
        consequence.ask_approval = original


def test_subresource_to_a_disallowed_host_is_blocked_and_noted(site, browser_on, tmp_path):
    from olano.tools.browser import browser_navigate

    with as_agent("alice", tmp_path, allow=["127.0.0.1"]):
        result = _invoke(browser_navigate, {"url": f"{site}/img", "wait_for": "load"})
    assert result.startswith("# img"), result
    assert "[egress] Blocked 1 request(s)" in result and "localhost" in result


def test_isolated_capture_is_guarded(site, browser_on, tmp_path):
    import olano.tools.browser as b

    dest = tmp_path / "never.png"
    with as_agent("alice", tmp_path, allow=["127.0.0.1"]):
        result = _invoke(
            b.browser_navigate_and_screenshot, {"url": f"{site}/redir", "save_path": str(dest)}
        )
    assert result.startswith("Error: the page redirected"), result
    assert not dest.exists()
    assert not b._active_isolated


def test_capture_save_path_stays_inside_the_workspace(site, browser_on, tmp_path, tmp_path_factory):
    import olano.tools.browser as b

    outside = tmp_path_factory.mktemp("runtime-host") / "config.yaml"
    with as_agent("alice", tmp_path):
        result = _invoke(
            b.browser_navigate_and_screenshot, {"url": f"{site}/", "save_path": str(outside)}
        )
        assert result.startswith("Error:") and "outside this agent's workspace" in result
        assert not outside.exists()
        result = _invoke(
            b.browser_navigate_and_screenshot, {"url": f"{site}/", "save_path": "shots/home.png"}
        )
    assert "Screenshot saved" in result, result
    assert (tmp_path / "shots" / "home.png").is_file()


def test_snapshot_refs_drive_click_through_the_ref_argument(live_server, browser_on, tmp_path):
    """Pins the ``snapshotForAI`` protocol call and the ``aria-ref=`` selector
    on the installed Playwright: if an upgrade drops either, this fails."""
    from olano.tools.browser import browser_click, browser_navigate

    with as_agent("alice", tmp_path):
        page = _invoke(browser_navigate, {"url": _url(live_server)})
        ref = _ref(page, "button", "Add")
        assert _invoke(browser_click, {"ref": ref}).startswith(f"Clicked ref {ref}.")
        both = _invoke(browser_click, {"ref": ref, "selector": "#btn"})
        assert both.startswith("Error: pass exactly one of")


def test_websocket_to_a_disallowed_host_is_refused(site, browser_on, tmp_path):
    import time

    from olano.tools.browser import browser_extract, browser_navigate

    with as_agent("alice", tmp_path, allow=["127.0.0.1"]):
        _invoke(browser_navigate, {"url": f"{site}/ws"})
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline:
            text = _invoke(browser_extract, {"selector": "#ws"})
            if "pending" not in text:
                break
            time.sleep(0.2)
    # Refused at the egress proxy before the handshake: the page sees the
    # connection fail (1006), and the refusal is recorded for the agent.
    assert "closed 1006" in text
    refused = [b for b in browser_on.get("alice").guard.blocked if "localhost" in b["url"]]
    assert refused and "localhost" in refused[0]["reason"], refused


@pytest.fixture(scope="module")
def echo_server():
    """A WebSocket server that answers ``x`` with ``echo:x``."""
    import asyncio

    from websockets.asyncio.server import serve

    loop = asyncio.new_event_loop()
    box: dict = {}
    ready = threading.Event()

    async def handler(ws):
        async for message in ws:
            await ws.send(f"echo:{message}")

    async def main():
        box["server"] = await serve(handler, "127.0.0.1", 0)
        box["port"] = next(iter(box["server"].sockets)).getsockname()[1]
        ready.set()
        await box["server"].serve_forever()

    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    ready.wait(10)
    yield box["port"]
    loop.call_soon_threadsafe(box["server"].close)


def test_an_allowed_websocket_works_through_the_egress_proxy(
    site, echo_server, browser_on, tmp_path
):
    """WebSockets are checked by the egress proxy now (browser/egress_proxy.py),
    not by Playwright's WebSocket routing, which put a binding on every page:
    an allowed one opens and talks, and nothing of ours is on the page."""
    import time

    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_extract, browser_navigate

    with as_agent("alice", tmp_path, allow=["127.0.0.1"]):
        _invoke(browser_navigate, {"url": f"{site}/ws-echo?port={echo_server}"})
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline:
            text = _invoke(browser_extract, {"selector": "#ws"})
            if "pending" not in text:
                break
            time.sleep(0.2)
    assert "echo:ping" in text and "closed" not in text
    sess = browser_on.get("alice")
    assert sess.egress is not None

    async def _globals():
        return await sess.page.evaluate(
            "Object.getOwnPropertyNames(window).filter(n => /^__(olano|playwright|pw)/i.test(n))"
        )

    assert browser_loop.run_sync(_globals()) == []


def test_local_files_stay_inside_the_workspace(browser_on, tmp_path, tmp_path_factory):
    """Chromium runs on the runtime host: a page opened from the workspace may
    load workspace files, never a host file, whether by iframe or by link."""
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_click, browser_extract, browser_navigate

    secret = tmp_path_factory.mktemp("runtime-host") / "secret.txt"
    secret.write_text("RUNTIME-HOST-SECRET")
    (tmp_path / "note.txt").write_text("workspace-note")
    page = tmp_path / "report.html"
    page.write_text(
        "<html><head><title>Report</title></head><body><h1>report</h1>"
        '<iframe id="ok" src="note.txt"></iframe>'
        f'<iframe id="leak" src="{secret.as_uri()}"></iframe>'
        '<a id="passwd" href="file:///etc/passwd">passwd</a>'
        "</body></html>"
    )

    with as_agent("alice", tmp_path):
        result = _invoke(browser_navigate, {"url": page.as_uri(), "wait_for": "load"})
        assert result.startswith("# Report"), result
        assert "[egress] Blocked 1 request(s)" in result and "secret.txt" in result
        assert "RUNTIME-HOST-SECRET" not in result

        async def _frame_texts() -> list[str]:
            texts = []
            for frame in browser_on.get("alice").page.frames[1:]:
                try:
                    texts.append(await frame.inner_text("body"))
                except Exception:
                    texts.append("")
            return texts

        texts = browser_loop.run_sync(_frame_texts(), timeout_s=20)
        assert "workspace-note" in texts
        assert not any("RUNTIME-HOST-SECRET" in t for t in texts)

        clicked = _invoke(browser_click, {"selector": "#passwd"})
        assert clicked.startswith("Error: navigation to file:///etc/passwd was blocked."), clicked
        assert "report" in _invoke(browser_extract, {"selector": "h1"})

        assert "outside this agent's workspace" in _invoke(
            browser_navigate, {"url": "file:///etc/passwd"}
        )


def test_a_closed_popup_hands_the_view_back_to_its_opener(live_server, browser_on):
    """A sign-in popup closes itself when done; the session (and so the live
    view and the agent) must land back on the page that opened it, not stay on
    the closed one."""
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_navigate

    token = _as("popper")
    try:
        _invoke(browser_navigate, {"url": _url(live_server)})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)
    sess = browser_on.get("popper")
    opener = sess.page

    async def _popup_then_close() -> tuple[bool, bool]:
        async with opener.expect_popup() as info:
            await opener.evaluate(f"window.open({_url(live_server, '/other')!r})")
        popup = await info.value
        await popup.wait_for_load_state()
        followed = sess.page is popup
        await popup.evaluate("window.close()")
        for _ in range(50):
            if sess.page is opener:
                break
            await asyncio.sleep(0.05)
        return followed, sess.page is opener

    followed, back = browser_loop.run_sync(_popup_then_close(), timeout_s=30)
    assert followed
    assert back


def test_the_live_view_goes_back_and_forward(live_server, browser_on):
    from olano.browser import loop as browser_loop
    from olano.browser.live import dispatch_input
    from olano.tools.browser import browser_navigate

    token = _as("walker")
    try:
        _invoke(browser_navigate, {"url": _url(live_server, "/one")})
        _invoke(browser_navigate, {"url": _url(live_server, "/two")})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)
    sess = browser_on.get("walker")
    sess.controller = "user"

    async def _walk() -> list[str]:
        seen = []
        for kind in ("back", "forward"):
            assert await dispatch_input("walker", {"t": kind}) is None
            seen.append(sess.page.url.rsplit("/", 1)[-1])
        return seen

    assert browser_loop.run_sync(_walk(), timeout_s=30) == ["one", "two"]


class _FakeSocket:
    """The live view's WebSocket, driven from a test: inputs in, frames out."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.texts: list[dict] = []
        self.frames = 0

    async def receive_text(self) -> str:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        import json

        self.texts.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.frames += 1

    async def close(self, code: int = 1000) -> None:
        pass

    def send(self, event: dict) -> None:
        import json

        self.inbox.put_nowait(json.dumps(event))

    async def wait_for(self, predicate, timeout: float = 10.0) -> dict:
        for _ in range(int(timeout / 0.05)):
            for msg in self.texts:
                if predicate(msg):
                    return msg
            await asyncio.sleep(0.05)
        raise AssertionError(f"no matching message in {self.texts[-5:]}")


def test_the_live_view_socket_drives_lists_tabs_and_state(live_server, browser_on):
    """The route end to end on a real session: a click on a <select> brings
    its list to the viewer, a pick lands on the page, a new tab shows up in
    the state frame at once, and state frames carry history and tabs."""
    from olano.browser import loop as browser_loop
    from olano.runtime.routes.browser import _run_live_view
    from olano.tools.browser import browser_navigate

    token = _as("viewer")
    try:
        _invoke(browser_navigate, {"url": _url(live_server)})
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)
    sess = browser_on.get("viewer")
    sess.controller = "user"
    html = (
        "<title>Pick</title><body style=margin:0><select id=s style='position:absolute;"
        "left:100px;top:100px;width:200px;height:30px'><option>one</option><option>two</option>"
        "</select></body>"
    )
    browser_loop.run_sync(sess.page.set_content(html), timeout_s=20)

    async def _drive() -> None:
        ws = _FakeSocket()
        view = asyncio.ensure_future(_run_live_view(ws, "viewer"))
        try:
            state = await ws.wait_for(lambda m: m.get("type") == "state")
            assert state["session"]["tabs"][0]["active"] is True
            # It came from the blank page it opened on, so Back has somewhere to go.
            assert state["session"]["can_back"] is True
            assert state["session"]["can_forward"] is False
            at = {"x": 200 / 1280, "y": 115 / 800}
            ws.send({"t": "down", **at, "button": "left"})
            ws.send({"t": "up", **at, "button": "left"})
            picker = await ws.wait_for(lambda m: m.get("type") == "select")
            assert [o["label"] for o in picker["options"]] == ["one", "two"]
            ws.send({"t": "select", "index": 1})
            for _ in range(100):
                value = await browser_loop.run_async(
                    sess.page.evaluate("document.getElementById('s').value"), timeout_s=5
                )
                if value == "two":
                    break
                await asyncio.sleep(0.05)
            assert value == "two"
            ws.send({"t": "newtab"})
            two = await ws.wait_for(
                lambda m: m.get("type") == "state" and len(m["session"].get("tabs") or []) == 2,
                timeout=3.0,
            )
            assert [t["active"] for t in two["session"]["tabs"]] == [False, True]
        finally:
            view.cancel()
            with contextlib.suppress(BaseException):
                await view

    asyncio.run(_drive())


def _viewer_session(live_server, browser_on, agent: str, html: str):
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_navigate
    from olano.tools.talk_to import current_agent

    token = _as(agent)
    try:
        _invoke(browser_navigate, {"url": _url(live_server)})
    finally:
        current_agent.reset(token)
    sess = browser_on.get(agent)
    sess.controller = "user"
    browser_loop.run_sync(sess.page.set_content(html), timeout_s=20)
    return sess


def test_a_dialog_opened_by_a_click_is_answered_without_waiting(live_server, browser_on):
    """The click that opens a confirm() does not return until it is answered,
    so the answer must not queue behind it: it used to wait out the 30 s input
    timeout while the view sat frozen."""
    import time

    from olano.browser import loop as browser_loop
    from olano.runtime.routes.browser import _run_live_view

    html = (
        "<body style=margin:0><button id=b style='position:absolute;left:0;top:0;width:200px;"
        "height:50px' onclick=\"window.answer = confirm('Sure?')\">b</button></body>"
    )
    sess = _viewer_session(live_server, browser_on, "asker", html)

    async def _drive() -> float:
        ws = _FakeSocket()
        view = asyncio.ensure_future(_run_live_view(ws, "asker"))
        try:
            await ws.wait_for(lambda m: m.get("type") == "state")
            at = {"x": 100 / 1280, "y": 25 / 800}
            ws.send({"t": "down", **at, "button": "left"})
            ws.send({"t": "up", **at, "button": "left"})
            msg = await ws.wait_for(
                lambda m: m.get("type") == "state" and m["session"].get("dialog"), timeout=5
            )
            started = time.monotonic()
            ws.send({"t": "dialog", "id": msg["session"]["dialog"]["id"], "accept": True})
            for _ in range(200):
                answer = await browser_loop.run_async(
                    sess.page.evaluate("window.answer"), timeout_s=5
                )
                if answer is True:
                    break
                await asyncio.sleep(0.05)
            assert answer is True, [m for m in ws.texts if m.get("type") != "state"] + [
                m["session"].get("dialog") for m in ws.texts if m.get("type") == "state"
            ]
            assert not [m for m in ws.texts if m.get("type") == "error"], ws.texts
            return time.monotonic() - started
        finally:
            view.cancel()
            with contextlib.suppress(BaseException):
                await view

    assert asyncio.run(_drive()) < 5


def test_stop_cuts_a_navigation_the_person_started(live_server, browser_on):
    """Stop must not queue behind the navigation it is meant to stop."""
    import time

    from olano.browser import loop as browser_loop
    from olano.runtime.routes.browser import _run_live_view

    sess = _viewer_session(live_server, browser_on, "stopper", "<p>start</p>")

    async def _slow(route) -> None:
        await asyncio.sleep(15)
        with contextlib.suppress(Exception):
            await route.fulfill(body="<p>late</p>", content_type="text/html")

    browser_loop.run_sync(sess.page.route("https://slow.test/**", _slow), timeout_s=10)

    async def _drive() -> float:
        ws = _FakeSocket()
        view = asyncio.ensure_future(_run_live_view(ws, "stopper"))
        try:
            await ws.wait_for(lambda m: m.get("type") == "state")
            ws.send({"t": "navigate", "url": "https://slow.test/page"})
            await ws.wait_for(
                lambda m: m.get("type") == "state" and m["session"].get("loading"), timeout=5
            )
            started = time.monotonic()
            ws.send({"t": "stop"})
            await ws.wait_for(
                lambda m: m.get("type") == "state"
                and m["session"].get("loading") is False
                and m is ws.texts[-1],
                timeout=10,
            )
            return time.monotonic() - started
        finally:
            view.cancel()
            with contextlib.suppress(BaseException):
                await view

    assert asyncio.run(_drive()) < 5
