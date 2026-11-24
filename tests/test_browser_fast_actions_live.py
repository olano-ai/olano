"""Fewer model round trips, on a real headless Chromium.

Actions return the updated page (or, on the same page, what changed), so the
agent does not spend a separate ``browser_snapshot`` call to see what it just
did; ``browser_fill`` fills a whole form in one call; snapshots leave out the
layout wrappers that carry no text; and a narrow auto-approval rule lets one
kind of action on one site through the always-ask gate, visibly, while
everything else still stops. Skipped without playwright/Chromium.
"""

from __future__ import annotations

import contextvars
import faulthandler
import http.server
import re
import sys
import threading
import urllib.parse
from functools import lru_cache

import pytest


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True, timeout=30_000).close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)


@pytest.fixture(autouse=True)
def _no_silent_hang():
    """A stuck browser fails its own test loudly instead of stalling the whole
    CI job until the job timeout cancels it with no test named: after two
    minutes every thread's stack is dumped and the worker ends, so xdist
    reports exactly which test it was running."""
    faulthandler.dump_traceback_later(120, exit=True, file=sys.__stderr__)
    yield
    faulthandler.cancel_dump_traceback_later()


_PAGE = """<!doctype html><html><head><title>Fast Test</title></head><body>
<div><div><div><h1>Compose</h1></div></div></div>
<div><div><button id="more" onclick="document.getElementById('extra').hidden=false">Show more</button></div></div>
<div id="extra" hidden><div><span>Extra details shown</span></div></div>
<form method="post" action="/send">
  <div><div><label>To <input id="to" name="to"></label></div></div>
  <label>Plan <select id="plan" name="plan"><option>Basic</option><option>Pro</option></select></label>
  <label><input id="agree" type="checkbox" name="agree"> I agree</label>
  <label><input id="fast" type="radio" name="speed" value="fast"> Fast</label>
  <label><input id="slow" type="radio" name="speed" value="slow"> Slow</label>
  <textarea id="body" name="body"></textarea>
  <button id="send" type="submit">Send</button>
</form>
<form method="post" action="/buy"><button id="buy" type="submit">Place order</button></form>
</body></html>"""


@pytest.fixture(scope="module")
def site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = _PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.server.posts.append(
                (self.path, urllib.parse.parse_qs(self.rfile.read(n).decode()))
            )  # type: ignore[attr-defined]
            body = b"<title>Sent</title><p>Message sent</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.posts = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture()
def browser_on(monkeypatch, site):
    from olano.browser import loop as browser_loop
    from olano.browser import policy, sessions
    from olano.browser.guard import clear_dns_cache
    from olano.config.system_settings import set_browser_settings

    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setattr(policy, "_host", lambda: (4, 8 * policy.GIB))
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: None)
    set_browser_settings({"mode": "headless", "max_sessions": 4})
    clear_dns_cache()
    site.posts.clear()
    yield sessions
    browser_loop.run_sync(sessions.close_all(save=False), timeout_s=30)


def _invoke(tool, args, deadline_s: float = 60):
    out: dict = {}
    ctx = contextvars.copy_context()

    def run():
        out["r"] = ctx.run(tool.invoke, args)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(deadline_s)
    assert "r" in out, f"{tool.name} did not return within {deadline_s}s"
    return out["r"]


def _url(site) -> str:
    return f"http://127.0.0.1:{site.server_address[1]}/"


def _ref(snapshot: str, role: str, name: str) -> str:
    match = re.search(rf'{role} "{re.escape(name)}" \[ref=(e\d+)\]', snapshot)
    assert match, f"{role} {name!r} not in snapshot:\n{snapshot[:2000]}"
    return match.group(1)


def test_a_click_returns_what_changed_on_the_same_page(site, browser_on):
    from olano.tools.browser import browser_click, browser_navigate

    page = _invoke(browser_navigate, {"url": _url(site)})
    out = _invoke(browser_click, {"ref": _ref(page, "button", "Show more")})
    assert out.startswith("Clicked")
    assert "Extra details shown" in out  # the result, without a snapshot call
    assert "only these parts changed" in out
    assert "Use browser_snapshot" not in out


def test_fill_does_a_whole_form_in_one_call_and_submits_nothing(site, browser_on):
    from olano.tools.browser import browser_extract, browser_fill, browser_navigate

    page = _invoke(browser_navigate, {"url": _url(site)})
    out = _invoke(
        browser_fill,
        {
            "fields": [
                {"ref": _ref(page, "textbox", "To"), "value": "Jane Doe"},
                {"ref": _ref(page, "combobox", "Plan"), "value": "Pro"},
                {"ref": _ref(page, "checkbox", "I agree"), "value": "true"},
                {"ref": _ref(page, "radio", "Fast"), "value": "yes"},
                {"selector": "#body", "value": "Hello there"},
            ]
        },
    )
    assert out.startswith("Filled 5 field(s)") and "Nothing was submitted" in out
    assert site.posts == []
    values = _invoke(browser_extract, {"selector": "body"})  # sanity: the page is still the form
    assert "Compose" in values
    from olano.browser import loop as browser_loop
    from olano.browser import sessions

    async def _read():
        p = sessions.get("_default").page
        return await p.evaluate(
            "() => [to.value, plan.value, agree.checked, fast.checked, body.value]"
        )

    assert browser_loop.run_sync(_read(), timeout_s=30) == [
        "Jane Doe",
        "Pro",
        True,
        True,
        "Hello there",
    ]


def test_snapshots_leave_out_empty_wrappers_unless_asked_for_everything(site, browser_on):
    from olano.tools.browser import browser_navigate, browser_snapshot

    _invoke(browser_navigate, {"url": _url(site)})
    compact = _invoke(browser_snapshot, {})
    full = _invoke(browser_snapshot, {"full": True})
    assert len(compact) < len(full)
    for text in ("Compose", "Show more", "Send", "Place order"):
        assert text in compact
    assert "- generic [ref=" in full


def test_an_auto_approval_rule_sends_without_asking_and_says_so(site, browser_on, monkeypatch):
    """A rule for this site and `send` lets Send through; Place order still stops."""
    from olano.browser import auto_approve
    from olano.config.agent_config import BrowserAgentConfig
    from olano.tools.browser import browser_click, browser_navigate

    rules = BrowserAgentConfig.model_validate(
        {"auto_approve": [{"site": "shop.example.com", "actions": ["send", "submit", "signup"]}]}
    ).auto_approve
    monkeypatch.setattr(auto_approve, "rules_for", lambda agent: list(rules))
    # The harness serves on 127.0.0.1; a rule names a real site, so map it.
    monkeypatch.setattr(
        auto_approve,
        "host_of",
        lambda url: "shop.example.com" if "127.0.0.1" in url else "",
    )

    page = _invoke(browser_navigate, {"url": _url(site)})
    out = _invoke(browser_click, {"ref": _ref(page, "button", "Send")})
    assert "Approved automatically" in out and "shop.example.com" in out
    assert [p for p, _ in site.posts] == ["/send"]

    page = _invoke(browser_navigate, {"url": _url(site)})
    out = _invoke(browser_click, {"ref": _ref(page, "button", "Place order")})
    assert out.startswith("Not done:")  # purchases always ask, whatever the rules
    assert [p for p, _ in site.posts] == ["/send"]


def test_without_a_rule_send_still_asks(site, browser_on):
    from olano.tools.browser import browser_click, browser_navigate

    page = _invoke(browser_navigate, {"url": _url(site)})
    out = _invoke(browser_click, {"ref": _ref(page, "button", "Send")})
    assert out.startswith("Not done:")
    assert site.posts == []
