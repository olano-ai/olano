"""Live: a check only a person can pass, against a real Chromium through the real tools.

The site here plays Cloudflare's part: a "Just a moment..." page with a box a
person ticks (it posts back, gets a clearance cookie and reloads), and a
variant that reloads itself every second with a new box, as Cloudflare's does
every two minutes. The browser is headless, so the tests give it the screen a
live browser has (``_has_screen``); everything else is the real path: the
tool call blocks on the wait, the person takes control, ticks the box in the
page, and the tool returns what is past it.
"""

from __future__ import annotations

import http.server
import threading
import time

import pytest

from tests.test_tools_browser_live import (  # noqa: F401
    _chromium_available,
    as_agent,
    browser_on,
)

pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)

_CHECK = """<!doctype html><title>Just a moment...</title><body>
<h1>Performing security verification</h1>
<form method=post action="/verify"><button id=tick>Verify you are human</button></form>
{refresh}</body>"""
_REFRESH = "<script>setTimeout(() => location.reload(), 1000);</script>"


@pytest.fixture(scope="module")
def site():
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            hits.append(self.path)
            if "cleared=1" in self.headers.get("Cookie", ""):
                self._send(200, "<title>Listings</title><h1>listings</h1>")
            elif self.path.startswith("/refreshing"):
                self._send(403, _CHECK.format(refresh=_REFRESH))
            else:
                self._send(403, _CHECK.format(refresh=""))

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Set-Cookie", "cleared=1; Path=/")
            self.send_header("Location", "/listings")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hits = hits  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from olano.browser import blockpage, human_check
    from olano.tools import browser as browser_tools

    monkeypatch.setattr(blockpage, "INTERSTITIAL_WAIT_S", 1.0)
    monkeypatch.setattr(human_check, "SECONDS_PER_MINUTE", 3.0)
    monkeypatch.setattr(browser_tools, "_has_screen", lambda sess: True)
    blockpage.forget()
    yield
    blockpage.forget()


class _Call:
    """A browser tool call running in its own thread, the way the agent's
    tool call blocks while it waits for a person."""

    def __init__(self, tool, args, agent, workspace):
        import contextvars

        self.result: str | None = None

        def run():
            with as_agent(agent, workspace):
                self.result = tool.invoke(args)

        self.thread = threading.Thread(target=contextvars.copy_context().run, args=(run,))
        self.thread.daemon = True
        self.thread.start()

    def wait(self, timeout: float = 60) -> str:
        self.thread.join(timeout)
        assert self.result is not None, "the tool call did not return"
        return self.result


def _until(predicate, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition never held")


def _on_loop(coro, timeout: float = 30):
    from olano.browser import loop as browser_loop

    return browser_loop.run_sync(coro, timeout_s=timeout)


def test_a_person_passes_the_check_and_the_agent_carries_on(site, browser_on, tmp_path):  # noqa: F811
    from olano.browser import human_check
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate

    set_browser_settings({"human_check": "wait", "human_check_wait": 10})
    call = _Call(browser_navigate, {"url": _url(site, "/")}, "alice", tmp_path)
    _until(lambda: (s := browser_on.get("alice")) is not None and s.human_check is not None)
    sess = browser_on.get("alice")
    shown = sess.snapshot()
    assert shown["human_check"]["mode"] == "wait"
    assert shown["human_check"]["site"] == "127.0.0.1"
    assert shown["attention"].startswith("127.0.0.1 wants a person to confirm")

    async def _person_ticks_the_box() -> None:
        await browser_on.set_controller("alice", "user")
        async with sess.page.expect_navigation(url="**/listings"):
            await sess.page.click("#tick")

    _on_loop(_person_ticks_the_box())
    result = call.wait()
    assert result.startswith(f"[{human_check.MARKER}] A person completed"), result
    assert "# Listings" in result or "listings" in result
    # Handed back without anyone pressing Hand back, and nothing left pending.
    assert sess.controller == "agent"
    assert sess.human_check is None and sess.attention == ""


def test_nobody_comes_the_agent_is_told_and_does_not_wait_again(site, browser_on, tmp_path):  # noqa: F811
    from olano.browser import blockpage, human_check
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate

    set_browser_settings({"human_check": "wait", "human_check_wait": 1})
    started = time.monotonic()
    result = _Call(browser_navigate, {"url": _url(site, "/refreshing")}, "alice", tmp_path).wait()
    returned = time.time()
    assert result.startswith(f"{human_check.MARKER}: nobody completed"), result
    assert time.monotonic() - started >= human_check.SECONDS_PER_MINUTE
    sess = browser_on.get("alice")
    assert sess.human_check is None
    # The check reloaded itself all along the wait and nothing refused it.
    assert site.hits.count("/refreshing") >= 3
    refused = [b for b in sess.guard.blocked if b.get("site_block") and b["at"] < returned - 0.5]
    assert not refused, refused
    # Afterwards it is remembered like a skip: re-reading does not wait again.
    assert blockpage.recent("alice", _url(site, "/")) is not None


def test_skip_setting_moves_on_at_once(site, browser_on, tmp_path):  # noqa: F811
    from olano.browser import blockpage, human_check
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate

    set_browser_settings({"human_check": "skip"})
    started = time.monotonic()
    result = _Call(browser_navigate, {"url": _url(site, "/")}, "alice", tmp_path).wait()
    assert result.startswith(f"{human_check.MARKER}: 127.0.0.1 asks for a person"), result
    assert "set not to wait" in result
    assert time.monotonic() - started < human_check.SECONDS_PER_MINUTE + 5
    assert browser_on.get("alice").human_check is None
    # Remembered, so the agent does not retry it...
    again = _Call(browser_navigate, {"url": _url(site, "/")}, "alice", tmp_path).wait()
    assert again.startswith(f"{blockpage.MARKER}: Not opened."), again


def test_the_agent_can_choose_to_wait_for_one_task(site, browser_on, tmp_path):  # noqa: F811
    """...unless it asks for a person on purpose, which overrides both the
    setting and the memory of the skipped check."""
    from olano.browser import human_check
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate

    set_browser_settings({"human_check": "skip", "human_check_wait": 1})
    first = _Call(browser_navigate, {"url": _url(site, "/")}, "alice", tmp_path).wait()
    assert "set not to wait" in first
    call = _Call(
        browser_navigate, {"url": _url(site, "/"), "human_check": "hold"}, "alice", tmp_path
    )
    _until(lambda: browser_on.get("alice").human_check is not None)
    assert browser_on.get("alice").human_check["mode"] == "hold"
    # Hold has no limit: still waiting well past the one-"minute" setting.
    time.sleep(human_check.SECONDS_PER_MINUTE + 1)
    assert browser_on.get("alice").human_check is not None
    _on_loop(browser_on.skip_human_check("alice"))
    result = call.wait()
    assert "The person you asked chose to skip it." in result, result


def test_a_turn_stopped_while_waiting_leaves_nothing_pending(site, browser_on, tmp_path):  # noqa: F811
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_navigate, cancel_active_browser_operations

    set_browser_settings({"human_check": "hold"})
    call = _Call(browser_navigate, {"url": _url(site, "/")}, "alice", tmp_path)
    _until(lambda: (s := browser_on.get("alice")) is not None and s.human_check is not None)
    cancel_active_browser_operations()
    call.wait()
    _until(lambda: browser_on.get("alice").human_check is None, timeout=10)
    assert browser_on.get("alice").attention == ""
